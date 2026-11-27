"""dark-factory/gate is green only for the exact commit the factory reviewed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory.adapters.git import github
from dark_factory.contracts import ContractError, make_request
from dark_factory.controller import Controller
from dark_factory.store import Store
from dark_factory.worker import Worker

PROMPTS = Path("src/dark_factory/agents/prompts")
SHA = "a" * 40


class Host:
    """A stand-in code host: records statuses, serves open pull requests."""

    def __init__(self, real):
        self.real = real
        self.prs = []
        self.statuses = []

    def __call__(self, command, operation, payload, request_id, timeout):
        if operation == "list_open_prs":
            return {"ok": True, "operation": operation, "evidence": {"prs": self.prs}}
        if operation == "set_status":
            self.statuses.append(
                (payload["sha"], payload["state"], payload["description"])
            )
            return {"ok": True, "operation": operation, "evidence": {"observed": True}}
        return self.real(command, operation, payload, request_id, timeout)


@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path / "state")
    cfg.windows = []
    cfg.raw["gate"] = {"enabled": True, "interval_seconds": 0}
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, PROMPTS)
    host = Host(ctrl.runner.run_json)
    monkeypatch.setattr(ctrl.runner, "run_json", host)
    yield cfg, store, ctrl, host
    store.close()


def reviewed_candidate(cfg, store, ctrl):
    wid = ctrl.intake_issues(
        [{"repo": cfg.repositories[0], "number": 3, "title": "t", "body": ""}]
    )[0]
    ctrl.start_planning(wid)
    worker = Worker(cfg, store, ctrl, "w")
    worker.run_loop(2)
    ctrl.approve(wid, "human-tester")
    worker.run_loop()
    root = store.one("SELECT * FROM work_items WHERE id=?", (wid,))
    return wid, json.loads(root["candidate_commits"])[cfg.repositories[0]]["commit"]


def pr(sha, number=1):
    return {
        "number": number,
        "head_sha": sha,
        "head_ref": "x",
        "title": "t",
        "url": "u",
        "body": "",
    }


def test_a_pull_request_with_no_approved_plan_is_blocked_with_the_way_in(world):
    cfg, store, ctrl, host = world
    host.prs = [pr(SHA)]
    assert ctrl.gate_tick() == [(cfg.repositories[0], 1, "failure")]
    assert "work-start" in host.statuses[0][2]


def test_the_exact_reviewed_commit_is_green_and_a_new_push_is_not(world):
    cfg, store, ctrl, host = world
    wid, candidate = reviewed_candidate(cfg, store, ctrl)
    host.prs = [pr(candidate)]
    assert ctrl.gate_tick()[0][2] == "success"
    assert ctrl.gate_tick() == []  # unchanged state is not posted again
    # The agent pushes another commit to the same pull request.
    host.prs = [pr("b" * 40)]
    assert ctrl.gate_tick()[0][2] == "failure"
    assert host.statuses[-1][0] == "b" * 40


def test_work_still_being_checked_is_pending(world):
    cfg, store, ctrl, host = world
    wid, candidate = reviewed_candidate(cfg, store, ctrl)
    store.execute("UPDATE work_items SET status='running' WHERE id=?", (wid,))
    host.prs = [pr(candidate)]
    assert ctrl.gate_tick()[0][2] == "pending"


def test_the_gate_is_off_until_a_repository_adopts_it(world):
    cfg, store, ctrl, host = world
    cfg.raw["gate"] = {"enabled": False}
    host.prs = [pr(SHA)]
    assert ctrl.gate_tick() == [] and not host.statuses


def test_the_status_operation_is_restricted_to_the_gate_context(monkeypatch):
    monkeypatch.setattr(github, "api", lambda *a, **k: {})
    base = {"repo": "o/n", "sha": SHA, "state": "success"}
    for bad in (
        {**base, "context": "ci/build"},
        {**base, "context": "dark-factory/gate", "state": "ok"},
        {**base, "context": "dark-factory/gate", "sha": "short"},
    ):
        with pytest.raises(ContractError):
            github.handle(make_request("set_status", bad, "r"))
    ok = github.handle(
        make_request("set_status", {**base, "context": "dark-factory/gate"}, "r")
    )
    assert ok["evidence"]["observed"]


def test_the_gate_polls_on_its_own_clock_and_one_failure_does_not_starve_the_rest(
    world,
):
    cfg, store, ctrl, host = world
    cfg.raw["gate"] = {"enabled": True, "interval_seconds": 3600}
    host.prs = [pr(SHA, 1), pr("b" * 40, 2)]
    assert len(ctrl.gate_tick()) == 2
    host.prs = [pr("c" * 40, 3)]
    assert ctrl.gate_tick() == []  # not due again for an hour: no API storm
    cfg.raw["gate"] = {"enabled": True, "interval_seconds": 0}
    real = host.__call__
    calls = {"n": 0}

    def flaky(command, operation, payload, request_id, timeout):
        if operation == "set_status":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("rate limited")
        return real(command, operation, payload, request_id, timeout)

    ctrl.runner.run_json = flaky
    host.prs = [pr("d" * 40, 4), pr("e" * 40, 5)]
    assert [n for _, n, _ in ctrl.gate_tick()] == [
        5
    ]  # PR 4 failed, PR 5 still got its status
