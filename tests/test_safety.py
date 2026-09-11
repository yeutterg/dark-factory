"""Regression tests for boundaries that the successful simulation does not exercise."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory.adapters.env.native import NativeEnvironment
from dark_factory.adapters.git.github import handle as code_host
from dark_factory.adapters.projects.github import handle as tracker
from dark_factory.config import ConfigError, Window, parse_toml_bytes
from dark_factory.contracts import ContractError
from dark_factory.controller import Controller, ControllerError, window_open
from dark_factory.runner import CommandRunner, RunnerError
from dark_factory.store import Store
from dark_factory.worker import Worker


@pytest.fixture
def simulation(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(tmp_path)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    wid = ctrl.intake_issues(
        [{"repo": cfg.repositories[0], "number": 1, "title": "test"}]
    )[0]
    ctrl.start_planning(wid)
    yield cfg, store, ctrl, wid
    store.close()


def test_approval_requires_completed_critique(simulation):
    cfg, store, ctrl, wid = simulation
    with pytest.raises(ControllerError, match="cannot approve"):
        ctrl.approve(wid, "human")
    assert store.one("SELECT approved_digest FROM work_items")[0] is None


@pytest.mark.parametrize("mutation", ["command", "route", "budget", "graph", "plan"])
def test_changed_approval_inputs_stop_dispatch(simulation, mutation):
    cfg, store, ctrl, wid = simulation
    worker = Worker(cfg, store, ctrl)
    worker.run_loop(2)
    ctrl.approve(wid, "human")
    if mutation == "command":
        cfg.commands["tracking"].command.append("--different")
    elif mutation == "route":
        cfg.routes["fake-coder"].region = "CA"
    elif mutation == "budget":
        store.execute("UPDATE work_items SET spending_ceiling_cents=99999")
    elif mutation == "graph":
        store.execute("UPDATE jobs SET depends_on='[]' WHERE kind='implement'")
    else:
        store.execute("UPDATE work_items SET plan_json='{}'")
    with pytest.raises(ControllerError, match="approved inputs changed"):
        worker.run_once()
    assert store.one("SELECT COUNT(*) FROM attempts")[0] == 2


def test_claim_is_atomic_across_connections(simulation):
    cfg, store, ctrl, wid = simulation
    other = Store(cfg.state_dir)
    second = Controller(cfg, other, ctrl.packaged_prompts)
    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(
                pool.map(lambda c: c.claim_job("w", ["native", "fake"]), [ctrl, second])
            )
        assert sum(result is not None for result in results) == 1
        assert store.one("SELECT reserved_cents FROM work_items")[0] == 50
    finally:
        other.close()


def test_late_cancelled_result_cannot_publish(simulation):
    cfg, store, ctrl, wid = simulation
    claim = ctrl.claim_job("w", ["native", "fake"])
    ctrl.cancel_job(claim["job"]["id"], "human")
    ctrl.finish_attempt(claim["attempt_id"], True, "late success", 1, None)
    assert store.one("SELECT status FROM attempts")[0] == "cancelled"
    assert store.one("SELECT status FROM work_items")[0] == "cancelled"
    assert json.loads(store.one("SELECT payload_json FROM evidence")[0])["ok"] is False


def test_duplicate_result_does_not_charge_twice(simulation):
    cfg, store, ctrl, wid = simulation
    Worker(cfg, store, ctrl).run_once()
    attempt = store.one("SELECT * FROM attempts")
    with pytest.raises(ControllerError, match="no longer active"):
        ctrl.finish_attempt(
            attempt["id"], True, "duplicate", 100, attempt["archive_path"]
        )
    assert store.one("SELECT spent_cents FROM work_items")[0] == 1
    assert store.one("SELECT SUM(amount_cents) FROM costs")[0] == 1
    assert store.one("SELECT attempt_id FROM costs")[0] == attempt["id"]


def test_expired_result_fails_without_progress(simulation):
    cfg, store, ctrl, wid = simulation
    claim = ctrl.claim_job("w", ["native", "fake"])
    store.execute(
        "UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
        (claim["job"]["id"],),
    )
    ctrl.finish_attempt(claim["attempt_id"], True, "late", 1, None)
    assert store.one("SELECT status FROM attempts")[0] == "timed_out"
    assert store.one("SELECT status FROM work_items")[0] == "failed"
    assert ctrl.claim_job("w", ["native", "fake"]) is None


def test_archive_failure_preserves_workspace(simulation, monkeypatch):
    cfg, store, ctrl, wid = simulation

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("dark_factory.worker.artifacts.archive", fail)
    Worker(cfg, store, ctrl).run_once()
    attempt = store.one("SELECT * FROM attempts")
    assert attempt["status"] == "failed"
    assert attempt["archive_path"] is None
    assert (Path(attempt["workspace"]) / "message.py").is_file()
    runtime = cfg.state_dir / "runtime" / attempt["id"]
    assert json.loads((runtime / "result.json").read_text())["output"]
    assert (runtime / "events.jsonl").read_text()
    assert store.one("SELECT COUNT(*) FROM audit WHERE action='archive.failed'")[0] == 1


def test_existing_workspace_is_never_erased(tmp_path):
    env = NativeEnvironment(tmp_path)
    prepared = env.prepare("attempt", None)
    (prepared.workspace / "unique.txt").write_text("only copy")
    with pytest.raises(FileExistsError):
        env.prepare("attempt", None)
    assert (prepared.workspace / "unique.txt").read_text() == "only copy"
    with pytest.raises(ValueError):
        env.prepare("../escape", None)


def test_live_route_cannot_fall_back_to_fake(simulation):
    cfg, store, ctrl, wid = simulation
    cfg.routes["fake-coder"].harness = "pi"
    with pytest.raises(ControllerError, match="not qualified"):
        Worker(cfg, store, ctrl).run_once()
    assert store.one("SELECT COUNT(*) FROM attempts")[0] == 0


@pytest.mark.parametrize(
    "handle,operation",
    [(code_host, "create_pr"), (code_host, "inspect_pr"), (tracker, "write_status")],
)
def test_invalid_external_requests_never_claim_success(handle, operation):
    with pytest.raises(ContractError):
        handle(
            {
                "operation": operation,
                "input": {
                    "repo": "example/repo",
                    "title": "test",
                    "status": "done",
                    "external_id": "1",
                },
            }
        )


def test_overnight_window_uses_local_start_day(simulation):
    cfg, *_ = simulation
    cfg.windows = [Window(["Mon"], "22:00", "06:00", "America/Los_Angeles")]
    at = datetime(2026, 9, 15, 9, tzinfo=timezone.utc)  # Tuesday 02:00 PDT
    assert window_open(cfg, at)
    cfg.date_exceptions = ["2026-09-14"]
    assert not window_open(cfg, at)


@pytest.mark.parametrize(
    "old,new",
    [
        ("schema_version = 1", "schema_version = true"),
        ("root_cents = 2000", "root_cents = -1"),
        ("timeout_seconds = 30", 'timeout_seconds = "30"'),
        ('start = "00:00"', 'start = "25:00"'),
        ('timezone = "America/Los_Angeles"', 'timezone = "invalid"'),
        ("schema_version = 1", "schema_version = 1\nunknown_setting = true"),
    ],
)
def test_invalid_config_fails_early(old, new):
    text = Path("examples/factory.toml").read_text().replace(old, new)
    with pytest.raises(ConfigError):
        parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())


def test_stderr_is_bounded_while_process_runs(tmp_path):
    runner = CommandRunner(tmp_path, 1024)
    with pytest.raises(RunnerError, match="output limit"):
        runner.run_plain(
            [
                sys.executable,
                "-c",
                'import os,time; os.write(2,b"x"*2000); time.sleep(60)',
            ],
            2,
        )


def test_config_history_does_not_store_worker_token(simulation):
    cfg, store, *_ = simulation
    assert cfg.worker_token not in store.one("SELECT body FROM config_revisions")[0]


def test_success_without_durable_archive_cannot_advance(simulation):
    cfg, store, ctrl, wid = simulation
    claim = ctrl.claim_job("w", ["native", "fake"])
    ctrl.finish_attempt(claim["attempt_id"], True, "claimed success", 1, None)
    assert store.one("SELECT status FROM attempts")[0] == "failed"
    assert store.one("SELECT status FROM work_items")[0] == "failed"


def test_acceptance_rechecks_approved_inputs(simulation):
    cfg, store, ctrl, wid = simulation
    worker = Worker(cfg, store, ctrl)
    worker.run_loop(2)
    ctrl.approve(wid, "human")
    worker.run_loop(3)
    cfg.routes["fake-coder"].model = "different"
    with pytest.raises(ControllerError, match="approved inputs changed"):
        ctrl.accept(wid, "human")
