from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from test_loop import cfg_for

from dark_factory.api import serve
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store, now_iso
from dark_factory.ui.app import (
    DarkFactoryApp,
    IssueScreen,
    NewWorkScreen,
    OverviewScreen,
    SettingsScreen,
)
from dark_factory.ui.projection import controller_projection
from dark_factory.ui.transport import OperatorTransport, RemoteTransport


def test_projection_hides_terminal_progress_and_counts_done_today(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    try:
        selected, accepted, approved = ctrl.intake_issues(
            [
                {
                    "repo": cfg.repositories[0],
                    "number": 10,
                    "title": "Needs planning",
                    "body": "",
                    "is_root": True,
                },
                {
                    "repo": cfg.repositories[0],
                    "number": 11,
                    "title": "Already accepted",
                    "body": "",
                    "is_root": True,
                },
                {
                    "repo": cfg.repositories[0],
                    "number": 12,
                    "title": "Approved without a progress rail",
                    "body": "",
                    "is_root": True,
                },
            ]
        )
        store.execute(
            "UPDATE work_items SET status='accepted',presentation_stage='Done',accepted_at=? WHERE id=?",
            (now_iso(), accepted),
        )
        store.execute(
            "UPDATE work_items SET status='approved',presentation_stage='Approve' WHERE id=?",
            (approved,),
        )
        store.execute(
            "INSERT INTO workers(id,capabilities,last_heartbeat,busy) VALUES (?,?,?,0)",
            ("worker-a", '["native"]', now_iso()),
        )
        archive = tmp_path / "candidate-archive"
        archive.mkdir()
        (archive / "diff.patch").write_text("diff --git a/a.py b/a.py\n+ready = True\n")
        store.execute(
            "UPDATE work_items SET plan_json=?,candidate_commits=? WHERE id=?",
            (
                '{"proposal":{"what":"detail on demand"}}',
                json.dumps(
                    {
                        cfg.repositories[0]: {
                            "commit": "a" * 40,
                            "archive": str(archive),
                        }
                    }
                ),
                selected,
            ),
        )

        projection = controller_projection(ctrl)
        by_id = {item["id"]: item for item in projection["items"]}
        assert by_id[selected]["progress_visible"] is True
        assert by_id[accepted]["progress_visible"] is False
        assert by_id[approved]["progress_visible"] is True
        assert by_id[selected]["steps"] == []
        assert by_id[accepted]["lane"] == "done"
        assert projection["done_today"] == 1
        assert projection["workers"][0]["id"] == "worker-a"
        assert by_id[selected]["plan"] == {}
        detailed = controller_projection(ctrl, selected)
        detailed_item = next(
            item for item in detailed["items"] if item["id"] == selected
        )
        assert detailed_item["plan"]["proposal"]["what"] == "detail on demand"
        assert "+ready = True" in detailed_item["diff_patch"]
        serialized = json.dumps(projection)
        assert cfg.worker_token not in serialized
        assert cfg.operator_token not in serialized
    finally:
        store.close()


def test_operator_projection_requires_operator_credential(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    server = serve(ctrl, "127.0.0.1", 0, cfg.worker_token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(
            "GET",
            "/operator/view",
            headers={"Authorization": f"Bearer {cfg.worker_token}"},
        )
        unauthorized = conn.getresponse()
        assert unauthorized.status == 401
        unauthorized.read()

        conn.request(
            "GET",
            "/operator/view",
            headers={"Authorization": f"Bearer {cfg.operator_token}"},
        )
        response = conn.getresponse()
        assert response.status == 200
        result = json.loads(response.read())["result"]
        assert result["project"]["name"] == cfg.project_name
        remote = RemoteTransport(
            f"http://127.0.0.1:{server.server_port}", cfg.operator_token
        )
        assert remote.snapshot()["project"]["name"] == cfg.project_name
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def test_controller_discovery_uses_configured_scope_without_state_write(
    tmp_path, monkeypatch
):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    calls = []

    def discover(argv, operation, payload, request_id, timeout):
        calls.append((operation, payload))
        return {
            "ok": True,
            "items": [],
            "evidence": {"count": 0, "label": cfg.labels["selected"]},
        }

    monkeypatch.setattr(ctrl.runner, "run_json", discover)
    before = len(store.query("SELECT * FROM audit"))
    try:
        assert ctrl.discover_issues()["items"] == []
        assert calls == [
            (
                "list_candidate_issues",
                {
                    "repositories": cfg.repositories,
                    "label": cfg.labels["selected"],
                    "limit": 50,
                },
            )
        ]
        assert len(store.query("SELECT * FROM audit")) == before
    finally:
        store.close()


def test_remote_operator_transport_requires_tls_off_loopback():
    try:
        RemoteTransport("http://controller.example.test", "operator-token")
    except ControllerError as exc:
        assert "requires HTTPS" in str(exc)
    else:
        raise AssertionError("remote operator credential must not travel over HTTP")


def snapshot_fixture() -> dict[str, Any]:
    item = {
        "id": "work_1",
        "number": "12",
        "external_id": "github:example/project:issue:12",
        "url": "https://example.test/12",
        "title": "Bounded provider fallback",
        "body": "",
        "repo": "example/project",
        "status": "awaiting_approval",
        "stage": "Approve",
        "spent_cents": 84,
        "reserved_cents": 0,
        "cap_cents": 1800,
        "created_at": now_iso(),
        "accepted_at": None,
        "age": "12m",
        "jobs_complete": 2,
        "jobs_total": 7,
        "current_job": "",
        "progress_visible": False,
        "lane": "needs_you",
        "next_action": {"kind": "human_approval"},
        "work": [],
        "jobs": [],
        "attempts": [],
        "evidence": [],
        "audit": [],
        "packet": {},
        "plan": {},
        "critique": {},
        "candidates": {},
    }
    return {
        "schema": 1,
        "generated_at": now_iso(),
        "project": {
            "id": "example",
            "name": "example/project",
            "repositories": ["example/project"],
            "config": "/private/factory.toml",
            "digest": "abc",
        },
        "dispatch": {
            "allowed": True,
            "reason": "",
            "pause": "off",
            "drain": "off",
        },
        "items": [item],
        "done_today": 2,
        "workers": [
            {
                "id": "build-01",
                "status": "ready",
                "last_heartbeat": now_iso(),
                "age": "3s",
                "capabilities": ["native"],
                "environments": ["sandbox"],
                "lease_seconds": 120,
                "active": None,
            }
        ],
        "settings": {
            "routes": [],
            "role_routes": {},
            "environments": {},
            "workers": {},
            "repositories": ["example/project"],
            "budgets": {"root_cents": 1800},
            "windows": [],
            "people": ["operator"],
            "workflow": "native",
        },
    }


class FixtureTransport(OperatorTransport):
    def __init__(self) -> None:
        self.data = snapshot_fixture()
        self.commands: list[tuple[str, dict[str, Any], str]] = []

    def snapshot(self, detail_id: str | None = None) -> dict[str, Any]:
        return deepcopy(self.data)

    def command(
        self, name: str, params: dict[str, Any] | None = None, actor: str = "operator"
    ) -> Any:
        self.commands.append((name, params or {}, actor))
        if name == "pause":
            self.data["dispatch"]["pause"] = "on"
            self.data["dispatch"]["allowed"] = False
        if name == "discover":
            return {
                "items": [
                    {
                        "repo": "example/project",
                        "number": 13,
                        "title": "Discovered complete root",
                        "url": "https://example.test/13",
                        "is_root": True,
                        "parent_id": None,
                        "required_child_count": 0,
                    }
                ],
                "evidence": {"count": 1},
            }
        return None


def test_overview_panes_scroll_independently_and_navigate():
    async def exercise() -> None:
        transport = FixtureTransport()
        app = DarkFactoryApp(transport, "greg")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            assert app.focused is not None
            assert app.focused.id == "execution-table"
            assert app.focused.highlighted_option.id == "work_1"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, IssueScreen)
            assert app.screen.work_item_id == "work_1"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            await pilot.press("tab")
            assert app.focused is not None
            assert app.focused.id == "workers-table"

            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            await pilot.press("i")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, NewWorkScreen)
            assert app.focused is not None
            assert app.focused.id == "candidate-table"
            await pilot.press("space")
            assert screen.selected == ["example/project#13"]
            await pilot.press("enter")
            assert screen.selected == []
            await pilot.press("enter", "y")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            assert (
                "intake",
                {"issues": [{"repo": "example/project", "number": 13}]},
                "greg",
            ) in transport.commands

    asyncio.run(exercise())


def test_issue_screen_decisions_and_github_keys():
    async def exercise() -> None:
        transport = FixtureTransport()
        app = DarkFactoryApp(transport, "greg")
        opened: list[str] = []
        app.open_url = lambda url, **_: opened.append(url)  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, IssueScreen)
            await pilot.press("g", "i")
            assert opened == ["https://example.test/12"]
            assert isinstance(app.screen, IssueScreen)
            await pilot.press("4")
            assert app.screen.tab == "changes"
            await pilot.press("y")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            assert ("approve", {"work_item_id": "work_1"}, "greg") in transport.commands
            await pilot.press("n")
            await pilot.pause()
            await pilot.press(*"scope too wide", "enter")
            await pilot.pause()
            assert (
                "reject",
                {"work_item_id": "work_1", "reason": "scope too wide"},
                "greg",
            ) in transport.commands

    asyncio.run(exercise())


