"""Live work-start, frozen submissions, I/O outside transactions, pinned targets, audit origin."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import test_external_work as ext
import test_improvement_loop as imp

from dark_factory.adapters.git.fake_host import FakeCodeHost
from dark_factory.contracts import make_request
from dark_factory.control import planning
from dark_factory.control.base import _digest
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.worker import Worker

ACTOR = "contributor:agent-1"


# ---- #19: tracker-backed intake names only the repository and the number ----------


def tracker(cfg, seen, parent=None, source="github"):
    def run(command, operation, payload, request_id, timeout):
        seen["payload"] = payload
        item = {
            "external_id": "github:o/n:issue:7",
            "url": "u",
            "title": "Real title",
            "body": "Real body",
            "repo": cfg.repositories[0],
            "parent_id": parent,
            "is_root": parent is None,
            "required_child_count": 0,
            "iteration": None,
        }
        return {
            "ok": True,
            "operation": operation,
            "items": [item],
            "evidence": {
                "observations": [{"external_id": item["external_id"], "source": source}]
            },
        }

    return run


def test_live_work_start_sends_only_the_repository_and_number(tmp_path):
    cfg, store, ctrl = ext.build(tmp_path)
    try:
        for route in cfg.routes.values():
            route.evidence_kind = "live"
        seen: dict = {}
        ctrl.runner.run_json = tracker(cfg, seen)
        ctrl.work_start(cfg.repositories[0], 7, ACTOR, "Caller title", "Caller body")
        assert seen["payload"]["issues"] == [{"repo": cfg.repositories[0], "number": 7}]
        assert (
            store.one("SELECT title FROM work_items")[0] == "Real title"
        )  # from the tracker
    finally:
        store.close()


def test_a_child_issue_still_needs_its_root_and_fixtures_stay_out_of_live_intake(
    tmp_path,
):
    cfg, store, ctrl = ext.build(tmp_path)
    try:
        for route in cfg.routes.values():
            route.evidence_kind = "live"
        ctrl.runner.run_json = tracker(cfg, {}, parent="github:o/n:issue:1")
        with pytest.raises(ControllerError, match="select the root outcome"):
            ctrl.work_start(cfg.repositories[0], 7, ACTOR)
        ctrl.runner.run_json = tracker(cfg, {}, source="provided-fixture")
        with pytest.raises(
            ControllerError, match="fixtures cannot establish root scope"
        ):
            ctrl.work_start(cfg.repositories[0], 7, ACTOR)
    finally:
        store.close()


def test_fixture_data_is_built_only_explicitly_and_only_in_simulation(tmp_path):
    cfg, store, ctrl = ext.build(tmp_path)
    try:
        seen: dict = {}
        real = ctrl.runner.run_json

        def spy(command, operation, payload, request_id, timeout):
            seen["payload"] = payload
            return real(command, operation, payload, request_id, timeout)

        ctrl.runner.run_json = spy
        ctrl.work_start(cfg.repositories[0], 7, ACTOR, "Fixture title", "Fixture body")
        issue = seen["payload"]["issues"][0]
        assert issue["title"] == "Fixture title" and issue["is_root"] is True
    finally:
        store.close()


# ---- #21: a frozen submission --------------------------------------------------------


def to_submission(tmp_path):
    cfg, store, ctrl = ext.build(tmp_path)
    wid = ext.start(ctrl, cfg, ACTOR)
    ctrl.work_plan(wid, ACTOR)
    worker = Worker(cfg, store, ctrl, "w")
    worker.run_loop(2)
    ctrl.approve(wid, "human-tester")
    tree = Path(ctrl.work_checkout(wid, ACTOR)["worktree"])
    (tree / "message.py").write_text(ext.READY)
    return cfg, store, ctrl, wid, worker, tree


def test_edits_after_submission_cannot_change_what_is_checked(tmp_path):
    cfg, store, ctrl, wid, worker, tree = to_submission(tmp_path)
    try:
        ctrl.work_submit(wid, ACTOR)
        (tree / "message.py").write_text('def message():\n    return "TAMPERED"\n')
        worker.run_loop()
        assert store.one("SELECT status FROM work_items")[0] == "awaiting_review"
        root = store.one("SELECT * FROM work_items")
        candidate = json.loads(root["candidate_commits"])[root["repo"]]
        diff = (store.resolve_path(candidate["archive"]) / "diff.patch").read_text()
        assert '"ready"' in diff and "TAMPERED" not in diff
        row = store.one("SELECT * FROM external_submissions")
        assert row["plan_digest"] == root["approved_digest"]
        assert row["base"] == json.loads(root["snapshot_json"])["base"]
        assert row["consumed"] == 1
    finally:
        store.close()


def test_a_captured_submission_that_no_longer_matches_its_digest_is_refused(tmp_path):
    cfg, store, ctrl, wid, worker, tree = to_submission(tmp_path)
    try:
        ctrl.work_submit(wid, ACTOR)
        frozen = Path(store.one("SELECT path FROM external_submissions")[0])
        (frozen / "message.py").write_text("raise SystemExit\n")
        assert worker.run_loop() == 0  # nothing is built from a tree that changed
        assert store.one("SELECT consumed FROM external_submissions")[0] == 2
        assert "work.submission_refused" in {
            r["action"] for r in store.query("SELECT action FROM audit")
        }
    finally:
        store.close()


def test_a_submission_bound_to_an_earlier_plan_is_stale(tmp_path):
    cfg, store, ctrl, wid, worker, tree = to_submission(tmp_path)
    try:
        ctrl.work_submit(wid, ACTOR)
        store.execute("UPDATE external_submissions SET plan_digest='an-older-plan'")
        assert worker.run_loop() == 0
        assert store.one("SELECT consumed FROM external_submissions")[0] == 2
    finally:
        store.close()


def test_unsubmitted_edits_are_saved_before_a_cancelled_checkout_is_removed(tmp_path):
    cfg, store, ctrl, wid, worker, tree = to_submission(tmp_path)
    try:
        (tree / "notes.txt").write_text("hours of unsubmitted work")
        store.execute("UPDATE work_items SET status='cancelled' WHERE id=?", (wid,))
        ctrl.sweep_resources()
        assert not tree.exists()
        patches = list((cfg.state_dir / "external-recovery").glob(f"{wid}-*.patch"))
        assert (
            len(patches) == 1 and "hours of unsubmitted work" in patches[0].read_text()
        )
        assert "external.preserved" in {
            r["action"] for r in store.query("SELECT action FROM audit")
        }
    finally:
        store.close()


def test_nothing_is_saved_when_everything_was_submitted(tmp_path):
    cfg, store, ctrl, wid, worker, tree = to_submission(tmp_path)
    try:
        ctrl.work_submit(wid, ACTOR)
        worker.run_loop()
        store.execute("UPDATE work_items SET status='cancelled' WHERE id=?", (wid,))
        ctrl.sweep_resources()
        assert not tree.exists()
        assert not (cfg.state_dir / "external-recovery").exists() or not list(
            (cfg.state_dir / "external-recovery").glob("*.patch")
        )
    finally:
        store.close()


def test_an_unbuilt_submission_of_a_cancelled_root_is_kept_as_an_archive(tmp_path):
    cfg, store, ctrl, wid, worker, tree = to_submission(tmp_path)
    try:
        ctrl.work_submit(wid, ACTOR)
        store.execute("UPDATE work_items SET status='cancelled' WHERE id=?", (wid,))
        ctrl.sweep_resources()
        kept = list(
            (cfg.state_dir / "external-recovery").glob(f"{wid}-submitted-*.tar.gz")
        )
        assert len(kept) == 1
        assert not (cfg.state_dir / "external-submissions" / wid).exists()
    finally:
        store.close()


# ---- #22: no network inside a write transaction ------------------------------------------


def test_a_slow_code_host_does_not_delay_another_workers_heartbeat(tmp_path):
    cfg, store, ctrl = ext.build(tmp_path)
    try:
        cfg.raw["gate"] = {"enabled": True, "interval_seconds": 0}
        entered = threading.Event()

        def slow(command, operation, payload, request_id, timeout):
            if operation == "list_open_prs":
                entered.set()
                time.sleep(1.5)
                return {"ok": True, "operation": operation, "evidence": {"prs": []}}
            raise AssertionError(operation)

        ctrl.runner.run_json = slow
        store.execute(
            "INSERT INTO workers(id,capabilities,last_heartbeat,busy) VALUES ('w','[]','x',0)"
        )
        thread = threading.Thread(target=ctrl.recover, args=("slow",))
        thread.start()
        assert entered.wait(5)
        began = time.monotonic()
        ctrl.heartbeat("w", None)
        store.one("SELECT 1")
        elapsed = time.monotonic() - began
        assert thread.is_alive(), "the slow call must still be in flight"
        assert elapsed < 0.5, (
            f"a heartbeat waited {elapsed:.2f}s behind a code-host call"
        )
        thread.join()
    finally:
        store.close()


def test_the_execution_service_claim_runs_housekeeping_outside_its_transaction(
    tmp_path, monkeypatch
):
    from test_distributed import deployment

    from dark_factory.execution.service import ExecutionService

    cfg = deployment(tmp_path, monkeypatch)
    store = Store(cfg.state_dir)
    try:
        ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        depths = []
        ctrl.recover = lambda actor="x": depths.append(
            ("recover", store._transaction_depth)
        )
        ctrl.settle_interruptions = lambda actor="x": depths.append(
            ("settle", store._transaction_depth)
        )
        ExecutionService(ctrl).claim("worker1", "poll-1")
        assert depths == [("recover", 0), ("settle", 0)]
    finally:
        store.close()


# ---- shared helpers for publication tests --------------------------------------------------


class Bridge:
    """A code host in memory behind the controller's command runner."""

    def __init__(self, real, crash_after_push=False):
        self.real, self.host = real, FakeCodeHost()
        self.crash_after_push = crash_after_push
        self.pushes = 0
        self.operations = []

    def __call__(self, command, operation, payload, request_id, timeout):
        if operation in self.host.__class__.__dict__.get("OPS", ()) or hasattr(
            self.host, "op_" + operation
        ):
            self.operations.append(operation)
            if operation == "push_branch":
                self.pushes += 1
            result = self.host.handle(make_request(operation, payload, request_id))
            if operation == "push_branch" and self.crash_after_push:
                self.crash_after_push = False
                raise RuntimeError("process died after the remote push")
            return result
        return self.real(command, operation, payload, request_id, timeout)


