"""Workflow prerequisites cannot charge untouched assignments or poll Node while idle."""

import json
import shutil
from pathlib import Path

import pytest
from test_loop import cfg_for
from test_workflow import durable, setup

from dark_factory.adapters.workflow import pi_durable
from dark_factory.controller import Controller, ControllerError
from dark_factory.runner import CommandRunner
from dark_factory.store import Store
from dark_factory.worker import Worker


def test_idle_worker_does_not_start_node(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    cfg.raw["workflow"] = {"engine": "pi-durable"}
    store = Store(tmp_path)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    monkeypatch.setattr(
        pi_durable.PiDurableRuntime,
        "validate",
        lambda self: pytest.fail("idle Node probe"),
    )
    try:
        worker = Worker(cfg, store, ctrl)
        for _ in range(3):
            assert not worker.run_once()
    finally:
        store.close()


@pytest.mark.parametrize("state", ["pause", "drain"])
def test_disabled_dispatch_does_not_start_node(tmp_path, monkeypatch, state):
    cfg, store, ctrl, wid, worker = setup(tmp_path, "pi-durable")
    getattr(ctrl, state)("human")
    monkeypatch.setattr(
        pi_durable.PiDurableRuntime,
        "validate",
        lambda self: pytest.fail("disabled Node probe"),
    )
    try:
        assert not worker.run_once()
        assert not store.query("SELECT * FROM attempts")
    finally:
        store.close()


@pytest.mark.skipif(not shutil.which("node"), reason="Node required")
def test_broken_engine_only_subpath_cannot_lease_or_charge(tmp_path, monkeypatch):
    cfg, store, ctrl, wid, worker = setup(tmp_path / "state", "pi-durable")
    engine = tmp_path / "broken-engine"
    engine.mkdir()
    (engine / "package.json").write_text('{"type":"module"}')
    (engine / "main.ts").write_text((pi_durable.ENGINE_DIR / "main.ts").read_text())
    modules = {
        "chord": (
            {"./context": "./context.js"},
            {"context.js": "export const BACKGROUND_CONTEXT = {};"},
        ),
        "pi-ai": (
            {"./models": "./models.js"},
            {"models.js": "export const createModels = () => ({});"},
        ),
        "pi-durable": (
            {
                ".": "./index.js",
                "./storage/sqlite": "./storage.js",
                "./storage/sqlite/node": "./missing-node.js",
            },
            {
                "index.js": "export class Harness {}; export const createRegistry = () => ({}); export const defineDoc = x => x; export const defineExtension = x => x; export const defineTask = x => x;",
                "storage.js": "export class SqliteStorage {};",
            },
        ),
    }
    for name, (exports, files) in modules.items():
        directory = engine / "node_modules" / "@earendil-works" / name
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(
            json.dumps(
                {
                    "name": "@earendil-works/" + name,
                    "version": pi_durable.PACKAGE_VERSION,
                    "type": "module",
                    "exports": exports,
                }
            )
        )
        for filename, body in files.items():
            (directory / filename).write_text(body)
    code, _, _ = CommandRunner(engine).supervise(
        [
            "node",
            "--input-type=module",
            "-e",
            "await import('@earendil-works/pi-durable')",
        ],
        b"",
        10,
        engine,
        pi_durable.PiDurableRuntime._environment(),
    )
    assert code == 0, "the original shallow probe must succeed in this fixture"
    monkeypatch.setattr(pi_durable, "ENGINE_DIR", engine)
    try:
        with pytest.raises(RuntimeError, match="cannot load"):
            worker.run_once()
        assert not store.query("SELECT * FROM attempts")
        assert not store.query("SELECT * FROM costs")
        assert store.one("SELECT reserved_cents FROM work_items")[0] == 0
        assert not (cfg.state_dir / "workflow").exists()
        assert ctrl.continuation(wid)["next_action"]["kind"] == "blocked"
        assert "cannot load" in ctrl.continuation(wid)["next_action"]["reason"]
    finally:
        store.close()


@durable
def test_post_claim_startup_failure_is_fenced_unbilled_then_resumes(
    tmp_path, monkeypatch
):
    cfg, store, ctrl, wid, worker = setup(tmp_path, "pi-durable")
    original = pi_durable.PiDurableRuntime._drive
    calls = 0

    def fail_once(self, *args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("installation changed after validation")
        return original(self, *args)

    monkeypatch.setattr(pi_durable.PiDurableRuntime, "_drive", fail_once)
    try:
        deadline = json.loads(store.one("SELECT snapshot_json FROM work_items")[0])[
            "deadline"
        ]
        with pytest.raises(RuntimeError, match="installation changed"):
            worker.run_once()
        retired = store.one("SELECT * FROM attempts")
        assert retired["status"] == "not_started" and retired["cost_cents"] == 0
        assert retired["workspace"] is None and retired["pid"] is None
        root = store.one(
            "SELECT reserved_cents,spent_cents,snapshot_json FROM work_items"
        )
        assert root["reserved_cents"] == root["spent_cents"] == 0
        assert json.loads(root["snapshot_json"])["deadline"] == deadline
        assert ctrl.continuation(wid)["next_action"]["kind"] == "blocked"
        assert not ctrl.release_unstarted_claim(
            retired["id"], worker.worker_id, "duplicate"
        )
        with pytest.raises(ControllerError, match="no longer active"):
            ctrl.finish_attempt(retired["id"], True, "late result", 0, None)
        assert worker.run_once()
        assert not store.get_op("workflow_unavailable", "")
        assert len(store.query("SELECT * FROM attempts")) == 2
        assert len(store.query("SELECT * FROM attempts WHERE status='succeeded'")) == 1
        assert not store.one(
            "SELECT id FROM attempts WHERE status IN ('leased','running')"
        )
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
    claim = ctrl.claim_job(worker.worker_id, ["native", "fake"])
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
