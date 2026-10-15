"""Pure Rich rendering helpers for the Paper "Dark Factory TUI" screens.

Everything here maps projection facts to plain-language text, glyphs and
fixed-width rows. Nothing reads controller state or changes it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

BG = "#161616"
CHROME = "#1f1f1f"
CURSOR = "#262626"
LINE = "#333333"
FG = "#e6e6e6"
DIM = "#7a7a7a"
FAINT = "#4a4a4a"
MINT = "#58c4a0"
AMBER = "#f0b84a"
BLUE = "#8dbce8"
HAZARD = "#ff4d3d"

STAGES = ("plan", "critique", "approve", "build", "check", "review", "accept")
SHORT = {
    5: ("plan", "crit", "appr", "build", "check", "revw", "accpt"),
    3: ("pln", "crt", "apr", "bld", "chk", "rev", "acc"),
}
KIND_STAGE = {
    "plan": 0,
    "critique": 1,
    "implement": 3,
    "command": 4,
    "check": 4,
    "review": 5,
}
VERB = {
    "plan": "planning",
    "critique": "critiquing",
    "implement": "building",
    "command": "checking",
    "check": "checking",
    "review": "reviewing",
}
REACHED = {
    "awaiting_approval": 2,
    "approved": 3,
    "running": 3,
    "awaiting_review": 6,
    "accepted": 7,
}
ACTIVE = frozenset({"leased", "running", "cancelling"})
FAILED = frozenset({"failed", "timed_out", "cancelled"})
INTERRUPTED = "interrupted"
RESERVE = frozenset({"standby", "not_needed", "superseded_by_repair"})


def money(cents: int | None) -> str:
    return f"${(cents or 0) / 100:,.2f}"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        then = datetime.fromisoformat(value)
    except ValueError:
        return None
    return then if then.tzinfo else then.replace(tzinfo=UTC)


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def clock(value: str | None, seconds: bool = False) -> str:
    then = parse_time(value)
    if not then:
        return ""
    return then.astimezone().strftime("%H:%M:%S" if seconds else "%H:%M")


def cell(text: Text | str, width: int, justify: str = "left") -> Text:
    """Truncate or pad one field to an exact width so rows align."""

    value = Text(text) if isinstance(text, str) else text.copy()
    if width <= 0:
        return Text()
    value.truncate(width, overflow="ellipsis")
    if justify == "right":
        value.pad_left(width - value.cell_len)
    else:
        value.pad_right(width - value.cell_len)
    return value


def spread(left: Text, right: Text, width: int, fill: str = " ") -> Text:
    """Left and right text with a fill between, exactly ``width`` cells."""

    gap = width - left.cell_len - right.cell_len
    least = 2 if right.cell_len else 0
    if gap < least:
        left = cell(left, max(0, width - right.cell_len - least))
        gap = max(0, width - left.cell_len - right.cell_len)
    if fill == " ":
        return Text.assemble(left, " " * gap, right)
    middle = Text(" " + fill * max(0, gap - 2) + " ", style=LINE) if gap >= 2 else ""
    return Text.assemble(left, middle, right)


def section(label: str, note: str = "", right: str = "", width: int = 40) -> Text:
    """A panel heading: ``label  note ───────── right``."""

    head = Text()
    head.append(label, style=f"bold {DIM}")
    if note:
        head.append("  " + note, style=DIM)
    return spread(head, Text(right, style=DIM), width, "─")


def keys(pairs: list[tuple[str, str]]) -> Text:
    out = Text()
    for index, (key, label) in enumerate(pairs):
        if index:
            out.append("   ")
        out.append(key, style=f"bold {FG}")
        out.append(" " + label, style=DIM)
    return out


def visible_steps(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Planned steps, excluding repair work held in reserve."""

    return [
        s for s in item.get("steps", []) if not (s["repair"] and s["status"] in RESERVE)
    ]


def current_step(item: dict[str, Any]) -> tuple[dict[str, Any] | None, int, int]:
    """The running (or next ready) step and its 1-based position."""

    steps = visible_steps(item)
    for wanted in (ACTIVE, {"ready"}):
        for index, step in enumerate(steps):
            if step["status"] in wanted:
                return step, index + 1, len(steps)
    return None, 0, len(steps)


