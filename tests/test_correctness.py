"""Regression tests for the repair plan's correctness items (C1-C18)."""

import json

import pytest
from test_workflow import setup

from dark_factory.controller import ControllerError


def _reach_review(ctrl, worker, wid, store, cfg):
    ctrl.approve(wid, "human-tester")
    worker.run_loop()
    root = store.one("SELECT * FROM work_items WHERE id=?", (wid,))
    candidate = json.loads(root["candidate_commits"])[cfg.repositories[0]]["commit"]
    ctrl.record_manual_gate(
        wid, "Inspect candidate diff", candidate, "human-tester", "Inspected diff"
    )


def test_c1_replanned_root_can_be_accepted(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    try:
        assert worker.run_loop(2) == 2
        ctrl.reject(wid, "human-tester", "revise the plan")
        ctrl.start_planning(wid)
        assert worker.run_loop(2) == 2
        _reach_review(ctrl, worker, wid, store, cfg)
        view = ctrl.tasks_view(wid)
        assert {t["plan_version"] for t in view["tasks"]} == {2}
        ctrl.accept(wid, "human-tester")
        assert (
            store.one("SELECT status FROM work_items WHERE id=?", (wid,))["status"]
            == "accepted"
        )
    finally:
        store.close()


def test_c2_step_time_limit_is_an_interruption_not_an_operator_cancel(
    tmp_path, monkeypatch
):
    import time

    import dark_factory.worker as wmod
    from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult

    cfg, store, ctrl, wid, worker = setup(tmp_path)
    job = store.one("SELECT * FROM jobs WHERE kind='plan'")
    spec = json.loads(job["spec_json"])
    spec["timeout_seconds"] = 2
    store.execute(
        "UPDATE jobs SET spec_json=? WHERE id=?", (json.dumps(spec), job["id"])
    )
    real_env_for = wmod.environment_for

    def slow_env_for(*args, **kwargs):
        env = real_env_for(*args, **kwargs)
        prepare = env.prepare

        def slow_prepare(*a, **k):
            time.sleep(2.5)  # a slow clone or container start eats the deadline
            return prepare(*a, **k)

        env.prepare = slow_prepare
        return env

    monkeypatch.setattr(wmod, "environment_for", slow_env_for)

    class Waits:
        def __init__(self):
            self.stopped = False

        def start(self, prompt, workspace, extra=None):
            while not self.stopped:
                time.sleep(0.05)
                yield HarnessEvent("heartbeat")

        def cancel(self):
            self.stopped = True

        def wait(self):
            return HarnessResult(False, "", {}, None, error="stopped")

    worker._harness = lambda route=None: Waits()
    try:
        worker.run_once()
        attempt = store.one("SELECT status FROM attempts")
        assert attempt["status"] == "timed_out"
        assert store.one("SELECT status FROM work_items")["status"] != "cancelled"
        assert store.one("SELECT status FROM jobs WHERE kind='plan'")[0] != "cancelled"
    finally:
        store.close()


def test_c3_lease_survives_slow_archiving_and_good_result_is_kept(
    tmp_path, monkeypatch
):
    import time

    import dark_factory.worker as wmod

    cfg, store, ctrl, wid, worker = setup(tmp_path)
    monkeypatch.setattr(type(cfg), "lease_seconds", lambda self, name: 2)
    real = wmod.artifacts.archive

    def slow(*args, **kwargs):
        time.sleep(3)  # longer than the lease, with no harness output at all
        return real(*args, **kwargs)

    monkeypatch.setattr(wmod.artifacts, "archive", slow)
    try:
        worker.run_once()
        assert store.one("SELECT status FROM attempts")["status"] == "succeeded"
        assert not store.query("SELECT * FROM interruptions")
    finally:
        store.close()


def test_c4_failed_start_planning_can_be_retried(tmp_path, monkeypatch):
    from pathlib import Path

    from test_loop import cfg_for

    from dark_factory.control import planning
    from dark_factory.controller import Controller
    from dark_factory.store import Store

    cfg = cfg_for(tmp_path)
    store = Store(tmp_path)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    wid = ctrl.intake_issues(
        [
            {
                "repo": cfg.repositories[0],
                "number": 1,
                "title": "Whole outcome",
                "body": "Change and verify the behavior",
                "is_root": True,
            }
        ]
    )[0]
    try:
        real = planning.clone

        def failing_clone(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("disk full")

        monkeypatch.setattr(planning, "clone", failing_clone)
        with pytest.raises(RuntimeError, match="disk full"):
            ctrl.start_planning(wid)
        monkeypatch.setattr(planning, "clone", real)
        ctrl.start_planning(wid)  # no FileExistsError from the failed attempt
        assert store.one("SELECT status FROM work_items")["status"] == "planning"
    finally:
        store.close()


def test_c5_unresolved_external_action_is_scoped_and_abandonable(tmp_path):
    from test_workflow import setup

    cfg, store, ctrl, wid, worker = setup(tmp_path)
    store.execute(
        "INSERT INTO pending_actions(id,kind,idempotency_key,payload_json,status,created_at)"
        " VALUES ('a1','create_pr','a1',?, 'ambiguous','2026-01-01T00:00:00+00:00')",
        (json.dumps({"root": wid, "input": {}}),),
    )
    assert ctrl.dispatch_allowed()[0]
    assert ctrl.unresolved_action(wid)
    assert ctrl.unresolved_action("some-other-root") is None
    assert ctrl.claim_job("local", ["native", "fake", "environment:local"]) is None
    with pytest.raises(ControllerError):
        ctrl.abandon_action(wid, "  ", "human")
    ctrl.abandon_action(wid, "PR was opened by hand", "human")
    assert ctrl.unresolved_action(wid) is None
    assert store.one("SELECT status FROM pending_actions")[0] == "abandoned"
    assert ctrl.claim_job("local", ["native", "fake", "environment:local"]) is not None


def _supervised(tmp_path, monkeypatch, transport):
    from dark_factory.config import FactoryConfig, IntegrationCommand

    cfg, store, ctrl, wid, worker = setup(tmp_path)
    settings = {
        "command": "task-supervisor",
        "region": "US",
        "model": "m",
        "provider": "p",
        "pricing_evidence": "x",
        "max_requests_per_root": 2,
        "budget_cents": 10,
        "max_input_tokens": 100000,
        "max_output_tokens": 1000,
    }
    monkeypatch.setattr(
        FactoryConfig, "task_supervisor", property(lambda self: settings)
    )
    cfg.commands["task-supervisor"] = IntegrationCommand("task-supervisor", ["x"], 30)
    monkeypatch.setattr(ctrl.runner, "run_json", transport)
    store.execute(
        "INSERT INTO task_events(id,root_id,task_id,source,actor,kind,payload_json,created_at,idempotency_key)"
        " VALUES ('e1',?,NULL,'human','human','note','{}','2026-01-01T00:00:00+00:00','k1')",
        (wid,),
    )
    return cfg, store, ctrl, wid


def test_c6_failed_supervisor_retries_count_against_the_request_cap(
    tmp_path, monkeypatch
):
    from dark_factory.runner import RunnerError

    def transport(*args, **kwargs):
        raise RunnerError("down")

    cfg, store, ctrl, wid = _supervised(tmp_path, monkeypatch, transport)
    assert ctrl.reconcile_task_events(wid)["status"] == "failed"
    assert ctrl.reconcile_task_events(wid)["status"] == "failed"
    # Two real requests were made; the cap of two is now reached.
    assert ctrl.reconcile_task_events(wid)["status"] == "request_limit"
    call = store.one("SELECT requests,cost_cents,reserved_cents FROM supervisor_calls")
    assert (call["requests"], call["reserved_cents"]) == (2, 0)
    assert call["cost_cents"] == 10  # both failed calls were charged, not overwritten
    root = store.one("SELECT reserved_cents FROM work_items WHERE id=?", (wid,))
    assert root["reserved_cents"] == 0


def test_c6_crashed_supervisor_call_releases_its_reservation(tmp_path, monkeypatch):
    cfg, store, ctrl, wid = _supervised(tmp_path, monkeypatch, lambda *a, **k: None)
    store.execute(
        "UPDATE work_items SET reserved_cents=reserved_cents+5 WHERE id=?", (wid,)
    )
    store.execute(
        "INSERT INTO supervisor_calls(id,root_id,input_digest,request_json,status,"
        "cost_cents,created_at,requests,reserved_cents)"
        " VALUES ('s1',?,'d','{}','pending',0,'2026-01-01T00:00:00+00:00',1,5)",
        (wid,),
    )
    assert ctrl.release_orphaned_supervisor_calls() == 1
    root = store.one("SELECT reserved_cents FROM work_items WHERE id=?", (wid,))
    assert root["reserved_cents"] == 0
    assert store.one("SELECT status FROM supervisor_calls")[0] == "failed"


def test_c7_unavailable_policy_still_settles_an_active_attempt(tmp_path, monkeypatch):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    claimed = ctrl.claim_job("local", ["native", "fake", "environment:local"])
    assert claimed is not None
    attempt_id = claimed["attempt_id"]

    def broken(self):
        raise OSError("definition file unreadable")

    monkeypatch.setattr(type(ctrl), "_evaluate_policy", broken)
    ctrl.finish_attempt(attempt_id, False, "x", 0, None, error="stopped")
    attempt = store.one("SELECT status,error FROM attempts WHERE id=?", (attempt_id,))
    assert attempt["status"] == "failed"
    assert attempt["error"].startswith("policy_unavailable")
    root = store.one("SELECT status,reserved_cents FROM work_items WHERE id=?", (wid,))
    assert (root["status"], root["reserved_cents"]) == ("failed", 0)


def test_c9_task_event_keys_are_namespaced_per_attempt(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    ctrl.approve(wid, "human-tester")
    claim = ctrl.claim_job("w", ["native", "fake", "environment:local"])
    task = next(
        t
        for t in ctrl.tasks_view(wid)["tasks"]
        if t["owner_kind"] == "agent" and t["job_id"] == claim["job"]["id"]
    )
    args = (claim["attempt_id"], task["id"], "task_started", {}, "w")
    ctrl.record_task_progress(*args, "same-id")
    ctrl.record_task_progress(*args, "same-id")
    rows = store.query(
        "SELECT idempotency_key FROM task_events WHERE attempt_id=?",
        (claim["attempt_id"],),
    )
    assert [r[0] for r in rows] == [f"{claim['attempt_id']}:same-id"]


def test_c9_one_bad_task_event_does_not_kill_the_attempt(tmp_path, monkeypatch):
    from dark_factory.adapters.harness.base import HarnessEvent
    from dark_factory.adapters.harness.fake import FakeHarness
    from dark_factory.worker import Worker

    class Chatty(FakeHarness):
        def start(self, prompt, workspace, extra=None):
            yield HarnessEvent("task_started", "x", {"task_id": "no-such-task"})
            yield from super().start(prompt, workspace, extra)

    monkeypatch.setattr(Worker, "_harness", lambda self, route: Chatty())
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    ctrl.approve(wid, "human-tester")
    worker.run_loop()
    assert store.one("SELECT status FROM work_items")[0] == "awaiting_review"


def _admit(ctrl, wid, task_id, criterion, event_id, base):
    return ctrl.apply_supervisor_response(
        wid,
        {
            "schema_version": 1,
            "root_id": wid,
            "base_revision": base,
            "processed_event_ids": [event_id],
            "operations": [
                {
                    "op": "admit_task",
                    "task_id": task_id,
                    "criterion_ids": [criterion],
                    "source_event_ids": [event_id],
                }
            ],
            "current_summary": "x",
            "suggested_next_task_id": None,
            "needs_decision": [],
        },
    )


def test_c11_late_admission_cannot_bind_to_a_finished_step(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    ctrl.approve(wid, "human-tester")
    added = ctrl.add_task(wid, "Late request", "agent", "human-tester", None)
    event = store.one("SELECT id FROM task_events WHERE task_id=?", (added["task_id"],))
    criterion = next(iter(ctrl._criteria(ctrl._task_root(wid))))
    worker.run_loop()  # the implementation step finishes
    with pytest.raises(ControllerError, match="no open implementation step"):
        _admit(
            ctrl, wid, added["task_id"], criterion, event["id"], ctrl.task_revision(wid)
        )


def test_c11_supervisor_response_must_name_a_base_revision(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    added = ctrl.add_task(wid, "Request", "agent", "human-tester", None)
    event = store.one("SELECT id FROM task_events WHERE task_id=?", (added["task_id"],))
    criterion = next(iter(ctrl._criteria(ctrl._task_root(wid))))
    with pytest.raises(ControllerError, match="base revision"):
        _admit(ctrl, wid, added["task_id"], criterion, event["id"], None)


def test_c12_replaced_archive_with_a_regenerated_manifest_is_rejected(tmp_path):
    from dark_factory import artifacts

    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    _reach_review(ctrl, worker, wid, store, cfg)
    root = store.one("SELECT * FROM work_items WHERE id=?", (wid,))
    candidate = json.loads(root["candidate_commits"])[root["repo"]]
    archive = store.resolve_path(candidate["archive"])
    manifest = json.loads((archive / "manifest.json").read_text())
    (archive / "diff.patch").write_text("diff --git a/x b/x\n+replaced\n")
    artifacts.seal(archive, {k: v for k, v in manifest.items() if k != "files"})
    artifacts.verify(archive)  # self-consistent, as an attacker would leave it
    with pytest.raises(ControllerError, match="differs from the"):
        ctrl.accept(wid, "human-tester")


def test_c13_each_command_records_its_own_pid_and_identity(tmp_path):
    import os

    cfg, store, ctrl, wid, worker = setup(tmp_path)
    claimed = ctrl.claim_job("w", ["native", "fake", "environment:local"])
    aid = claimed["attempt_id"]
    ctrl.record_process(aid, os.getpid())
    ctrl.record_process(aid, os.getppid())
    row = store.one("SELECT pid,pid_identity,status FROM attempts WHERE id=?", (aid,))
    from dark_factory.runner import process_identity

    assert row["pid"] == os.getppid()
    assert row["pid_identity"] == process_identity(os.getppid())
    assert row["status"] == "running"


def test_c14_cancelling_a_root_signals_attempts_and_blocks_replan(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    claimed = ctrl.claim_job("w", ["native", "fake", "environment:local"])
    ctrl.cancel_job(claimed["job"]["id"], "human-tester")
    row = store.one("SELECT status FROM attempts WHERE id=?", (claimed["attempt_id"],))
    assert row["status"] == "cancelling"
    # The process has not stopped yet: replanning would race its reservation.
    with pytest.raises(ControllerError, match="stop active attempts"):
        ctrl.start_planning(wid)


def test_c15_account_cap_is_named_in_the_continuation_view(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    ctrl.approve(wid, "human-tester")
    other = ctrl.intake_issues(
        [{"repo": cfg.repositories[0], "number": 99, "title": "Other", "body": ""}]
    )[0]
    store.execute(
        "UPDATE work_items SET spent_cents=? WHERE id=?",
        (cfg.budgets["account_cents"], other),
    )
    assert ctrl.claim_job("w", ["native", "fake", "environment:local"]) is None
    reason = ctrl.continuation(wid)["next_action"]
    assert "account allowance" in json.dumps(reason)


def test_c16_a_bad_markdown_line_applies_nothing(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    ctrl.export_tasks(wid)
    path = ctrl.tasks_path(wid)
    before = ctrl.task_revision(wid)
    count = store.one("SELECT COUNT(*) FROM tasks WHERE root_id=?", (wid,))[0]
    body = path.read_text()
    body += "- [ ] Valid new request\n- [ ] Ghost <!-- task:tsk_nope owner:human -->\n"
    path.write_text(body)
    with pytest.raises(ControllerError):
        ctrl.import_tasks(wid, "human-tester")
    assert ctrl.task_revision(wid) == before
    assert store.one("SELECT COUNT(*) FROM tasks WHERE root_id=?", (wid,))[0] == count


def test_c17_requirements_match_on_kind_and_name():
    from dark_factory.control.review import assess_external

    sha = "a" * 40
    # A status context that happens to share a reviewer's name is not a review.
    evidence = {
        "head_sha": sha,
        "statuses": [{"context": "Cursor Bugbot", "state": "success"}],
        "checks": [],
        "reviews": [],
    }
    assert not assess_external(["review:Cursor Bugbot"], evidence, sha)[0]["passed"]
    assert assess_external(["status:Cursor Bugbot"], evidence, sha)[0]["passed"]
    assert not assess_external(["check:Cursor Bugbot"], evidence, sha)[0]["passed"]


def test_c17_bare_requirement_names_are_rejected_at_config_load(tmp_path):
    import tomllib
    from pathlib import Path

    from dark_factory.config import ConfigError, validate_raw

    path = Path("examples/factory.toml").resolve()
    raw = tomllib.loads(path.read_text())
    raw["commands"]["verify"] = {"argv": ["true"], "timeout_seconds": 5}
    raw["sources"] = {
        raw["repositories"][0]: {
            "path": str(tmp_path),
            "revision": "HEAD",
            "checks": ["verify"],
            "allowed_paths": ["a.py"],
            "protected_paths": ["v.py"],
            "external_acceptance": ["Cursor Bugbot"],
        }
    }
    with pytest.raises(ConfigError, match="external_acceptance"):
        validate_raw(raw, path, "digest")
    raw["sources"][raw["repositories"][0]]["external_acceptance"] = [
        "check:Cursor Bugbot"
    ]
    validate_raw(raw, path, "digest")


def test_c18_check_commands_share_one_attempt_deadline(tmp_path, monkeypatch):
    from datetime import datetime as real
    from datetime import timedelta

    from dark_factory import worker as wmod

    offset = [0]

    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + timedelta(seconds=offset[0])

    monkeypatch.setattr(wmod, "datetime", Clock)
    timeouts = []

    real_supervise = wmod.CommandRunner.supervise

    def supervise(self, argv, stdin, timeout, cwd, env, **kwargs):
        if not kwargs.get("on_start"):
            return real_supervise(self, argv, stdin, timeout, cwd, env, **kwargs)
        timeouts.append(timeout)
        offset[0] += 10**6  # the first command used up the whole step
        return 0, b"", b""

    monkeypatch.setattr(wmod.CommandRunner, "supervise", supervise)
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    snapshot = json.loads(store.one("SELECT snapshot_json FROM work_items")[0])
    snapshot["source"]["checks"] = ["fixture-check"] * 2  # an approved repeat
    store.execute("UPDATE work_items SET snapshot_json=?", (json.dumps(snapshot),))
    ctrl.approve(wid, "human-tester")
    worker.run_loop()
    assert len(timeouts) == 1, "the second command must not start after the deadline"
    row = store.one("SELECT category FROM interruptions ORDER BY at LIMIT 1")
    assert row["category"] == "time_limit"


def test_one_protected_list_covers_capture_and_every_sandbox(tmp_path):
    from dark_factory.adapters.git.local import capture, git
    from dark_factory.protected import ALWAYS_PROTECTED

    assert {".claude", ".mcp.json", ".gitmodules"} <= set(ALWAYS_PROTECTED)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "a.py").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / ".mcp.json").write_text("{}")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{}")
    (repo / "a.py").write_text("y")
    # Even a profile that allows these paths cannot make them editable.
    result = capture(repo, base, ["a.py", ".mcp.json", ".claude"], [])
    assert set(result["scope_violations"]) == {".mcp.json", ".claude/settings.json"}
