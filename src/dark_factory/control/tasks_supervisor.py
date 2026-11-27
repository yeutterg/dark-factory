"""Bounded structured reconciliation of free-text task requests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from dark_factory.control.base import ControllerError, atomic
from dark_factory.control.tasks import TaskMixin
from dark_factory.control.tasks_common import (
    _digest,
)
from dark_factory.models import OPEN_JOB
from dark_factory.store import new_id, now_iso


class TaskSupervisorMixin(TaskMixin):
    """Bounded structured reconciliation of free-text task requests."""

    @atomic
    def apply_supervisor_response(
        self, wid: str, response: dict[str, Any], actor="task-supervisor"
    ) -> dict[str, Any]:
        """Validate and apply a small model's structured proposal."""
        root = self._task_root(wid)
        if not isinstance(response, dict) or response.get("schema_version") != 1:
            raise ControllerError("invalid task supervisor schema")
        if response.get("root_id") != wid:
            raise ControllerError("task supervisor response belongs to another root")
        required_fields = {
            "schema_version",
            "root_id",
            "base_revision",
            "processed_event_ids",
            "operations",
            "current_summary",
            "suggested_next_task_id",
            "needs_decision",
        }
        if set(response) != required_fields:
            raise ControllerError("task supervisor response has invalid fields")
        pending_events = {
            row["id"]
            for row in self.store.query(
                "SELECT id FROM task_events WHERE root_id=? AND processed_at IS NULL",
                (wid,),
            )
        }
        processed = response["processed_event_ids"]
        if (
            not isinstance(processed, list)
            or not all(
                isinstance(event_id, str) and event_id in pending_events
                for event_id in processed
            )
            or len(processed) != len(set(processed))
            or not isinstance(response["current_summary"], str)
            or len(response["current_summary"]) > 2000
            or not isinstance(response["needs_decision"], list)
            or not all(isinstance(item, str) for item in response["needs_decision"])
        ):
            raise ControllerError("invalid task supervisor event reconciliation")
        base = response.get("base_revision")
        if type(base) is not int:
            raise ControllerError("task supervisor response needs a base revision")
        self._expect_task_revision(wid, base)
        operations = response.get("operations")
        if not isinstance(operations, list) or len(operations) > 50:
            raise ControllerError("invalid task supervisor operations")
        known = {task["id"]: task for task in self._task_rows(wid)}
        criteria = self._criteria(root)
        suggested = response["suggested_next_task_id"]
        if suggested is not None and suggested not in known:
            raise ControllerError("task supervisor suggested an unknown task")
        applied: list[str] = []
        for operation in operations:
            if not isinstance(operation, dict):
                raise ControllerError("task supervisor operation must be an object")
            op = operation.get("op")
            sources = operation.get("source_event_ids")
            if (
                not isinstance(sources, list)
                or not sources
                or any(event_id not in processed for event_id in sources)
            ):
                raise ControllerError("task supervisor operation lacks source events")
            if op == "admit_task":
                task_id = operation.get("task_id")
                if not isinstance(task_id, str):
                    raise ControllerError("supervisor task identity must be text")
                task = known.get(task_id)
                criterion_ids = operation.get("criterion_ids") or []
                if (
                    not task
                    or task["state"] != "inbox"
                    or task["owner_kind"] != "agent"
                    or not criterion_ids
                    or any(cid not in criteria for cid in criterion_ids)
                ):
                    raise ControllerError(
                        "supervisor cannot admit an unbound agent task"
                    )
                implement = next(
                    (
                        job
                        for job in self.jobs(wid)
                        if job["kind"] == "implement"
                        and not json.loads(job["spec_json"]).get("repair")
                        and job["status"] in OPEN_JOB
                    ),
                    None,
                )
                if not implement:
                    # A finished step cannot do new work; binding a task to it
                    # would block acceptance forever.
                    raise ControllerError(
                        "no open implementation step can own the task; "
                        "the approved implementation has already finished"
                    )
                self.store.execute(
                    """UPDATE tasks SET state='queued',required=1,job_id=?,criterion_ids=?,updated_at=?
                    WHERE id=?""",
                    (implement["id"], json.dumps(criterion_ids), now_iso(), task_id),
                )
                active = self._active_attempt_for_root(wid)
                payload = {
                    "task_id": task_id,
                    "title": task["title"],
                    "description": task["description"],
                }
                self.store.execute(
                    """INSERT INTO task_instructions
                    (id,root_id,task_id,job_id,attempt_id,task_revision,payload_json,
                     payload_digest,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id("ins"),
                        wid,
                        task_id,
                        implement["id"],
                        active["id"]
                        if active and active["job_id"] == implement["id"]
                        else None,
                        base,
                        json.dumps(payload),
                        _digest(json.dumps(payload, sort_keys=True)),
                        "queued",
                        now_iso(),
                    ),
                )
                applied.append(task_id)
            elif op in {"report_progress", "block_task", "report_done"}:
                task_id = operation.get("task_id")
                if not isinstance(task_id, str):
                    raise ControllerError("supervisor task identity must be text")
                task = known.get(task_id)
                if not task or task["owner_kind"] != "agent":
                    raise ControllerError("supervisor referenced an unknown agent task")
                state = {
                    "report_progress": "active",
                    "block_task": "waiting",
                    "report_done": "reported_done",
                }[op]
                self.store.execute(
                    "UPDATE tasks SET state=?,result=?,waiting_reason=?,updated_at=? WHERE id=?",
                    (
                        state,
                        str(operation.get("summary") or "")[:4000],
                        str(operation.get("reason") or "")[:1000]
                        if state == "waiting"
                        else "",
                        now_iso(),
                        task_id,
                    ),
                )
                applied.append(task_id)
            else:
                raise ControllerError(f"unsupported task supervisor operation {op!r}")
        revision = self._set_task_revision(wid, actor) if applied else base
        self.store.audit(
            actor,
            "tasks.reconcile",
            {"root": wid, "revision": revision, "tasks": applied},
        )
        if applied:
            self._refresh_task_document(wid, actor)
        return {"revision": revision, "applied": applied}

    def _fail_supervisor_call(self, wid, call_id, reservation, error, actor):
        """Charge the whole reservation for a failed call and release it."""
        with self.store.transaction():
            self.store.execute(
                """UPDATE work_items SET reserved_cents=MAX(reserved_cents-?,0),
                spent_cents=spent_cents+? WHERE id=?""",
                (reservation, reservation, wid),
            )
            self.store.execute(
                "UPDATE supervisor_calls SET status='failed',"
                "cost_cents=cost_cents+?,reserved_cents=0 WHERE id=?",
                (reservation, call_id),
            )
            self.store.execute(
                """INSERT INTO costs(id,root_id,source,amount_cents,currency,status,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    new_id("cost"),
                    wid,
                    "task-supervisor:" + self.cfg.task_supervisor["command"],
                    reservation,
                    "USD",
                    "unknown",
                    now_iso(),
                ),
            )
            self.store.audit(
                actor,
                "task_supervisor.failed",
                {"root": wid, "error": error[:500]},
            )

    def release_orphaned_supervisor_calls(self, actor="controller") -> int:
        """Settle calls left pending by a crash, once their time bound has passed."""
        settings = self.cfg.task_supervisor
        if not settings:
            return 0
        bound = self.cfg.commands[settings["command"]].timeout_seconds + 60
        cutoff = (datetime.now(UTC) - timedelta(seconds=bound)).isoformat()
        released = 0
        for call in self.store.query(
            "SELECT id,root_id,reserved_cents FROM supervisor_calls "
            "WHERE status='pending' AND created_at<?",
            (cutoff,),
        ):
            self._fail_supervisor_call(
                call["root_id"],
                call["id"],
                call["reserved_cents"],
                "interrupted before the call settled",
                actor,
            )
            released += 1
        return released

    def reconcile_task_events(self, wid: str, actor="controller") -> dict[str, Any]:
        """Run one bounded structured reconciliation call for pending free text."""
        root = self._task_root(wid)
        settings = self.cfg.task_supervisor
        pending = [
            dict(row)
            for row in self.store.query(
                """SELECT * FROM task_events WHERE root_id=? AND processed_at IS NULL
                ORDER BY created_at,rowid LIMIT 50""",
                (wid,),
            )
        ]
        if not pending:
            return {"status": "idle", "pending": 0}
        if not settings:
            return {"status": "unconfigured", "pending": len(pending)}
        calls = self.store.one(
            "SELECT COALESCE(SUM(requests),0) n,COALESCE(SUM(cost_cents),0) cost FROM supervisor_calls WHERE root_id=?",
            (wid,),
        )
        assert calls is not None
        if calls["n"] >= settings["max_requests_per_root"]:
            return {"status": "request_limit", "pending": len(pending)}
        remaining = settings["budget_cents"] - calls["cost"]
        per_call = max(1, settings["budget_cents"] // settings["max_requests_per_root"])
        reservation = min(per_call, remaining)
        if (
            reservation <= 0
            or root["spent_cents"] + root["reserved_cents"] + reservation
            > root["spending_ceiling_cents"]
        ):
            return {"status": "budget_exhausted", "pending": len(pending)}
        request = {
            "schema_version": 1,
            "root_id": wid,
            "base_revision": self.task_revision(wid),
            "approved_outcome": {
                "title": root["title"],
                "body": root["body"],
                "criteria": list(self._criteria(root).values()),
            },
            "tasks": self.tasks_view(wid)["tasks"],
            "events": [
                {
                    "id": event["id"],
                    "task_id": event["task_id"],
                    "source": event["source"],
                    "actor": event["actor"],
                    "kind": event["kind"],
                    "payload": json.loads(event["payload_json"]),
                }
                for event in pending
            ],
            "limits": {
                "max_input_tokens": settings["max_input_tokens"],
                "max_output_tokens": settings["max_output_tokens"],
                "max_cost_cents": reservation,
            },
        }
        serialized = json.dumps(request, sort_keys=True)
        if len(serialized) > settings["max_input_tokens"] * 8:
            return {"status": "input_too_large", "pending": len(pending)}
        input_digest = _digest(serialized)
        previous = self.store.one(
            "SELECT * FROM supervisor_calls WHERE root_id=? AND input_digest=?",
            (wid, input_digest),
        )
        if previous and previous["status"] == "applied":
            return {"status": "applied", "pending": len(pending), "cached": True}
        call_id = previous["id"] if previous else new_id("sup")
        with self.store.transaction():
            self.store.execute(
                "UPDATE work_items SET reserved_cents=reserved_cents+? WHERE id=?",
                (reservation, wid),
            )
            # A retry of the same request is another request and another
            # reservation: the cap and the cost counters only ever grow.
            self.store.execute(
                """INSERT INTO supervisor_calls
                (id,root_id,input_digest,request_json,status,cost_cents,created_at,
                 requests,reserved_cents)
                VALUES (?,?,?,?,?,0,?,1,?) ON CONFLICT(root_id,input_digest) DO UPDATE SET
                status='pending',requests=requests+1,reserved_cents=excluded.reserved_cents,
                created_at=excluded.created_at""",
                (
                    call_id,
                    wid,
                    input_digest,
                    serialized,
                    "pending",
                    now_iso(),
                    reservation,
                ),
            )
        command = self.cfg.commands[settings["command"]]
        try:
            result = self.runner.run_json(
                command.command,
                "reconcile_tasks",
                request,
                call_id,
                command.timeout_seconds,
            )
            proposal = result.get("proposal")
            if not isinstance(proposal, dict):
                raise ControllerError("task supervisor returned no structured proposal")
            if (result.get("evidence") or {}).get("model") != settings["model"]:
                raise ControllerError(
                    "task supervisor response does not match the approved model"
                )
            usage = result["evidence"].get("usage") or {}
            cost = usage.get("cost_cents", 0)
            if type(cost) is not int or cost < 0 or cost > reservation:
                raise ControllerError("task supervisor cost exceeds its reservation")
            applied = self.apply_supervisor_response(wid, proposal, "task-supervisor")
        except Exception as exc:
            self._fail_supervisor_call(wid, call_id, reservation, str(exc), actor)
            return {"status": "failed", "pending": len(pending), "error": str(exc)}
        with self.store.transaction():
            self.store.execute(
                """UPDATE work_items SET reserved_cents=MAX(reserved_cents-?,0),
                spent_cents=spent_cents+? WHERE id=?""",
                (reservation, cost, wid),
            )
            self.store.execute(
                "UPDATE supervisor_calls SET response_json=?,status='applied',"
                "cost_cents=cost_cents+?,reserved_cents=0 WHERE id=?",
                (json.dumps(result), cost, call_id),
            )
            for event in pending:
                if event["id"] not in proposal["processed_event_ids"]:
                    continue
                self.store.execute(
                    "UPDATE task_events SET processed_at=? WHERE id=?",
                    (now_iso(), event["id"]),
                )
            self.store.execute(
                """INSERT INTO costs(id,root_id,source,amount_cents,currency,status,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    new_id("cost"),
                    wid,
                    "task-supervisor:" + settings["command"],
                    cost,
                    "USD",
                    "reported",
                    now_iso(),
                ),
            )
        pending_row = self.store.one(
            "SELECT COUNT(*) n FROM task_events WHERE root_id=? AND processed_at IS NULL",
            (wid,),
        )
        assert pending_row is not None
        remaining_pending = pending_row["n"]
        return {"status": "applied", "pending": remaining_pending, **applied}