def test_overview_operator_action_uses_command_boundary():
    async def exercise() -> None:
        transport = FixtureTransport()
        app = DarkFactoryApp(transport, "greg")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            await screen.action_pause()
            await pilot.pause()
            await pilot.click("#confirm")
            await pilot.pause()
            assert transport.commands == [("pause", {}, "greg")]

    asyncio.run(exercise())


def test_primary_screens_mount_in_compact_terminal():
    async def exercise() -> None:
        app = DarkFactoryApp(FixtureTransport(), "greg")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for key in ("s", "escape", "i", "escape", "w"):
                await pilot.press(key)
                await pilot.pause()
            assert app.size == (80, 24)
            keybar = app.screen.query_one("#keybar")
            assert keybar.region.bottom == 24

    asyncio.run(exercise())


def test_tui_quit_bindings_exit_cleanly():
    quit_keys = {
        binding.key for binding in DarkFactoryApp.BINDINGS if binding.action == "quit"
    }
    assert quit_keys == {"q", "ctrl+q", "ctrl+c"}

    async def exercise(key: str) -> None:
        app = DarkFactoryApp(FixtureTransport(), "greg")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.is_running
            await pilot.press(key)
            await pilot.pause()
            assert not app.is_running
            assert app.return_code == 0

    asyncio.run(exercise("ctrl+q"))
    asyncio.run(exercise("ctrl+c"))
