"""Controller acceptance and continuation regressions from issues #8 and #9."""

import json

import pytest
from test_workflow import setup

from dark_factory.controller import ControllerError
from dark_factory.worker import Worker


def ready(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    ctrl.approve(wid, "human-tester")
    worker.run_loop()
    candidate = json.loads(
        store.one("SELECT candidate_commits FROM work_items WHERE id=?", (wid,))[0]
    )[cfg.repositories[0]]["commit"]
    return cfg, store, ctrl, wid, worker, candidate


def test_worker_cannot_attest_or_accept_own_candidate(tmp_path):
    cfg, store, ctrl, wid, worker, sha = ready(tmp_path)
    try:
        for actor in (worker.worker_id, "agent:reviewer", "model-review"):
            with pytest.raises(ControllerError, match="worker identities"):
                ctrl.record_manual_gate(
                    wid, "Inspect candidate diff", sha, actor, "claims passed"
                )
        ctrl.record_manual_gate(
            wid, "Inspect candidate diff", sha, "human-tester", "checked candidate"
        )
        with pytest.raises(ControllerError, match="worker identities"):
            ctrl.accept(wid, worker.worker_id)
        packet = json.loads(
            store.one("SELECT packet_json FROM work_items WHERE id=?", (wid,))[0]
        )
        gate = packet["manual_attestations"][0]
        assert (
            gate["actor"] == "human-tester" and gate["candidate"] == sha and gate["at"]
        )
        assert gate["evidence"] == "checked candidate"
    finally:
        store.close()


def test_configured_human_identity_is_required_and_candidate_is_exact(tmp_path):
    cfg, store, ctrl, wid, worker, sha = ready(tmp_path)
    try:
        cfg.raw["operators"] = {"humans": ["authorized-human"]}
        with pytest.raises(ControllerError, match="not configured"):
            ctrl.record_manual_gate(
                wid, "Inspect candidate diff", sha, "outsider", "checked"
            )
        with pytest.raises(ControllerError, match="candidate mismatch"):
            ctrl.record_manual_gate(
                wid, "Inspect candidate diff", "stale", "authorized-human", "checked"
            )
        with pytest.raises(ControllerError, match="approved inputs changed"):
            ctrl.accept(wid, "authorized-human")
    finally:
        store.close()


def test_green_jobs_without_connected_check_evidence_are_not_ready(tmp_path):
    cfg, store, ctrl, wid, worker, sha = ready(tmp_path)
    try:
        att = store.one(
            "SELECT a.* FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.work_item_id=? AND j.kind='check'",
            (wid,),
        )
        result = json.loads(att["result_json"])
        result[
            "checks"
        ] = []  # Green component completion with the required link absent.
        store.execute(
            "UPDATE attempts SET result_json=? WHERE id=?",
            (json.dumps(result), att["id"]),
        )
        ctrl.reconcile_continuations()
        assert all(j["status"] == "succeeded" for j in ctrl.jobs(wid))
        assert ctrl.continuation(wid)["next_action"]["kind"] == "blocked"
        assert not ctrl._packet(wid)["ready_for_human_review"]
        assert "check:fixture-check" in ctrl._packet(wid)["missing_evidence"]
    finally:
        store.close()


def test_dropped_handoff_reconciles_without_new_scope_or_duplicate_dispatch(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    try:
        worker.run_loop(2)
        ctrl.approve(wid, "human-tester")
        assert worker.run_once()
        check = next(j for j in ctrl.jobs(wid) if j["kind"] == "check")
        store.execute("UPDATE jobs SET status='blocked' WHERE id=?", (check["id"],))
        before = ctrl._approval_payload(wid)
        # A fresh worker reconciles the saved root, including after a status request.
        assert ctrl.status()["continuations"][0]["root_id"] == wid
        assert Worker(cfg, store, ctrl, worker.worker_id).run_loop() == 2
        assert ctrl._approval_payload(wid) == before
        assert ctrl.continuation(wid)["next_action"]["kind"] == "human_review"
        assert len(store.query("SELECT * FROM attempts")) == 5
        persisted = json.loads(store.get_op("continuation:" + wid))
        assert persisted["next_action"]["kind"] == "human_review"
    finally:
        store.close()


def test_rejection_is_input_to_revised_acceptance_and_requires_new_approval(tmp_path):
    cfg, store, ctrl, wid, worker, sha = ready(tmp_path)
    try:
        ctrl.reject(
            wid, "human-tester", "Add an acceptance criterion for integration behavior"
        )
        ctrl.start_planning(wid)
        snapshot = json.loads(
            store.one("SELECT snapshot_json FROM work_items WHERE id=?", (wid,))[0]
        )
        assert snapshot["revision_feedback"][0]["decision"]["reason"].startswith(
            "Add an acceptance"
        )
        assert not store.one(
            "SELECT approved_digest FROM work_items WHERE id=?", (wid,)
        )[0]
        worker.run_loop(2)
        assert ctrl.continuation(wid)["next_action"]["kind"] == "human_approval"
    finally:
        store.close()


def test_waived_model_assessment_is_invalid(tmp_path):
    cfg, store, ctrl, wid, worker, sha = ready(tmp_path)
    try:
        row = store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        proposal = json.loads(row["plan_json"])["proposal"]
        criteria = [
            {
                "id": c["id"],
                "status": "waived",
                "evidence": ["check:" + v for v in c["checks"]],
            }
            for c in proposal["acceptance"]
        ]
        with pytest.raises(ValueError, match="invalid criterion status"):
            ctrl._parse_role(
                "review",
                json.dumps(
                    {
                        "passed": True,
                        "summary": "waived",
                        "findings": [],
                        "criteria": criteria,
                    }
                ),
                json.loads(row["snapshot_json"]),
                proposal,
            )
    finally:
        store.close()
