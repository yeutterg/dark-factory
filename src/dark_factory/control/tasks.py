"""Durable, human-visible task lists below the controller's approval boundary."""

from __future__ import annotations

import json
from typing import Any

from dark_factory.control.base import ControllerCore, ControllerError, atomic
from dark_factory.control.tasks_common import (  # noqa: F401
    OWNER_KINDS,
    PROGRESS_KINDS,
    TASK_LINE,
    TASK_STATES,
    VERIFICATION_STATES,
    _digest,
)
from dark_factory.models import ACTIVE_ATTEMPT, TERMINAL_ROOT
from dark_factory.store import new_id, now_iso


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
        # A replan replaces the earlier plan's tasks; they must not block acceptance.
        self.store.execute(
            """UPDATE tasks SET superseded_by=?,updated_at=?
               WHERE root_id=? AND plan_version<? AND origin='plan'
               AND superseded_by IS NULL""",
            (
                f"plan_version:{root['plan_version']}",
                now_iso(),
                wid,
                root["plan_version"],
            ),
        )
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
            if status in ACTIVE_ATTEMPT:
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
