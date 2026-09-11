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
    server = serve(ctrl, "127.0.0.1", 0, cfg.worker_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/health")
    resp = conn.getresponse()
    assert resp.status == 200
    conn.request(
        "POST", "/claim", body="{}", headers={"Content-Type": "application/json"}
    )
    resp = conn.getresponse()
    assert resp.status == 401
    server.shutdown()


def test_worker_token_cannot_approve_or_pause(tmp_path):
    import json
    import threading
    from http.client import HTTPConnection

    from test_loop import cfg_for

    cfg = cfg_for(tmp_path)
    store = Store(tmp_path)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    server = serve(ctrl, "127.0.0.1", 0, cfg.worker_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(
            "POST",
            "/operator",
            body=json.dumps({"cmd": "pause"}),
            headers={"Authorization": f"Bearer {cfg.worker_token}"},
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
