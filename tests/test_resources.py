"""Every resource has an owner and a removal rule; nothing stale survives."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from test_workflow import setup

from dark_factory.models import TERMINAL_ROOT


def age(path: Path, seconds: int) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def finish(ctrl, worker, store, cfg, wid):
    ctrl.approve(wid, "human-tester")
    worker.run_loop()
    root = store.one("SELECT * FROM work_items WHERE id=?", (wid,))
    import json

    candidate = json.loads(root["candidate_commits"])[cfg.repositories[0]]["commit"]
    ctrl.record_manual_gate(
        wid, "Inspect candidate diff", candidate, "human-tester", "ok"
    )
    ctrl.accept(wid, "human-tester")


def test_orphans_and_finished_roots_are_swept_but_active_work_is_not(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    state = cfg.state_dir
    # Debris with no owner, old enough to be sure.
    for area in ("workspaces", "runtime", "dispatch"):
        orphan = state / area / "att_deadbeefdead"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "big.bin").write_bytes(b"x" * 1000)
        age(orphan, 7200)
    stale = state / "inputs" / "wi_gone" / "1"
    stale.mkdir(parents=True)
    (stale / "repo").write_text("x")
    # An attempt that is running now keeps its directories.
    ctrl.approve(wid, "human-tester")
    claimed = ctrl.claim_job("w", ["native", "fake", "environment:local"])
    active = state / "workspaces" / claimed["attempt_id"]
    active.mkdir(parents=True, exist_ok=True)
    (active / "work").write_text("unique work in progress")
    result = ctrl.sweep_resources()
    assert result["freed_bytes"] >= 3000
    for area in ("workspaces", "runtime", "dispatch"):
        assert not (state / area / "att_deadbeefdead").exists()
    assert not (state / "inputs" / "wi_gone").exists()
    assert (active / "work").read_text() == "unique work in progress"
    audit = store.one("SELECT payload_json FROM audit WHERE action='resources.swept'")
    assert "freed_bytes" in audit[0]


def test_a_finished_root_leaves_no_inputs_and_prunes_old_checkouts(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    finish(ctrl, worker, store, cfg, wid)
    assert (
        store.one("SELECT status FROM work_items WHERE id=?", (wid,))[0]
        in TERMINAL_ROOT
    )
    inputs = cfg.state_dir / "inputs" / wid
    assert inputs.exists()
    ctrl.sweep_resources()
    assert not inputs.exists(), "the clone of a finished root must not outlive it"
    # Evidence is kept at first; after the retention window the checkout goes too.
    archives = [
        r["archive_path"]
        for r in store.query(
            "SELECT archive_path FROM attempts WHERE archive_path IS NOT NULL"
        )
    ]
    checkouts = [store.resolve_path(a) / "workspace" for a in archives]
    held = [c for c in checkouts if c.is_dir()]
    assert held, "fixture archives keep their checkout until the window passes"
    store.execute(
        "UPDATE attempts SET ended_at='2020-01-01T00:00:00+00:00' WHERE archive_path IS NOT NULL"
    )
    ctrl.sweep_resources()
    assert not any(c.is_dir() for c in checkouts)
    for archive in archives:  # the result, output and diff remain
        assert (store.resolve_path(archive) / "result.json").exists()


def test_new_work_is_refused_below_the_disk_floor(tmp_path, monkeypatch):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    assert ctrl.dispatch_allowed()[0]
    real = shutil.disk_usage(cfg.state_dir)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda path: type(real)(real.total, real.total - 1024, 1024),
    )
    assert ctrl.dispatch_allowed() == (False, "disk_low")
    assert ctrl.claim_job("w", ["native", "fake", "environment:local"]) is None


def test_state_over_its_budget_refuses_new_work(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    cfg.raw["disk"] = {"max_state_gb": 1, "min_free_gb": 0}
    assert ctrl.disk_blocker() is None
    ctrl._state_size = (time.monotonic(), 2 * 1024**3)
    assert ctrl.disk_blocker() == "state_over_budget"


def test_disk_report_names_areas_and_roots(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    report = ctrl.disk_usage()
    assert {"workspaces", "inputs", "archives"} <= set(report["areas"])
    assert wid in report["roots"] and report["free_bytes"] > 0


def test_a_killed_build_leaves_nothing_after_restart_sweep(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    claimed = ctrl.claim_job("w", ["native", "fake", "environment:local"])
    workspace = cfg.state_dir / "workspaces" / claimed["attempt_id"]
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "partial").write_text("half a build")
    # The worker dies: its lease lapses and recovery fences the attempt.
    store.execute(
        "UPDATE attempts SET deadline='2020-01-01T00:00:00+00:00' WHERE id=?",
        (claimed["attempt_id"],),
    )
    store.execute("UPDATE jobs SET lease_until='2020-01-01T00:00:00+00:00'")
    ctrl.recover("restart")
    store.execute(
        "UPDATE attempts SET ended_at='2020-01-01T00:00:00+00:00' WHERE id=?",
        (claimed["attempt_id"],),
    )
    ctrl.sweep_resources()
    assert not workspace.exists()
