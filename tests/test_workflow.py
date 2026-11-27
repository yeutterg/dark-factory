"""Native dispatch: complete outcome, backup, and restart of unstarted claims."""

import json
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory import artifacts
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.worker import Worker


def setup(tmp_path, repairs=0):
    cfg = cfg_for(tmp_path)
    if repairs:
        cfg.raw["continuation"] = {"max_repairs": repairs}
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
    ctrl.start_planning(wid)
    return cfg, store, ctrl, wid, Worker(cfg, store, ctrl, "fixture-worker")


def test_backup_excludes_interrupted_credentials(tmp_path):
    store = Store(tmp_path / "state")
    credential_dir = store.state_dir / "runtime" / "interrupted" / "codex-agent"
    credential_dir.mkdir(parents=True)
    (credential_dir / "auth.json").write_text("fixture-secret")
    destination = tmp_path / "backup"
    try:
        artifacts.backup(store, destination)
        artifacts.verify(destination)
        assert not list((destination / "runtime").rglob("auth.json"))
        artifacts.restore(destination, tmp_path / "restored")
    finally:
        store.close()


def test_complete_outcome_preserves_cli_and_acceptance_contract(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    try:
        assert worker.run_loop(2) == 2
        assert ctrl.continuation(wid)["next_action"]["kind"] == "human_approval"
        ctrl.approve(wid, "human-tester")
        assert worker.run_loop() == 3
        view = ctrl.continuation(wid)
        assert view["next_action"]["kind"] == "human_review"
        assert all(j["state"] == "verified" for j in view["work"])
        candidate = json.loads(
            store.one("SELECT candidate_commits FROM work_items WHERE id=?", (wid,))[0]
        )[cfg.repositories[0]]["commit"]
        with pytest.raises(ControllerError, match="manual gate missing"):
            ctrl.accept(wid, "human-tester")
        ctrl.record_manual_gate(
            wid,
            "Inspect candidate diff",
            candidate,
            "human-tester",
            "Inspected candidate behavior",
        )
        ctrl.accept(wid, "human-tester")
        assert ctrl.continuation(wid)["next_action"]["kind"] == "complete"
        assert not list((tmp_path / "workspaces").iterdir())
    finally:
        store.close()


def test_restart_reacquires_unstarted_claim_without_a_second_attempt(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    claim = ctrl.claim_job(worker.worker_id, ["native", "fake", "environment:local"])
    aid = claim["attempt_id"]
    try:
        # The worker died between claim and start; a new process finds the claim.
        restarted = Worker(cfg, store, ctrl, worker.worker_id)
        assert restarted.run_once()
        assert len(store.query("SELECT * FROM attempts")) == 1
        assert (
            store.one("SELECT status FROM attempts WHERE id=?", (aid,))[0]
            == "succeeded"
        )
    finally:
        store.close()


def test_unstarted_claim_of_another_worker_is_not_taken(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    claim = ctrl.claim_job("other-worker", ["native", "fake", "environment:local"])
    try:
        assert worker._unstarted_claims() == []
        assert not worker.run_once()
        assert (
            store.one("SELECT status FROM attempts WHERE id=?", (claim["attempt_id"],))[
                0
            ]
            == "leased"
        )
    finally:
        store.close()


def test_running_interrupted_cli_is_not_replayed(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    claim = ctrl.claim_job(worker.worker_id, ["native", "fake", "environment:local"])
    store.execute(
        "UPDATE attempts SET status='running' WHERE id=?", (claim["attempt_id"],)
    )
    try:
        assert worker._unstarted_claims() == []
        assert len(store.query("SELECT * FROM attempts")) == 1
    finally:
        store.close()
