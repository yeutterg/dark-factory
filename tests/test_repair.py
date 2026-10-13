"""Continue authorized work through bounded healing and fresh verification before review."""

import json
from pathlib import Path

import pytest
from test_workflow import durable, setup

from dark_factory.adapters.harness.fake import FakeHarness
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.worker import Worker


def install_failure(worker, monkeypatch, mode="check", repeat=False):
    calls = {"coder": 0, "reviewer": 0}

    class Harness(FakeHarness):
        def start(self, prompt, workspace, extra=None):
            yield from super().start(prompt, workspace, extra)
            role = extra["role"]
            if role in calls:
                calls[role] += 1
            if role == "coder" and mode == "check" and (calls[role] == 1 or repeat):
                (Path(workspace) / "message.py").write_text(
                    'def message():\n    return "incorrect"\n'
                )
                self._last.output = json.dumps(
                    {
                        "summary": "First implementation",
                        "assumptions": ["Preserve the existing public function name."],
                    }
                )
            if role == "reviewer" and mode == "review" and (calls[role] == 1 or repeat):
                review = json.loads(self._last.output)
                review.update(
                    passed=False,
                    findings=["Correct the identified edge case before human review"],
                )
                for criterion in review["criteria"]:
                    criterion["status"] = "failed"
                self._last.output = json.dumps(review)

    monkeypatch.setattr(worker, "_harness", lambda route: Harness())
    return calls, Harness


@pytest.mark.parametrize(
    "engine", ["native", pytest.param("pi-durable", marks=durable)]
)
@pytest.mark.parametrize("failure", ["check", "review"])
def test_self_heals_and_rechecks_before_first_human_gate(
    tmp_path, monkeypatch, engine, failure
):
    cfg, store, ctrl, wid, worker = setup(tmp_path, engine, repairs=1)
    calls, _ = install_failure(worker, monkeypatch, failure)
    try:
        worker.run_loop(2)
        ctrl.approve(wid, "human-tester")
        digest = store.one("SELECT approved_digest FROM work_items WHERE id=?", (wid,))[
            0
        ]
        worker.run_loop()
        assert calls["coder"] == 2
        assert calls["reviewer"] == (2 if failure == "review" else 1)
        assert (
            store.one("SELECT approved_digest FROM work_items WHERE id=?", (wid,))[0]
            == digest
        )
        packet = ctrl._packet(wid)
        assert packet["ready_for_human_review"]
        assert packet["exec_summary"]["repairs"]
        assert packet["exec_summary"]["assumptions"]
        assert packet["exec_summary"]["implementation_assumptions_recorded"]
        assert all(
            not [e for e in c["missing_evidence"] if not e.startswith("manual:")]
            for c in packet["acceptance_coverage"]
        )
        assert any(a["status"] == "failed" for a in packet["attempts"])
        candidate = packet["candidate"][cfg.repositories[0]]["commit"]
        ctrl.record_manual_gate(
            wid,
            "Inspect candidate diff",
            candidate,
            "human-tester",
            "Checked repaired candidate",
        )
        ctrl.accept(wid, "human-tester")
    finally:
        store.close()


def test_repair_allowance_is_not_reset_when_checks_keep_failing(tmp_path, monkeypatch):
    cfg, store, ctrl, wid, worker = setup(tmp_path, repairs=1)
    calls, _ = install_failure(worker, monkeypatch, repeat=True)
    try:
        worker.run_loop(2)
        ctrl.approve(wid, "human-tester")
        worker.run_loop()
        assert calls["coder"] == 2
        assert ctrl.continuation(wid)["next_action"]["kind"] == "blocked"
        assert (
            store.one("SELECT status FROM work_items WHERE id=?", (wid,))[0] == "failed"
        )
        assert not worker.run_once()
        with pytest.raises(ControllerError):
            ctrl.accept(wid, "human-tester")
    finally:
        store.close()


def test_unused_repair_does_not_block_human_review(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path, repairs=1)
    try:
        worker.run_loop(2)
        ctrl.approve(wid, "human-tester")
        assert worker.run_loop() == 3
        assert ctrl._packet(wid)["ready_for_human_review"]
        optional = [
            j for j in ctrl._jobs(wid) if json.loads(j["spec_json"]).get("repair")
        ]
        assert len(optional) == 3
        assert all(j["status"] == "not_needed" for j in optional)
    finally:
        store.close()


def test_restart_and_status_between_failure_and_repair_preserve_next_action(
    tmp_path, monkeypatch
):
    cfg, store, ctrl, wid, worker = setup(tmp_path, repairs=1)
    calls, harness = install_failure(worker, monkeypatch)
    worker.run_loop(2)
    ctrl.approve(wid, "human-tester")
    worker.run_loop(2)  # implementation plus failed check; repair is queued durably
    assert ctrl.status()["continuations"][0]["next_action"]["kind"] == "dispatch"
    store.close()
    store = Store(tmp_path)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    worker = Worker(cfg, store, ctrl, "fixture-worker")
    monkeypatch.setattr(worker, "_harness", lambda route: harness())
    try:
        assert worker.run_loop() == 3
        assert calls["coder"] == 2
        assert ctrl.continuation(wid)["next_action"]["kind"] == "human_review"
        assert len(store.query("SELECT * FROM attempts")) == 7
    finally:
        store.close()
