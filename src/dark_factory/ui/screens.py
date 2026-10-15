"""Textual operator console matching the Paper Dark Factory TUI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from dark_factory.control.base import ControllerError

if TYPE_CHECKING:
    from dark_factory.ui.app import DarkFactoryApp


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _pretty(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _status_text(item: dict[str, Any]) -> str:
    action = item["next_action"]
    kind = action.get("kind", "")
    if kind == "human_approval":
        return "needs your approval"
    if kind == "human_review":
        return "ready to accept"
    if kind == "blocked":
        return action.get("reason", "stuck")
    if kind == "complete":
        return "accepted"
    if item["current_job"]:
        progress = (
            f" {item['jobs_complete']}/{item['jobs_total']}"
            if item["jobs_total"]
            else ""
        )
        return f"{item['current_job']}{progress}"
    return item["status"].replace("_", " ")


def _stage_rail(item: dict[str, Any]) -> str:
    if not item["progress_visible"]:
        return ""
    stages = ("Plan", "Crit", "Appr", "Build", "Check", "Review", "Accept")
    current = item["stage"].lower()
    parts = []
    reached = True
    for stage in stages:
        active = stage.lower().startswith(current[:4])
        parts.append(f"[bold #8fe3c0]{stage}[/]" if reached and active else stage)
        if active:
            reached = False
    return " · ".join(parts)


class Confirm(ModalScreen[bool]):
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 60; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    Confirm Button { margin: 1 1 0 0; }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt)
            with Horizontal():
                yield Button("Confirm", id="confirm", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)


class Help(ModalScreen[None]):
    DEFAULT_CSS = """
    Help { align: center middle; }
    Help > VerticalScroll { width: 72; height: 28; border: round $accent; padding: 1 2; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(
                "[b]dark-factory keys[/b]\n\n"
                "j/k or arrows  move in the focused pane\n"
                "Tab            execution / workers\n"
                "Enter          open selection\n"
                "i              new work\n"
                "w              workers\n"
                "s              settings\n"
                "Shift+P        pause / resume dispatch\n"
                "Shift+D        drain / undrain\n"
                "Shift+R        recover expired leases\n"
                "r              refresh\n"
                "Esc            back\n"
                "q              quit\n\n"
                "All mutations use the controller's operator command boundary."
            )

    def on_key(self, event: Any) -> None:
        if event.key in {"escape", "question_mark", "enter"}:
            self.dismiss(None)


class EvidencePrompt(ModalScreen[str | None]):
    DEFAULT_CSS = """
    EvidencePrompt { align: center middle; }
    EvidencePrompt > Vertical { width: 76; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    EvidencePrompt Input { margin-top: 1; }
    EvidencePrompt Button { margin: 1 1 0 0; }
    """

    def __init__(self, gate: str) -> None:
        super().__init__()
        self.gate = gate

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Record evidence for:\n{self.gate}")
            yield Input(placeholder="What you inspected and observed", id="evidence")
            with Horizontal():
                yield Button("Record", id="record", variant="primary")
                yield Button("Cancel", id="cancel-evidence")

    def on_mount(self) -> None:
        self.query_one("#evidence", Input).focus()

    @on(Button.Pressed, "#record")
    def record(self) -> None:
        evidence = self.query_one("#evidence", Input).value.strip()
        if not evidence:
            self.notify("Evidence is required", severity="warning")
            return
        self.dismiss(evidence)

    @on(Input.Submitted, "#evidence")
    def submit_evidence(self, event: Input.Submitted) -> None:
        evidence = event.value.strip()
        if evidence:
            self.dismiss(evidence)

    @on(Button.Pressed, "#cancel-evidence")
    def cancel(self) -> None:
        self.dismiss(None)


MOVE_BINDINGS = [
    Binding("j", "move_down", "Move", show=True),
    Binding("k", "move_up", "Move up", show=False),
]


class FactoryScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("question_mark", "help", "Keys", key_display="?"),
        Binding("r", "refresh", "Refresh"),
    ]

    @property
    def factory(self) -> DarkFactoryApp:
        return self.app  # type: ignore[return-value]

    def action_back(self) -> None:
        self.factory.show_overview()

    def action_help(self) -> None:
        self.app.push_screen(Help())

    async def action_refresh(self) -> None:
        await self.factory.refresh_snapshot()

    def _move(self, down: bool) -> None:
        focused = self.focused
        action = getattr(
            focused, "action_cursor_down" if down else "action_cursor_up", None
        )
        if action:
            action()
            return
        for scroll in self.query(VerticalScroll):
            if scroll.display:
                (scroll.scroll_down if down else scroll.scroll_up)(animate=False)
                return

    def action_move_down(self) -> None:
        self._move(True)

    def action_move_up(self) -> None:
        self._move(False)