def stage_states(item: dict[str, Any]) -> list[str]:
    """done / current / you / failed / todo for each of the seven stages."""

    status = item["status"]
    reached = REACHED.get(status, 0)
    states = ["done" if index < reached else "todo" for index in range(7)]
    by_stage: dict[int, list[dict[str, Any]]] = {}
    for step in visible_steps(item):
        by_stage.setdefault(KIND_STAGE.get(step["kind"], 4), []).append(step)
    for index, steps in by_stage.items():
        statuses = {s["status"] for s in steps}
        if statuses == {"succeeded"}:
            states[index] = "done"
        elif statuses & ACTIVE:
            states[index] = "current"
        elif INTERRUPTED in statuses:
            states[index] = "you" if status == "interrupted" else "current"
        elif statuses & FAILED and status in {"failed", "running"}:
            states[index] = "failed"
    if status == "awaiting_approval":
        states[2] = "you"
    elif status == "awaiting_review":
        states[6] = "you"
    elif status in {"failed", "rejected"} and "failed" not in states:
        stage = {"plan": 0, "build": 3, "check": 4, "review": 5}.get(
            str(item.get("stage", "")).lower(), 0
        )
        states[stage] = "failed"
    furthest = max((i for i, s in enumerate(states) if s != "todo"), default=-1)
    if furthest > 2 and states[2] == "todo":
        states[2] = "done"
    if item["lane"] == "needs_you" and status == "running":
        index = next((i for i, s in enumerate(states) if s != "done"), 6)
        states[index] = "you"
    return states


