"""The Markdown projection of a task list and its authenticated import."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from dark_factory.control.base import ControllerError, atomic
from dark_factory.control.tasks import TaskMixin
from dark_factory.control.tasks_common import (
    TASK_LINE,
    _digest,
)
from dark_factory.store import now_iso


class TaskMarkdownMixin(TaskMixin):
    """The Markdown projection of a task list and its authenticated import."""

    def tasks_path(self, wid: str) -> Path:
        return self.store.state_dir / "work-items" / wid / "tasks.md"

    def render_tasks_markdown(self, wid: str) -> str:
        root = self._task_root(wid)
        view = self.tasks_view(wid)
        current = "; ".join(item["title"] for item in view["current_actions"])
        next_action = (
            "; ".join(item["title"] for item in view["next_actions"])
            or "No eligible task."
        )
        lines = [
            "---",
            "schema_version: 1",
            f"root_id: {wid}",
            f"plan_version: {root['plan_version']}",
            f"task_revision: {view['revision']}",
            "---",
            "",
            f"# {root['title']}",
            "",
            "## Current state",
            current,
            "",
            "## Next action",
            next_action,
            "",
            "## Tasks",
        ]
        for task in view["tasks"]:
            mark = "x" if task["state"] == "done" else " "
            lines.append(
                f"- [{mark}] {task['title']} <!-- task:{task['id']} owner:{task['owner_kind']} -->"
            )
        lines.extend(("", "## Inbox"))
        if not any(task["state"] == "inbox" for task in view["tasks"]):
            lines.append("<!-- Add unchecked tasks without an ID here. -->")
        return "\n".join(lines) + "\n"

    def export_tasks(self, wid: str) -> dict[str, Any]:
        """Write a projection without overwriting unimported human edits."""
        body = self.render_tasks_markdown(wid)
        path = self.tasks_path(wid)
        row = self.store.one("SELECT * FROM task_documents WHERE root_id=?", (wid,))
        if (
            path.exists()
            and row
            and _digest(path.read_text()) != row["generated_digest"]
        ):
            pending = path.with_name("tasks.pending.md")
            if not pending.exists() or pending.read_text() != path.read_text():
                pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                pending.write_text(path.read_text())
            self.store.execute(
                "UPDATE task_documents SET pending_path=? WHERE root_id=?",
                (str(pending), wid),
            )
            return {"path": str(path), "pending_path": str(pending), "written": False}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(body)
        os.replace(temporary, path)
        digest = _digest(body)
        self.store.execute(
            """INSERT INTO task_documents(root_id,revision,generated_digest,generated_at,pending_path)
            VALUES (?,?,?,?,NULL) ON CONFLICT(root_id) DO UPDATE SET
            revision=excluded.revision,generated_digest=excluded.generated_digest,
            generated_at=excluded.generated_at,pending_path=NULL""",
            (wid, self.task_revision(wid), digest, now_iso()),
        )
        return {"path": str(path), "written": True, "revision": self.task_revision(wid)}

    @atomic
    def import_tasks(
        self, wid: str, actor: str, expected_revision: int | None = None
    ) -> dict[str, Any]:
        """Import the small documented Markdown subset as attributed proposals.

        The whole file is applied in one transaction: a line that fails leaves
        no earlier line applied.
        """
        self._task_root(wid)
        self._expect_task_revision(wid, expected_revision)
        path = self.tasks_path(wid)
        if not path.is_file() or path.is_symlink():
            raise ControllerError("task Markdown file not found")
        body = path.read_text()
        match = re.search(r"^task_revision: (\d+)$", body, re.MULTILINE)
        if not match or int(match.group(1)) != self.task_revision(wid):
            raise ControllerError("task Markdown base revision is stale")
        known = {task["id"]: task for task in self._task_rows(wid)}
        additions: list[str] = []
        completions: list[str] = []
        updated: list[str] = []
        seen: set[str] = set()
        section = ""
        position = 0
        for raw in body.splitlines():
            if raw.startswith("## "):
                section = raw[3:].strip()
                continue
            parsed = TASK_LINE.match(raw)
            if not parsed:
                continue
            task_id = parsed.group("id")
            checked = parsed.group("checked").lower() == "x"
            if not task_id:
                if section != "Inbox":
                    raise ControllerError(
                        "new Markdown tasks must be added under the Inbox heading"
                    )
                if checked:
                    raise ControllerError(
                        "a new Markdown task cannot arrive pre-completed"
                    )
                result = self.add_task(
                    wid,
                    parsed.group("title"),
                    "agent",
                    actor,
                    self.task_revision(wid),
                    idempotency_key=f"markdown:{wid}:" + _digest(raw),
                )
                additions.append(result["task_id"])
                continue
            task = known.get(task_id)
            if not task:
                raise ControllerError("task Markdown references an unknown task")
            if task_id in seen:
                raise ControllerError("task Markdown contains a duplicate task")
            if parsed.group("owner") != task["owner_kind"]:
                raise ControllerError("task Markdown cannot change task ownership")
            seen.add(task_id)
            position += 10
            title = parsed.group("title").strip()
            if title != task["title"] or position != task["priority"]:
                result = self.update_task(
                    wid,
                    task_id,
                    actor,
                    self.task_revision(wid),
                    title=title if title != task["title"] else None,
                    priority=position,
                )
                updated.append(result["task_id"])
            if checked and task["state"] != "done":
                if task["manual_gate"]:
                    raise ControllerError(
                        "manual verification needs evidence in the UI or CLI"
                    )
                if task["owner_kind"] not in {"human", "external"}:
                    raise ControllerError(
                        "only human or external tasks can be checked off in Markdown"
                    )
                result = self.complete_task(
                    wid,
                    task_id,
                    actor,
                    self.task_revision(wid),
                    "Completed from authenticated Markdown import.",
                )
                completions.append(result["task_id"])
        missing = set(known) - seen
        if missing:
            raise ControllerError("task Markdown cannot remove canonical tasks")
        self.store.execute(
            """UPDATE task_documents SET generated_digest=?,imported_digest=?,pending_path=NULL
            WHERE root_id=?""",
            (_digest(body), _digest(body), wid),
        )
        result = self.export_tasks(wid)
        return {
            **result,
            "added": additions,
            "updated": updated,
            "completed": completions,
            "revision": self.task_revision(wid),
        }
