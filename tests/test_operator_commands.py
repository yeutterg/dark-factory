import argparse
import json
import threading
from http.client import HTTPConnection
from pathlib import Path

from dark_factory import operator_commands
from dark_factory.api import serve
from dark_factory.cli import build_parser, main
from dark_factory.controller import Controller
from dark_factory.store import Store


def cli_commands():
    parser = build_parser()
    action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return set(action.choices)


def test_cli_exposes_shared_and_explicit_local_commands():
    shared = set(operator_commands.COMMANDS)
    assert not shared & operator_commands.LOCAL_ONLY
    assert cli_commands() == shared | operator_commands.LOCAL_ONLY


def test_api_accepts_shared_commands_and_rejects_local_only(tmp_path):
    from test_loop import cfg_for

    cfg = cfg_for(tmp_path)
    store = Store(tmp_path)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    server = serve(ctrl, "127.0.0.1", 0, cfg.worker_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    headers = {"Authorization": f"Bearer {cfg.operator_token}"}
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        for command, expected in (
            ("status", 200),
            *((n, 409) for n in sorted(operator_commands.LOCAL_ONLY)),
            ("create-pr", 409),
        ):
            conn.request(
                "POST", "/operator", body=json.dumps({"cmd": command}), headers=headers
            )
            response = conn.getresponse()
            assert response.status == expected, command
            response.read()
        conn.request(
            "POST",
            "/operator",
            body=json.dumps({"cmd": "review", "work_item_id": "missing"}),
            headers=headers,
        )
        response = conn.getresponse()
        assert response.status == 409
        assert json.loads(response.read())["error"] == "work item not found"
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def test_cli_rejects_local_only_command_over_api(tmp_path, capsys):
    from test_loop import cfg_for

    cfg = cfg_for(tmp_path)
    code = main(
        [
            "--config",
            str(cfg.source_path),
            "--api-url",
            "http://127.0.0.1:1",
            "backup",
            str(tmp_path / "b"),
        ]
    )
    assert code == 2
    assert "runs locally" in capsys.readouterr().err


def test_remote_cli_needs_only_operator_credential(tmp_path, monkeypatch, capsys):
    from test_loop import cfg_for

    cfg = cfg_for(tmp_path)
    store = Store(tmp_path)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    ctrl.reload = lambda actor="operator": False
    server = serve(ctrl, "127.0.0.1", 0, cfg.worker_token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    client = tmp_path / "operator.toml"
    client.write_text(
        '[runtime]\noperator_token_env = "REMOTE_OPERATOR_TOKEN"\n'
        'worker_token_env = "INTENTIONALLY_MISSING"\n'
        '[sources."missing/repo"]\npath = "/also/intentionally/missing"\n'
    )
    monkeypatch.setenv("REMOTE_OPERATOR_TOKEN", cfg.operator_token)
    base = ["--config", str(client), "--api-url", url]
    try:
        assert main([*base, "reload"]) == 2
        capsys.readouterr()
        assert main([*base, "status"]) == 0
        assert "project" in json.loads(capsys.readouterr().out)
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def test_tui_defaults_to_separate_configured_controller(tmp_path, monkeypatch):
    from dark_factory.ui.transport import RemoteTransport

    client = tmp_path / "operator.toml"
    client.write_text(
        '[runtime]\noperator_token_env = "REMOTE_OPERATOR_TOKEN"\n'
        'api_host = "127.0.0.1"\napi_port = 9876\n'
        '[sources."missing/repo"]\npath = "/intentionally/missing"\n'
    )
    monkeypatch.setenv("REMOTE_OPERATOR_TOKEN", "separate-operator-token")
    observed = {}

    def run_tui(transport, actor):
        observed["transport"] = transport
        observed["actor"] = actor

    monkeypatch.setattr("dark_factory.ui.app.run_tui", run_tui)
    assert main(["--config", str(client), "tui", "--actor", "greg"]) == 0
    transport = observed["transport"]
    assert isinstance(transport, RemoteTransport)
    assert transport.base_url == "http://127.0.0.1:9876"
    assert observed["actor"] == "greg"