class OverviewScreen(FactoryScreen):
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        *MOVE_BINDINGS,
        Binding("enter", "open", "Open"),
        Binding("i", "new_work", "New work"),
        Binding("w", "workers", "Workers"),
        Binding("s", "settings", "Settings"),
        Binding("d", "show_done", "Show done"),
        Binding("shift+p", "pause", "Pause"),
        Binding("shift+d", "drain", "Drain"),
        Binding("shift+r", "recover", "Recover"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="overview-title", classes="screen-title")
        yield Static(id="dispatch-state", classes="subtle")
        yield Static("EXECUTION STATUS", classes="section-label")
        yield DataTable(id="execution-table", cursor_type="row")
        yield Static(id="done-today", classes="done-line")
        yield Static("WORKERS", classes="section-label")
        yield DataTable(id="workers-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        execution = self.query_one("#execution-table", DataTable)
        execution.add_columns("issue", "progress", "where it is", "spent / cap", "age")
        workers = self.query_one("#workers-table", DataTable)
        workers.add_columns("worker", "state", "assignment", "last seen")
        self.render_snapshot()
        execution.focus()

    def render_snapshot(self) -> None:
        snapshot = self.factory.snapshot_data
        if not snapshot:
            return
        project = snapshot["project"]
        self.query_one("#overview-title", Static).update(
            f"[b]dark-factory[/b] — controller · {escape(project['name'])}"
        )
        dispatch = snapshot["dispatch"]
        state = (
            "dispatching" if dispatch["allowed"] else dispatch.get("reason") or "paused"
        )
        if dispatch["pause"] == "on":
            state = "paused"
        elif dispatch["drain"] == "on":
            state = "draining"
        self.query_one("#dispatch-state", Static).update(
            f"controller · {escape(state)}"
        )
        table = self.query_one("#execution-table", DataTable)
        table.clear()
        visible = [item for item in snapshot["items"] if item["lane"] != "done"]
        for lane, label in (("needs_you", "NEEDS YOU"), ("running", "RUNNING")):
            rows = [item for item in visible if item["lane"] == lane]
            if not rows:
                continue
            table.add_row(
                f"[b]{label}  {len(rows)}[/b]", "", "", "", "", key=f"lane:{lane}"
            )
            for item in rows:
                issue = f"#{item['number']}  {escape(item['title'])}"
                table.add_row(
                    issue,
                    _stage_rail(item),
                    escape(_status_text(item)),
                    f"{_money(item['spent_cents'])} / {_money(item['cap_cents'])}",
                    item["age"],
                    key=item["id"],
                )
        done = snapshot["done_today"]
        self.query_one("#done-today", Static).update(
            f"DONE TODAY  {done}\n[bold]d[/bold] show"
        )
        workers = self.query_one("#workers-table", DataTable)
        workers.clear()
        for worker in snapshot["workers"]:
            active = worker["active"]
            assignment = (
                f"{active['kind']} · {active['work_item_id']}" if active else "—"
            )
            workers.add_row(
                worker["id"],
                worker["status"],
                assignment,
                worker["age"],
                key=worker["id"],
            )
        if not snapshot["workers"]:
            workers.add_row("local", "not observed", "—", "—", key="local")

    def _selected_key(self, table_id: str) -> str | None:
        table = self.query_one(table_id, DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def action_open(self) -> None:
        focused = self.focused
        if focused and focused.id == "workers-table":
            key = self._selected_key("#workers-table")
            if key:
                self.factory.show_worker(key)
            return
        key = self._selected_key("#execution-table")
        if key and not key.startswith("lane:"):
            self.factory.show_issue(key)

    def action_new_work(self) -> None:
        self.factory.show_new_work()

    def action_workers(self) -> None:
        self.factory.show_workers()

    def action_settings(self) -> None:
        self.factory.show_settings()

    def action_show_done(self) -> None:
        self.factory.show_done()

    async def action_pause(self) -> None:
        await self.factory.toggle("pause", "resume", "pause")

    async def action_drain(self) -> None:
        await self.factory.toggle("drain", "undrain", "drain")

    async def action_recover(self) -> None:
        await self.factory.confirm_command("recover", "Recover expired leases now?")


class IssueScreen(FactoryScreen):
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        *MOVE_BINDINGS,
        Binding("1", "tab('activity')", "Activity", show=False),
        Binding("2", "tab('plan')", "Plan", show=False),
        Binding("3", "tab('critique')", "Critique", show=False),
        Binding("4", "tab('changes')", "Changes", show=False),
        Binding("5", "tab('checks')", "Checks", show=False),
        Binding("6", "tab('review')", "Review", show=False),
        Binding("7", "tab('log')", "Full log", show=False),
        Binding("a", "advance", "Approve / accept"),
        Binding("g", "manual_gate", "Record manual gate"),
        Binding("n", "reject", "Reject"),
    ]

    def __init__(self, work_item_id: str) -> None:
        super().__init__()
        self.work_item_id = work_item_id

    def compose(self) -> ComposeResult:
        yield Static(id="issue-title", classes="screen-title")
        yield Static(id="issue-summary")
        with TabbedContent(id="issue-tabs"):
            for identifier, title in (
                ("activity", "1 activity"),
                ("plan", "2 plan"),
                ("critique", "3 critique"),
                ("changes", "4 changes"),
                ("checks", "5 checks"),
                ("review", "6 review"),
                ("log", "7 full log"),
            ):
                with TabPane(title, id=identifier):
                    yield VerticalScroll(
                        Static(
                            id=f"issue-{identifier}", markup=identifier == "activity"
                        )
                    )
        yield Footer()

    async def on_mount(self) -> None:
        self.render_snapshot()
        await self.factory.refresh_snapshot(detail_id=self.work_item_id)

    def _item(self) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.factory.snapshot_data.get("items", [])
                if item["id"] == self.work_item_id
            ),
            None,
        )

    def render_snapshot(self) -> None:
        item = self._item()
        if not item:
            self.factory.show_overview()
            return
        self.query_one("#issue-title", Static).update(
            f"[b]#{item['number']}  {escape(item['title'])}[/b]\n{escape(item['repo'] or '')}"
        )
        self.query_one("#issue-summary", Static).update(
            f"{escape(item['status'])} · {escape(_status_text(item))} · "
            f"{_money(item['spent_cents'])} / {_money(item['cap_cents'])}"
        )
        activity = [
            f"{work['kind']:<12} {work['state']:<9} {work['job_id']}"
            for work in item["work"]
        ]
        next_action = _pretty(item["next_action"])
        self.query_one("#issue-activity", Static).update(
            "[b]steps[/b]\n"
            + "\n".join(activity)
            + "\n\n[b]next action[/b]\n"
            + next_action
        )
        self.query_one("#issue-plan", Static).update(_pretty(item["plan"]))
        self.query_one("#issue-critique", Static).update(_pretty(item["critique"]))
        changes = item.get("diff_patch") or _pretty(item["candidates"])
        self.query_one("#issue-changes", Static).update(changes)
        self.query_one("#issue-checks", Static).update(
            _pretty([e for e in item["evidence"] if e["kind"] != "review"])
        )
        self.query_one("#issue-review", Static).update(_pretty(item["packet"]))
        self.query_one("#issue-log", Static).update(_pretty(item["audit"]))

    def action_tab(self, tab: str) -> None:
        self.query_one("#issue-tabs", TabbedContent).active = tab

    async def action_advance(self) -> None:
        item = self._item()
        if not item:
            return
        if item["status"] == "awaiting_approval":
            await self.factory.confirm_command(
                "approve",
                "Approve this exact proposal and its bounded authority?",
                {"work_item_id": item["id"]},
            )
        elif item["status"] == "awaiting_review":
            remaining = item.get("packet", {}).get("manual_remaining", [])
            if remaining:
                self.notify(
                    f"Record {len(remaining)} manual gate(s) with g before acceptance",
                    severity="warning",
                )
                return
            await self.factory.confirm_command(
                "accept",
                "Accept this exact candidate and recorded evidence?",
                {"work_item_id": item["id"]},
            )
        elif item["status"] == "selected":
            await self.factory.confirm_command(
                "plan",
                "Start planning this complete root?",
                {"work_item_id": item["id"]},
            )
        else:
            self.notify(
                "No human advance is available in this state", severity="warning"
            )

    async def action_manual_gate(self) -> None:
        item = self._item()
        if not item or item["status"] != "awaiting_review":
            self.notify(
                "Manual gates are recorded during candidate review", severity="warning"
            )
            return
        remaining = item.get("packet", {}).get("manual_remaining", [])
        candidate = item.get("candidates", {}).get(item["repo"], {}).get("commit")
        if not remaining:
            self.notify("No manual gates remain")
            return
        if not candidate:
            self.notify("Candidate identity is unavailable", severity="error")
            return
        gate = remaining[0]

        async def record(evidence: str | None) -> None:
            if not evidence:
                return
            try:
                await asyncio.to_thread(
                    self.factory.transport.command,
                    "manual-gate",
                    {
                        "work_item_id": item["id"],
                        "name": gate,
                        "candidate": candidate,
                        "evidence": evidence,
                    },
                    self.factory.actor,
                )
                await self.factory.refresh_snapshot()
                self.notify(f"Recorded manual gate: {gate}")
            except (ControllerError, OSError, ValueError) as exc:
                self.notify(str(exc), severity="error", timeout=8)

        self.app.push_screen(EvidencePrompt(gate), record)

    async def action_reject(self) -> None:
        item = self._item()
        if not item or item["status"] not in {"awaiting_approval", "awaiting_review"}:
            self.notify(
                "Only a proposal or candidate awaiting review can be rejected",
                severity="warning",
            )
            return
        await self.factory.confirm_command(
            "reject",
            "Reject this item and retain its evidence for replanning?",
            {"work_item_id": item["id"], "reason": "Rejected from TUI review"},
        )


