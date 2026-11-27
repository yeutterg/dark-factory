"""Distributed qualification: a second worker completes checkpointed work."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from test_distributed import deployment, selected
from test_workflow import setup

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult
from dark_factory.adapters.harness.fake import FakeHarness
from dark_factory.adapters.harness.registry import ADAPTERS, HarnessAdapter
from dark_factory.controller import Controller
from dark_factory.execution.executor import execute_assignment
from dark_factory.execution.packages import pack
from dark_factory.execution.service import ExecutionService
from dark_factory.store import Store
from dark_factory.ui.projection import controller_projection


class CheckpointThenDisconnect(FakeHarness):
    def start(self, prompt, workspace, extra=None):
        if (extra or {}).get("role") != "coder":
            yield from super().start(prompt, workspace, extra)
            return
        Path(workspace, "message.py").write_text(
            '# worker-a-milestone\ndef message():\n    return "partial"\n'
        )
        yield HarnessEvent("task_progress", "saved unique edit", {"task_id": "fixture"})
        Path(workspace, "message.py").write_text(
            '# worker-a-milestone-newest\ndef message():\n    return "partial"\n'
        )
        yield HarnessEvent("task_progress", "saved newest edit", {"task_id": "fixture"})
        self._last = HarnessResult(
            False, "", {}, "worker-a", cost_cents=1, error="HTTP 503 disconnected"
        )


class CompleteRestoredWork(FakeHarness):
    def start(self, prompt, workspace, extra=None):
        if (extra or {}).get("role") != "coder":
            yield from super().start(prompt, workspace, extra)
            return
        path = Path(workspace, "message.py")
        assert "worker-a-milestone-newest" in path.read_text()
        path.write_text(
            '# worker-a-milestone-newest\ndef message():\n    return "ready"\n'
        )
        body = json.dumps(
            {
                "summary": "Completed worker A's checkpointed edit",
                "assumptions": ["The canonical fixture check remains authoritative."],
            }
        )
        yield HarnessEvent("text", body)
        self._last = HarnessResult(
            True, body, {"input": 10, "output": 10}, "worker-b", cost_cents=1
        )


def run_remote(service, worker, request_id, directory):
    assignment = service.claim(worker, request_id)
    assert assignment is not None
    aid = assignment["claim"]["attempt_id"]
    assert service.start(worker, aid, uuid.uuid4().hex)
    result = execute_assignment(
        assignment, directory, lambda: service.heartbeat(worker, aid)
    )
    assert result["receipt"]["ok"], result["receipt"]
    service.finish(worker, result)
    return assignment, result


def test_worker_b_reaches_review_once_after_worker_a_disk_is_unavailable(
    tmp_path, monkeypatch
):
    cfg = deployment(tmp_path, monkeypatch)
    store = Store(cfg.state_dir)
    try:
        ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        root_id = selected(ctrl)
        service = ExecutionService(ctrl)

        run_remote(service, "worker1", "plan", tmp_path / "plan")
        run_remote(service, "worker1", "critique", tmp_path / "critique")
        ctrl.approve(root_id, "human")

        assignment = service.claim("worker1", "worker-a")
        aid = assignment["claim"]["attempt_id"]
        assert service.start("worker1", aid, uuid.uuid4().hex)
        monkeypatch.setitem(
            ADAPTERS, "fake", HarnessAdapter("fake", CheckpointThenDisconnect)
        )
        late_result = execute_assignment(assignment, tmp_path / "worker-a")
        checkpoints = list((tmp_path / "worker-a" / "checkpoints" / aid).iterdir())
        assert len(checkpoints) == 2
        for checkpoint in reversed(checkpoints):
            service.checkpoint(
                "worker1",
                {
                    "attempt_id": aid,
                    "checkpoint_id": checkpoint.name,
                    "checkpoint": pack({"checkpoint": checkpoint}),
                },
            )
        active = next(
            worker["active"]
            for worker in controller_projection(ctrl)["workers"]
            if worker["id"] == "worker1"
        )
        assert active["last_checkpoint"]["detail"]["summary"] == "saved newest edit"
        assert active["remaining_seconds"] > 0
        assert active["remaining_cents"] >= 0

        store.execute(
            "UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' "
            "WHERE id=(SELECT job_id FROM attempts WHERE id=?)",
            (aid,),
        )
        assert ctrl.recover("qualification") == 1
        ctrl.settle_interruptions("qualification")

        monkeypatch.setitem(
            ADAPTERS, "fake", HarnessAdapter("fake", CompleteRestoredWork)
        )
        resumed, _ = run_remote(service, "worker2", "worker-b", tmp_path / "worker-b")
        resume = resumed["claim"]["inputs"]["resume"]
        assert resume["attempt"] == aid
        assert "archives/resume" in resumed["references"].values()

        # Historical artifacts can be unavailable or exceed the transport cap;
        # only the current candidate and selected resume are assignment inputs.
        store.execute(
            "UPDATE attempts SET archive_path=? WHERE id=?",
            (str(tmp_path / "unavailable-oversized-history"), aid),
        )
        monkeypatch.setitem(ADAPTERS, "fake", HarnessAdapter("fake", FakeHarness))
        run_remote(service, "worker2", "check", tmp_path / "check")
        run_remote(service, "worker2", "review", tmp_path / "review")

        root = store.one("SELECT * FROM work_items WHERE id=?", (root_id,))
        assert root["status"] == "awaiting_review"
        candidate = json.loads(root["candidate_commits"])[root["repo"]]
        archive = store.resolve_path(candidate["archive"])
        assert (
            "worker-a-milestone" in (archive / "workspace" / "message.py").read_text()
        )
        before = candidate["commit"]

        terminal = service.finish_remote("worker1", late_result)
        assert terminal["terminal"] and terminal["disposition"] == "fenced"
        root = store.one("SELECT * FROM work_items WHERE id=?", (root_id,))
        assert json.loads(root["candidate_commits"])[root["repo"]]["commit"] == before
        assert (
            store.one(
                "SELECT COUNT(*) n FROM attempts a JOIN jobs j ON j.id=a.job_id "
                "WHERE j.work_item_id=? AND j.kind='review'",
                (root_id,),
            )["n"]
            == 1
        )
        assert root["reserved_cents"] == 0
        assert not store.query(
            "SELECT id FROM attempts WHERE status IN ('leased','running','cancelling')"
        )
    finally:
        store.close()


def test_interrupted_repair_restores_its_verified_checkpoint(tmp_path, monkeypatch):
    cfg, store, ctrl, root_id, worker = setup(tmp_path, repairs=1)
    coder_calls = 0

    class RepairCheckpoint(FakeHarness):
        def start(self, prompt, workspace, extra=None):
            nonlocal coder_calls
            if (extra or {}).get("role") != "coder":
                yield from super().start(prompt, workspace, extra)
                return
            coder_calls += 1
            path = Path(workspace, "message.py")
            if coder_calls == 1:
                yield from super().start(prompt, workspace, extra)
                path.write_text('def message():\n    return "incorrect"\n')
                return
            if coder_calls == 2:
                path.write_text(
                    '# repair-milestone\ndef message():\n    return "still-partial"\n'
                )
                yield HarnessEvent("task_progress", "saved repair", {})
                self._last = HarnessResult(
                    False,
                    "",
                    {},
                    "repair-a",
                    cost_cents=1,
                    error="HTTP 503 disconnected",
                )
                return
            assert "repair-milestone" in path.read_text()
            path.write_text('# repair-milestone\ndef message():\n    return "ready"\n')
            body = json.dumps(
                {
                    "summary": "Completed checkpointed repair",
                    "assumptions": ["The canonical check defines correctness."],
                }
            )
            yield HarnessEvent("text", body)
            self._last = HarnessResult(True, body, {}, "repair-b", cost_cents=1)

    monkeypatch.setattr(worker, "_harness", lambda route: RepairCheckpoint())
    try:
        worker.run_loop(2)
        ctrl.approve(root_id, "human")
        worker.run_loop(2)  # initial implementation, then failed canonical check
        assert worker.run_once()  # interrupted repair with a milestone checkpoint
        interrupted = store.one("SELECT * FROM interruptions ORDER BY at DESC LIMIT 1")
        assert (
            store.one(
                "SELECT COUNT(*) n FROM attempt_checkpoints WHERE attempt_id=?",
                (interrupted["attempt_id"],),
            )["n"]
            == 1
        )
        # Model loss of worker A's teardown archive; the independently persisted
        # checkpoint remains eligible for worker B's bounded repair resume.
        store.execute(
            "UPDATE attempts SET archive_path=NULL WHERE id=?",
            (interrupted["attempt_id"],),
        )
        ctrl.settle_interruptions("qualification")
        assert worker.run_once()
        resumed = store.one(
            "SELECT inputs_json FROM attempts WHERE id=?",
            (
                store.one(
                    "SELECT resumed_by_attempt FROM interruptions WHERE id=?",
                    (interrupted["id"],),
                )[0],
            ),
        )
        assert json.loads(resumed[0])["resume"]["checkpoint"].startswith("cp_")
        assert coder_calls == 3
    finally:
        store.close()