def bar(states: list[str], seg: int) -> Text:
    out = Text()
    for index, state in enumerate(states):
        if index:
            out.append(" ")
        if state == "current":
            half = max(1, seg // 2)
            out.append("━" * half, style=BLUE)
            out.append("━" * (seg - half), style=FAINT)
            continue
        style = {
            "done": MINT,
            "you": AMBER,
            "failed": HAZARD,
        }.get(state, FAINT)
        out.append("━" * seg, style=style)
    return out


def stage_header(seg: int, waiting: set[int]) -> Text:
    labels = STAGES if seg >= 8 else SHORT[5 if seg >= 5 else 3]
    out = Text()
    for index, label in enumerate(labels):
        if index:
            out.append(" ")
        out.append_text(
            cell(Text(label, style=AMBER if index in waiting else DIM), seg)
        )
    return out


def where(item: dict[str, Any]) -> Text:
    """Plain-language state, coloured by who has to act."""

    status = item["status"]
    action = item.get("next_action", {})
    kind = action.get("kind", "")
    reason = str(action.get("reason", ""))
    if status == "selected":
        return Text("ready to plan", style=AMBER)
    if kind == "human_approval":
        return Text("needs your approval", style=AMBER)
    if kind == "human_review":
        return Text("ready for you to accept", style=AMBER)
    if status == "awaiting_review":
        return Text("evidence missing · your call", style=HAZARD)
    if status == "accepted":
        return Text("accepted", style=MINT)
    if status == "cancelled":
        return Text("cancelled", style=DIM)
    if status == "rejected":
        return Text("sent back · replan", style=AMBER)
    if status == "failed":
        return Text("stuck · your call", style=HAZARD)
    if kind == "human_resume":
        return Text("interrupted · resume?", style=AMBER)
    if kind == "blocked":
        if "allowance" in reason:
            return Text("out of budget · paused", style=AMBER)
        if "deadline" in reason:
            return Text("out of time · your call", style=AMBER)
        if "pause" in reason or "drain" in reason:
            return Text("waiting · dispatch paused", style=DIM)
        return Text("stuck · your call", style=HAZARD)
    step, position, total = current_step(item)
    if step and step["status"] in ACTIVE:
        verb = "fixing" if step["repair"] else VERB.get(step["kind"], step["kind"])
        return Text(f"{verb} · step {position} of {total}", style=BLUE)
    if status == "planning":
        return Text("planning", style=BLUE)
    if step:
        return Text(f"waiting for a worker · step {position} of {total}", style=DIM)
    return Text(status.replace("_", " "), style=DIM)


def glyph(item: dict[str, Any]) -> Text:
    tone = where(item).style
    if tone == HAZARD:
        return Text("×", style=HAZARD)
    if item["lane"] == "needs_you":
        return Text("!", style=f"bold {AMBER}")
    if item["status"] == "accepted":
        return Text("✓", style=MINT)
    if tone == BLUE:
        return Text("●", style=MINT)
    return Text("○", style=DIM)


def step_glyph(step: dict[str, Any]) -> Text:
    status = step["status"]
    if step["repair"] and status in RESERVE:
        return Text("◇", style=FAINT)
    if status == "succeeded":
        return Text("✓", style=MINT)
    if status in ACTIVE:
        return Text("●", style=MINT)
    if status in FAILED:
        return Text("×", style=HAZARD)
    if status == INTERRUPTED:
        return Text("‖", style=AMBER)
    if status in RESERVE:
        return Text("–", style=FAINT)
    return Text("○", style=FG)


def running_line(item: dict[str, Any], routes: dict[str, Any]) -> str:
    """One quiet sentence under a running item."""

    step, _, _ = current_step(item)
    if not step:
        return ""
    route = routes.get(step.get("route_id") or "", {})
    who = route.get("id") or step.get("role") or step["kind"]
    parts = [f"{who} on {step['worker_id']}" if step.get("worker_id") else who]
    started = parse_time(step.get("started_at"))
    if step["status"] in ACTIVE and started:
        parts.append(f"{duration((datetime.now(UTC) - started).total_seconds())} in")
        deadline = parse_time(step.get("deadline"))
        if deadline:
            left = (deadline - datetime.now(UTC)).total_seconds()
            parts.append(f"{duration(left)} left" if left > 0 else "over time")
    elif step["status"] == "ready":
        parts = ["waiting for a free worker"]
    return " · ".join(parts)


def columns(width: int) -> dict[str, int]:
    """Overview column widths for one terminal width."""

    seg = 8 if width >= 150 else 5 if width >= 112 else 3
    money_w = 17 if width >= 100 else 0
    where_w = 26 if width >= 100 else 22
    fixed = 3 + (7 * seg + 6) + 2 + where_w + money_w + 5
    return {
        "seg": seg,
        "title": max(12, width - fixed),
        "where": where_w,
        "money": money_w,
        "age": 5,
    }


def item_line(item: dict[str, Any], cols: dict[str, int]) -> Text:
    title = Text(f"{item['title']} #{item['number']}", style=FG)
    line = Text.assemble(
        cell(glyph(item), 3),
        cell(title, cols["title"]),
        bar(stage_states(item), cols["seg"])
        if item["progress_visible"]
        else Text(" " * (7 * cols["seg"] + 6)),
        "  ",
        cell(where(item), cols["where"]),
    )
    if cols["money"]:
        line.append_text(
            cell(
                f"{money(item['spent_cents'])} / {money(item['cap_cents'])}",
                cols["money"],
                "right",
            )
        )
    line.append_text(cell(Text(item["age"], style=DIM), cols["age"], "right"))
    line.stylize(DIM, 3 + cols["title"] + 7 * cols["seg"] + 8 + cols["where"])
    return line


def header_line(items: list[dict[str, Any]], cols: dict[str, int]) -> Text:
    waiting = set()
    for item in items:
        for index, state in enumerate(stage_states(item)):
            if state == "you":
                waiting.add(index)
    line = Text.assemble(
        cell(Text("   issue", style=DIM), 3 + cols["title"]),
        stage_header(cols["seg"], waiting),
        "  ",
        cell(Text("where it is", style=DIM), cols["where"]),
    )
    if cols["money"]:
        line.append_text(cell(Text("spent / cap", style=DIM), cols["money"], "right"))
    line.append_text(cell(Text("age", style=DIM), cols["age"], "right"))
    return line


def bullets(lines: list[str], dot: str = AMBER) -> RenderableType:
    grid = Table.grid(padding=0)
    grid.add_column(width=6, no_wrap=True)
    grid.add_column(ratio=1)
    for line in lines:
        grid.add_row(Text("   •", style=dot), Text(line, style=FG))
    return grid


def item_prompt(
    item: dict[str, Any],
    cols: dict[str, int],
    routes: dict[str, Any],
    highlighted: bool,
) -> RenderableType:
    parts: list[RenderableType] = [item_line(item, cols)]
    if item["lane"] == "needs_you":
        if item.get("summary"):
            parts.append(bullets(item["summary"]))
        if highlighted:
            hints = decision_keys(item)
            if hints:
                parts.append(Text.assemble("   ", keys(hints)))
    else:
        quiet = running_line(item, routes)
        if quiet:
            parts.append(Text("   " + quiet, style=DIM))
    parts.append(Text(""))
    return Group(*parts)


def decision_keys(item: dict[str, Any]) -> list[tuple[str, str]]:
    status = item["status"]
    if status == "awaiting_approval":
        return [("y", "approve"), ("n", "reject (asks why)"), ("↵", "open proposal")]
    if status == "awaiting_review" and item["next_action"].get("kind") == (
        "human_review"
    ):
        return [("y", "accept"), ("n", "reject (asks why)"), ("↵", "open result")]
    if status == "selected":
        return [("y", "start planning"), ("↵", "open")]
    if status == "interrupted":
        return [("y", "resume"), ("n", "stop (asks why)"), ("↵", "open")]
    return [("↵", "open")]


def worker_line(
    worker: dict[str, Any], items: dict[str, dict[str, Any]], width: int
) -> Text:
    offline = worker["status"] == "offline"
    dot = Text("○ " if offline else "● ", style=FAINT if offline else MINT)
    name = Text(worker["id"], style=DIM if offline else FG)
    where_text = " · ".join(worker.get("environments") or []) or "—"
    active = worker.get("active")
    if active:
        item = items.get(active["work_item_id"], {})
        title = f"{item.get('title', active['work_item_id'])} #{item.get('number', '')}"
        doing = Text(f"{VERB.get(active['kind'], active['kind'])} {title}", style=FG)
    else:
        doing = Text("idle", style=DIM)
    if offline:
        right = Text(
            f"offline {worker['age']}"
            if worker.get("last_heartbeat")
            else "never seen",
            style=DIM,
        )
    else:
        right = Text(
            f"beat {worker['age']} · lease {worker.get('lease_seconds', '—')}s",
            style=DIM,
        )
    left = Text.assemble(
        dot,
        cell(name, 16),
        cell(Text(where_text, style=DIM), 22),
        doing,
    )
    return spread(left, right, width)
