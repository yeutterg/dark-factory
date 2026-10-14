"""Regression cases for the five post-merge durable-runtime review findings."""

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from test_continuation import ready
from test_repair import install_failure
from test_workflow import durable, setup

from dark_factory import artifacts
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.worker import git


@durable
def test_claim_without_engine_checkpoint_resumes_once(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path, "pi-durable")
    try:
        claim = ctrl.claim_job(worker.worker_id, ["native", "fake"])
        assert not (tmp_path / "workflow").exists()
        assert worker.run_once()
        assert len(store.query("SELECT * FROM attempts")) == 1
        assert (
            store.one("SELECT status FROM attempts WHERE id=?", (claim["attempt_id"],))[
                0
            ]
            == "succeeded"
        )
        assert (
            tmp_path / "workflow" / claim["attempt_id"] / "dispatch.sqlite"
        ).exists()
        assert worker.run_once()  # the next job, not the original assignment
        assert len(store.query("SELECT * FROM attempts")) == 2
    finally:
        store.close()


@durable
@pytest.mark.parametrize(
    "guard", ["worker", "workspace", "pid", "lease", "pause", "policy"]
)
def test_missing_checkpoint_discovery_does_not_grant_dispatch_authority(
    tmp_path, guard
):
    cfg, store, ctrl, wid, worker = setup(tmp_path, "pi-durable")
    try:
        claim = ctrl.claim_job(worker.worker_id, ["native", "fake"])
        if guard == "worker":
            worker.worker_id = "different-worker"
        elif guard == "workspace":
            store.execute(
                "UPDATE attempts SET workspace=? WHERE id=?",
                (str(tmp_path / "preserved"), claim["attempt_id"]),
            )
        elif guard == "pid":
            store.execute(
                "UPDATE attempts SET pid=123 WHERE id=?", (claim["attempt_id"],)
            )
        elif guard == "lease":
            store.execute(
                "UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                (claim["job"]["id"],),
            )
        elif guard == "pause":
            ctrl.pause("human")
        else:
            cfg.routes["fake-coder"].region = "CA"
        if guard == "policy":
            with pytest.raises(ControllerError, match="policy changed"):
                worker.run_once()
        else:
            assert not worker.run_once()
        assert not (tmp_path / "workflow").exists()
        assert len(store.query("SELECT * FROM attempts")) == 1
    finally:
        store.close()


def test_completed_candidate_remains_reviewable_after_execution_limits(tmp_path):
    cfg, store, ctrl, wid, worker, sha = ready(tmp_path)
    try:
        store.execute(
            "UPDATE work_items SET spent_cents=spending_ceiling_cents WHERE id=?",
            (wid,),
        )
        assert ctrl._packet(wid)["ready_for_human_review"]
        ctrl.pause("human")
        ctrl.drain("human")

        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=30)

        with patch("dark_factory.controller.datetime", FutureClock):
            assert ctrl._packet(wid)["ready_for_human_review"]
            ctrl.record_manual_gate(
                wid,
                "Inspect candidate diff",
                sha,
                "human-tester",
                "Reviewed exact candidate",
            )
            ctrl.accept(wid, "human-tester")
        assert ctrl.continuation(wid)["next_action"]["kind"] == "complete"
    finally:
        store.close()


def test_limits_do_not_hide_changed_approval_at_human_gate(tmp_path):
    cfg, store, ctrl, wid, worker, sha = ready(tmp_path)
    try:
        store.execute(
            "UPDATE work_items SET spent_cents=spending_ceiling_cents WHERE id=?",
            (wid,),
        )
        cfg.routes["fake-coder"].region = "CA"
        packet = ctrl._packet(wid)
        assert not packet["ready_for_human_review"]
        assert (
            "approved inputs changed" in packet["continuation"]["next_action"]["reason"]
        )
    finally:
        store.close()


def test_rejection_refreshes_stored_result_before_replanning(tmp_path):
    cfg, store, ctrl, wid, worker, sha = ready(tmp_path)
    try:
        ctrl.reject(wid, "human-tester", "Required integration behavior is missing")
        packet = json.loads(
            store.one("SELECT packet_json FROM work_items WHERE id=?", (wid,))[0]
        )
        assert packet["status"] == packet["exec_summary"]["outcome"] == "rejected"
        assert not packet["ready_for_human_review"]
        assert packet["exec_summary"]["next_action"]["kind"] == "blocked"
        assert packet["error"] == "Required integration behavior is missing"
        assert "replan" in packet["recommended_next_step"]
    finally:
        store.close()


