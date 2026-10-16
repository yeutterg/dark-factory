from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest
from test_loop import cfg_for
from textual.widgets import OptionList

from dark_factory.api import serve
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store, now_iso
from dark_factory.ui.app import (
    DarkFactoryApp,
    IssueScreen,
    NewWorkScreen,
    OverviewScreen,
    SettingsScreen,
    run_tui,
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


@pytest.mark.parametrize(
    "change,message",
    [
        ({"external_id": ""}, "external_id"),
        ({"number": "12"}, "positive number"),
        ({"is_root": True, "parent_id": "opaque-parent"}, "inconsistent"),
        ({"required_child_count": -1}, "non-negative"),
    ],
)
def test_controller_rejects_malformed_tracker_discovery(
    tmp_path, monkeypatch, change, message
):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    candidate = {
        "external_id": "other-provider:opaque-12",
        "repo": cfg.repositories[0],
        "number": 12,
        "title": "Candidate",
        "url": "https://tracker.example.test/items/12",
        "is_root": True,
        "parent_id": None,
        "required_child_count": 0,
    }
    candidate.update(change)
    monkeypatch.setattr(
        ctrl.runner,
        "run_json",
        lambda *args: {
            "ok": True,
            "items": [candidate],
            "evidence": {"count": 1},
        },
    )
    try:
        with pytest.raises(ControllerError, match=message):
            ctrl.discover_issues()
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
        if name == "pr-evidence":
            return {"remaining": ["Cursor Bugbot"]}
        if name == "discover":
            return {
                "items": [
                    {
                        "external_id": "github:example/project:issue:13",
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


def test_tui_fails_before_terminal_mode_when_controller_is_unavailable():
    class Unavailable(OperatorTransport):
        def snapshot(self, detail_id=None):
            raise ControllerError("controller unavailable")

        def command(self, name, params=None, actor="operator"):
            raise AssertionError("no command should run")

    with pytest.raises(ControllerError, match="controller unavailable"):
        run_tui(Unavailable())


def test_tui_keeps_last_snapshot_and_marks_lost_connection():
    async def exercise() -> None:
        transport = FixtureTransport()
        app = DarkFactoryApp(transport, "greg")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()

            def unavailable(detail_id=None):
                raise ControllerError("controller unavailable")

            transport.snapshot = unavailable  # type: ignore[method-assign]
            assert await app.refresh_snapshot() is False
            assert app.snapshot_data["project"]["name"] == "example/project"
            crumb = app.screen.query_one("#crumb")
            assert "disconnected · showing last update" in crumb.right.plain
            keybar = app.screen.query_one("#keybar")
            assert "disconnected · showing last update" in keybar.right.plain

    asyncio.run(exercise())


def test_connection_state_update_is_safe_during_screen_mount():
    NewWorkScreen().show_connection_state(True)


def test_issue_screen_reads_outside_reviews_before_accepting():
    async def exercise() -> None:
        transport = FixtureTransport()
        item = transport.data["items"][0]
        item.update(
            status="awaiting_review",
            lane="needs_you",
            next_action={"kind": "human_review"},
            packet={
                "manual_remaining": [],
                "external_remaining": ["Cursor Bugbot"],
                "external_reviews": {
                    "required": [
                        {
                            "name": "Cursor Bugbot",
                            "passed": False,
                            "observed": [
                                {
                                    "source": "check",
                                    "passed": False,
                                    "detail": "failure",
                                }
                            ],
                        }
                    ],
                    "remaining": ["Cursor Bugbot"],
                    "pr": {
                        "number": 42,
                        "url": "https://example.test/pull/42",
                        "read_at": "2026-10-15T08:00:00+00:00",
                        "read_by": "greg",
                    },
                    "reviews": [],
                    "comments": [
                        {
                            "author": "cursor[bot]",
                            "path": "app.py",
                            "line": 3,
                            "body": "Bug",
                        }
                    ],
                },
            },
        )
        app = DarkFactoryApp(transport, "greg")
        opened: list[str] = []
        app.open_url = lambda url, **_: opened.append(url)  # type: ignore[method-assign]
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, IssueScreen)
            await pilot.press("g", "p")
            assert opened == ["https://example.test/pull/42"]
            review = app.screen.query_one("#issue-review").render()
            assert "Cursor Bugbot" in str(review)
            assert "cursor[bot]" in str(review)
            await pilot.press("o")
            await pilot.pause()
            assert (
                "pr-evidence",
                {"work_item_id": "work_1", "number": 42},
                "greg",
            ) in transport.commands
            await pilot.press("y")
            await pilot.pause()
            assert not any(name == "accept" for name, *_ in transport.commands)

    asyncio.run(exercise())


def test_new_work_groups_project_issues_by_theme():
    async def exercise() -> None:
        transport = FixtureTransport()
        board = {
            "id": "example/3",
            "title": "Roadmap",
            "url": "https://example.test/projects/3",
            "status": "Todo",
            "theme": "Reliability",
        }
        original = transport.command

        def command(name, params=None, actor="operator"):
            result = original(name, params, actor)
            if name == "discover":
                result["items"][0]["project"] = board
            return result

        transport.command = command  # type: ignore[method-assign]
        app = DarkFactoryApp(transport, "greg")
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, NewWorkScreen)
            sources = screen.query_one("#repos", OptionList)
            ids = [
                sources.get_option_at_index(i).id for i in range(sources.option_count)
            ]
            assert ids[:3] == [
                "all",
                "project:example/3",
                "theme:example/3|Reliability",
            ]
            sources.highlighted = 1
            await pilot.pause()
            assert screen.repo == "project:example/3"
            table = screen.query_one("#candidate-table", OptionList)
            assert table.get_option_at_index(0).id == "heading:Reliability"
            assert "Todo" in str(table.get_option_at_index(1).prompt)

    asyncio.run(exercise())


def test_new_work_nests_children_using_opaque_tracker_ids():
    async def exercise() -> None:
        transport = FixtureTransport()
        original = transport.command

        def command(name, params=None, actor="operator"):
            if name != "discover":
                return original(name, params, actor)
            return {
                "items": [
                    {
                        "external_id": "linear:parent:opaque",
                        "repo": "example/project",
                        "number": 20,
                        "title": "Parent outcome",
                        "url": "https://example.test/20",
                        "is_root": True,
                        "parent_id": None,
                        "required_child_count": 1,
                    },
                    {
                        "external_id": "linear:child:not-a-number",
                        "repo": "example/project",
                        "number": 21,
                        "title": "Required child",
                        "url": "https://example.test/21",
                        "is_root": False,
                        "parent_id": "linear:parent:opaque",
                        "required_child_count": 0,
                    },
                ],
                "evidence": {"count": 2},
            }

        transport.command = command  # type: ignore[method-assign]
        app = DarkFactoryApp(transport, "greg")
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, NewWorkScreen)
            table = screen.query_one("#candidate-table", OptionList)
            ids = [table.get_option_at_index(i).id for i in range(table.option_count)]
            assert ids == ["example/project#20", "example/project#21"]
            child = table.get_option_at_index(1)
            assert "part of #20" in child.prompt.plain

    asyncio.run(exercise())


def test_interrupted_work_resumes_or_stops_from_the_overview():
    async def exercise() -> None:
        transport = FixtureTransport()
        transport.data["items"][0].update(
            status="interrupted",
            lane="needs_you",
            next_action={"kind": "human_resume"},
            summary=["build was interrupted: the step ran out of time."],
            interruption={
                "job_id": "j",
                "cause": "the step ran out of time",
                "outcome": "asked",
                "recommendation": "resume",
                "reasons": ["rules: the step ran out of time"],
                "blockers": [],
            },
        )
        app = DarkFactoryApp(transport, "greg")
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            from rich.console import Console

            table = app.screen.query_one("#execution-table", OptionList)
            console = Console(width=140, record=True)
            console.print(table.get_option("work_1").prompt)
            assert "interrupted · resume?" in console.export_text()
            await pilot.press("y")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            assert (
                "continue-work",
                {"work_item_id": "work_1"},
                "greg",
            ) in transport.commands
            await pilot.press("n")
            await pilot.pause()
            await pilot.press(*"keeps timing out", "enter")
            await pilot.pause()
            assert (
                "stop-work",
                {"work_item_id": "work_1", "reason": "keeps timing out"},
                "greg",
            ) in transport.commands

    asyncio.run(exercise())
