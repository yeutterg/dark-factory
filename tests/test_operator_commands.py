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