def test_proposal_rejection_preserves_proposal_packet(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    try:
        worker.run_loop(2)
        before = store.one("SELECT packet_json FROM work_items WHERE id=?", (wid,))[0]
        ctrl.reject(wid, "human-tester", "Revise the proposal")
        assert (
            store.one("SELECT packet_json FROM work_items WHERE id=?", (wid,))[0]
            == before
        )
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["integrity", "execution", "archive", "approval"])
def test_non_correctness_failure_cannot_activate_repair(tmp_path, monkeypatch, failure):
    cfg, store, ctrl, wid, worker = setup(tmp_path, repairs=1)
    calls, _ = install_failure(worker, monkeypatch)
    try:
        worker.run_loop(2)
        ctrl.approve(wid, "human-tester")
        worker.run_once()
        if failure in {"integrity", "execution"}:

            def verification(workspace, *args):
                if args == ("diff", "HEAD", "--"):
                    if failure == "execution":
                        raise RuntimeError("verification inspection failed")
                    (Path(workspace) / "message.py").write_text(
                        'def message():\n    return "check-mutated-source"\n'
                    )
                return git(workspace, *args)

            monkeypatch.setattr("dark_factory.worker.git", verification)
        else:
            finish = ctrl.finish_attempt

            def invalidate(*args, **kwargs):
                if failure == "approval":
                    cfg.routes["fake-coder"].region = "CA"
                    return finish(*args, **kwargs)
                with patch.object(
                    artifacts,
                    "verify",
                    side_effect=ValueError("invalid archive fixture"),
                ):
                    return finish(*args, **kwargs)

            monkeypatch.setattr(ctrl, "finish_attempt", invalidate)
        worker.run_once()
        assert calls["coder"] == 1
        assert not store.query("SELECT * FROM audit WHERE action='repair.activate'")
        assert (
            store.one("SELECT status FROM work_items WHERE id=?", (wid,))[0] == "failed"
        )
        assert all(
            j["status"] == "standby"
            for j in ctrl.jobs(wid)
            if json.loads(j["spec_json"]).get("repair")
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    "invalid",
    [
        "classification",
        "missing",
        "duplicate",
        "binding",
        "argv",
        "candidate",
        "exit",
        "command",
    ],
)
def test_repair_requires_complete_typed_check_evidence(tmp_path, invalid):
    cfg, store, ctrl, wid, worker = setup(tmp_path, repairs=1)
    try:
        root = dict(store.one("SELECT * FROM work_items WHERE id=?", (wid,)))
        root["candidate_commits"] = json.dumps(
            {root["repo"]: {"commit": "candidate-sha"}}
        )
        snapshot = json.loads(root["snapshot_json"])
        cid = snapshot["source"]["checks"][0]
        binding = snapshot["bindings"][cid]
        result = {
            "failure_kind": "canonical-check-failure",
            "checks": [
                {
                    "command": cid,
                    "exit_code": 1,
                    "binding": copy.deepcopy(binding),
                    "argv": binding["argv"],
                    "candidate": "candidate-sha",
                }
            ],
        }
        assert ctrl._checks_authorize_repair(root, snapshot, result)
        check = result["checks"][0]
        if invalid == "classification":
            result["failure_kind"] = "integrity"
        elif invalid == "missing":
            result["checks"] = []
        elif invalid == "duplicate":
            result["checks"].append(copy.deepcopy(check))
        elif invalid == "binding":
            check["binding"] = {}
        elif invalid == "argv":
            check["argv"] = ["unapproved"]
        elif invalid == "candidate":
            check["candidate"] = "stale"
        elif invalid == "exit":
            check["exit_code"] = -9
        else:
            check["command"] = "invented"
        assert not ctrl._checks_authorize_repair(root, snapshot, result)
    finally:
        store.close()


@pytest.mark.parametrize("recovery", ["startup", "expired_lease"])
def test_retired_attempt_scrubs_only_declared_credentials(tmp_path, recovery):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    try:
        claim = ctrl.claim_job(worker.worker_id, ["native", "fake"])
        inputs = claim["inputs"]
        inputs["route"]["harness"] = "codex"
        store.execute(
            "UPDATE attempts SET inputs_json=? WHERE id=?",
            (json.dumps(inputs), claim["attempt_id"]),
        )
        runtime = tmp_path / "runtime" / claim["attempt_id"]
        agent = runtime / "codex-agent"
        agent.mkdir(parents=True)
        auth = agent / "auth.json"
        auth.write_text("legacy-fixture-token")
        evidence = runtime / "session.jsonl"
        evidence.write_text("preserved evidence")
        ctrl.scrub_retired_credentials()
        assert auth.exists()  # still leased, not safe to scrub legacy active work
        if recovery == "startup":
            store.execute(
                "UPDATE attempts SET status='failed' WHERE id=?", (claim["attempt_id"],)
            )
            store.close()
            store = Store(tmp_path)
            ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        else:
            store.execute(
                "UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                (claim["job"]["id"],),
            )
            assert ctrl.recover() == 1
        assert not auth.exists()
        assert evidence.read_text() == "preserved evidence"
        assert (
            len(store.query("SELECT * FROM audit WHERE action='credentials.scrub'"))
            == 1
        )
    finally:
        store.close()


def test_explicitly_repeated_checks_can_authorize_only_the_approved_sequence(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path, repairs=1)
    try:
        root = dict(store.one("SELECT * FROM work_items WHERE id=?", (wid,)))
        root["candidate_commits"] = json.dumps(
            {root["repo"]: {"commit": "candidate-sha"}}
        )
        snapshot = json.loads(root["snapshot_json"])
        cid = snapshot["source"]["checks"][0]
        snapshot["source"]["checks"] = [cid, cid]
        binding = snapshot["bindings"][cid]
        check = {
            "command": cid,
            "exit_code": 0,
            "binding": binding,
            "argv": binding["argv"],
            "candidate": "candidate-sha",
        }
        result = {
            "failure_kind": "canonical-check-failure",
            "checks": [copy.deepcopy(check), copy.deepcopy(check)],
        }
        result["checks"][1]["exit_code"] = 1
        assert ctrl._checks_authorize_repair(root, snapshot, result)
        result["checks"].pop()
        assert not ctrl._checks_authorize_repair(root, snapshot, result)
        # Matching counts are insufficient: different commands cannot be substituted.
        snapshot["source"]["checks"] = [cid, "other"]
        snapshot["bindings"]["other"] = binding
        result["checks"] = [dict(check, command="other", exit_code=1), check]
        assert not ctrl._checks_authorize_repair(root, snapshot, result)
    finally:
        store.close()