def go_live(ctrl, store, wid):
    """Treat a simulated, reviewed root as live and re-bind its approval to the new state."""
    snapshot = json.loads(
        store.one("SELECT snapshot_json FROM work_items WHERE id=?", (wid,))[0]
    )
    snapshot["simulation"] = False
    store.execute(
        "UPDATE work_items SET snapshot_json=? WHERE id=?", (json.dumps(snapshot), wid)
    )
    store.execute(
        "UPDATE work_items SET approved_digest=? WHERE id=?",
        (_digest(ctrl._approval_payload(wid)), wid),
    )


def reviewed_external_root(cfg, store, ctrl, number=7):
    wid = ctrl.work_start(cfg.repositories[0], number, ACTOR, "Make it ready", "")[
        "work_item_id"
    ]
    ctrl.work_plan(wid, ACTOR)
    worker = Worker(cfg, store, ctrl, "w")
    worker.run_loop(2)
    ctrl.approve(wid, "human-tester")
    tree = Path(ctrl.work_checkout(wid, ACTOR)["worktree"])
    (tree / "message.py").write_text(ext.READY)
    ctrl.work_submit(wid, ACTOR)
    worker.run_loop()
    assert (
        store.one("SELECT status FROM work_items WHERE id=?", (wid,))[0]
        == "awaiting_review"
    )
    return wid


