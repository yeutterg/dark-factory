"""Durable, human-visible task lists below the controller's approval boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from dark_factory.control.base import ControllerCore, ControllerError, atomic
from dark_factory.models import TERMINAL_ROOT
from dark_factory.store import new_id, now_iso

TASK_STATES = frozenset(
    {"inbox", "queued", "active", "waiting", "reported_done", "done", "cancelled"}
)
OWNER_KINDS = frozenset({"agent", "controller", "human", "external"})
VERIFICATION_STATES = frozenset(
    {"not_required", "pending", "passed", "failed", "stale"}
)
PROGRESS_KINDS = frozenset(
    {
        "task_started",
        "task_progress",
        "task_reported_done",
        "task_blocked",
        "task_proposed",
    }
)
TASK_LINE = re.compile(
    r"^- \[(?P<checked>[ xX])\] (?P<title>.*?)"
    r"(?: <!-- task:(?P<id>[A-Za-z0-9_-]+) owner:(?P<owner>[a-z]+) -->)?$"
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class TaskMixin(ControllerCore):
    """Task projections and mutations; jobs remain the execution authority."""

    def task_revision(self, wid: str) -> int:
        row = self.store.one(
            "SELECT value FROM ops WHERE key=?", (f"task_revision:{wid}",)
        )
        return int(row["value"]) if row else 0

    def _set_task_revision(self, wid: str, actor: str) -> int:
        revision = self.task_revision(wid) + 1
        self.store.execute(
            """INSERT INTO ops(key,value,actor,at) VALUES (?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
            actor=excluded.actor,at=excluded.at""",
            (f"task_revision:{wid}", str(revision), actor, now_iso()),
        )
        return revision

    def _refresh_task_document(self, wid: str, actor: str) -> None:
        try:
            self.export_tasks(wid)
        except OSError as exc:
            self.store.audit(
                actor, "tasks.export_failed", {"root": wid, "error": str(exc)[:500]}
            )

    def _task_root(self, wid: str):
        root = self.store.one(
            "SELECT * FROM work_items WHERE id=? AND is_root=1", (wid,)
        )
        if not root:
            raise ControllerError("work item not found")
        return root

    def _expect_task_revision(self, wid: str, expected: int | None) -> int:
        current = self.task_revision(wid)
        if expected is not None and (type(expected) is not int or expected != current):
            raise ControllerError(
                f"task list changed; expected revision {expected}, current revision {current}"
            )
        return current

    def _task_rows(self, wid: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.store.query(
                """SELECT * FROM tasks WHERE root_id=? AND superseded_by IS NULL
                ORDER BY priority,created_at,id""",
                (wid,),
            )
        ]

    def _criteria(self, root) -> dict[str, dict[str, Any]]:
        if not root["plan_json"]:
            return {}
        proposal = json.loads(root["plan_json"]).get("proposal") or {}
        return {
            item["id"]: item
            for item in proposal.get("acceptance", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

    def seed_plan_tasks(
        self, wid: str, proposal: dict[str, Any], actor="controller"
    ) -> int:
        """Create the task view of a new proposal once, without inference."""
        root = self._task_root(wid)
        existing = self.store.one(
            "SELECT id FROM tasks WHERE root_id=? AND plan_version=? LIMIT 1",
            (wid, root["plan_version"]),
        )
        if existing:
            return self.task_revision(wid)
        jobs = self.jobs(wid)
        by_kind = {
            job["kind"]: job
            for job in jobs
            if not json.loads(job["spec_json"]).get("repair")
        }
        acceptance = proposal.get("acceptance", [])
        position = 0

        def add(
            title: str,
            owner: str,
            *,
            job=None,
            gate: str | None = None,
            criteria: list[str] | None = None,
            condition: str = "",
            required: bool = True,
        ) -> None:
            nonlocal position
            position += 10
            task_id = new_id("task")
            verification = (
                "pending" if owner in {"controller", "human"} else "not_required"
            )
            self.store.execute(
                """INSERT INTO tasks
                (id,root_id,plan_version,title,completion_condition,owner_kind,required,
                 job_id,manual_gate,criterion_ids,priority,state,verification,origin,
                 created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    wid,
                    root["plan_version"],
                    title.strip(),
                    condition.strip(),
                    owner,
                    int(required),
                    job["id"] if job else None,
                    gate,
                    json.dumps(criteria or []),
                    position,
                    "queued",
                    verification,
                    "plan",
                    actor,
                    now_iso(),
                    now_iso(),
                ),
            )

        for task in proposal.get("tasks", []):
            if (
                not isinstance(task, dict)
                or not str(task.get("description", "")).strip()
            ):
                continue
            checks = set(task.get("checks") or [])
            criteria = [
                c["id"]
                for c in acceptance
                if isinstance(c, dict) and checks.intersection(c.get("checks") or [])
            ]
            add(
                str(task["description"]),
                "agent",
                job=by_kind.get("implement"),
                criteria=criteria,
                condition="Agent reports the implementation work complete; bound checks still verify it.",
            )
        check_job = by_kind.get("check")
        for check in (
            json.loads(check_job["spec_json"]).get("check_names", [])
            if check_job
            else []
        ):
            criteria = [
                c["id"]
                for c in acceptance
                if isinstance(c, dict) and check in (c.get("checks") or [])
            ]
            add(
                f"Run {check}",
                "controller",
                job=check_job,
                criteria=criteria,
                condition="The canonical check succeeds on the exact candidate.",
            )
        gates_seen: set[str] = set()
        for criterion in acceptance:
            if not isinstance(criterion, dict):
                continue
            for gate in criterion.get("manual_gates") or []:
                gates_seen.add(str(gate))
                add(
                    str(gate),
                    "human",
                    gate=str(gate),
                    criteria=[str(criterion["id"])],
                    condition="A named human records evidence for the exact candidate.",
                )
        snapshot = json.loads(root["snapshot_json"] or "{}")
        fallback_criteria = [
            str(criterion["id"])
            for criterion in acceptance
            if isinstance(criterion, dict)
        ][:1]
        for gate in snapshot.get("manual_acceptance", []):
            if str(gate) not in gates_seen:
                add(
                    str(gate),
                    "human",
                    gate=str(gate),
                    criteria=fallback_criteria,
                    condition="A named human records evidence for the exact candidate.",
                )
        add(
            "Independent review",
            "controller",
            job=by_kind.get("review"),
            criteria=[str(c["id"]) for c in acceptance if isinstance(c, dict)],
            condition="Independent review assesses every approved criterion.",
        )
        add(
            "Review and accept the complete outcome",
            "human",
            criteria=[str(c["id"]) for c in acceptance if isinstance(c, dict)],
            condition="A named human accepts the exact candidate after all evidence is present.",
            required=False,
        )
        revision = self._set_task_revision(wid, actor)
        self.store.audit(actor, "tasks.seed", {"root": wid, "revision": revision})
        self._refresh_task_document(wid, actor)
        return revision

    def _effective_task(self, task: dict[str, Any], root, jobs) -> dict[str, Any]:
        task = dict(task)
        task["required"] = bool(task["required"])
        task["criterion_ids"] = json.loads(task["criterion_ids"] or "[]")
        task["depends_on"] = json.loads(task["depends_on"] or "[]")
        job = jobs.get(task.get("job_id"))
        if job and job["status"] in {
            "failed",
            "superseded_by_repair",
            "superseded",
        }:
            replacement = next(
                (
                    candidate
                    for candidate in jobs.values()
                    if candidate["kind"] == job["kind"]
                    and json.loads(candidate["spec_json"]).get("repair")
                    and candidate["status"] == "succeeded"
                ),
                None,
            )
            if replacement:
                job = replacement
        if job:
            status = job["status"]
            if status in {"leased", "running", "cancelling"}:
                task["state"] = "active"
                latest_attempt = self.store.one(
                    """SELECT id FROM attempts WHERE job_id=?
                    ORDER BY started_at DESC,id DESC LIMIT 1""",
                    (job["id"],),
                )
                task["attempt_id"] = latest_attempt["id"] if latest_attempt else None
            elif status == "succeeded":
                task["state"] = "done"
                task["verification"] = (
                    "passed"
                    if task["owner_kind"] == "controller"
                    else task["verification"]
                )
            elif status in {"failed", "cancelled", "timed_out", "interrupted"}:
                task["state"] = "waiting"
                task["waiting_reason"] = status.replace("_", " ")
        if task.get("manual_gate"):
            candidate = (
                json.loads(root["candidate_commits"] or "{}")
                .get(root["repo"], {})
                .get("commit")
            )
            if candidate:
                gate = self.store.one(
                    "SELECT * FROM manual_gates WHERE root_id=? AND candidate=? AND name=?",
                    (root["id"], candidate, task["manual_gate"]),
                )
                if gate:
                    task["state"] = "done" if gate["passed"] else "waiting"
                    task["verification"] = "passed" if gate["passed"] else "failed"
                    task["evidence"] = gate["evidence"]
                    task["candidate"] = candidate
                    task["completed_by"] = gate["actor"]
                    task["completed_at"] = gate["at"]
                elif self.store.one(
                    "SELECT 1 FROM manual_gates WHERE root_id=? AND name=? LIMIT 1",
                    (root["id"], task["manual_gate"]),
                ):
                    task["verification"] = "stale"
                    task["waiting_reason"] = (
                        "previous evidence belongs to another candidate"
                    )
            elif task["state"] not in {"done", "cancelled"}:
                task["state"] = "waiting"
                task["waiting_reason"] = "waiting for an exact candidate"
        if task["title"] == "Review and accept the complete outcome":
            if root["status"] == "accepted":
                task["state"], task["verification"] = "done", "passed"
            elif root["status"] != "awaiting_review":
                task["state"] = "waiting"
                task["waiting_reason"] = "waiting for required evidence"
        instruction = self.store.one(
            """SELECT state FROM task_instructions WHERE task_id=?
            ORDER BY created_at DESC,id DESC LIMIT 1""",
            (task["id"],),
        )
        task["delivery_state"] = instruction["state"] if instruction else None
        if (
            task["required"]
            and instruction
            and instruction["state"] not in {"resolved", "superseded"}
            and task["state"] == "done"
        ):
            task["state"] = "waiting"
            task["waiting_reason"] = "required instruction has not been resolved"
        return task

    def tasks_view(self, wid: str) -> dict[str, Any]:
        root = self._task_root(wid)
        jobs = {job["id"]: job for job in self.jobs(wid)}
        tasks = [
            self._effective_task(task, root, jobs) for task in self._task_rows(wid)
        ]
        done = {task["id"] for task in tasks if task["state"] == "done"}
        for task in tasks:
            missing = [dep for dep in task["depends_on"] if dep not in done]
            task["waiting_on"] = missing
            if missing and task["state"] in {"queued", "inbox"}:
                task["state"] = "waiting"
                task["waiting_reason"] = "waiting on task dependencies"
        active = [task for task in tasks if task["state"] == "active"]
        eligible = [
            task
            for task in tasks
            if task["state"] == "queued" and not task["waiting_on"]
        ]
        human = [
            task
            for task in tasks
            if task["owner_kind"] == "human"
            and task["state"] in {"queued", "waiting"}
            and not task["waiting_on"]
        ]
        current = [
            {
                "task_id": task["id"],
                "owner_kind": task["owner_kind"],
                "title": task["title"],
            }
            for task in active
        ]
        if not current:
            current = [
                {
                    "task_id": None,
                    "owner_kind": "controller",
                    "title": "Task-level progress has not been reported.",
                }
            ]
        next_actions = [
            {
                "task_id": task["id"],
                "owner_kind": task["owner_kind"],
                "title": task["title"],
            }
            for task in sorted(
                eligible, key=lambda item: (item["priority"], item["created_at"])
            )[:3]
        ]
        return {
            "revision": self.task_revision(wid),
            "tasks": tasks,
            "current_actions": current,
            "next_actions": next_actions,
            "needs_human": [task["id"] for task in human],
            "inbox_count": sum(task["state"] == "inbox" for task in tasks),
            "complete": sum(task["state"] == "done" for task in tasks),
            "total": len(tasks),
            "last_progress_at": max(
                (str(task["updated_at"]) for task in tasks if task["updated_at"]),
                default=None,
            ),
        }

    @atomic
    def add_task(
        self,
        wid: str,
        title: str,
        owner_kind: str,
        actor: str,
        expected_revision: int | None = None,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        root = self._task_root(wid)
        key = idempotency_key or new_id("op")
        event = self.store.one(
            "SELECT root_id,task_id FROM task_events WHERE idempotency_key=?", (key,)
        )
        if event:
            if event["root_id"] != wid:
                raise ControllerError("idempotency key belongs to another root")
            return {"task_id": event["task_id"], "revision": self.task_revision(wid)}
        self._expect_task_revision(wid, expected_revision)
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 500:
            raise ControllerError(
                "task title is required and must be at most 500 characters"
            )
        if owner_kind not in OWNER_KINDS:
            raise ControllerError("invalid task owner")
        task_id = new_id("task")
        closed = root["status"] in {str(value) for value in TERMINAL_ROOT}
        at = now_iso()
        priority_row = self.store.one(
            "SELECT COALESCE(MAX(priority),0)+10 n FROM tasks WHERE root_id=?", (wid,)
        )
        assert priority_row is not None
        self.store.execute(
            """INSERT INTO tasks
            (id,root_id,plan_version,title,description,owner_kind,required,priority,
             state,verification,origin,created_by,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                wid,
                root["plan_version"],
                title.strip(),
                description.strip()[:4000],
                owner_kind,
                0,
                priority_row["n"],
                "inbox",
                "not_required",
                "follow_up" if closed else "human",
                actor,
                at,
                at,
            ),
        )
        self.store.execute(
            """INSERT INTO task_events
            (id,root_id,task_id,source,actor,kind,payload_json,idempotency_key,created_at,processed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("tev"),
                wid,
                task_id,
                "operator",
                actor,
                "task_added",
                json.dumps({"title": title.strip(), "owner_kind": owner_kind}),
                key,
                at,
                at if owner_kind != "agent" else None,
            ),
        )
        revision = self._set_task_revision(wid, actor)
        self.store.audit(
            actor, "task.add", {"root": wid, "task": task_id, "revision": revision}
        )
        self._refresh_task_document(wid, actor)
        return {"task_id": task_id, "revision": revision, "state": "inbox"}

    @atomic
    def update_task(
        self,
        wid: str,
        task_id: str,
        actor: str,
        expected_revision: int,
        *,
        title: str | None = None,
        priority: int | None = None,
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        self._task_root(wid)
        self._expect_task_revision(wid, expected_revision)
        task = self.store.one(
            "SELECT * FROM tasks WHERE id=? AND root_id=?", (task_id, wid)
        )
        if not task:
            raise ControllerError("task not found")
        values: list[Any] = []
        fields: list[str] = []
        if title is not None:
            if task["required"] and title.strip() != task["title"]:
                raise ControllerError(
                    "changing a required task needs a revised proposal"
                )
            if (
                not isinstance(title, str)
                or not title.strip()
                or len(title.strip()) > 500
            ):
                raise ControllerError(
                    "task title is required and must be at most 500 characters"
                )
            fields.append("title=?")
            values.append(title.strip())
        if priority is not None:
            if type(priority) is not int:
                raise ControllerError("task priority must be an integer")
            fields.append("priority=?")
            values.append(priority)
        if depends_on is not None:
            if task["required"] and depends_on != json.loads(
                task["depends_on"] or "[]"
            ):
                raise ControllerError(
                    "changing required task dependencies needs a revised proposal"
                )
            if not isinstance(depends_on, list) or task_id in depends_on:
                raise ControllerError("invalid task dependencies")
            known = {
                row["id"]
                for row in self.store.query(
                    "SELECT id FROM tasks WHERE root_id=?", (wid,)
                )
            }
            if any(
                not isinstance(item, str) or item not in known for item in depends_on
            ):
                raise ControllerError("task dependency is not in this root")
            graph = {
                row["id"]: json.loads(row["depends_on"] or "[]")
                for row in self.store.query(
                    "SELECT id,depends_on FROM tasks WHERE root_id=?", (wid,)
                )
            }
            graph[task_id] = depends_on
            visiting: set[str] = set()
            seen: set[str] = set()

            def cyclic(node: str) -> bool:
                if node in visiting:
                    return True
                if node in seen:
                    return False
                visiting.add(node)
                if any(cyclic(dep) for dep in graph.get(node, [])):
                    return True
                visiting.remove(node)
                seen.add(node)
                return False

            if any(cyclic(node) for node in graph):
                raise ControllerError("task dependencies contain a cycle")
            fields.append("depends_on=?")
            values.append(json.dumps(depends_on))
        if not fields:
            return {"task_id": task_id, "revision": expected_revision}
        fields.append("updated_at=?")
        values.append(now_iso())
        values.extend((task_id, wid))
        self.store.execute(
            f"UPDATE tasks SET {','.join(fields)} WHERE id=? AND root_id=?",
            tuple(values),
        )
        revision = self._set_task_revision(wid, actor)
        self.store.audit(
            actor, "task.update", {"root": wid, "task": task_id, "revision": revision}
        )
        self._refresh_task_document(wid, actor)
        return {"task_id": task_id, "revision": revision}

    @atomic
    def complete_task(
        self,
        wid: str,
        task_id: str,
        actor: str,
        expected_revision: int,
        evidence: str = "",
    ) -> dict[str, Any]:
        root = self._task_root(wid)
        self._expect_task_revision(wid, expected_revision)
        task = self.store.one(
            "SELECT * FROM tasks WHERE id=? AND root_id=?", (task_id, wid)
        )
        if not task:
            raise ControllerError("task not found")
        if task["state"] in {"done", "cancelled"}:
            return {
                "task_id": task_id,
                "revision": expected_revision,
                "state": task["state"],
            }
        if task["owner_kind"] == "controller" or task["job_id"]:
            raise ControllerError(
                "controller tasks complete only from bound execution evidence"
            )
        if task["manual_gate"]:
            candidate = (
                json.loads(root["candidate_commits"] or "{}")
                .get(root["repo"], {})
                .get("commit")
            )
            if not candidate or not evidence.strip():
                raise ControllerError(
                    "manual verification requires the exact candidate and evidence"
                )
            self.record_manual_gate(
                wid, task["manual_gate"], candidate, actor, evidence, True
            )
        else:
            at = now_iso()
            self.store.execute(
                """UPDATE tasks SET state='done',verification='not_required',result=?,
                completed_by=?,completed_at=?,updated_at=? WHERE id=?""",
                (evidence.strip(), actor, at, at, task_id),
            )
        revision = self._set_task_revision(wid, actor)
        self.store.audit(
            actor, "task.complete", {"root": wid, "task": task_id, "revision": revision}
        )
        self._refresh_task_document(wid, actor)
        return {"task_id": task_id, "revision": revision, "state": "done"}

    @atomic
    def reopen_task(
        self, wid: str, task_id: str, actor: str, expected_revision: int, reason: str
    ) -> dict[str, Any]:
        root = self._task_root(wid)
        self._expect_task_revision(wid, expected_revision)
        task = self.store.one(
            "SELECT * FROM tasks WHERE id=? AND root_id=?", (task_id, wid)
        )
        if not task:
            raise ControllerError("task not found")
        if root["status"] == "accepted":
            raise ControllerError(
                "accepted work needs a follow-up proposal; it cannot be reopened in place"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ControllerError("a reopen reason is required")
        self.store.execute(
            """UPDATE tasks SET state='inbox',verification=CASE WHEN verification='not_required'
            THEN verification ELSE 'stale' END,waiting_reason=?,completed_by=NULL,
            completed_at=NULL,updated_at=? WHERE id=?""",
            (reason.strip(), now_iso(), task_id),
        )
        revision = self._set_task_revision(wid, actor)
        self.store.audit(
            actor,
            "task.reopen",
            {"root": wid, "task": task_id, "reason": reason, "revision": revision},
        )
        self._refresh_task_document(wid, actor)
        return {"task_id": task_id, "revision": revision, "state": "inbox"}

    def _active_attempt_for_root(self, wid: str):
        return self.store.one(
            """SELECT a.*,j.work_item_id FROM attempts a JOIN jobs j ON j.id=a.job_id
            WHERE j.work_item_id=? AND a.status IN ('leased','running','cancelling')
            ORDER BY a.started_at DESC,a.id DESC LIMIT 1""",
            (wid,),
        )

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
                    ),
                    None,
                )
                if not implement:
                    raise ControllerError(
                        "no approved implementation job can own the task"
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

    def reconcile_task_events(self, wid: str, actor="controller") -> dict[str, Any]:
        """Run one bounded structured reconciliation call for pending free text."""
        root = self._task_root(wid)
        settings = self.cfg.task_supervisor
        pending = [
            dict(row)
            for row in self.store.query(
                """SELECT * FROM task_events WHERE root_id=? AND processed_at IS NULL
                ORDER BY created_at,id LIMIT 50""",
                (wid,),
            )
        ]
        if not pending:
            return {"status": "idle", "pending": 0}
        if not settings:
            return {"status": "unconfigured", "pending": len(pending)}
        calls = self.store.one(
            "SELECT COUNT(*) n,COALESCE(SUM(cost_cents),0) cost FROM supervisor_calls WHERE root_id=?",
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
            self.store.execute(
                """INSERT INTO supervisor_calls
                (id,root_id,input_digest,request_json,status,cost_cents,created_at)
                VALUES (?,?,?,?,?,0,?) ON CONFLICT(root_id,input_digest) DO UPDATE SET
                status='pending'""",
                (call_id, wid, input_digest, serialized, "pending", now_iso()),
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
            with self.store.transaction():
                self.store.execute(
                    """UPDATE work_items SET reserved_cents=MAX(reserved_cents-?,0),
                    spent_cents=spent_cents+? WHERE id=?""",
                    (reservation, reservation, wid),
                )
                self.store.execute(
                    "UPDATE supervisor_calls SET status='failed',cost_cents=? WHERE id=?",
                    (reservation, call_id),
                )
                self.store.execute(
                    """INSERT INTO costs(id,root_id,source,amount_cents,currency,status,created_at)
                    VALUES (?,?,?,?,?,?,?)""",
                    (
                        new_id("cost"),
                        wid,
                        "task-supervisor:" + settings["command"],
                        reservation,
                        "USD",
                        "unknown",
                        now_iso(),
                    ),
                )
                self.store.audit(
                    actor,
                    "task_supervisor.failed",
                    {"root": wid, "error": str(exc)[:500]},
                )
            return {"status": "failed", "pending": len(pending), "error": str(exc)}
        with self.store.transaction():
            self.store.execute(
                """UPDATE work_items SET reserved_cents=MAX(reserved_cents-?,0),
                spent_cents=spent_cents+? WHERE id=?""",
                (reservation, cost, wid),
            )
            self.store.execute(
                "UPDATE supervisor_calls SET response_json=?,status='applied',cost_cents=? WHERE id=?",
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
        if not attempt or attempt["status"] not in {"leased", "running", "cancelling"}:
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
            ORDER BY created_at,id""",
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

    def import_tasks(
        self, wid: str, actor: str, expected_revision: int | None = None
    ) -> dict[str, Any]:
        """Import the small documented Markdown subset as attributed proposals."""
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
