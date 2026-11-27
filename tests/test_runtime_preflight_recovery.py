"""Idle, paused and drained workers do no work; failed starts are settled safely."""

from pathlib import Path

import pytest
from test_loop import cfg_for
from test_workflow import setup

from dark_factory.controller import Controller
from dark_factory.store import Store
from dark_factory.worker import Worker


def test_idle_worker_claims_nothing(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(tmp_path)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    try:
        worker = Worker(cfg, store, ctrl)
        for _ in range(3):
            assert not worker.run_once()
        assert not store.query("SELECT * FROM attempts")
    finally:
        store.close()


@pytest.mark.parametrize("state", ["pause", "drain"])
def test_disabled_dispatch_claims_nothing(tmp_path, state):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    getattr(ctrl, state)("human")
    try:
        assert not worker.run_once()
        assert not store.query("SELECT * FROM attempts")
    finally:
        store.close()


def test_started_callback_error_cannot_release_unbilled(tmp_path, monkeypatch):
    cfg, store, ctrl, wid, worker = setup(tmp_path)

    def interrupted(claim):
        raise RuntimeError("execution handoff already started")

    monkeypatch.setattr(worker, "_execute", interrupted)
    try:
        with pytest.raises(RuntimeError, match="handoff"):
            worker.run_once()
        attempt = store.one("SELECT * FROM attempts")
        assert attempt["status"] == "failed" and attempt["cost_cents"] == 50
        assert store.one("SELECT status FROM work_items")[0] == "failed"
    finally:
        store.close()


@pytest.mark.parametrize("guard", ["worker", "workspace", "pid", "running"])
def test_unstarted_release_cannot_fence_other_owner_or_started_work(tmp_path, guard):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    claim = ctrl.claim_job(worker.worker_id, ["native", "fake", "environment:local"])
    aid = claim["attempt_id"]
    owner = worker.worker_id
    if guard == "worker":
        owner = "different-owner"
    elif guard == "workspace":
        store.execute(
            "UPDATE attempts SET workspace='retained-work' WHERE id=?", (aid,)
        )
    elif guard == "pid":
        store.execute("UPDATE attempts SET pid=42 WHERE id=?", (aid,))
    else:
        store.execute("UPDATE attempts SET status='running' WHERE id=?", (aid,))
    try:
        assert not ctrl.release_unstarted_claim(aid, owner, "not eligible")
        assert store.one("SELECT reserved_cents FROM work_items")[0] == 50
        assert not store.query("SELECT * FROM costs")
    finally:
        store.close()


def test_stopping_the_controller_stops_process_groups_but_keeps_roots(tmp_path):
    import os
    import signal
    import subprocess

    from test_workflow import setup

    from dark_factory.runner import process_identity

    cfg, store, ctrl, wid, worker = setup(tmp_path)
    claimed = ctrl.claim_job("w", ["native", "fake", "environment:local"])
    child = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        ctrl.record_process(claimed["attempt_id"], child.pid)
        assert process_identity(child.pid)
        ctrl.stop_active_processes("SIGTERM", grace_seconds=3)
        assert child.wait(timeout=5) == -signal.SIGTERM
        assert (
            store.one("SELECT status FROM work_items WHERE id=?", (wid,))[0]
            != "cancelled"
        )
        audit = store.one("SELECT payload_json FROM audit WHERE action='shutdown'")
        assert "SIGTERM" in audit[0] and claimed["attempt_id"] in audit[0]
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)


def test_serve_exits_cleanly_on_sigterm_and_records_why(tmp_path):
    import json
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    text = Path("examples/factory.toml").read_text()
    text = text.replace('state_dir = "./state"', f'state_dir = "{tmp_path / "state"}"')
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    text = text.replace("api_port = 8745", f"api_port = {port}")
    profile = tmp_path / "factory.toml"
    profile.write_text(text)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dark_factory.cli",
            "--config",
            str(profile),
            "serve",
            "--worker",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "PYTHONPATH": "src", "PYTHONUNBUFFERED": "1"},
    )
    try:
        assert "listening" in proc.stdout.readline()
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    from dark_factory.store import Store

    store = Store(tmp_path / "state")
    try:
        row = store.one("SELECT payload_json FROM audit WHERE action='shutdown'")
        assert json.loads(row[0])["reason"] == "SIGTERM"
    finally:
        store.close()
