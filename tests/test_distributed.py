from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from urllib.error import HTTPError

import pytest
from test_loop import cfg_for

from dark_factory import artifacts
from dark_factory.api import serve
from dark_factory.config import ConfigError, validate_raw
from dark_factory.controller import Controller, ControllerError
from dark_factory.execution.client import WorkerClient
from dark_factory.execution.executor import execute_assignment
from dark_factory.execution.packages import identity, pack, unpack
from dark_factory.execution.service import ExecutionService
from dark_factory.store import Store


def deployment(tmp_path, monkeypatch, project="project-one"):
    cfg = cfg_for(tmp_path / "controller")
    raw = copy.deepcopy(cfg.raw)
    raw["project"]["id"] = project
    raw["environments"] = {"local": {"kind": "native"}}
    raw["role_routes"] = {
        role: "fake-coder" for role in ("planner", "critic", "coder", "reviewer")
    }
    raw["routes"]["fake-coder"].update(
        project_id=project, account_kind="simulation", environment="local"
    )
    raw["workers"] = {
        "worker1": {
            "token_env": "PROJECT_WORKER_ONE",
            "environments": ["local"],
            "lease_seconds": 180,
        },
        "worker2": {"token_env": "PROJECT_WORKER_TWO", "environments": ["local"]},
    }
    monkeypatch.setenv("PROJECT_WORKER_ONE", "first-worker-token-" + "a" * 32)
    monkeypatch.setenv("PROJECT_WORKER_TWO", "second-worker-token-" + "b" * 32)
    return validate_raw(raw, cfg.source_path, identity(raw))


def selected(ctrl):
    wid = ctrl.intake_issues(
        [
            {
                "repo": ctrl.cfg.repositories[0],
                "number": 1,
                "title": "Portable task",
                "body": "Change the fixture",
                "is_root": True,
            }
        ]
    )[0]
    ctrl.start_planning(wid)
    return wid


def test_explicit_role_and_project_validation(tmp_path, monkeypatch):
    cfg = deployment(tmp_path, monkeypatch)
    for update in (
        {"project_id": "another-project"},
        {"environment": "missing"},
        {"account_kind": "api"},
    ):
        raw = copy.deepcopy(cfg.raw)
        raw["routes"]["fake-coder"].update(update)
        with pytest.raises(ConfigError):
            validate_raw(raw, cfg.source_path, "test")
    raw = copy.deepcopy(cfg.raw)
    del raw["role_routes"]["reviewer"]
    with pytest.raises(ConfigError, match="all four"):
        validate_raw(raw, cfg.source_path, "test")


def test_state_cannot_be_reused_by_another_project(tmp_path, monkeypatch):
    cfg = deployment(tmp_path, monkeypatch)
    with_store = Store(cfg.state_dir)
    try:
        Controller(cfg, with_store, Path("src/dark_factory/agents/prompts"))
        second = deployment(tmp_path, monkeypatch, "project-two")
        with pytest.raises(ControllerError, match="different project"):
            Controller(second, with_store, Path("src/dark_factory/agents/prompts"))
    finally:
        with_store.close()


def test_project_credentials_never_discover_host_login(monkeypatch):
    from dark_factory.adapters.harness.pi import resolve_credential

    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: pytest.fail("host credential discovery")
    )
    assert (
        resolve_credential(
            {
                "project_id": "project-one",
                "provider": "together",
                "auth_env": "TOGETHER_API_KEY",
            }
        )
        == ""
    )


def test_full_portable_loop_and_idempotent_settlement(tmp_path, monkeypatch):
    cfg = deployment(tmp_path, monkeypatch)
    store = Store(cfg.state_dir)
    try:
        ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        wid = selected(ctrl)
        service = ExecutionService(ctrl)
        assert service.authenticate("worker1", "first-worker-token-" + "a" * 32)
        assert not service.authenticate("worker2", "first-worker-token-" + "a" * 32)
        for n in range(5):
            assignment = service.claim("worker1", f"request-{n}")
            assert assignment == service.claim("worker1", f"request-{n}")
            serialized = json.dumps(assignment)
            assert (
                cfg.operator_token not in serialized
                and cfg.worker_token not in serialized
            )
            with pytest.raises(ControllerError):
                service.heartbeat("worker2", assignment["claim"]["attempt_id"])
            import uuid

            assert service.start(
                "worker1", assignment["claim"]["attempt_id"], uuid.uuid4().hex
            )
            result = execute_assignment(
                assignment,
                tmp_path / f"executor-{n}",
                lambda: service.heartbeat("worker1", assignment["claim"]["attempt_id"]),
            )
            assert result["receipt"]["ok"], result["receipt"]
            ack = service.finish("worker1", result)
            assert ack == service.finish("worker1", result)
            assert ack["result_digest"] == identity(result)
            changed = copy.deepcopy(result)
            changed["receipt"]["cost"] += 1
            with pytest.raises(ControllerError, match="conflicting"):
                service.finish("worker1", changed)
            if n == 1:
                ctrl.approve(wid, "tester")
        packet = json.loads(
            store.one("SELECT packet_json FROM work_items WHERE id=?", (wid,))[0]
        )
        assert packet["ready_for_human_review"]
        assert (
            store.one("SELECT reserved_cents FROM work_items WHERE id=?", (wid,))[0]
            == 0
        )
        for path in (cfg.state_dir / "archives").iterdir():
            artifacts.verify(path)
    finally:
        store.close()


def test_outbound_worker_process_and_verified_cleanup(tmp_path, monkeypatch):
    cfg = deployment(tmp_path, monkeypatch)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    wid = selected(ctrl)
    server = serve(ctrl, "127.0.0.1", 0, cfg.worker_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        client = WorkerClient(
            url,
            cfg.project_id,
            "worker1",
            "first-worker-token-" + "a" * 32,
            tmp_path / "worker",
        )
        for n in range(5):
            assert client.run_once()
            assert not list(client.state.glob("att_*")), (
                "verified task replicas/builds must be deleted"
            )
            if n == 1:
                ctrl.approve(wid, "tester")
        assert not client.run_once()
        assert len(list((client.state / "receipts").glob("*.json"))) == 5
        assert (
            store.one("SELECT status FROM work_items WHERE id=?", (wid,))[0]
            == "awaiting_review"
        )
        wrong = WorkerClient(
            url, "project-two", "worker1", client.token, tmp_path / "wrong-project"
        )
        with pytest.raises(HTTPError) as caught:
            wrong.run_once()
        assert caught.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def test_nonlocal_plaintext_transport_rejected(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        WorkerClient(
            "http://remote.example:8745", "project-one", "worker1", "a" * 40, tmp_path
        )


def test_package_tamper_and_escaping_links_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("data")
    package = pack({"source": source})
    package["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        unpack(package, tmp_path / "tampered")
    (source / "link").symlink_to("../../private")
    with pytest.raises(ValueError, match="escaping"):
        unpack(pack({"source": source}), tmp_path / "escaping")


def test_fenced_result_rejected(tmp_path, monkeypatch):
    cfg = deployment(tmp_path, monkeypatch)
    store = Store(cfg.state_dir)
    try:
        ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        selected(ctrl)
        service = ExecutionService(ctrl)
        assignment = service.claim("worker1", "request-one")
        result = execute_assignment(assignment, tmp_path / "executor")
        store.execute(
            "UPDATE attempts SET status='failed' WHERE id=?", (result["attempt_id"],)
        )
        with pytest.raises(ControllerError, match="fenced"):
            service.finish("worker1", result)
    finally:
        store.close()