class SettingsScreen(FactoryScreen):
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        *MOVE_BINDINGS,
        Binding("shift+r", "reload", "Reload config"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            "[b]dark-factory — controller · settings[/b]", classes="screen-title"
        )
        yield Static(
            "factory.toml is the source of truth · settings apply to new work; approved work retains its authority",
            classes="subtle",
        )
        yield DataTable(id="routes-table", cursor_type="row")
        yield VerticalScroll(Static(id="settings-detail", markup=False))
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#routes-table", DataTable)
        table.add_columns(
            "role", "route", "agent", "model", "provider", "account", "environment"
        )
        self.render_snapshot()

    def render_snapshot(self) -> None:
        settings = self.factory.snapshot_data.get("settings", {})
        table = self.query_one("#routes-table", DataTable)
        table.clear()
        role_routes = settings.get("role_routes", {})
        routes = {route["id"]: route for route in settings.get("routes", [])}
        bindings = (
            [
                (role, role_routes.get(role, "—"))
                for role in ("planner", "critic", "coder", "reviewer")
            ]
            if role_routes
            else [(route["role"], route["id"]) for route in routes.values()]
        )
        for role, route_id in bindings:
            route = routes.get(route_id, {})
            table.add_row(
                role,
                route_id,
                route.get("harness", "—"),
                route.get("model", "—"),
                route.get("provider", "—"),
                route.get("billing_account", "—"),
                route.get("environment", "local") or "local",
            )
        detail = {
            "repositories": settings.get("repositories", []),
            "budgets": settings.get("budgets", {}),
            "schedule": settings.get("windows", []),
            "people": settings.get("people", []),
            "machines": settings.get("workers", {}),
            "environments": settings.get("environments", {}),
            "workflow": settings.get("workflow"),
        }
        self.query_one("#settings-detail", Static).update(_pretty(detail))

    async def action_reload(self) -> None:
        await self.factory.confirm_command(
            "reload", "Reload validated factory.toml for future work?"
        )


