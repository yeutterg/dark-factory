"""Kill and restart the worker repeatedly mid-attempt; nothing may be lost or stuck."""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

from test_loop import cfg_for

from dark_factory import artifacts
from dark_factory.config import FactoryConfig
from dark_factory.controller import Controller
from dark_factory.store import Store
from dark_factory.worker import Worker


def kill_cycle(state, seconds):
    """Start a worker, wait until an attempt is really running, then kill -9 it."""
    marker = state.parent / "attempt-started"
    marker.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "tests/soak_worker.py", str(state)],
        stdout=subprocess.DEVNULL,
        stderr=(state.parent / "worker.log").open("ab"),
        start_new_session=True,
        env={**os.environ, "SOAK_MARKER": str(marker)},
    )
    try:
        end = time.monotonic() + 8  # no attempt appears while a root waits for a human
        while not marker.exists() and proc.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(seconds)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    return marker.exists()


def drive(ctrl, cfg, store, wid, until, deadline_seconds=90):
    """Recover, settle and (as the human) resume until the root reaches a state."""
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        status = store.one("SELECT status FROM work_items WHERE id=?", (wid,))[0]
        if status in until:
            return status
        ctrl.recover("soak")
        ctrl.settle_interruptions("soak")
        if status == "interrupted":
            ctrl.continue_work(wid, "human")
        Worker(cfg, store, ctrl, "soak").run_loop()
        time.sleep(0.5)
    raise AssertionError(f"root stuck; last status {status}")


def test_repeated_kill_and_restart_never_loses_or_wedges_a_root(tmp_path, monkeypatch):
    monkeypatch.setattr(FactoryConfig, "lease_seconds", lambda self, worker_id: 2)
    state = tmp_path / "state"

    def open_state():
        cfg = cfg_for(state)
        cfg.windows = []
        # A killed attempt is charged its whole reservation; leave room for several.
        cfg.raw["budgets"] = {**cfg.raw["budgets"], "planning_cents": 1000}
        store = Store(cfg.state_dir)
        return (
            cfg,
            store,
            Controller(cfg, store, Path("src/dark_factory/agents/prompts")),
        )

    cfg, store, ctrl = open_state()
    wid = ctrl.intake_issues(
        [{"repo": cfg.repositories[0], "number": 1, "title": "Soak", "body": ""}]
    )[0]
    ctrl.start_planning(wid)
    rng = random.Random(7)
    killed = 0

    def kills(count):
        # One controller owns a state directory, so the test lets go of it while
        # a separate controller-and-worker process runs and is killed.
        nonlocal cfg, store, ctrl, killed
        store.close()
        for _ in range(count):
            killed += kill_cycle(state, rng.uniform(0.05, 0.4))
        cfg, store, ctrl = open_state()

    try:
        kills(3)  # planning, killed mid-attempt
        drive(ctrl, cfg, store, wid, {"awaiting_approval"})
        ctrl.approve(wid, "human")
        kills(3)  # implementation and checks, killed mid-attempt
        assert drive(ctrl, cfg, store, wid, {"awaiting_review"}) == "awaiting_review"

        # Some restarts find the root waiting for a person or for a lease to
        # lapse and have nothing to run; enough kills must still land mid-attempt.
        assert killed >= 3, "too few kills landed while an attempt was running"
        assert store.one("SELECT COUNT(*) FROM interruptions")[0] >= 1
        root = store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        assert root["reserved_cents"] == 0, "a reservation leaked"
        assert not store.query(
            "SELECT id FROM attempts WHERE status IN ('leased','running','cancelling')"
        ), "an attempt is still active"
        for attempt in store.query(
            "SELECT archive_path FROM attempts WHERE archive_path IS NOT NULL"
        ):
            artifacts.verify(store.resolve_path(attempt["archive_path"]))
        ctrl.verify_candidate(root)
        assert json.loads(root["candidate_commits"])[root["repo"]]["commit"]
    finally:
        store.close()
