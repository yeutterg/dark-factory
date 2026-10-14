"""Textual application shell for the modular operator screens."""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen

from dark_factory.control.base import ControllerError
from dark_factory.ui.screens import (
    Confirm,
    DoneScreen,
    IssueScreen,
    NewWorkScreen,
    OverviewScreen,
    SettingsScreen,
    WorkerScreen,
    WorkersScreen,
)
from dark_factory.ui.transport import OperatorTransport


class DarkFactoryApp(App[None]):
    CSS_PATH = "dark_factory.tcss"
    TITLE = "dark-factory"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("q", "quit", "Quit", priority=True)]

    def __init__(self, transport: OperatorTransport, actor: str = "operator") -> None:
        super().__init__()
        self.transport = transport
        self.actor = actor
        self.snapshot_data: dict[str, Any] = {}

    async def on_mount(self) -> None:
        await self.refresh_snapshot(show_error=False)
        self.push_screen(OverviewScreen())
        self.set_interval(2.0, self.refresh_snapshot)

    async def refresh_snapshot(
        self, show_error: bool = True, detail_id: str | None = None
    ) -> None:
        try:
            if detail_id is None and isinstance(self.screen, IssueScreen):
                detail_id = self.screen.work_item_id
            self.snapshot_data = await asyncio.to_thread(
                self.transport.snapshot, detail_id
            )
            render = getattr(self.screen, "render_snapshot", None)
            if render:
                render()
        except Exception as exc:
            if show_error:
                self.notify(str(exc), severity="error", timeout=8)

    def _replace(self, screen: Screen[Any]) -> None:
        if isinstance(self.screen, OverviewScreen) and isinstance(
            screen, OverviewScreen
        ):
            return
        if len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(screen)

    def show_overview(self) -> None:
        self._replace(OverviewScreen())

    def show_issue(self, work_item_id: str) -> None:
        self._replace(IssueScreen(work_item_id))

    def show_settings(self) -> None:
        self._replace(SettingsScreen())

    def show_new_work(self) -> None:
        self._replace(NewWorkScreen())

    def show_done(self) -> None:
        self._replace(DoneScreen())

    def show_workers(self) -> None:
        self._replace(WorkersScreen())

    def show_worker(self, worker_id: str) -> None:
        self._replace(WorkerScreen(worker_id))

    async def toggle(self, on: str, off: str, label: str) -> None:
        current = self.snapshot_data.get("dispatch", {}).get(label) == "on"
        command = off if current else on
        await self.confirm_command(command, f"{command.title()} controller dispatch?")

    async def confirm_command(
        self,
        name: str,
        prompt: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        async def run(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                await asyncio.to_thread(
                    self.transport.command, name, params or {}, self.actor
                )
                await self.refresh_snapshot()
                self.notify(f"{name} recorded")
            except (ControllerError, OSError, ValueError) as exc:
                self.notify(str(exc), severity="error", timeout=8)

        self.push_screen(Confirm(prompt), run)


def run_tui(transport: OperatorTransport, actor: str = "operator") -> None:
    DarkFactoryApp(transport, actor).run()