class NewWorkScreen(FactoryScreen):
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        Binding("space", "select_candidate", "Select"),
        Binding("y", "start", "Start planning"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.candidates: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        yield Static(
            "[b]dark-factory — controller · new work[/b]", classes="screen-title"
        )
        yield Static(
            "Open issues carrying the configured factory label. Space selects the highlighted root; r refreshes."
        )
        yield DataTable(id="candidate-table", cursor_type="row")
        yield Input(placeholder="owner/repo#number", id="issue-input")
        yield Static(id="new-work-summary")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#candidate-table", DataTable)
        table.add_columns("project", "issue", "title", "scope")
        self.query_one("#issue-input", Input).focus()
        await self.action_refresh()

    async def action_refresh(self) -> None:
        table = self.query_one("#candidate-table", DataTable)
        try:
            result = await asyncio.to_thread(
                self.factory.transport.command,
                "discover",
                {},
                self.factory.actor,
            )
            items = result.get("items", []) if isinstance(result, dict) else []
            table.clear()
            self.candidates = {}
            for item in items:
                key = f"{item['repo']}#{item['number']}"
                self.candidates[key] = item
                if not item.get("is_root"):
                    scope = "child — select its root"
                elif item.get("required_child_count"):
                    scope = f"root · {item['required_child_count']} required children"
                else:
                    scope = "complete root"
                table.add_row(
                    item["repo"],
                    f"#{item['number']}",
                    escape(item.get("title", "")),
                    scope,
                    key=key,
                )
            if not items:
                table.add_row("—", "—", "No labeled open issues", "", key="empty")
        except (ControllerError, OSError, ValueError) as exc:
            self.notify(str(exc), severity="error", timeout=8)

    def action_select_candidate(self) -> None:
        if (
            not isinstance(self.focused, DataTable)
            or self.focused.id != "candidate-table"
        ):
            return
        table = self.query_one("#candidate-table", DataTable)
        key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        candidate = self.candidates.get(key)
        if not candidate:
            return
        if not candidate.get("is_root"):
            self.notify("Select the complete root, not this child", severity="warning")
            return
        if candidate.get("required_child_count"):
            self.notify(
                "This root requires child-graph execution, which is not available yet",
                severity="warning",
            )
            return
        field = self.query_one("#issue-input", Input)
        selected = [entry.strip() for entry in field.value.split(",") if entry.strip()]
        if key not in selected:
            selected.append(key)
            field.value = ", ".join(selected)

    @on(Input.Changed, "#issue-input")
    def update_summary(self, event: Input.Changed) -> None:
        try:
            issues = list(self._parse(event.value))
            text = "\n".join(
                f"  {issue['repo']} #{issue['number']}" for issue in issues
            )
            self.query_one("#new-work-summary", Static).update(
                f"[b]selected {len(issues)}[/b]\n{text}\n\ny  start planning"
            )
        except ValueError as exc:
            self.query_one("#new-work-summary", Static).update(
                f"[red]{escape(str(exc))}[/red]"
            )

    @staticmethod
    def _parse(value: str) -> Iterable[dict[str, Any]]:
        if not value.strip():
            return []
        issues = []
        for entry in value.split(","):
            entry = entry.strip()
            repo, marker, number = entry.rpartition("#")
            if (
                not marker
                or "/" not in repo
                or not number.isdigit()
                or int(number) <= 0
            ):
                raise ValueError(f"invalid tracker root: {entry}")
            issues.append({"repo": repo, "number": int(number)})
        return issues

    async def action_start(self) -> None:
        try:
            issues = list(self._parse(self.query_one("#issue-input", Input).value))
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        if not issues:
            self.notify("Select at least one complete root", severity="warning")
            return

        async def submit(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                result = await asyncio.to_thread(
                    self.factory.transport.command,
                    "intake",
                    {"issues": issues},
                    self.factory.actor,
                )
                for work_item_id in result["ids"]:
                    await asyncio.to_thread(
                        self.factory.transport.command,
                        "plan",
                        {"work_item_id": work_item_id},
                        self.factory.actor,
                    )
                await self.factory.refresh_snapshot()
                self.factory.show_overview()
                self.notify(f"Planning started for {len(result['ids'])} root(s)")
            except Exception as exc:  # Textual must surface controller errors.
                self.notify(str(exc), severity="error", timeout=8)

        self.app.push_screen(
            Confirm("Intake these complete roots and start bounded planning?"), submit
        )


class DoneScreen(FactoryScreen):
    BINDINGS = [*FactoryScreen.BINDINGS, *MOVE_BINDINGS]

    def compose(self) -> ComposeResult:
        yield Static("[b]dark-factory — accepted[/b]", classes="screen-title")
        yield DataTable(id="done-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#done-table", DataTable)
        table.add_columns("issue", "accepted", "spent")
        for item in self.factory.snapshot_data.get("items", []):
            if item["lane"] == "done":
                table.add_row(
                    f"#{item['number']}  {escape(item['title'])}",
                    item["accepted_at"] or "—",
                    _money(item["spent_cents"]),
                    key=item["id"],
                )


class WorkersScreen(FactoryScreen):
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        *MOVE_BINDINGS,
        Binding("enter", "open", "Open"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("[b]dark-factory — sandbox hosts[/b]", classes="screen-title")
        yield DataTable(id="all-workers", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#all-workers", DataTable)
        table.add_columns("host", "state", "environments", "capabilities", "assignment")
        for worker in self.factory.snapshot_data.get("workers", []):
            active = worker["active"]
            table.add_row(
                worker["id"],
                worker["status"],
                ", ".join(worker["environments"]) or "—",
                ", ".join(worker["capabilities"]) or "—",
                active["work_item_id"] if active else "—",
                key=worker["id"],
            )

    def action_open(self) -> None:
        table = self.query_one("#all-workers", DataTable)
        if table.row_count:
            key = str(
                table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            )
            self.factory.show_worker(key)


class WorkerScreen(FactoryScreen):
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        *MOVE_BINDINGS,
        Binding("f", "preflight", "Preflight"),
        Binding("enter", "assignment", "Assignment"),
    ]

    def __init__(self, worker_id: str) -> None:
        super().__init__()
        self.worker_id = worker_id
        self.mode = "overview"

    def compose(self) -> ComposeResult:
        yield Static(id="worker-title", classes="screen-title")
        yield VerticalScroll(Static(id="worker-detail", markup=False))
        yield Footer()

    def on_mount(self) -> None:
        self.render_snapshot()

    def _worker(self) -> dict[str, Any]:
        return next(
            (
                worker
                for worker in self.factory.snapshot_data.get("workers", [])
                if worker["id"] == self.worker_id
            ),
            {
                "id": self.worker_id,
                "status": "not observed",
                "environments": [],
                "capabilities": [],
                "active": None,
            },
        )

    def render_snapshot(self) -> None:
        worker = self._worker()
        self.query_one("#worker-title", Static).update(
            f"[b]dark-factory worker — {escape(worker['id'])}[/b] · {escape(worker['status'])}"
        )
        if self.mode == "preflight":
            detail = {
                "controller": "connected"
                if worker["status"] != "offline"
                else "not observed",
                "environments": worker["environments"],
                "capabilities": worker["capabilities"],
                "lease_seconds": worker.get("lease_seconds"),
                "authority": "execution only; no operator decisions",
            }
        elif self.mode == "assignment":
            detail = worker.get("active") or {"assignment": "none"}
        else:
            detail = {
                "host": worker["id"],
                "state": worker["status"],
                "last_heartbeat": worker.get("last_heartbeat"),
                "environments": worker["environments"],
                "harness_capabilities": worker["capabilities"],
                "current_assignment": worker.get("active"),
            }
        self.query_one("#worker-detail", Static).update(_pretty(detail))

    def action_preflight(self) -> None:
        self.mode = "preflight"
        self.render_snapshot()

    def action_assignment(self) -> None:
        self.mode = "assignment"
        self.render_snapshot()
