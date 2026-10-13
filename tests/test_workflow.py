"""Real durable runtime integration with native fixture workers and private test state."""

import fcntl
import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory import artifacts
from dark_factory.adapters.workflow.pi_durable import ENGINE_DIR, PiDurableRuntime
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.worker import Worker


def setup(tmp_path, engine="native", repairs=0):
    cfg = cfg_for(tmp_path)
    cfg.raw["workflow"] = {"engine": engine}
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


durable = pytest.mark.skipif(
    not (ENGINE_DIR / "node_modules").is_dir(),
    reason="optional pinned Pi Durable dependencies are not installed",
)


def test_backup_preserves_checkpoints_not_interrupted_credentials(tmp_path):
    store = Store(tmp_path / "state")
    workflow = store.state_dir / "workflow"
    workflow.mkdir()
    (workflow / "dispatch.sqlite").write_bytes(b"checkpoint-fixture")
    credential_dir = store.state_dir / "runtime" / "interrupted" / "codex-agent"
    credential_dir.mkdir(parents=True)
    (credential_dir / "auth.json").write_text("fixture-secret")
    destination = tmp_path / "backup"
    try:
        with (workflow / "owner.lock").open("w") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(ValueError, match="drain the workflow"):
                artifacts.backup(store, destination)
        artifacts.backup(store, destination)
        artifacts.verify(destination)
        assert (
            destination / "workflow" / "dispatch.sqlite"
        ).read_bytes() == b"checkpoint-fixture"
        assert not list((destination / "runtime").rglob("auth.json"))
        artifacts.restore(destination, tmp_path / "restored")
        assert (tmp_path / "restored" / "workflow" / "dispatch.sqlite").exists()
    finally:
        store.close()


@pytest.mark.parametrize(
    "engine", ["native", pytest.param("pi-durable", marks=durable)]
)
def test_complete_outcome_preserves_cli_and_acceptance_contract(tmp_path, engine):
    cfg, store, ctrl, wid, worker = setup(tmp_path, engine)
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
        if engine == "pi-durable":
            assert len(list((tmp_path / "workflow").glob("att_*/dispatch.sqlite"))) == 5
        assert not list((tmp_path / "workspaces").iterdir())
    finally:
        store.close()


@durable
def test_restart_after_cli_completion_does_not_execute_twice(tmp_path, monkeypatch):
    cfg, store, ctrl, wid, worker = setup(tmp_path, "pi-durable")
    claim = ctrl.claim_job(worker.worker_id, ["native", "fake"])
    runtime = PiDurableRuntime(cfg, store)
    actual = subprocess.Popen
    processes = []

    def spawn(*args, **kwargs):
        proc = actual(*args, **kwargs)
        if str(ENGINE_DIR / "main.ts") in args[0] and "--preflight" not in args[0]:
            assert not any(
                "TOKEN" in k or "KEY" in k or k == "NODE_OPTIONS" for k in kwargs["env"]
            )
            processes.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", spawn)

    def execute_and_crash():
        worker._execute(claim)
        os.killpg(processes[0].pid, signal.SIGKILL)

    try:
        with pytest.raises((RuntimeError, BrokenPipeError)):
            runtime.run(claim, execute_and_crash)
        assert (
            store.one("SELECT status FROM attempts WHERE id=?", (claim["attempt_id"],))[
                0
            ]
            == "succeeded"
        )

        def duplicate():
            raise AssertionError("native CLI was replayed")

        runtime.run(claim, duplicate)
        runtime.run(claim, duplicate)
        assert len(store.query("SELECT * FROM attempts")) == 1
    finally:
        store.close()


@durable
def test_crash_before_execution_resumes_same_dispatch(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path, "pi-durable")
    claim = ctrl.claim_job(worker.worker_id, ["native", "fake"])
    aid = claim["attempt_id"]
    digest = hashlib.sha256(
        json.dumps(claim["inputs"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    process = subprocess.Popen(
        ["node", "--experimental-strip-types", str(ENGINE_DIR / "main.ts")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        process.stdin.write(
            (
                json.dumps(
                    {
                        "version": 1,
                        "database": str(
                            tmp_path / "workflow" / aid / "dispatch.sqlite"
                        ),
                        "attemptId": aid,
                        "digest": digest,
                    }
                )
                + "\n"
            ).encode()
        )
        process.stdin.flush()
        request = json.loads(process.stdout.readline())
        assert request["operation"] == "observe"
        process.stdin.write(
            (
                json.dumps({"version": 1, "id": request["id"], "status": "leased"})
                + "\n"
            ).encode()
        )
        process.stdin.flush()
        assert json.loads(process.stdout.readline())["operation"] == "execute"
        process.kill()
        process.wait()
        assert worker.run_once()  # startup discovers and revalidates the saved dispatch
        assert len(store.query("SELECT * FROM attempts")) == 1
        assert (
            store.one("SELECT status FROM attempts WHERE id=?", (aid,))[0]
            == "succeeded"
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        store.close()


@durable
def test_running_interrupted_cli_is_not_replayed(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path, "pi-durable")
    claim = ctrl.claim_job(worker.worker_id, ["native", "fake"])
    store.execute(
        "UPDATE attempts SET status='running' WHERE id=?", (claim["attempt_id"],)
    )
    try:
        PiDurableRuntime(cfg, store).run(
            claim, lambda: pytest.fail("interrupted native CLI replayed")
        )
        assert (
            store.one("SELECT status FROM attempts WHERE id=?", (claim["attempt_id"],))[
                0
            ]
            == "running"
        )
        assert len(store.query("SELECT * FROM attempts")) == 1
    finally:
        store.close()
