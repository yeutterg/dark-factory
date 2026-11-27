"""Agent progress events and instructions, bound to the active attempt."""

from __future__ import annotations

import json
from typing import Any

from dark_factory.control.base import ControllerError, atomic
from dark_factory.control.tasks import TaskMixin
from dark_factory.control.tasks_common import (
    PROGRESS_KINDS,
)
from dark_factory.models import ACTIVE_ATTEMPT
from dark_factory.store import new_id, now_iso


class TaskProgressMixin(TaskMixin):
    """Agent progress events and instructions, bound to the active attempt."""

    @atomic
    def record_task_progress(
        self,
        attempt_id: str,
        task_id: str,
        kind: str,
        payload: dict[str, Any],
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if kind not in PROGRESS_KINDS or not isinstance(payload, dict):
            raise ControllerError("invalid task progress event")
        attempt = self.store.one(
            """SELECT a.*,j.work_item_id FROM attempts a JOIN jobs j ON j.id=a.job_id
            WHERE a.id=?""",
            (attempt_id,),
        )
        if not attempt or attempt["status"] not in ACTIVE_ATTEMPT:
            raise ControllerError("task progress attempt is no longer active")
        task = self.store.one(
            "SELECT * FROM tasks WHERE id=? AND root_id=?",
            (task_id, attempt["work_item_id"]),
        )
        if (
            not task
            or task["owner_kind"] != "agent"
            or task["job_id"] != attempt["job_id"]
        ):
            raise ControllerError("task progress is not bound to this attempt")
        # A model chooses its own event ids; they only deduplicate within its attempt.
        idempotency_key = f"{attempt_id}:{idempotency_key}"[:300]
        prior = self.store.one(
            "SELECT id FROM task_events WHERE idempotency_key=?", (idempotency_key,)
        )
        if prior:
            return {"accepted": True, "revision": self.task_revision(task["root_id"])}
        state = {
            "task_started": "active",
            "task_progress": "active",
            "task_reported_done": "reported_done",
            "task_blocked": "waiting",
            "task_proposed": task["state"],
        }[kind]
        at = now_iso()
        self.store.execute(
            """INSERT INTO task_events
            (id,root_id,task_id,attempt_id,source,actor,kind,payload_json,
             idempotency_key,created_at,processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("tev"),
                task["root_id"],
                task_id,
                attempt_id,
                "worker",
                actor,
                kind,
                json.dumps(payload),
                idempotency_key,
                at,
                at,
            ),
        )
        if kind != "task_proposed":
            self.store.execute(
                """UPDATE tasks SET state=?,result=?,waiting_reason=?,attempt_id=?,updated_at=?
                WHERE id=?""",
                (
                    state,
                    str(payload.get("summary") or "")[:4000],
                    str(payload.get("reason") or "")[:1000]
                    if state == "waiting"
                    else "",
                    attempt_id,
                    at,
                    task_id,
                ),
            )
            if kind == "task_reported_done":
                self.store.execute(
                    """UPDATE task_instructions SET state='resolved',resolved_at=?
                    WHERE task_id=? AND attempt_id=? AND state IN ('queued','delivered','acknowledged')""",
                    (at, task_id, attempt_id),
                )
        revision = self._set_task_revision(task["root_id"], actor)
        self._refresh_task_document(task["root_id"], actor)
        return {"accepted": True, "revision": revision}

    @atomic
    def pending_task_instructions(
        self, attempt_id: str, actor: str
    ) -> list[dict[str, Any]]:
        attempt = self.store.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        if (
            not attempt
            or attempt["status"] not in {"leased", "running"}
            or attempt["worker_id"] != actor
        ):
            raise ControllerError("attempt is not active for this worker")
        rows = self.store.query(
            """SELECT * FROM task_instructions WHERE attempt_id=? AND state='queued'
            ORDER BY created_at,rowid""",
            (attempt_id,),
        )
        at = now_iso()
        for row in rows:
            self.store.execute(
                "UPDATE task_instructions SET state='delivered',delivered_at=? WHERE id=?",
                (at, row["id"]),
            )
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "task_revision": row["task_revision"],
                "payload": json.loads(row["payload_json"]),
                "payload_digest": row["payload_digest"],
            }
            for row in rows
        ]

    @atomic
    def acknowledge_task_instruction(
        self, attempt_id: str, instruction_id: str, actor: str
    ) -> dict[str, Any]:
        row = self.store.one(
            """SELECT i.*,a.worker_id,a.status attempt_status FROM task_instructions i
            JOIN attempts a ON a.id=i.attempt_id WHERE i.id=? AND i.attempt_id=?""",
            (instruction_id, attempt_id),
        )
        if (
            not row
            or row["worker_id"] != actor
            or row["attempt_status"] not in {"leased", "running"}
        ):
            raise ControllerError("instruction is not active for this worker")
        if row["state"] == "delivered":
            self.store.execute(
                "UPDATE task_instructions SET state='acknowledged',acknowledged_at=? WHERE id=?",
                (now_iso(), instruction_id),
            )
        return {"accepted": True, "instruction_id": instruction_id}
