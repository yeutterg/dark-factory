"""Any coding agent can carry a change through the one pipeline, and cannot skip it."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory.adapters.harness.fake import FakeHarness
from dark_factory.api import serve
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.worker import Worker

PROMPTS = Path("src/dark_factory/agents/prompts")
READY = 'def message():\n    return "ready"\n'


def build(tmp_path):
    cfg = cfg_for(tmp_path / "state")
    cfg.windows = []
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, PROMPTS)
    return cfg, store, ctrl


def a_plan():
    harness = FakeHarness()
    list(harness.start("", ".", {"role": "planner", "checks": ["fixture-check"]}))
    return json.loads(harness.wait().output)


def start(ctrl, cfg, actor="contributor:agent-1"):
    return ctrl.work_start(cfg.repositories[0], 7, actor, "Make it ready", "")[
        "work_item_id"
    ]


def test_an_outside_agent_goes_from_issue_to_an_accepted_change(tmp_path):
    cfg, store, ctrl = build(tmp_path)
    try:
        actor = "contributor:agent-1"
        wid = start(ctrl, cfg, actor)
        ctrl.work_plan(wid, actor, a_plan())  # the agent's own plan draft
        worker = Worker(cfg, store, ctrl, "w")
        worker.run_loop(2)  # the plan is adopted; the factory's critic still reviews it
        plan = store.one("SELECT status FROM work_items WHERE id=?", (wid,))
        assert plan["status"] == "awaiting_approval"
        with pytest.raises(
            ControllerError, match="no worktree until a person approves"
        ):
            ctrl.work_checkout(wid, actor)
        with pytest.raises(ControllerError):
            ctrl.approve(wid, actor)  # a contributor is not a human decision
        ctrl.approve(wid, "human-tester")
        checkout = ctrl.work_checkout(wid, actor)
        tree = Path(checkout["worktree"])
        (tree / "message.py").write_text(READY)
        assert ctrl.work_status(wid, actor)["awaiting_submission"]
        ctrl.work_submit(wid, actor)
        worker.run_loop()  # implement, build/test/check and independent review
        status = ctrl.work_status(wid, actor)
        assert status["status"] == "awaiting_review" and status["candidate"]
        kinds = {
            r["kind"]
            for r in store.query("SELECT kind FROM jobs WHERE status='succeeded'")
        }
        assert {"plan", "critique", "implement", "check", "review"} <= kinds
        implement = store.one(
            "SELECT inputs_json FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.kind='implement'"
        )
        assert json.loads(implement["inputs_json"])["external"]["actor"] == actor
        candidate = json.loads(
            store.one("SELECT candidate_commits FROM work_items")[0]
        )[cfg.repositories[0]]["commit"]
        ctrl.record_manual_gate(
            wid, "Inspect candidate diff", candidate, "human-tester", "ok"
        )
        ctrl.accept(wid, "human-tester")
        assert ctrl.work_status(wid, actor)["status"] == "accepted"
    finally:
        store.close()


def test_the_factory_planner_can_draft_the_plan(tmp_path):
    cfg, store, ctrl = build(tmp_path)
    try:
        wid = start(ctrl, cfg)
        ctrl.work_plan(wid, "contributor:agent-1")  # no draft: the factory plans
        Worker(cfg, store, ctrl, "w").run_loop(2)
        assert store.one("SELECT status FROM work_items")[0] == "awaiting_approval"
    finally:
        store.close()


def test_a_submission_is_judged_like_any_other_candidate(tmp_path):
    cfg, store, ctrl = build(tmp_path)
    try:
        actor = "contributor:agent-1"
        wid = start(ctrl, cfg, actor)
        ctrl.work_plan(wid, actor)
        worker = Worker(cfg, store, ctrl, "w")
        worker.run_loop(2)
        ctrl.approve(wid, "human-tester")
        tree = Path(ctrl.work_checkout(wid, actor)["worktree"])
        (tree / "message.py").write_text(READY)
        (tree / "verify.py").write_text("raise SystemExit(0)\n")  # a protected path
        ctrl.work_submit(wid, actor)
        worker.run_loop()
        assert store.one("SELECT status FROM work_items")[0] == "failed"
        error = store.one("SELECT error FROM attempts WHERE status='failed'")[0]
        assert "out-of-scope" in error
    finally:
        store.close()


def test_nothing_is_dispatched_until_the_agent_submits(tmp_path):
    cfg, store, ctrl = build(tmp_path)
    try:
        actor = "contributor:agent-1"
        wid = start(ctrl, cfg, actor)
        ctrl.work_plan(wid, actor)
        worker = Worker(cfg, store, ctrl, "w")
        worker.run_loop(2)
        ctrl.approve(wid, "human-tester")
        ctrl.work_checkout(wid, actor)
        assert worker.run_loop() == 0  # the implement step waits for the agent
        with pytest.raises(ControllerError, match="belongs to another contributor"):
            ctrl.work_submit(wid, "contributor:someone-else")
    finally:
        store.close()


def test_a_contributor_credential_runs_work_commands_and_nothing_else(
    tmp_path, monkeypatch
):
    cfg, store, ctrl = build(tmp_path)
    monkeypatch.setenv("CONTRIB_TOKEN", "c" * 40)
    cfg.raw["runtime"] = {
        **cfg.raw["runtime"],
        "contributor_token_env": "CONTRIB_TOKEN",
    }
    server = serve(ctrl, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:

        def call(body, token):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request(
                "POST",
                "/operator",
                body=json.dumps(body),
                headers={"Authorization": f"Bearer {token}"},
            )
            response = conn.getresponse()
            return response.status, json.loads(response.read())

        contributor = "c" * 40
        status, body = call(
            {
                "cmd": "work-start",
                "repo": cfg.repositories[0],
                "number": 5,
                "title": "x",
                "actor": "agent-9",
            },
            contributor,
        )
        assert status == 200 and body["result"]["contributor"] == "contributor:agent-9"
        wid = body["result"]["work_item_id"]
        for forbidden in ("approve", "accept", "reload", "status", "gc"):
            status, _ = call({"cmd": forbidden, "work_item_id": wid}, contributor)
            assert status == 403, forbidden
        assert call({"cmd": "status"}, cfg.operator_token)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        store.close()
