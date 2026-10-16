"""Textual application shell for the modular operator screens."""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import App
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import Screen

from dark_factory.control.base import ControllerError
from dark_factory.ui.screens import (
    Confirm,
    DoneScreen,
    IssueScreen,
    NewWorkScreen,
    OverviewScreen,
    SettingsScreen,
    TextPrompt,
    WorkerScreen,
    WorkersScreen,
)
from dark_factory.ui.transport import OperatorTransport


class DarkFactoryApp(App[None]):
    CSS_PATH = "dark_factory.tcss"
    TITLE = "dark-factory"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+q", "quit", "Quit", priority=True, show=False),
        Binding("ctrl+c", "quit", "Quit", priority=True, show=False),
    ]

    def __init__(
        self,
        transport: OperatorTransport,
        actor: str = "operator",
        initial_snapshot: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.transport = transport
        self.actor = actor
        self.snapshot_data: dict[str, Any] = initial_snapshot or {}
        self.connection_error: str | None = None

    async def on_mount(self) -> None:
        if not self.snapshot_data:
            await self.refresh_snapshot(show_error=False)
        self.push_screen(OverviewScreen())
        self.set_interval(2.0, self.refresh_snapshot)

    def _render_mounted_screen(self, disconnected: bool) -> None:
        screen = self.screen
        if not screen.is_mounted:
            return
        render = getattr(screen, "render_snapshot", None)
        if render:
            try:
                render()
            except NoMatches:
                # A timer may finish while Textual is removing the old screen's
                # children. Its on_mount or the next refresh renders the replacement.
                return
        connection = getattr(screen, "show_connection_state", None)
        if connection:
            connection(disconnected)

    async def refresh_snapshot(
        self, show_error: bool = True, detail_id: str | None = None
    ) -> bool:
        try:
            if detail_id is None and isinstance(self.screen, IssueScreen):
                detail_id = self.screen.work_item_id
            snapshot = await asyncio.to_thread(self.transport.snapshot, detail_id)
        except Exception as exc:
            message = str(exc)
            changed = message != self.connection_error
            self.connection_error = message
            self._render_mounted_screen(True)
            if show_error and changed:
                self.notify(str(exc), severity="error", timeout=8)
            return False
        self.snapshot_data = snapshot
        was_disconnected = self.connection_error is not None
        self.connection_error = None
        self._render_mounted_screen(False)
        if was_disconnected:
            self.notify("Controller connection restored")
        return True

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
        prompts = {
            "pause": "Pause? Running steps finish; nothing new starts.",
            "resume": "Resume starting new steps?",
            "drain": "Drain? Running steps finish, then the factory stays idle.",
            "undrain": "Stop draining and start new steps again?",
        }
        await self.confirm_command(command, prompts.get(command, f"{command}?"))

    async def _run(self, name: str, params: dict[str, Any]) -> Any:
        result = await asyncio.to_thread(
            self.transport.command, name, params, self.actor
        )
        await self.refresh_snapshot()
        return result

    async def decide(self, item: dict[str, Any]) -> None:
        """The one forward decision a person can make on this item, if any."""

        status = item["status"]
        number = f"#{item['number']}"
        if status == "awaiting_approval":
            await self.confirm_command(
                "approve",
                f"Approve the plan for {number}? Building starts within its "
                f"{item['cap_cents'] / 100:,.2f} dollar limit.",
                {"work_item_id": item["id"]},
            )
        elif status == "awaiting_review":
            if item["next_action"].get("kind") != "human_review":
                self.notify(
                    "Automated evidence is missing; this can't be accepted",
                    severity="warning",
                )
                return
            remaining = item.get("packet", {}).get("manual_remaining", [])
            if remaining:
                self.notify(
                    f"Record {len(remaining)} manual check(s) with m first",
                    severity="warning",
                )
                return
            outside = item.get("packet", {}).get("external_remaining", [])
            if outside:
                self.notify(
                    f"Waiting on {', '.join(outside)} in the PR; o reads it again",
                    severity="warning",
                )
                return
            await self.confirm_command(
                "accept",
                f"Accept {number} as it is? Merging stays with you.",
                {"work_item_id": item["id"]},
            )
        elif status == "selected":
            await self.confirm_command(
                "plan", f"Start planning {number}?", {"work_item_id": item["id"]}
            )
        elif status == "interrupted":
            blockers = (item.get("interruption") or {}).get("blockers") or []
            if blockers:
                self.notify(
                    "Can't resume: " + "; ".join(blockers) + ". Stop it with n.",
                    severity="warning",
                )
                return
            await self.confirm_command(
                "continue-work",
                f"Resume {number} from its saved work? It stays within the "
                "approved budget and time limit.",
                {"work_item_id": item["id"]},
            )
        else:
            self.notify("Nothing needs your decision here", severity="warning")

    async def reject(self, item: dict[str, Any]) -> None:
        if item["status"] == "interrupted":

            async def stop(reason: str | None) -> None:
                if not reason:
                    return
                try:
                    await self._run(
                        "stop-work", {"work_item_id": item["id"], "reason": reason}
                    )
                    self.notify(f"#{item['number']} stopped; replan to continue")
                except (ControllerError, OSError, ValueError) as exc:
                    self.notify(str(exc), severity="error", timeout=8)

            self.push_screen(
                TextPrompt(
                    f"Why are you stopping #{item['number']}?",
                    "This goes to the next plan",
                ),
                stop,
            )
            return
        if item["status"] not in {"awaiting_approval", "awaiting_review"}:
            self.notify(
                "Only a plan or result waiting for you can be rejected",
                severity="warning",
            )
            return

        async def run(reason: str | None) -> None:
            if not reason:
                return
            try:
                await self._run(
                    "reject", {"work_item_id": item["id"], "reason": reason}
                )
                self.notify(f"#{item['number']} sent back")
            except (ControllerError, OSError, ValueError) as exc:
                self.notify(str(exc), severity="error", timeout=8)

        self.push_screen(
            TextPrompt(
                f"Why are you rejecting #{item['number']}?",
                "This goes to the next plan",
            ),
            run,
        )

    async def record_manual_gate(self, item: dict[str, Any]) -> None:
        if item["status"] != "awaiting_review":
            self.notify(
                "Manual checks are recorded when the result is ready",
                severity="warning",
            )
            return
        remaining = item.get("packet", {}).get("manual_remaining", [])
        candidate = item.get("candidates", {}).get(item["repo"], {}).get("commit")
        if not remaining:
            self.notify("No manual checks remain")
            return
        if not candidate:
            self.notify("The candidate isn't available", severity="error")
            return
        gate = remaining[0]

        async def run(evidence: str | None) -> None:
            if not evidence:
                return
            try:
                await self._run(
                    "manual-gate",
                    {
                        "work_item_id": item["id"],
                        "name": gate,
                        "candidate": candidate,
                        "evidence": evidence,
                    },
                )
                self.notify(f"Recorded: {gate}")
            except (ControllerError, OSError, ValueError) as exc:
                self.notify(str(exc), severity="error", timeout=8)

        self.push_screen(
            TextPrompt(
                f"What did you check for “{gate}”?", "What you inspected and saw"
            ),
            run,
        )

    async def read_outside_reviews(self, item: dict[str, Any]) -> None:
        if item["status"] != "awaiting_review":
            self.notify(
                "Outside reviews are read once the result is ready",
                severity="warning",
            )
            return
        pr = (item.get("packet", {}).get("external_reviews") or {}).get("pr")

        async def run(number: int | None) -> bool:
            """Read the PR; False when the controller needs a PR number."""
            try:
                result = await self._run(
                    "pr-evidence", {"work_item_id": item["id"], "number": number}
                )
            except (ControllerError, OSError, ValueError) as exc:
                if number is None and "name the pull request" in str(exc):
                    return False
                self.notify(str(exc), severity="error", timeout=8)
                return True
            waiting = result.get("remaining", [])
            self.notify(
                f"Still waiting on {', '.join(waiting)}"
                if waiting
                else "Outside reviews passed on this candidate"
            )
            return True

        # A known PR, or one opened by create-pr, is read without asking.
        if await run(pr["number"] if pr else None):
            return

        async def ask(answer: str | None) -> None:
            digits = (answer or "").strip().lstrip("#")
            if digits.isdigit():
                await run(int(digits))
            elif digits:
                self.notify("Enter a PR number", severity="warning")

        self.push_screen(
            TextPrompt(
                f"Which pull request holds the candidate for #{item['number']}?",
                "PR number, e.g. 42",
            ),
            ask,
        )

    async def start_planning(self, issues: list[dict[str, Any]]) -> None:
        async def run(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                result = await asyncio.to_thread(
                    self.transport.command, "intake", {"issues": issues}, self.actor
                )
                for work_item_id in result["ids"]:
                    await asyncio.to_thread(
                        self.transport.command,
                        "plan",
                        {"work_item_id": work_item_id},
                        self.actor,
                    )
                await self.refresh_snapshot()
                self.show_overview()
                self.notify(f"Planning started for {len(result['ids'])}")
            except Exception as exc:  # Textual must surface controller errors.
                self.notify(str(exc), severity="error", timeout=8)

        count = len(issues)
        self.push_screen(
            Confirm(
                f"Start planning {count} issue{'s' if count != 1 else ''}? "
                "Nothing is built until you approve each plan."
            ),
            run,
        )

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
    # Connect before switching the terminal into application mode. A mistyped
    # URL or stopped controller should fail as a normal CLI error, not leave a
    # blank TUI refreshing forever.
    snapshot = transport.snapshot()
    DarkFactoryApp(transport, actor, snapshot).run()
