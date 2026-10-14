from __future__ import annotations

import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.error import HTTPError

import pytest
from test_distributed import deployment, selected

from dark_factory.api import serve
from dark_factory.controller import Controller, ControllerError
from dark_factory.execution.client import WorkerClient
from dark_factory.execution.service import ExecutionService
from dark_factory.store import Store


def prepared(tmp_path, monkeypatch):
    cfg = deployment(tmp_path, monkeypatch)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    wid = selected(ctrl)
    return cfg, store, ctrl, ExecutionService(ctrl), wid


def test_unstarted_expired_delivery_is_released_without_charge_or_replay(
    tmp_path, monkeypatch
):
    cfg, store, ctrl, service, _ = prepared(tmp_path, monkeypatch)
    try:
        assignment = service.claim("worker1", "lost-delivery")
        aid = assignment["claim"]["attempt_id"]
        store.execute(
            "UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
            (assignment["claim"]["job"]["id"],),
        )
        assert service.start("worker1", aid, uuid.uuid4().hex) is False
        assert ctrl.recover("test") == 1
        assert (
            store.one("SELECT status,cost_cents FROM attempts WHERE id=?", (aid,))[
                "status"
            ]
            == "not_started"
        )
        assert (
            store.one("SELECT amount_cents FROM costs WHERE attempt_id=?", (aid,))[0]
            == 0
        )
        with pytest.raises(ControllerError, match="fenced"):
            service.claim("worker1", "lost-delivery")
        replacement = service.claim("worker1", "fresh-delivery")
        assert replacement["claim"]["attempt_id"] != aid
    finally:
        store.close()


def test_start_intent_is_idempotent_and_owned_by_one_executor(tmp_path, monkeypatch):
    _, store, _, service, _ = prepared(tmp_path, monkeypatch)
    try:
        assignment = service.claim("worker1", "delivery")
        aid = assignment["claim"]["attempt_id"]
        eid = uuid.uuid4().hex
        assert service.start("worker1", aid, eid)
        assert service.start("worker1", aid, eid)
        with pytest.raises(ControllerError, match="another executor"):
            service.start("worker1", aid, uuid.uuid4().hex)
        with pytest.raises(ControllerError, match="fenced"):
            service.claim("worker1", "delivery")
    finally:
        store.close()


def test_cancelled_attempt_cannot_authorize_start(tmp_path, monkeypatch):
    _, store, ctrl, service, _ = prepared(tmp_path, monkeypatch)
    try:
        assignment = service.claim("worker1", "delivery")
        ctrl.cancel_job(assignment["claim"]["job"]["id"], "tester")
        assert (
            service.start(
                "worker1", assignment["claim"]["attempt_id"], uuid.uuid4().hex
            )
            is False
        )
    finally:
        store.close()


def test_worker_state_and_cleanup_ownership_fail_closed(tmp_path):
    state = tmp_path / "worker"
    client = WorkerClient(
        "http://127.0.0.1:8745", "project-one", "worker1", "a" * 40, state
    )
    with pytest.raises(ValueError, match="different deployment"):
        WorkerClient(client.url, "project-two", "worker1", client.token, state)
    directory = state / "att_unowned"
    directory.mkdir()
    (directory / "unique-code.py").write_text("preserve")
    with pytest.raises(ValueError, match="ownership"):
        client._cleanup({"directory": str(directory), "cleanup": True})
    assert (directory / "unique-code.py").exists()
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "factory.sqlite").touch()
    with pytest.raises(ValueError, match="supervisor state"):
        WorkerClient(
            client.url, client.project, client.worker, client.token, controller
        )


@pytest.mark.skipif(not shutil.which("openssl"), reason="OpenSSL required")
def test_tls_peer_validation_and_project_worker_scope(tmp_path, monkeypatch):
    cfg, store, ctrl, _, _ = prepared(tmp_path, monkeypatch)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            shutil.which("openssl"),
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    server = serve(ctrl, "127.0.0.1", 0, cfg.worker_token, (str(cert), str(key)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"https://127.0.0.1:{server.server_port}"
        client = WorkerClient(
            url,
            cfg.project_id,
            "worker1",
            "first-worker-token-" + "a" * 32,
            tmp_path / "worker",
            ca_file=cert,
        )
        info = client.request("preflight", {})
        assert info["project_id"] == cfg.project_id
        wrong = WorkerClient(
            url,
            cfg.project_id,
            "worker2",
            client.token,
            tmp_path / "wrong",
            ca_file=cert,
        )
        with pytest.raises(HTTPError) as caught:
            wrong.request("preflight", {})
        assert caught.value.code == 401
        with pytest.raises(ControllerError):
            serve(ctrl, "0.0.0.0", 0, cfg.worker_token)
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def test_transport_failure_retries_saved_result_without_rerunning_agent(
    tmp_path, monkeypatch
):
    cfg, store, ctrl, service, _ = prepared(tmp_path, monkeypatch)
    server = serve(ctrl, "127.0.0.1", 0, cfg.worker_token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = WorkerClient(
        f"http://127.0.0.1:{server.server_port}",
        cfg.project_id,
        "worker1",
        "first-worker-token-" + "a" * 32,
        tmp_path / "worker",
    )
    real = client.request
    failed = False

    def request(operation, payload):
        nonlocal failed
        result = real(operation, payload)
        if operation == "finish" and not failed:
            failed = True
            raise OSError("lost result acknowledgement")
        return result

    monkeypatch.setattr(client, "request", request)
    try:
        with pytest.raises(OSError, match="acknowledgement"):
            client.run_once()
        assert list(client.state.glob("att_*")), (
            "retain replicas until verified readback"
        )
        assert store.one("SELECT COUNT(*) FROM attempts")[0] == 1
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda *a, **k: pytest.fail("must not replay execution"),
        )
        assert client.run_once()
        assert not list(client.state.glob("att_*"))
        assert store.one("SELECT COUNT(*) FROM attempts")[0] == 1
    finally:
        server.shutdown()
        server.server_close()
        store.close()