def test_a_restart_between_the_remote_push_and_local_reconciliation_never_pushes_twice(
    tmp_path,
):
    cfg, store, ctrl = ext.build(tmp_path)
    try:
        wid = reviewed_external_root(cfg, store, ctrl)
        go_live(ctrl, store, wid)
        bridge = Bridge(ctrl.runner.run_json, crash_after_push=True)
        ctrl.runner.run_json = bridge
        with pytest.raises(RuntimeError, match="died after the remote push"):
            ctrl.external_tick()  # the branch is on the host; the local record is not settled
        assert store.one("SELECT status FROM pending_actions")[0] == "ambiguous"
        store.close()
        # A new process opens the same state and finds the pending action.
        store2 = Store(cfg.state_dir)
        try:
            ctrl2 = Controller(cfg, store2, Path("src/dark_factory/agents/prompts"))
            ctrl2.runner.run_json = bridge
            published = ctrl2.external_tick()
            assert len(published) == 1 and published[0]["published"].startswith(
                "https://fake.test/"
            )
            assert bridge.pushes == 1, (
                "the retry must reconcile with the host, not push again"
            )
            assert "reconcile_push_branch" in bridge.operations
            assert {
                r["status"] for r in store2.query("SELECT status FROM pending_actions")
            } == {"succeeded"}
            assert len(bridge.host.prs[cfg.repositories[0]]) == 1
        finally:
            store2.close()
        store = Store(cfg.state_dir)  # so the outer finally can close it
    finally:
        store.close()


