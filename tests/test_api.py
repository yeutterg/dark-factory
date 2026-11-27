from __future__ import annotations

from pathlib import Path

from dark_factory.api import serve
from dark_factory.config import parse_toml_bytes
from dark_factory.controller import Controller
from dark_factory.store import Store


def test_health_and_unauthorized(tmp_path: Path) -> None:
    import threading
    from http.client import HTTPConnection

    text = Path("examples/factory.toml").read_text(encoding="utf-8")
    text = text.replace('state_dir = "./state"', f'state_dir = "{tmp_path}"')
    cfg = parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    server = serve(ctrl, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/health")
    resp = conn.getresponse()
    assert resp.status == 200
    resp.read()
    # The shared-token worker channel no longer exists.
    for legacy in ("/claim", "/heartbeat", "/finish"):
        conn.request(
            "POST", legacy, body="{}", headers={"Content-Type": "application/json"}
        )
        resp = conn.getresponse()
        assert resp.status == 404
        resp.read()
    server.shutdown()


def test_non_operator_bearer_cannot_approve_or_pause(tmp_path):
    import json
    import threading
    from http.client import HTTPConnection

    from test_loop import cfg_for

    cfg = cfg_for(tmp_path)
    store = Store(tmp_path)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    server = serve(ctrl, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(
            "POST",
            "/operator",
            body=json.dumps({"cmd": "pause"}),
            headers={"Authorization": "Bearer not-the-operator-credential"},
        )
        response = conn.getresponse()
        assert response.status == 401
        response.read()
        assert store.get_op("pause") == "off"
        conn.request(
            "POST",
            "/operator",
            body=json.dumps({"cmd": "pause"}),
            headers={"Authorization": f"Bearer {cfg.operator_token}"},
        )
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        assert store.get_op("pause") == "on"
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def _server(tmp_path):
    from pathlib import Path

    from test_loop import cfg_for

    from dark_factory.api import serve
    from dark_factory.controller import Controller
    from dark_factory.store import Store

    cfg = cfg_for(tmp_path)
    store = Store(tmp_path)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    server = serve(ctrl, "127.0.0.1", 0)
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    return cfg, store, server


def test_failed_logins_are_throttled(tmp_path):
    import json
    from http.client import HTTPConnection

    cfg, store, server = _server(tmp_path)
    try:
        codes = []
        for _ in range(7):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request(
                "POST",
                "/operator/session",
                body=json.dumps({"token": "wrong", "actor": "x"}),
            )
            response = conn.getresponse()
            codes.append(response.status)
            response.read()
        assert codes[:5] == [401] * 5
        assert codes[5:] == [429, 429]
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def test_unexpected_error_returns_a_500_body_not_a_dropped_connection(
    tmp_path, monkeypatch
):
    import json
    from http.client import HTTPConnection

    from dark_factory import operator_commands

    cfg, store, server = _server(tmp_path)
    monkeypatch.setattr(
        operator_commands, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    )
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(
            "POST",
            "/operator",
            body=json.dumps({"cmd": "status"}),
            headers={"Authorization": f"Bearer {cfg.operator_token}"},
        )
        response = conn.getresponse()
        body = json.loads(response.read())
        assert response.status == 500
        assert body == {"ok": False, "error": "internal error"}  # nothing leaked
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def test_short_operator_credential_is_refused_at_config_load():
    import tomllib
    from pathlib import Path

    import pytest

    from dark_factory.config import ConfigError, validate_raw

    path = Path("examples/factory.toml").resolve()
    raw = tomllib.loads(path.read_text())
    raw["runtime"]["operator_token"] = "short"
    with pytest.raises(ConfigError, match="32 characters"):
        validate_raw(raw, path, "digest")
