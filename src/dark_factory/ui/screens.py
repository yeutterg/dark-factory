"""Textual operator console matching the Paper Dark Factory TUI."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import (
    Horizontal,
    ScrollableContainer,
    Vertical,
    VerticalScroll,
)
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Button, ContentSwitcher, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from dark_factory.control.base import ControllerError
from dark_factory.ui import render as r

if TYPE_CHECKING:
    from dark_factory.ui.app import DarkFactoryApp

GLOBAL_KEYS = [("i", "new work"), ("w", "workers"), ("s", "settings"), ("?", "keys")]
TABS = (
    ("activity", "activity"),
    ("plan", "plan"),
    ("critique", "critique"),
    ("changes", "changes"),
    ("checks", "checks"),
    ("review", "review"),
    ("log", "full log"),
)
ISSUE_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


class Line(Widget):
    """One terminal row: left text, fill, right text."""

    DEFAULT_CSS = "Line { height: 1; }"

    def __init__(
        self,
        left: Text | None = None,
        right: Text | None = None,
        fill: str = " ",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.left = left or Text()
        self.right = right or Text()
        self.fill = fill

    def set(self, left: Text, right: Text | None = None) -> None:
        self.left = left
        self.right = right or Text()
        self.refresh()

    def render(self) -> Text:
        return r.spread(self.left, self.right, self.content_region.width, self.fill)


def crumb(*parts: Text | str) -> Text:
    out = Text()
    out.append("dark-factory", style=r.DIM)
    for part in parts:
        out.append(" › ", style=r.DIM)
        out.append_text(Text(part, style=r.FG) if isinstance(part, str) else part)
    return out


class Confirm(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", show=False),
        Binding("n", "answer(False)", show=False),
        Binding("escape", "answer(False)", show=False),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.prompt)
            yield Static(r.keys([("y", "yes"), ("n", "no")]), classes="dialog-keys")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Confirm", id="confirm")
                yield Button("Cancel", id="cancel")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)

    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)


class TextPrompt(ModalScreen[str | None]):
    """Ask for one required sentence, such as a rejection reason."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, prompt: str, placeholder: str) -> None:
        super().__init__()
        self.prompt = prompt
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.prompt)
            yield Input(placeholder=self.placeholder, id="prompt-input")
            yield Static(
                r.keys([("↵", "record"), ("esc", "cancel")]), classes="dialog-keys"
            )

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    @on(Input.Submitted, "#prompt-input")
    def submit(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            self.notify("This needs a sentence", severity="warning")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# Kept for callers that recorded manual evidence through the earlier dialog.
def EvidencePrompt(gate: str) -> TextPrompt:  # noqa: N802
    return TextPrompt(
        f"What did you check for “{gate}”?", "What you inspected and observed"
    )


class Help(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("question_mark", "close", show=False),
        Binding("enter", "close", show=False),
    ]

    def compose(self) -> ComposeResult:
        rows = [
            ("j/k", "move in the focused pane"),
            ("↹", "switch pane"),
            ("↵", "open"),
            ("y / n", "approve or accept / reject (asks why)"),
            ("1–7", "issue tabs"),
            ("gi gb gp", "open the issue, branch or pull request on GitHub"),
            ("c", "stop the running step"),
            ("m", "record a manual check"),
            ("o", "read checks and reviews from the PR"),
            ("i", "new work"),
            ("w", "workers"),
            ("s", "settings"),
            ("P D R", "pause, drain, recover"),
            ("r", "refresh"),
            ("esc", "back"),
            ("q", "quit this screen only; the controller keeps running"),
        ]
        table = Table.grid(padding=(0, 3))
        table.add_column(style=f"bold {r.FG}", no_wrap=True)
        table.add_column(style=r.DIM)
        for key, label in rows:
            table.add_row(key, label)
        with Vertical(classes="dialog"):
            yield Static(Text("keys", style=f"bold {r.FG}"))
            yield Static(table)

    def action_close(self) -> None:
        self.dismiss(None)


class FactoryScreen(Screen):
    KEYS: list[tuple[str, str]] = []
    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("question_mark", "help", "Keys", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("i", "new_work", "New work", show=False),
        Binding("w", "workers", "Workers", show=False),
        Binding("s", "settings", "Settings", show=False),
        Binding("j", "move_down", "Move", show=False),
        Binding("k", "move_up", "Move up", show=False),
    ]

    @property
    def factory(self) -> DarkFactoryApp:
        return self.app  # type: ignore[return-value]

    @on(events.Mount)
    def render_after_layout(self) -> None:
        # Lists size themselves after the first layout; render again at the real width.
        render = getattr(self, "render_snapshot", None)
        if render:
            self.call_after_refresh(render)

    def keybar(self) -> Line:
        return Line(r.keys(self.KEYS), r.keys(GLOBAL_KEYS), id="keybar")

    def width_of(self, widget: Widget) -> int:
        width = widget.scrollable_content_region.width
        return width if width > 0 else max(40, self.app.size.width - 6)

    def action_back(self) -> None:
        self.factory.show_overview()

    def action_help(self) -> None:
        self.app.push_screen(Help())

    async def action_refresh(self) -> None:
        await self.factory.refresh_snapshot()

    def action_new_work(self) -> None:
        self.factory.show_new_work()

    def action_workers(self) -> None:
        self.factory.show_workers()

    def action_settings(self) -> None:
        self.factory.show_settings()

    def _move(self, down: bool) -> None:
        focused = self.focused
        action = getattr(
            focused, "action_cursor_down" if down else "action_cursor_up", None
        )
        if action:
            action()
            return
        target = (
            focused
            if isinstance(focused, ScrollableContainer)
            else next((s for s in self.query(VerticalScroll) if s.display), None)
        )
        if target:
            (target.scroll_down if down else target.scroll_up)(animate=False)

    def action_move_down(self) -> None:
        self._move(True)

    def action_move_up(self) -> None:
        self._move(False)

    def on_resize(self) -> None:
        render = getattr(self, "render_snapshot", None)
        if render:
            self.call_after_refresh(render)


def set_options(
    widget: OptionList, options: list[Option], prefer: str | None = None
) -> None:
    """Replace options while keeping the highlighted row when it still exists."""

    current = widget.highlighted_option
    keep = current.id if current else prefer
    widget.set_options(options)
    ids = [option.id for option in options]
    if keep in ids:
        widget.highlighted = ids.index(keep)
    else:
        widget.highlighted = next(
            (i for i, option in enumerate(options) if not option.disabled), None
        )


class OverviewScreen(FactoryScreen):
    KEYS = [
        ("j/k", "move"),
        ("↵", "open"),
        ("P", "pause"),
        ("D", "drain"),
        ("R", "recover"),
    ]
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        Binding("y", "decide", "Approve", show=False),
        Binding("n", "reject", "Reject", show=False),
        Binding("d", "show_done", "Show done", show=False),
        Binding("P,shift+p", "pause", "Pause", show=False),
        Binding("D,shift+d", "drain", "Drain", show=False),
        Binding("R,shift+r", "recover", "Recover", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Line(crumb(), id="crumb")
        with Vertical(id="overview-body"):
            with Vertical(id="exec-panel", classes="panel"):
                yield Line(id="exec-head")
                yield OptionList(id="execution-table")
                yield Line(id="done-today", fill="─")
            with Vertical(id="workers-panel", classes="panel"):
                yield Line(id="workers-head", fill="─")
                yield OptionList(id="workers-table")
        yield self.keybar()

    def on_mount(self) -> None:
        self.render_snapshot()
        self.query_one("#execution-table", OptionList).focus()

    def _items(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.factory.snapshot_data.get("items", [])
            if item["lane"] != "done"
        ]

    def _routes(self) -> dict[str, Any]:
        settings = self.factory.snapshot_data.get("settings", {})
        return {route["id"]: route for route in settings.get("routes", [])}

    def render_snapshot(self) -> None:
        snapshot = self.factory.snapshot_data
        if not snapshot:
            return
        dispatch = snapshot["dispatch"]
        state = Text()
        if dispatch["pause"] == "on":
            state = Text("paused · P resumes", style=r.AMBER)
        elif dispatch["drain"] == "on":
            state = Text("draining · D undrains", style=r.AMBER)
        self.query_one("#crumb", Line).set(crumb(), state)
        self.query_one("#keybar", Line).set(
            r.keys(
                [
                    ("j/k", "move"),
                    ("↵", "open"),
                    ("P", "resume" if dispatch["pause"] == "on" else "pause"),
                    ("D", "undrain" if dispatch["drain"] == "on" else "drain"),
                    ("R", "recover"),
                ]
            ),
            r.keys(GLOBAL_KEYS),
        )
        table = self.query_one("#execution-table", OptionList)
        width = self.width_of(table)
        cols = r.columns(width)
        items = self._items()
        self.query_one("#exec-head", Line).set(r.header_line(items, cols))
        routes = self._routes()
        highlighted = table.highlighted_option
        selected_id = highlighted.id if highlighted else None
        options: list[Option] = []
        for lane, label, colour in (
            ("needs_you", "needs you", r.AMBER),
            ("running", "running", r.MINT),
        ):
            rows = [item for item in items if item["lane"] == lane]
            if not rows:
                continue
            head = Text.assemble(
                ("   " + label, f"bold {colour}"), (f"  {len(rows)}", r.DIM)
            )
            options.append(
                Option(
                    r.spread(head, Text(), width, "─"), id=f"lane:{lane}", disabled=True
                )
            )
            for item in rows:
                options.append(
                    Option(
                        r.item_prompt(item, cols, routes, item["id"] == selected_id),
                        id=item["id"],
                    )
                )
        if not options:
            options.append(
                Option(
                    Text(
                        "   Nothing in progress. Press i to choose new work.",
                        style=r.DIM,
                    ),
                    id="empty",
                    disabled=True,
                )
            )
        set_options(table, options)
        self._rehint()
        done = snapshot["done_today"]
        self.query_one("#done-today", Line).set(
            Text.assemble(("   done today", f"bold {r.DIM}"), (f"  {done}", r.DIM)),
            r.keys([("d", "show")]),
        )
        workers = self.query_one("#workers-table", OptionList)
        by_id = {item["id"]: item for item in snapshot["items"]}
        live = sum(w["status"] != "offline" for w in snapshot["workers"])
        offline = len(snapshot["workers"]) - live
        note = f"{live} live" + (f" · {offline} offline" if offline else "")
        self.query_one("#workers-head", Line).set(
            Text.assemble(("workers", f"bold {r.DIM}"), (f"  {note}", r.DIM))
        )
        worker_width = self.width_of(workers)
        worker_options = [
            Option(r.worker_line(worker, by_id, worker_width), id=worker["id"])
            for worker in snapshot["workers"]
        ] or [
            Option(Text("no workers registered", style=r.DIM), id="none", disabled=True)
        ]
        set_options(workers, worker_options)

    def _rehint(self) -> None:
        """Show decision keys only under the highlighted needs-you item."""

        table = self.query_one("#execution-table", OptionList)
        current = table.highlighted_option
        cols = r.columns(self.width_of(table))
        routes = self._routes()
        for item in self._items():
            try:
                table.replace_option_prompt(
                    item["id"],
                    r.item_prompt(
                        item, cols, routes, bool(current and current.id == item["id"])
                    ),
                )
            except Exception:  # Option disappeared between refreshes.
                continue

    @on(OptionList.OptionHighlighted, "#execution-table")
    def highlighted(self) -> None:
        self._rehint()

    @on(OptionList.OptionSelected, "#execution-table")
    def open_item(self, event: OptionList.OptionSelected) -> None:
        if event.option_id and not event.option_id.startswith(("lane:", "empty")):
            self.factory.show_issue(event.option_id)

    @on(OptionList.OptionSelected, "#workers-table")
    def open_worker(self, event: OptionList.OptionSelected) -> None:
        if event.option_id and event.option_id != "none":
            self.factory.show_worker(event.option_id)

    def _selected_item(self) -> dict[str, Any] | None:
        option = self.query_one("#execution-table", OptionList).highlighted_option
        return next(
            (item for item in self._items() if option and item["id"] == option.id),
            None,
        )

    async def action_decide(self) -> None:
        item = self._selected_item()
        if item:
            await self.factory.decide(item)

    async def action_reject(self) -> None:
        item = self._selected_item()
        if item:
            await self.factory.reject(item)

    def action_show_done(self) -> None:
        self.factory.show_done()

    async def action_pause(self) -> None:
        await self.factory.toggle("pause", "resume", "pause")

    async def action_drain(self) -> None:
        await self.factory.toggle("drain", "undrain", "drain")

    async def action_recover(self) -> None:
        await self.factory.confirm_command(
            "recover", "Recover work from workers that stopped responding?"
        )


def _diff_files(patch: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1]
            files.append({"path": path, "plus": 0, "minus": 0})
        elif files and line.startswith("+") and not line.startswith("+++"):
            files[-1]["plus"] += 1
        elif files and line.startswith("-") and not line.startswith("---"):
            files[-1]["minus"] += 1
    return files


def _diff_text(patch: str) -> tuple[Text, list[int]]:
    """Coloured diff and the rendered line of each file header."""

    out = Text()
    offsets: list[int] = []
    for index, line in enumerate(patch.splitlines()):
        if line.startswith("diff --git "):
            offsets.append(index)
            out.append(line.split(" b/", 1)[-1] + "\n", style=f"bold {r.FG}")
        elif line.startswith(("index ", "--- ", "+++ ")):
            out.append(line + "\n", style=r.FAINT)
        elif line.startswith("@@"):
            out.append(line + "\n", style=r.BLUE)
        elif line.startswith("+"):
            out.append(line + "\n", style=f"{r.MINT} on #18261f")
        elif line.startswith("-"):
            out.append(line + "\n", style=f"{r.HAZARD} on #2a1a1a")
        else:
            out.append(line + "\n", style=r.FG)
    return out, offsets


def _bullet_list(title: str, values: list[Any]) -> Text:
    out = Text()
    out.append(title + "\n", style=f"bold {r.DIM}")
    for value in values:
        if isinstance(value, dict):
            value = " · ".join(str(v) for v in value.values() if v not in (None, ""))
        out.append("  • ", style=r.DIM)
        out.append(str(value) + "\n", style=r.FG)
    return out


def _paragraph(title: str, value: Any) -> Text:
    out = Text()
    out.append(title + "\n", style=f"bold {r.DIM}")
    out.append(str(value) + "\n", style=r.FG)
    return out


def _outside_reviews(item: dict[str, Any]) -> Text:
    """Outside checks and reviewers from the PR, judged on the exact candidate."""

    external = item.get("packet", {}).get("external_reviews") or {}
    required = external.get("required") or []
    out = Text()
    if not required and not external.get("reviews") and not external.get("comments"):
        return out
    pr = external.get("pr")
    out.append("outside reviews", style=f"bold {r.DIM}")
    out.append(
        f"  PR #{pr['number']} · read {pr['read_at'][11:16]}\n"
        if pr
        else "  not read yet · o reads the PR\n",
        style=r.DIM,
    )
    for result in required:
        seen = result.get("observed") or []
        if result["passed"]:
            glyph, style = "✓", r.MINT
        elif seen:
            glyph, style = "×", r.HAZARD
        else:
            glyph, style = "○", r.DIM
        out.append(f"{glyph} ", style=style)
        out.append(result["name"], style=r.FG)
        detail = " · ".join(f"{o['source']} {o['detail']}".strip() for o in seen)
        out.append(
            f"  {detail or 'nothing on this commit yet'}\n",
            style=r.DIM,
        )
    comments = external.get("comments") or []
    reviews = [rv for rv in external.get("reviews") or [] if rv.get("body")]
    if reviews or comments:
        out.append(f"\n{len(reviews) + len(comments)} comments\n", style=r.DIM)
    for review in reviews:
        out.append(f"  {review['author']}", style=r.FG)
        out.append(f"  {review['state'].lower().replace('_', ' ')}\n", style=r.DIM)
        out.append(f"    {review['body'].strip().splitlines()[0][:160]}\n", style=r.FG)
    for comment in comments:
        where = f"{comment.get('path', '')}:{comment.get('line') or ''}".rstrip(":")
        out.append(f"  {comment['author']}", style=r.FG)
        out.append(f"  {where}\n", style=r.DIM)
        first = (comment.get("body") or "").strip().splitlines() or [""]
        out.append(f"    {first[0][:160]}\n", style=r.FG)
    out.append("\n")
    return out


class IssueScreen(FactoryScreen):
    KEYS = [
        ("esc", "back"),
        ("c", "stop step"),
        ("1–7", "tabs"),
        ("j/k", "pick step"),
        ("gi gb gp", "open on GitHub"),
    ]
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        *[
            Binding(str(n), f"tab('{tab}')", show=False)
            for n, (tab, _) in enumerate(TABS, 1)
        ],
        Binding("y", "decide", "Approve / accept", show=False),
        Binding("n", "reject", "Reject", show=False),
        Binding("m", "manual_gate", "Record manual check", show=False),
        Binding("o", "outside_reviews", "Read PR reviews", show=False),
        Binding("c", "stop", "Stop step", show=False),
        Binding("g", "prefix('g')", show=False),
        Binding("right_square_bracket", "prefix(']')", show=False),
        Binding("left_square_bracket", "prefix('[')", show=False),
        Binding("b", "suffix('b')", show=False),
        Binding("p", "suffix('p')", show=False),
        Binding("f", "suffix('f')", show=False),
    ]

    def __init__(self, work_item_id: str) -> None:
        super().__init__()
        self.work_item_id = work_item_id
        self.tab = "activity"
        self.prefix = ""
        self.file_offsets: list[int] = []
        self.file_index = -1

    def compose(self) -> ComposeResult:
        yield Line(crumb(), id="crumb")
        with Horizontal(id="issue-body"):
            with Vertical(id="issue-left", classes="panel"):
                yield Static(id="links")
                yield Line(id="steps-head", fill="─")
                yield OptionList(id="steps")
                yield VerticalScroll(Static(id="acceptance"), id="acceptance-scroll")
            with Vertical(id="issue-right", classes="panel"):
                yield Line(id="tab-bar")
                with ContentSwitcher(initial="activity", id="issue-tabs"):
                    for tab, _ in TABS:
                        yield VerticalScroll(Static(id=f"issue-{tab}"), id=tab)
                yield Line(id="next-line")
        yield self.keybar()

    async def on_mount(self) -> None:
        self.render_snapshot()
        self.query_one("#steps", OptionList).focus()
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

    def _routes(self) -> dict[str, Any]:
        settings = self.factory.snapshot_data.get("settings", {})
        return {route["id"]: route for route in settings.get("routes", [])}

    def _selected_step(self, item: dict[str, Any]) -> dict[str, Any] | None:
        option = self.query_one("#steps", OptionList).highlighted_option
        step = next(
            (s for s in item.get("steps", []) if option and s["id"] == option.id),
            None,
        )
        return step or r.current_step(item)[0]

    def render_snapshot(self) -> None:
        item = self._item()
        if not item:
            if self.factory.snapshot_data:
                self.factory.show_overview()
            return
        dot = r.glyph(item)
        title = Text.assemble(dot, " ", (f"{item['title']} #{item['number']}", "bold"))
        self.query_one("#crumb", Line).set(
            crumb(title),
            Text.assemble(
                r.where(item),
                "    ",
                (
                    f"{r.money(item['spent_cents'])} / {r.money(item['cap_cents'])}",
                    r.DIM,
                ),
            ),
        )
        self._render_links(item)
        self._render_steps(item)
        self._render_acceptance(item)
        self._render_tabs(item)
        self._render_next(item)
        hints = [k for k in r.decision_keys(item) if k[0] != "↵"]
        if item["status"] == "awaiting_review" and item.get("packet", {}).get(
            "manual_remaining"
        ):
            hints.append(("m", "record manual check"))
        if item["status"] == "awaiting_review" and item.get("packet", {}).get(
            "external_remaining"
        ):
            hints.append(("o", "read PR reviews"))
        self.query_one("#keybar", Line).set(
            r.keys(
                [
                    key
                    for key in self.KEYS
                    if key[0] != "c"
                    or (step := r.current_step(item)[0])
                    and step["status"] in r.ACTIVE
                ]
                + hints
            ),
            r.keys(GLOBAL_KEYS),
        )

    def _render_links(self, item: dict[str, Any]) -> None:
        candidate = item.get("candidates", {}).get(item["repo"] or "", {})
        commit = candidate.get("commit")
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=f"bold {r.FG}", no_wrap=True)
        grid.add_column(style=r.DIM, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_row(
            "gi",
            "issue",
            Text(f"{item['repo']}#{item['number']} ↗", style=r.BLUE)
            if item.get("url")
            else Text("—", style=r.FAINT),
        )
        grid.add_row(
            "gb",
            "branch",
            Text(f"not pushed · candidate {commit[:7]} is saved", style=r.DIM)
            if commit
            else Text("none yet · made by the build step", style=r.FAINT),
        )
        pr = (item.get("packet", {}).get("external_reviews") or {}).get("pr")
        grid.add_row(
            "gp" if pr else Text("gp", style=r.FAINT),
            "pull request",
            Text(f"#{pr['number']} ↗", style=r.BLUE)
            if pr
            else Text("none yet · you open it after review", style=r.FAINT),
        )
        self.query_one("#links", Static).update(grid)

    def _render_steps(self, item: dict[str, Any]) -> None:
        steps = item.get("steps", [])
        widget = self.query_one("#steps", OptionList)
        width = self.width_of(widget)
        reserve = sum(s["repair"] and s["status"] in r.RESERVE for s in steps)
        note = f"plan v{item.get('plan_version') or 1}" if steps else "no plan yet"
        if reserve:
            note += " · one fix held in reserve"
        self.query_one("#steps-head", Line).set(
            Text.assemble(("steps", f"bold {r.DIM}"), (f"  {note}", r.DIM))
        )
        routes = self._routes()
        now = datetime.now(UTC)
        options = []
        for step in steps:
            reserved = step["repair"] and step["status"] in r.RESERVE
            started = r.parse_time(step.get("started_at"))
            ended = r.parse_time(step.get("ended_at"))
            route = step.get("route_id") or ""
            if reserved:
                detail = (
                    "reserve · on check failure"
                    if step["kind"] == "implement"
                    else "reserve"
                )
            elif step["status"] in r.ACTIVE:
                parts = [route or step.get("role") or "", step.get("worker_id") or ""]
                if started:
                    parts.append(r.duration((now - started).total_seconds()))
                detail = " · ".join(p for p in parts if p)
            elif step["status"] == "succeeded":
                parts = [route or step.get("role") or ""]
                if started and ended:
                    parts.append(r.duration((ended - started).total_seconds()))
                detail = " · ".join(p for p in parts if p)
            elif step["status"] in r.FAILED:
                detail = step.get("error") or step["status"].replace("_", " ")
            elif step["kind"] in {"check", "command"}:
                detail = " · ".join(step.get("checks") or []) or "checks"
            else:
                detail = (
                    routes.get(route, {}).get("id") or step.get("role") or step["kind"]
                )
            name_style = (
                r.FAINT
                if reserved
                else f"bold {r.FG}"
                if step["status"] in r.ACTIVE
                else r.FG
            )
            cost = r.money(step["spent_cents"]) if step["attempt_count"] else "—"
            name_w = 16
            cost_w = 8
            line = Text.assemble(
                r.cell(r.step_glyph(step), 3),
                r.cell(Text(step["name"], style=name_style), name_w),
                r.cell(
                    Text(
                        detail,
                        style=r.HAZARD
                        if step["status"] in r.FAILED
                        else r.FAINT
                        if reserved
                        else r.DIM,
                    ),
                    max(4, width - 3 - name_w - cost_w),
                ),
                r.cell(
                    Text(cost, style=r.FAINT if reserved else r.FG), cost_w, "right"
                ),
            )
            options.append(Option(line, id=step["id"]))
        if not options:
            options.append(
                Option(
                    Text("Steps appear once planning starts.", style=r.DIM),
                    id="none",
                    disabled=True,
                )
            )
        current = r.current_step(item)[0]
        set_options(widget, options, prefer=current["id"] if current else None)

    def _render_acceptance(self, item: dict[str, Any]) -> None:
        criteria = item.get("acceptance", [])
        met = sum(c.get("status") == "verified" for c in criteria)
        width = self.width_of(self.query_one("#acceptance-scroll"))
        parts: list[RenderableType] = [
            r.section(
                "acceptance",
                f"{met} of {len(criteria)} met" if criteria else "",
                width=width,
            ),
            Text(""),
        ]
        if not criteria:
            parts.append(Text("Criteria appear once the plan is written.", style=r.DIM))
        failed = item["status"] in {"failed", "rejected"}
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(width=7, no_wrap=True)
        grid.add_column(ratio=1)
        for criterion in criteria:
            missing = criterion.get("missing_evidence", [])
            needed = (
                len(criterion.get("checks", []))
                + len(criterion.get("manual_gates", []))
                + 1
            )
            manual_only = bool(missing) and all(
                m.startswith("manual:") for m in missing
            )
            if not missing:
                mark = Text("✓", style=r.MINT)
            elif failed:
                mark = Text("×", style=r.HAZARD)
            elif manual_only:
                mark = Text("◇", style=r.AMBER)
            elif len(missing) < needed:
                mark = Text("◐", style=r.BLUE)
            else:
                mark = Text("○", style=r.FG)
            proof = [f"check {c}" for c in criterion.get("checks", [])]
            proof.append("review")
            proof += [f"you · {g}" for g in criterion.get("manual_gates", [])]
            waiting = sorted({m.split(":", 1)[0] for m in missing})
            how = " · ".join(proof)
            if waiting:
                how += " — waiting on " + ", ".join(waiting).replace("-", " ")
            grid.add_row(
                mark,
                Text(criterion["id"], style=r.DIM),
                Group(
                    Text(criterion.get("description", ""), style=r.FG),
                    Text(how, style=r.AMBER if manual_only else r.DIM),
                ),
            )
        if criteria:
            parts.append(grid)
        parts.append(Text(""))
        parts.append(
            Text("✓ met  ◐ in progress  ○ not yet  × failed  ◇ yours", style=r.FAINT)
        )
        out = Group(*parts)
        self.query_one("#acceptance", Static).update(out)

    def _render_tabs(self, item: dict[str, Any]) -> None:
        files = _diff_files(item.get("diff_patch") or "")
        plus = sum(f["plus"] for f in files)
        minus = sum(f["minus"] for f in files)
        bar = Text()
        for n, (tab, label) in enumerate(TABS, 1):
            if n > 1:
                bar.append("   ")
            active = tab == self.tab
            bar.append(f"{n} ", style=r.DIM)
            bar.append(label, style=f"bold underline {r.FG}" if active else r.DIM)
            if tab == "changes" and files:
                bar.append(f" +{plus}", style=r.MINT)
                bar.append(f" −{minus}", style=r.HAZARD)
        self.query_one("#tab-bar", Line).set(bar)
        renderers = {
            "activity": self._activity,
            "plan": self._plan,
            "critique": self._critique,
            "changes": self._changes,
            "checks": self._checks,
            "review": self._review,
            "log": self._log,
        }
        for tab, _ in TABS:
            self.query_one(f"#issue-{tab}", Static).update(renderers[tab](item))

    def _render_next(self, item: dict[str, Any]) -> None:
        action = item.get("next_action", {})
        kind = action.get("kind")
        if kind == "human_approval":
            text = "you approve the plan (y) → build → check → independent review"
        elif kind == "human_review":
            remaining = item.get("packet", {}).get("manual_remaining") or []
            outside = item.get("packet", {}).get("external_remaining") or []
            text = (
                f"you check “{remaining[0]}” (m) → accept (y)"
                if remaining
                else f"waiting on {', '.join(outside)} in the PR (o reads it) → accept (y)"
                if outside
                else "you check the result and accept it (y)"
            )
        elif kind == "complete":
            text = "accepted · merging stays with you"
        elif kind == "blocked":
            text = action.get("next_step") or action.get("reason", "")
        elif item["status"] == "selected":
            text = "start planning (y)"
        else:
            step, position, _ = r.current_step(item)
            later = r.visible_steps(item)[position:] if step else []
            names = [s["name"] for s in later] + ["you accept"]
            text = " → ".join(names)
        self.query_one("#next-line", Line).set(
            Text.assemble(("next  ", f"bold {r.BLUE}"), (text, r.FG))
        )

    def _activity(self, item: dict[str, Any]) -> RenderableType:
        step = self._selected_step(item)
        if not step:
            return Text("Nothing has run yet.", style=r.DIM)
        steps = r.visible_steps(item)
        position = next(
            (i + 1 for i, s in enumerate(steps) if s["id"] == step["id"]), 0
        )
        head = Text(step["name"], style=f"bold {r.FG}")
        meta = []
        if position:
            meta.append(f"step {position} of {len(steps)}")
        if step.get("started_at"):
            meta.append("started " + r.clock(step["started_at"]))
        if step["attempt_count"]:
            meta.append(f"attempt {step['attempt_count']}")
        head.append("   " + " · ".join(meta), style=r.DIM)
        route = self._routes().get(step.get("route_id") or "", {})
        fields = Table.grid(padding=(0, 4))
        for _ in range(4):
            fields.add_column(no_wrap=True)
        fields.add_row(
            Text("route", style=r.DIM),
            Text("worker", style=r.DIM),
            Text("spend", style=r.DIM),
            Text("time limit", style=r.DIM),
        )
        route_text = (
            " · ".join(
                v
                for v in (route.get("id"), route.get("provider"), route.get("region"))
                if v
            )
            or step.get("role")
            or "—"
        )
        now = datetime.now(UTC)
        started = r.parse_time(step.get("started_at"))
        ended = r.parse_time(step.get("ended_at"))
        deadline = r.parse_time(step.get("deadline"))
        if step["status"] in r.ACTIVE and deadline:
            left = (deadline - now).total_seconds()
            limit = f"{r.duration(left)} left" if left > 0 else "over time"
        else:
            limit = r.duration(step.get("timeout_seconds") or 0)
        fields.add_row(
            route_text,
            step.get("worker_id") or "—",
            f"{r.money(step['spent_cents'])} / {r.money(step['budget_cents'])}"
            if step["budget_cents"]
            else r.money(step["spent_cents"]),
            limit,
        )
        parts: list[RenderableType] = [head, Text(""), fields, Text("")]
        if step["status"] in r.ACTIVE and started and step.get("timeout_seconds"):
            used = (now - started).total_seconds() / step["timeout_seconds"]
            width = 30
            filled = min(width, int(width * used))
            colour = r.AMBER if used >= 0.8 else r.BLUE
            parts.append(
                Text.assemble(
                    ("━" * filled, colour),
                    ("━" * (width - filled), r.FAINT),
                    (
                        f"  {int(used * 100)}% of its time · at the limit "
                        f"#{item['number']} comes to you",
                        colour,
                    ),
                )
            )
            parts.append(Text(""))
        elif step["status"] == "succeeded" and started and ended:
            parts.append(
                Text(
                    f"finished in {r.duration((ended - started).total_seconds())}",
                    style=r.DIM,
                )
            )
            parts.append(Text(""))
        if step.get("error"):
            parts.append(Text(step["error"], style=r.HAZARD))
            parts.append(Text(""))
        parts.append(r.section("log", width=60))
        log = Text()
        for entry in item.get("audit", [])[-40:]:
            log.append(r.clock(entry["at"], seconds=True).ljust(10), style=r.DIM)
            log.append(entry["action"].ljust(22), style=r.FG)
            payload = json.loads(entry.get("payload_json") or "{}")
            detail = " · ".join(
                f"{k} {str(v)[:24]}"
                for k, v in payload.items()
                if k not in {"root", "work_item_id"} and isinstance(v, (str, int))
            )
            log.append(detail[:120] + "\n", style=r.DIM)
        parts.append(log if log else Text("No events yet.", style=r.DIM))
        return Group(*parts)

    def _plan(self, item: dict[str, Any]) -> RenderableType:
        proposal = item.get("plan", {}).get("proposal") or item.get("plan", {})
        if not proposal:
            return Text("No plan yet. Planning writes one.", style=r.DIM)
        out = Text()
        for key, title in (("why", "why"), ("what", "what")):
            if proposal.get(key):
                out.append_text(_paragraph(title, proposal[key]))
                out.append("\n")
        tasks = proposal.get("tasks") or []
        if tasks:
            out.append_text(
                _bullet_list(
                    "tasks",
                    [
                        f"{t.get('description', '')}"
                        + (
                            f"  (checks: {', '.join(t['checks'])})"
                            if t.get("checks")
                            else ""
                        )
                        if isinstance(t, dict)
                        else t
                        for t in tasks
                    ],
                )
            )
            out.append("\n")
        acceptance = proposal.get("acceptance") or []
        if acceptance:
            out.append_text(
                _bullet_list(
                    "acceptance",
                    [
                        f"{c.get('id')}  {c.get('description')}"
                        if isinstance(c, dict)
                        else c
                        for c in acceptance
                    ],
                )
            )
            out.append("\n")
        for key, title in (
            ("assumptions", "assumptions"),
            ("test_instructions", "how you'll test it"),
            ("needs_input", "questions"),
        ):
            if proposal.get(key):
                out.append_text(_bullet_list(title, proposal[key]))
                out.append("\n")
        return out

    def _critique(self, item: dict[str, Any]) -> RenderableType:
        critique = item.get("critique") or {}
        if not critique:
            return Text("No critique yet.", style=r.DIM)
        out = Text()
        if "passed" in critique:
            out.append(
                "passed\n\n" if critique["passed"] else "found blocking issues\n\n",
                style=r.MINT if critique["passed"] else r.HAZARD,
            )
        if critique.get("summary"):
            out.append_text(_paragraph("summary", critique["summary"]))
            out.append("\n")
        findings = critique.get("findings") or []
        out.append_text(_bullet_list("findings", findings or ["none"]))
        return out

    def _changes(self, item: dict[str, Any]) -> RenderableType:
        patch = item.get("diff_patch") or ""
        if not patch:
            self.file_offsets = []
            return Text("No changes yet. The build step makes them.", style=r.DIM)
        files = _diff_files(patch)
        listing = Text()
        for index, entry in enumerate(files):
            marker = "› " if index == self.file_index else "  "
            listing.append(marker + entry["path"].ljust(60), style=r.FG)
            listing.append(f"+{entry['plus']}".rjust(6), style=r.MINT)
            listing.append(f"-{entry['minus']}".rjust(6) + "\n", style=r.HAZARD)
        diff, offsets = _diff_text(patch)
        header = Text(f"{len(files)} files changed\n", style=r.DIM)
        base = 1 + len(files) + 1
        self.file_offsets = [base + offset for offset in offsets]
        return Group(header, listing, diff)

    def _checks(self, item: dict[str, Any]) -> RenderableType:
        rows = [e for e in item.get("evidence", []) if e["kind"] != "review"]
        if not rows:
            return Text("Checks run after the build step.", style=r.DIM)
        out = Text()
        for evidence in rows:
            payload = json.loads(evidence.get("payload_json") or "{}")
            checks = payload.get("checks") if isinstance(payload, dict) else None
            if isinstance(checks, list):
                for check in checks:
                    ok = check.get("exit_code") == 0
                    out.append("✓ " if ok else "× ", style=r.MINT if ok else r.HAZARD)
                    out.append(str(check.get("command", "check")), style=r.FG)
                    out.append(f"   exit {check.get('exit_code')}\n", style=r.DIM)
            else:
                out.append(f"• {evidence['kind']}", style=r.FG)
                out.append(
                    f"   {str(evidence.get('digest') or '')[:12]}\n", style=r.DIM
                )
        return out

    def _review(self, item: dict[str, Any]) -> RenderableType:
        out = Text()
        reviews = [e for e in item.get("evidence", []) if e["kind"] == "review"]
        for evidence in reviews[-1:]:
            try:
                parsed = json.loads(json.loads(evidence["payload_json"])["output"])
            except (ValueError, KeyError, TypeError):
                continue
            out.append(
                "review passed\n\n"
                if parsed.get("passed")
                else "review found problems\n\n",
                style=r.MINT if parsed.get("passed") else r.HAZARD,
            )
            if parsed.get("summary"):
                out.append_text(_paragraph("summary", parsed["summary"]))
                out.append("\n")
            if parsed.get("findings"):
                out.append_text(_bullet_list("findings", parsed["findings"]))
                out.append("\n")
        summary = item.get("packet", {}).get("exec_summary") or {}
        if summary.get("assumptions"):
            out.append_text(_bullet_list("assumptions made", summary["assumptions"]))
            out.append("\n")
        remaining = summary.get("remaining_manual_steps") or []
        if remaining:
            out.append_text(_bullet_list("left for you (m records one)", remaining))
            out.append("\n")
        out.append_text(_outside_reviews(item))
        tests = item.get("packet", {}).get("What to test") or []
        if tests:
            out.append_text(
                _bullet_list("how to test it", [t.get("action", t) for t in tests])
            )
        return out or Text("The independent review runs after checks.", style=r.DIM)

    def _log(self, item: dict[str, Any]) -> RenderableType:
        out = Text()
        for entry in item.get("audit", []):
            out.append(entry["at"][:19].replace("T", " ") + "  ", style=r.DIM)
            out.append(entry["actor"].ljust(12), style=r.DIM)
            out.append(entry["action"] + "  ", style=r.FG)
            out.append((entry.get("payload_json") or "")[:300] + "\n", style=r.FAINT)
        return out or Text("No events yet.", style=r.DIM)

    @on(OptionList.OptionHighlighted, "#steps")
    def step_changed(self) -> None:
        item = self._item()
        if item:
            self.query_one("#issue-activity", Static).update(self._activity(item))

    def action_tab(self, tab: str) -> None:
        self.tab = tab
        self.query_one("#issue-tabs", ContentSwitcher).current = tab
        item = self._item()
        if item:
            self._render_tabs(item)

    def action_prefix(self, key: str) -> None:
        self.prefix = key

    def _take(self, prefix: str) -> bool:
        taken = self.prefix == prefix
        self.prefix = ""
        return taken

    def action_new_work(self) -> None:
        if self._take("g"):
            self._open_link("i")
            return
        super().action_new_work()

    def action_suffix(self, key: str) -> None:
        prefix, self.prefix = self.prefix, ""
        if prefix == "g" and key in {"b", "p"}:
            self._open_link(key)
        elif prefix in {"]", "["} and key == "f":
            self._jump_file(1 if prefix == "]" else -1)

    def _open_link(self, which: str) -> None:
        item = self._item()
        if not item:
            return
        if which == "i" and item.get("url"):
            self.app.open_url(item["url"])
        elif which == "b":
            self.notify("The candidate isn't pushed to a branch yet.")
        elif pr := (item.get("packet", {}).get("external_reviews") or {}).get("pr"):
            self.app.open_url(pr["url"])
        else:
            self.notify("No pull request yet. You open it after review.")

    def _jump_file(self, step: int) -> None:
        if not self.file_offsets:
            return
        self.action_tab("changes")
        self.file_index = (self.file_index + step) % len(self.file_offsets)
        item = self._item()
        if item:
            self._render_tabs(item)
        self.query_one("#changes", VerticalScroll).scroll_to(
            y=self.file_offsets[self.file_index], animate=False
        )

    async def action_decide(self) -> None:
        item = self._item()
        if item:
            await self.factory.decide(item)

    async def action_reject(self) -> None:
        item = self._item()
        if item:
            await self.factory.reject(item)

    async def action_manual_gate(self) -> None:
        item = self._item()
        if item:
            await self.factory.record_manual_gate(item)

    async def action_outside_reviews(self) -> None:
        item = self._item()
        if item:
            await self.factory.read_outside_reviews(item)

    async def action_stop(self) -> None:
        item = self._item()
        if not item:
            return
        step, _, _ = r.current_step(item)
        if not step or step["status"] not in r.ACTIVE | {"ready"}:
            self.notify("No step is running", severity="warning")
            return
        await self.factory.confirm_command(
            "cancel",
            f"Stop {step['name']}? Its workspace is saved; restarting needs a new plan.",
            {"job_id": step["id"]},
        )


class SettingsScreen(FactoryScreen):
    KEYS = [
        ("j/k h/l", "move"),
        ("↹", "section"),
        ("↵", "edit"),
        ("⌃r", "check + reload"),
        ("esc", "back"),
    ]
    SECTIONS = ("machines", "agents", "repos", "budgets", "schedule", "people")
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        Binding("h", "column(-1)", show=False),
        Binding("l", "column(1)", show=False),
        Binding("e", "edit", show=False),
        Binding("ctrl+r", "reload", "Reload config", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.column = 0

    def compose(self) -> ComposeResult:
        yield Line(crumb("settings"), id="crumb")
        with Horizontal(id="settings-body"):
            with Vertical(id="settings-nav", classes="panel"):
                yield OptionList(id="sections")
            with Vertical(id="settings-main", classes="panel"):
                yield VerticalScroll(Static(id="settings-detail"), id="settings-scroll")
                yield Static(id="save-bar")
        yield self.keybar()

    def on_mount(self) -> None:
        self.render_snapshot()
        self.query_one("#sections", OptionList).focus()

    def _settings(self) -> dict[str, Any]:
        return self.factory.snapshot_data.get("settings", {})

    def _machines(self) -> list[dict[str, Any]]:
        settings = self._settings()
        observed = {w["id"]: w for w in self.factory.snapshot_data.get("workers", [])}
        names = sorted(set(settings.get("workers", {})) | set(observed))
        machines = []
        for name in names:
            configured = settings.get("workers", {}).get(name, {})
            seen = observed.get(name, {})
            machines.append(
                {
                    "id": name,
                    "environments": configured.get("environments")
                    or seen.get("environments")
                    or [],
                    "status": seen.get("status", "offline"),
                    "age": seen.get("age", "—"),
                    "busy": bool(seen.get("active")),
                    "lease_seconds": configured.get(
                        "lease_seconds", seen.get("lease_seconds")
                    ),
                    "token_env": configured.get("token_env"),
                }
            )
        return machines

    def render_snapshot(self) -> None:
        settings = self._settings()
        if not settings:
            return
        budgets = settings.get("budgets", {})
        counts = {
            "machines": str(len(self._machines())),
            "agents": str(len(settings.get("routes", []))),
            "repos": str(len(settings.get("repositories", []))),
            "budgets": r.money(budgets.get("root_cents", 0)),
            "schedule": str(len(settings.get("windows", [])) or "always"),
            "people": str(len(settings.get("people", []))),
        }
        nav = self.query_one("#sections", OptionList)
        width = self.width_of(nav)
        set_options(
            nav,
            [
                Option(
                    r.spread(
                        Text(name, style=r.FG), Text(counts[name], style=r.DIM), width
                    ),
                    id=name,
                )
                for name in self.SECTIONS
            ],
        )
        self._render_section()
        config = self.factory.snapshot_data.get("project", {}).get("config", "")
        self.query_one("#save-bar", Static).update(
            Group(
                Text.assemble(("factory.toml  ", f"bold {r.AMBER}"), (config, r.FG)),
                Text(
                    "↵ opens it in your editor, then checks and reloads it. Applies to "
                    "new work. Work already running keeps the settings you approved.",
                    style=r.DIM,
                ),
            )
        )

    def _render_section(self) -> None:
        option = self.query_one("#sections", OptionList).highlighted_option
        name = option.id if option else "machines"
        settings = self._settings()
        width = self.width_of(self.query_one("#settings-scroll"))
        detail = self.query_one("#settings-detail", Static)
        if name == "machines":
            detail.update(self._machine_matrix(width))
        elif name == "agents":
            table = Table.grid(padding=(0, 3))
            for _ in range(5):
                table.add_column(no_wrap=True)
            table.add_row(
                *[
                    Text(h, style=r.DIM)
                    for h in ("agent · model", "role", "runs on", "account", "login")
                ]
            )
            role_routes = settings.get("role_routes", {})
            for route in settings.get("routes", []):
                roles = [
                    role for role, rid in role_routes.items() if rid == route["id"]
                ]
                table.add_row(
                    Group(
                        Text(route["id"], style=r.FG),
                        Text(
                            " · ".join(
                                v
                                for v in (route.get("model"), route.get("provider"))
                                if v
                            ),
                            style=r.DIM,
                        ),
                    ),
                    ", ".join(roles) or route.get("role") or "—",
                    route.get("environment") or "any machine",
                    route.get("billing_account") or route.get("account_kind") or "—",
                    Text(route.get("auth_reference") or "—", style=r.DIM),
                )
            detail.update(
                Group(
                    r.section("agents", "which model does each job", width=width),
                    Text(""),
                    table,
                )
            )
        elif name == "repos":
            out = Text()
            for repo in settings.get("repositories", []):
                out.append(f"{repo}\n", style=r.FG)
            detail.update(Group(r.section("repos", width=width), Text(""), out))
        elif name == "budgets":
            table = Table.grid(padding=(0, 4))
            table.add_column(style=r.DIM)
            table.add_column(style=r.FG)
            for key, value in settings.get("budgets", {}).items():
                label = key.removesuffix("_cents").replace("_", " ")
                table.add_row(
                    label, r.money(value) if key.endswith("_cents") else str(value)
                )
            detail.update(
                Group(
                    r.section("budgets", "limits for new work", width=width),
                    Text(""),
                    table,
                    Text(""),
                    Text(
                        "When a step reaches its limit it stops and the issue comes to you.",
                        style=r.DIM,
                    ),
                )
            )
        elif name == "schedule":
            out = Text()
            for window in settings.get("windows", []):
                out.append(
                    " · ".join(f"{k} {v}" for k, v in window.items() if v) + "\n",
                    style=r.FG,
                )
            detail.update(
                Group(
                    r.section("schedule", "when work may start", width=width),
                    Text(""),
                    out or Text("any time", style=r.FG),
                )
            )
        else:
            out = Text()
            for person in settings.get("people", []):
                out.append(f"{person}\n", style=r.FG)
            detail.update(
                Group(
                    r.section("people", "who can approve and accept", width=width),
                    Text(""),
                    out or Text("nobody named yet", style=r.AMBER),
                )
            )

    def _machine_matrix(self, width: int) -> RenderableType:
        settings = self._settings()
        machines = self._machines()
        envs = settings.get("environments", {})
        if not machines:
            return Group(
                r.section("machines", width=width),
                Text(""),
                Text("No machines registered in factory.toml.", style=r.DIM),
            )
        self.column = max(0, min(self.column, len(machines) - 1))
        table = Table.grid(padding=(0, 2))
        table.add_column(width=34, no_wrap=True)
        for _ in machines:
            table.add_column(width=24, no_wrap=True)
        heads: list[RenderableType] = [Text("\n\nagent · model", style=r.FAINT)]
        for index, machine in enumerate(machines):
            offline = machine["status"] == "offline"
            kinds = " · ".join(
                str(envs.get(e, {}).get("kind", e)) for e in machine["environments"]
            )
            state = (
                Text(f"○ offline {machine['age']}", style=r.FAINT)
                if offline
                else Text(
                    "● online · " + ("busy" if machine["busy"] else "free"),
                    style=r.MINT,
                )
            )
            name_style = (
                f"bold {r.FG}" if index == self.column else (r.DIM if offline else r.FG)
            )
            heads.append(
                Group(
                    Text(
                        ("› " if index == self.column else "") + machine["id"],
                        style=name_style,
                    ),
                    Text(kinds or "—", style=r.DIM),
                    state,
                )
            )
        table.add_row(*heads)
        table.add_row(*[Text("")] * (len(machines) + 1))
        for route in settings.get("routes", []):
            cells: list[RenderableType] = [
                Group(
                    Text(route["id"], style=r.FG),
                    Text(
                        " · ".join(
                            v for v in (route.get("model"), route.get("provider")) if v
                        ),
                        style=r.DIM,
                    ),
                )
            ]
            for machine in machines:
                allowed = (
                    not route.get("environment")
                    or route["environment"] in machine["environments"]
                )
                if not allowed:
                    cells.append(Text("— not allowed", style=r.FAINT))
                elif machine["status"] == "offline":
                    cells.append(Text("✓ machine offline", style=r.FAINT))
                else:
                    cells.append(Text("✓ ready", style=r.MINT))
            table.add_row(*cells)
            table.add_row(*[Text("")] * (len(machines) + 1))
        machine = machines[self.column]
        facts = Table.grid(padding=(0, 4))
        facts.add_column(style=r.DIM, width=18)
        facts.add_column(style=r.FG)
        facts.add_row("name", machine["id"])
        facts.add_row(
            "sandbox",
            " · ".join(
                f"{e} ({envs.get(e, {}).get('kind', '?')})"
                for e in machine["environments"]
            )
            or "—",
        )
        facts.add_row("lease", f"{machine['lease_seconds'] or '—'}s")
        facts.add_row("worker login", machine["token_env"] or "—")
        facts.add_row(
            "agents allowed",
            "  ".join(
                route["id"]
                for route in settings.get("routes", [])
                if not route.get("environment")
                or route["environment"] in machine["environments"]
            )
            or "none",
        )
        return Group(
            r.section("machines", "which agents can run where", width=width),
            Text(""),
            table,
            Text(""),
            r.section(machine["id"], width=width),
            Text(""),
            facts,
        )

    @on(OptionList.OptionHighlighted, "#sections")
    def section_changed(self) -> None:
        self._render_section()

    @on(OptionList.OptionSelected, "#sections")
    def section_selected(self) -> None:
        self.action_edit()

    def action_column(self, step: int) -> None:
        self.column += step
        self._render_section()

    def action_edit(self) -> None:
        from dark_factory.ui.transport import LocalTransport

        path = self.factory.snapshot_data.get("project", {}).get("config")
        if not isinstance(self.factory.transport, LocalTransport) or not path:
            self.notify(
                "Edit factory.toml on the controller host, then press ⌃r here.",
                severity="warning",
            )
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        with self.app.suspend():
            subprocess.call([*editor.split(), path])
        self.call_after_refresh(self.action_reload)

    async def action_reload(self) -> None:
        await self.factory.confirm_command(
            "reload",
            "Check factory.toml and use it for new work? An invalid file is "
            "rejected and the current settings stay.",
        )


class NewWorkScreen(FactoryScreen):
    KEYS = [
        ("j/k", "move"),
        ("↹", "sources ⇄ issues"),
        ("/", "filter or add owner/repo#n"),
        ("r", "refresh from GitHub"),
        ("esc", "back"),
    ]
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        Binding("space", "toggle_issue", "Select", show=False),
        Binding("y", "start", "Start planning", show=False),
        Binding("slash", "filter", show=False),
        Binding("g", "prefix", show=False),
        Binding("h", "fold(True)", show=False),
        Binding("l", "fold(False)", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.candidates: dict[str, dict[str, Any]] = {}
        self.selected: list[str] = []
        self.folded: set[str] = set()
        self.repo = "all"
        self.query_text = ""
        self.prefix = False
        self.loaded = False

    def compose(self) -> ComposeResult:
        yield Line(crumb("new work"), id="crumb")
        with Horizontal(id="new-work-body"):
            with Vertical(id="repo-nav", classes="panel"):
                yield Line(Text("sources", style=f"bold {r.DIM}"), fill="─")
                yield OptionList(id="repos")
            with Vertical(id="issues-panel", classes="panel"):
                yield Line(id="issues-head", fill="─")
                yield Input(
                    placeholder="filter, or owner/repo#123 to add", id="issue-input"
                )
                yield OptionList(id="candidate-table")
                yield Static(id="new-work-summary")
        yield self.keybar()

    async def on_mount(self) -> None:
        self.query_one("#issue-input", Input).display = False
        self.query_one("#candidate-table", OptionList).focus()
        self.render_snapshot()
        await self.action_refresh()

    async def action_refresh(self) -> None:
        import asyncio

        try:
            result = await asyncio.to_thread(
                self.factory.transport.command, "discover", {}, self.factory.actor
            )
        except (ControllerError, OSError, ValueError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        items = result.get("items", []) if isinstance(result, dict) else []
        self.candidates = {f"{i['repo']}#{i['number']}": i for i in items}
        self.loaded = True
        self.render_snapshot()

    def _known(self) -> dict[str, dict[str, Any]]:
        """Issues the factory already has, by owner/repo#number."""

        known = {}
        for item in self.factory.snapshot_data.get("items", []):
            known[f"{item['repo']}#{item['number']}"] = item
        return known

    def _status(self, key: str, issue: dict[str, Any]) -> tuple[Text, bool]:
        """Right-hand status text and whether the row can be selected."""

        known = self._known().get(key)
        if known:
            if known["lane"] == "done":
                return Text("already accepted", style=r.DIM), False
            return Text.assemble(
                ("already in the factory · ", r.BLUE), r.where(known)
            ), False
        if not issue.get("is_root", True):
            parent = str(issue.get("parent_id") or "").rsplit(":", 1)[-1]
            return Text(f"part of #{parent}", style=r.DIM), False
        children = issue.get("required_child_count") or 0
        if children:
            return (
                Text(f"has {children} sub-issues · can't run these yet", style=r.AMBER),
                False,
            )
        board_status = (issue.get("project") or {}).get("status")
        if board_status:
            return Text.assemble(("ready", r.MINT), (f" · {board_status}", r.DIM)), True
        return Text("ready", style=r.MINT), True

    def _in_scope(self, issue: dict[str, Any], scope: str) -> bool:
        """Whether an issue belongs to a sidebar entry: all, a project, a theme or a repo."""

        project = issue.get("project") or {}
        if scope == "all":
            return True
        if scope.startswith("project:"):
            return project.get("id") == scope.removeprefix("project:")
        if scope.startswith("theme:"):
            board, _, theme = scope.removeprefix("theme:").partition("|")
            return project.get("id") == board and (project.get("theme") or "") == theme
        return issue["repo"] == scope

    def _scope_label(self) -> str:
        if self.repo == "all":
            return "all issues"
        if self.repo.startswith(("project:", "theme:")):
            board, _, theme = (
                self.repo.removeprefix("project:").removeprefix("theme:").partition("|")
            )
            title = next(
                (
                    i["project"]["title"]
                    for i in self.candidates.values()
                    if (i.get("project") or {}).get("id") == board
                ),
                board,
            )
            return (
                f"{title} › {theme or 'no theme'}"
                if self.repo.startswith("theme:")
                else title
            )
        return self.repo

    def _nav_option(
        self, label: Text, scope: str, width: int, indent: int = 0
    ) -> Option:
        count = sum(self._in_scope(i, scope) for i in self.candidates.values())
        return Option(
            r.spread(
                Text.assemble(" " * indent, label),
                Text(str(count), style=r.DIM),
                width,
            ),
            id=scope,
        )

    def render_snapshot(self) -> None:
        repos_widget = self.query_one("#repos", OptionList)
        nav_width = self.width_of(repos_widget)
        boards: dict[str, dict[str, Any]] = {}
        for issue in self.candidates.values():
            project = issue.get("project")
            if project:
                board = boards.setdefault(
                    project["id"], {"title": project["title"], "themes": set()}
                )
                board["themes"].add(project.get("theme") or "")
        nav = [self._nav_option(Text("all issues", style=r.FG), "all", nav_width)]
        for board_id, board in sorted(boards.items(), key=lambda b: b[1]["title"]):
            nav.append(
                self._nav_option(
                    Text(board["title"], style=f"bold {r.FG}"),
                    f"project:{board_id}",
                    nav_width,
                )
            )
            for theme in sorted(board["themes"], key=lambda t: (not t, t)):
                nav.append(
                    self._nav_option(
                        Text(theme or "no theme", style=r.FG if theme else r.DIM),
                        f"theme:{board_id}|{theme}",
                        nav_width,
                        indent=2,
                    )
                )
        if boards:
            nav.append(
                Option(
                    Text("repos", style=f"bold {r.DIM}"), id="repos-head", disabled=True
                )
            )
        nav += [
            self._nav_option(
                Text(repo, style=r.FG), repo, nav_width, 2 if boards else 0
            )
            for repo in sorted({i["repo"] for i in self.candidates.values()})
        ]
        set_options(repos_widget, nav, prefer=self.repo)
        table = self.query_one("#candidate-table", OptionList)
        width = self.width_of(table)
        on_board = self.repo.startswith(("project:", "theme:"))
        self.query_one("#issues-head", Line).set(
            Text.assemble(
                (self._scope_label(), f"bold {r.FG}"),
                (
                    "  open issues on this project"
                    if on_board
                    else "  open issues for the factory",
                    r.DIM,
                ),
            ),
            Text(
                f"/ {self.query_text}" if self.query_text else "/ filter", style=r.DIM
            ),
        )
        visible = {
            key: issue
            for key, issue in self.candidates.items()
            if self._in_scope(issue, self.repo)
            and (
                not self.query_text
                or self.query_text.lower() in f"{key} {issue.get('title', '')}".lower()
            )
        }
        if self.repo.startswith("project:"):
            # Group a board's issues under their themes, as the board does.
            visible = dict(
                sorted(
                    visible.items(),
                    key=lambda kv: (
                        not (kv[1].get("project") or {}).get("theme"),
                        (kv[1].get("project") or {}).get("theme") or "",
                    ),
                )
            )
        children: dict[str, list[str]] = {}
        for key, issue in visible.items():
            parent = issue.get("parent_id")
            if parent:
                repo, _, number = (
                    str(parent).removeprefix("github:").rpartition(":issue:")
                )
                parent_key = f"{repo}#{number}"
                if parent_key in visible:
                    children.setdefault(parent_key, []).append(key)
        nested = {k for kids in children.values() for k in kids}
        options = []
        for key in [k for k in self.selected if k not in self.candidates]:
            options.append(
                Option(
                    self._row(
                        key,
                        {
                            "repo": key.split("#")[0],
                            "number": key.split("#")[1],
                            "title": "added by hand",
                        },
                        width,
                        depth=0,
                    ),
                    id=key,
                )
            )
        theme_heading = None
        for key, issue in visible.items():
            if key in nested:
                continue
            if self.repo.startswith("project:"):
                theme = (issue.get("project") or {}).get("theme") or "no theme"
                if theme != theme_heading:
                    theme_heading = theme
                    options.append(
                        Option(
                            r.section(theme, width=width),
                            id=f"heading:{theme}",
                            disabled=True,
                        )
                    )
            options.append(
                Option(self._row(key, issue, width, 0, key in children), id=key)
            )
            if key in children and key not in self.folded:
                kids = children[key]
                for index, kid in enumerate(kids):
                    options.append(
                        Option(
                            self._row(
                                kid, visible[kid], width, 1, last=index == len(kids) - 1
                            ),
                            id=kid,
                        )
                    )
        if not options:
            message = (
                "Loading issues from GitHub…"
                if not self.loaded
                else "No open issues are labeled or on a project board. Press / to add one by number."
            )
            options.append(
                Option(Text(message, style=r.DIM), id="empty", disabled=True)
            )
        set_options(table, options)
        self._render_summary()

    def _row(
        self,
        key: str,
        issue: dict[str, Any],
        width: int,
        depth: int,
        has_children: bool = False,
        last: bool = False,
    ) -> Text:
        status, selectable = self._status(key, issue)
        if key in self.selected:
            box = Text("[x]", style=f"bold {r.MINT}")
        elif not selectable:
            box = Text(" – ", style=r.FAINT)
        else:
            box = Text("[ ]", style=r.FG)
        fold = (
            ("▸ " if key in self.folded else "▾ ")
            if has_children
            else ("└ " if last else "├ ")
            if depth
            else "  "
        )
        number = Text(f"#{issue['number']}", style=r.DIM)
        title_style = r.DIM if depth or not selectable else r.FG
        repo_w = 18 if self.repo == "all" and width >= 110 else 0
        repo = Text(issue["repo"].split("/")[-1], style=r.DIM)
        status_w = min(46, max(14, width // 3))
        title_w = max(8, width - 4 - 2 - 7 - repo_w - status_w - 2)
        return Text.assemble(
            r.cell(box, 4),
            (fold, r.FAINT),
            r.cell(number, 7),
            r.cell(Text(issue.get("title", ""), style=title_style), title_w),
            r.cell(repo, repo_w),
            "  ",
            r.cell(status, status_w, "right"),
        )

    def _render_summary(self) -> None:
        count = len(self.selected)
        names = " · ".join(
            f"#{key.split('#')[1]} {self.candidates.get(key, {}).get('title', '')}".strip()
            for key in self.selected
        )
        head = (
            Text.assemble((f"{count} selected  ", f"bold {r.MINT}"), (names, r.FG))
            if count
            else Text("nothing selected yet", style=r.DIM)
        )
        self.query_one("#new-work-summary", Static).update(
            Group(
                head,
                Text(
                    "Each one gets its own plan and critique. Nothing is built until "
                    "you approve each plan in needs you.",
                    style=r.DIM,
                ),
                r.keys(
                    [
                        ("y", f"start planning {count}" if count else "start planning"),
                        ("space ↵", "select"),
                        ("h/l", "fold / unfold"),
                        ("gi", "open on GitHub"),
                    ]
                ),
            )
        )

    def _highlighted_key(self) -> str | None:
        option = self.query_one("#candidate-table", OptionList).highlighted_option
        return option.id if option and option.id != "empty" else None

    def action_toggle_issue(self) -> None:
        if self.focused is not self.query_one("#candidate-table", OptionList):
            return
        key = self._highlighted_key()
        if not key:
            return
        if key in self.selected:
            self.selected.remove(key)
        else:
            issue = self.candidates.get(key)
            if issue is None:
                return
            status, selectable = self._status(key, issue)
            if not selectable:
                self.notify(status.plain, severity="warning")
                return
            self.selected.append(key)
        self.render_snapshot()

    @on(OptionList.OptionSelected, "#candidate-table")
    def chosen(self) -> None:
        self.action_toggle_issue()

    @on(OptionList.OptionHighlighted, "#repos")
    def repo_changed(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_id and event.option_id != self.repo:
            self.repo = event.option_id
            self.render_snapshot()

    def action_fold(self, fold: bool) -> None:
        if self.prefix:
            self.prefix = False
        key = self._highlighted_key()
        if not key:
            return
        (self.folded.add if fold else self.folded.discard)(key)
        self.render_snapshot()

    def action_prefix(self) -> None:
        self.prefix = True

    def action_new_work(self) -> None:
        if self.prefix:
            self.prefix = False
            key = self._highlighted_key()
            url = self.candidates.get(key or "", {}).get("url")
            if url:
                self.app.open_url(url)
            return

    def action_filter(self) -> None:
        field = self.query_one("#issue-input", Input)
        field.display = True
        field.value = self.query_text
        field.focus()

    @on(Input.Changed, "#issue-input")
    def filter_changed(self, event: Input.Changed) -> None:
        if not ISSUE_REF.match(event.value.strip()):
            self.query_text = event.value.strip()
            self.render_snapshot()

    @on(Input.Submitted, "#issue-input")
    def filter_done(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if ISSUE_REF.match(value):
            if value not in self.selected:
                self.selected.append(value)
            self.query_text = ""
        field = self.query_one("#issue-input", Input)
        field.display = bool(self.query_text)
        self.query_one("#candidate-table", OptionList).focus()
        self.render_snapshot()

    def action_back(self) -> None:
        field = self.query_one("#issue-input", Input)
        if field.has_focus:
            field.value = ""
            self.query_text = ""
            field.display = False
            self.query_one("#candidate-table", OptionList).focus()
            self.render_snapshot()
            return
        super().action_back()

    async def action_start(self) -> None:
        if not self.selected:
            self.notify("Select at least one issue with space", severity="warning")
            return
        issues = []
        for key in self.selected:
            match = ISSUE_REF.match(key)
            if match:
                issues.append({"repo": match.group(1), "number": int(match.group(2))})
        await self.factory.start_planning(issues)


class DoneScreen(FactoryScreen):
    KEYS = [("j/k", "move"), ("↵", "open"), ("esc", "back")]

    def compose(self) -> ComposeResult:
        yield Line(crumb("done"), id="crumb")
        with Vertical(classes="panel"):
            yield OptionList(id="done-table")
        yield self.keybar()

    def on_mount(self) -> None:
        self.render_snapshot()
        self.query_one("#done-table", OptionList).focus()

    def render_snapshot(self) -> None:
        table = self.query_one("#done-table", OptionList)
        width = self.width_of(table)
        options = []
        for item in reversed(self.factory.snapshot_data.get("items", [])):
            if item["lane"] != "done":
                continue
            left = Text.assemble(
                ("✓  ", r.MINT), (f"{item['title']} #{item['number']}", r.FG)
            )
            right = Text(
                f"{r.money(item['spent_cents'])}   {(item['accepted_at'] or '')[:10]}",
                style=r.DIM,
            )
            options.append(Option(r.spread(left, right, width), id=item["id"]))
        set_options(
            table,
            options
            or [
                Option(
                    Text("Nothing accepted yet.", style=r.DIM), id="none", disabled=True
                )
            ],
        )

    @on(OptionList.OptionSelected, "#done-table")
    def open_item(self, event: OptionList.OptionSelected) -> None:
        if event.option_id and event.option_id != "none":
            self.factory.show_issue(event.option_id)


class WorkersScreen(FactoryScreen):
    KEYS = [("j/k", "move"), ("↵", "open"), ("esc", "back")]

    def compose(self) -> ComposeResult:
        yield Line(crumb("workers"), id="crumb")
        with Vertical(classes="panel"):
            yield OptionList(id="all-workers")
        yield self.keybar()

    def on_mount(self) -> None:
        self.render_snapshot()
        self.query_one("#all-workers", OptionList).focus()

    def render_snapshot(self) -> None:
        table = self.query_one("#all-workers", OptionList)
        snapshot = self.factory.snapshot_data
        by_id = {item["id"]: item for item in snapshot.get("items", [])}
        width = self.width_of(table)
        set_options(
            table,
            [
                Option(r.worker_line(worker, by_id, width), id=worker["id"])
                for worker in snapshot.get("workers", [])
            ]
            or [
                Option(
                    Text("No workers registered.", style=r.DIM),
                    id="none",
                    disabled=True,
                )
            ],
        )

    @on(OptionList.OptionSelected, "#all-workers")
    def open_worker(self, event: OptionList.OptionSelected) -> None:
        if event.option_id and event.option_id != "none":
            self.factory.show_worker(event.option_id)


class WorkerScreen(FactoryScreen):
    KEYS = [("↵", "open assignment"), ("f", "preflight"), ("esc", "back")]
    BINDINGS = [
        *FactoryScreen.BINDINGS,
        Binding("f", "preflight", "Preflight", show=False),
        Binding("enter", "assignment", "Assignment", show=False),
    ]

    def __init__(self, worker_id: str) -> None:
        super().__init__()
        self.worker_id = worker_id
        self.mode = "overview"

    def compose(self) -> ComposeResult:
        yield Line(id="crumb")
        with Horizontal(id="worker-body"):
            with Vertical(id="host-panel", classes="panel"):
                yield VerticalScroll(Static(id="host-detail"))
            with Vertical(id="assignment-panel", classes="panel"):
                yield VerticalScroll(Static(id="worker-detail"))
        yield self.keybar()

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
                "status": "offline",
                "age": "—",
                "environments": [],
                "capabilities": [],
                "active": None,
                "recent": [],
            },
        )

    def render_snapshot(self) -> None:
        worker = self._worker()
        offline = worker["status"] == "offline"
        state = (
            Text("○ not connected", style=r.DIM)
            if offline
            else Text("● connected", style=f"bold {r.MINT}")
        )
        crumb_text = Text(worker["id"] + "   ", style=r.DIM)
        crumb_text.append_text(state)
        if self.mode == "preflight":
            crumb_text = Text.assemble(
                (worker["id"], r.DIM), (" › ", r.DIM), ("preflight", r.FG)
            )
        self.query_one("#crumb", Line).set(crumb_text)
        envs = self.factory.snapshot_data.get("settings", {}).get("environments", {})
        width = self.width_of(self.query_one("#host-detail"))
        host = Text()
        host.append_text(r.section("environments", width=width))
        host.append("\n\n")
        for env in worker.get("environments") or []:
            host.append("✓  ", style=r.MINT)
            host.append(env.ljust(16), style=r.FG)
            host.append(str(envs.get(env, {}).get("kind", "")) + "\n", style=r.DIM)
        if not worker.get("environments"):
            host.append("none configured\n", style=r.DIM)
        host.append("\n")
        host.append_text(r.section("harnesses", width=width))
        host.append("\n\n")
        for capability in worker.get("capabilities") or []:
            host.append("✓  ", style=r.MINT)
            host.append(f"{capability}\n", style=r.FG)
        if not worker.get("capabilities"):
            host.append("not reported yet\n", style=r.DIM)
        self.query_one("#host-detail", Static).update(host)
        self.query_one("#worker-detail", Static).update(
            self._preflight(worker)
            if self.mode == "preflight"
            else self._assignment(worker)
        )

    def _assignment(self, worker: dict[str, Any]) -> RenderableType:
        active = worker.get("active")
        items = {i["id"]: i for i in self.factory.snapshot_data.get("items", [])}
        width = self.width_of(self.query_one("#worker-detail"))
        parts: list[RenderableType] = []
        if active:
            item = items.get(active["work_item_id"], {})
            parts.append(
                Text.assemble(
                    (
                        f"{item.get('title', active['work_item_id'])} #{item.get('number', '')}",
                        f"bold {r.FG}",
                    ),
                    (f"   {active['kind']} step", r.DIM),
                )
            )
            parts.append(Text(""))
            running = active["status"] in {"running", "cancelling"}
            started = r.parse_time(active.get("started_at"))
            deadline = r.parse_time(active.get("deadline"))
            now = datetime.now(UTC)
            detail = " · ".join(
                p
                for p in (
                    active.get("route_id") or "",
                    r.duration((now - started).total_seconds()) if started else "",
                    f"{r.duration((deadline - now).total_seconds())} left"
                    if deadline and deadline > now
                    else "",
                )
                if p
            )
            stages = [
                ("received", "lease from controller", "done"),
                (
                    "agent running" if active["status"] != "cancelling" else "stopping",
                    detail,
                    "now" if running else "todo",
                ),
                (
                    "archive + verify",
                    "copy, hash and fsync before anything is removed",
                    "todo",
                ),
                (
                    "result delivered",
                    "kept locally until the controller acknowledges",
                    "todo",
                ),
                ("cleaned up", "workspace removed · credentials scrubbed", "todo"),
            ]
            table = Table.grid(padding=(0, 2))
            table.add_column(width=2)
            table.add_column(width=20, no_wrap=True)
            table.add_column(ratio=1)
            for label, note, state in stages:
                mark = {
                    "done": Text("✓", style=r.MINT),
                    "now": Text("●", style=r.MINT),
                }.get(state, Text("○", style=r.FG))
                table.add_row(
                    mark,
                    Text(
                        label,
                        style=f"bold {r.FG}"
                        if state == "now"
                        else r.DIM
                        if state == "done"
                        else r.FG,
                    ),
                    Text(note, style=r.DIM),
                )
            parts.append(table)
        else:
            parts.append(Text("idle · waiting for work", style=r.DIM))
        parts += [Text(""), r.section("recent on this host", width=width), Text("")]
        recent = worker.get("recent") or []
        for entry in recent:
            ok = entry["status"] == "succeeded"
            number = str(entry.get("external_id", "")).rsplit(":", 1)[-1]
            parts.append(
                r.spread(
                    Text.assemble(
                        ("✓  " if ok else "×  ", r.MINT if ok else r.HAZARD),
                        r.cell(Text(entry["kind"], style=r.FG), 12),
                        (f"{entry['title']} #{number}", r.DIM),
                        (f" · {entry['error']}" if entry.get("error") else "", r.DIM),
                    ),
                    Text(r.clock(entry.get("ended_at")), style=r.DIM),
                    width,
                )
            )
        if not recent:
            parts.append(Text("nothing yet", style=r.DIM))
        return Group(*parts)

    def _preflight(self, worker: dict[str, Any]) -> RenderableType:
        offline = worker["status"] == "offline"
        rows = [
            (
                not offline,
                "controller",
                f"heartbeat {worker.get('age', '—')} ago"
                if not offline
                else "no heartbeat",
            ),
            (
                bool(worker.get("environments")),
                "sandbox",
                " · ".join(worker.get("environments") or []) or "none configured",
            ),
            (
                bool(worker.get("capabilities")),
                "harnesses",
                " · ".join(worker.get("capabilities") or []) or "not reported",
            ),
            (True, "authority", "runs assigned steps only · cannot approve or accept"),
        ]
        table = Table.grid(padding=(0, 2))
        table.add_column(width=2)
        table.add_column(width=14, style=r.FG)
        table.add_column(style=r.DIM)
        gaps = 0
        for ok, label, note in rows:
            gaps += not ok
            table.add_row(
                Text("✓", style=r.MINT) if ok else Text("!", style=r.AMBER), label, note
            )
        head = Text(
            "ready" if not gaps else f"{gaps} gap" + ("s" if gaps > 1 else ""),
            style=r.MINT if not gaps else r.AMBER,
        )
        return Group(head, Text(""), table)

    def action_preflight(self) -> None:
        self.mode = "overview" if self.mode == "preflight" else "preflight"
        self.render_snapshot()

    def action_assignment(self) -> None:
        active = self._worker().get("active")
        if active:
            self.factory.show_issue(active["work_item_id"])
        else:
            self.notify("This worker has no assignment")