# ---- #23: the target branch is pinned with the approved base ---------------------------------


def with_target(monkeypatch, branch):
    original = planning.fixture

    def fixture(root):
        return {**original(root), "target_branch": branch}

    monkeypatch.setattr(planning, "fixture", fixture)


def test_each_root_publishes_against_the_target_pinned_when_it_was_approved(
    tmp_path, monkeypatch
):
    with_target(monkeypatch, "release/2.4")
    cfg, store, ctrl = ext.build(tmp_path)
    cfg.raw["improvement"] = {"allowed_paths": ["message.py"], "base_branch": "develop"}
    try:
        contributor = reviewed_external_root(cfg, store, ctrl)
        imp.seed_rejections(store)
        ctrl.improvement_tick()
        worker = Worker(cfg, store, ctrl, "w")
        worker.run_loop(2)
        ctrl.improvement_tick()  # standing policy approves
        worker.run_loop()
        improvement = store.one(
            "SELECT id FROM work_items WHERE origin LIKE 'improvement:%'"
        )["id"]
        for wid in (contributor, improvement):
            go_live(ctrl, store, wid)
        bridge = Bridge(ctrl.runner.run_json)
        ctrl.runner.run_json = bridge
        ctrl.external_tick()
        ctrl.improvement_tick()
        prs = {
            pr["head_ref"]: pr["base"] for pr in bridge.host.prs[cfg.repositories[0]]
        }
        bases = {
            ref.split("/")[1]: base for ref, base in prs.items()
        }  # work or improvement
        assert bases == {"work": "release/2.4", "improvement": "develop"}
    finally:
        store.close()


def test_a_profile_change_after_approval_cannot_redirect_publication(
    tmp_path, monkeypatch
):
    with_target(monkeypatch, "release/2.4")
    cfg, store, ctrl = ext.build(tmp_path)
    try:
        wid = reviewed_external_root(cfg, store, ctrl)
        go_live(ctrl, store, wid)
        snapshot = json.loads(store.one("SELECT snapshot_json FROM work_items")[0])
        assert (
            snapshot["target_branch"] == "release/2.4"
        )  # pinned beside the base commit
        bridge = Bridge(ctrl.runner.run_json)
        ctrl.runner.run_json = bridge
        # The operator edits the profile afterwards: approval no longer covers it, so
        # nothing is published, and certainly not against the new branch.
        cfg.raw["improvement"] = {
            **cfg.raw.get("improvement", {}),
            "base_branch": "elsewhere",
        }
        with pytest.raises(ControllerError, match="approved inputs changed"):
            ctrl.external_tick()
        assert not bridge.host.prs
    finally:
        store.close()


# ---- #24: the publish event records the root's origin ------------------------------------------


def skipped_events(store):
    return [
        json.loads(r["payload_json"])
        for r in store.query(
            "SELECT payload_json FROM audit WHERE action='publish.skipped'"
        )
    ]


def test_the_shared_publication_event_names_the_root_origin_and_reason(tmp_path):
    cfg, store, ctrl = ext.build(tmp_path)
    cfg.raw["improvement"] = {
        "allowed_paths": ["message.py"],
        "max_files": 1,
        "max_changed_lines": 10,
    }
    try:
        contributor = reviewed_external_root(cfg, store, ctrl)
        ctrl.external_tick()
        imp.seed_rejections(store)
        ctrl.improvement_tick()
        worker = Worker(cfg, store, ctrl, "w")
        worker.run_loop(2)
        ctrl.improvement_tick()
        worker.run_loop()
        ctrl.improvement_tick()
        improvement = store.one(
            "SELECT id FROM work_items WHERE origin LIKE 'improvement:%'"
        )["id"]
        events = {e["root"]: e for e in skipped_events(store)}
        assert events[contributor] == {
            "root": contributor,
            "origin": "tracker",
            "reason": "simulation",
        }
        assert events[improvement] == {
            "root": improvement,
            "origin": "improvement:human-rejection",
            "reason": "simulation",
        }
    finally:
        store.close()
