"""Leases: claiming, renewing, fencing and cancelling attempts."""

from __future__ import annotations

import json
import os
import shutil
import signal
from datetime import UTC, datetime, timedelta

from dark_factory import artifacts
from dark_factory.adapters.harness.registry import adapter_for
from dark_factory.control.base import ControllerCore, ControllerError, _digest, atomic
from dark_factory.control.windows import window_open
from dark_factory.execution.cleanup import release_attempt_environment
from dark_factory.execution.policy import selected_route
from dark_factory.models import (
    ACTIVE_ATTEMPT,
    DISPATCHABLE_ROOT,
    PLANNING_KINDS,
    sql_set,
)
from dark_factory.runner import process_identity
from dark_factory.store import new_id, now_iso


class DispatchMixin(ControllerCore):
    """Hand out bounded attempts and fence the ones that stop reporting."""

    def pause(self, actor):
        self.store.set_op("pause", "on", actor)

    def resume(self, actor):
        self.store.set_op("pause", "off", actor)

    def drain(self, actor):
        self.store.set_op("drain", "on", actor)

    def undrain(self, actor):
        self.store.set_op("drain", "off", actor)

    def set_workflow_unavailable(self, reason, actor):
        """Record why the workflow runtime cannot dispatch; empty clears it."""
        if self.store.get_op("workflow_unavailable", "") != reason:
            self.store.set_op("workflow_unavailable", reason, actor)

    def dispatch_allowed(self):
        if self.store.get_op("pause") == "on":
            return False, "paused"
        if self.store.get_op("drain") == "on":
            return False, "draining"
        if not window_open(self.cfg):
            return False, "window_closed"
        if self.store.one(
            "SELECT id FROM pending_actions WHERE status IN ('pending','ambiguous')"
        ):
            return False, "external_action_needs_reconciliation"
        return True, "ok"

    def claim_job(self, worker_id, capabilities):
        """Lease the next eligible job.

        A root whose approved or planning inputs changed is skipped rather than
        blocking every other root. Its reason is raised only when nothing else
        could be dispatched, after the scan's own state changes are committed.
        """
        claim, blocked = self._claim(worker_id, capabilities)
        if claim is None and blocked:
            raise ControllerError(blocked[0])
        return claim

    @atomic
    def _claim(self, worker_id, capabilities):
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or not isinstance(capabilities, list)
        ):
            raise ControllerError("invalid worker identity/capabilities")
        if (
            any(r.harness == "pi" for r in self.cfg.routes.values())
            and self.cfg.execution.get("profile") != "macos-sandbox"
            and not self.cfg.project_id
        ):
            raise ControllerError("live execution is not qualified")
        blocked: list[str] = []
        if not self.dispatch_allowed()[0]:
            return None, blocked
        if self.store.one(
            f"SELECT id FROM attempts WHERE status IN {sql_set(ACTIVE_ATTEMPT)}"
        ):
            return None, blocked
        for root in self.store.query(
            f"SELECT * FROM work_items WHERE status IN {sql_set(DISPATCHABLE_ROOT)} ORDER BY created_at,id"
        ):
            try:
                self._require_root_scope(root)
            except ControllerError as exc:
                self._fail_root(root["id"], str(exc))
                continue
            snapshot = json.loads(root["snapshot_json"])
            if datetime.fromisoformat(snapshot["deadline"]) <= datetime.now(UTC):
                self._fail_root(root["id"], "root deadline expired")
                continue
            jobs = self.jobs(root["id"])
            succeeded = {j["logical_id"] for j in jobs if j["status"] == "succeeded"}
            for job in jobs:
                if job["status"] != "ready":
                    continue
                if not set(json.loads(job["depends_on"])) <= succeeded:
                    continue
                if job["kind"] not in PLANNING_KINDS:
                    if not root["approved_digest"]:
                        continue
                    if not self._approval_current(root):
                        blocked.append(
                            "approved inputs changed; dispatch requires a new decision"
                        )
                        break
                elif snapshot["policy"] != self._policy():
                    blocked.append("planning inputs changed; replan")
                    break
                spec = json.loads(job["spec_json"])
                role = spec.get("role")
                route = None
                if role:
                    route = selected_route(self.cfg, role)
                    if route is None:
                        continue
                    if (
                        route.harness != "fake"
                        and self.cfg.execution.get("profile") != "macos-sandbox"
                        and not self.cfg.project_id
                    ):
                        blocked.append("live execution is not qualified")
                        continue
                    required = {"native", route.harness}
                else:
                    required = {"native"}
                if self.cfg.project_id:
                    environment = (
                        route.environment
                        if route
                        else self.cfg.routes[self.cfg.role_routes["coder"]].environment
                    )
                    required.add("environment:" + environment)
                if not required.issubset(set(capabilities)):
                    continue
                need = spec["budget_cents"]
                if route and route.max_cents and need > route.max_cents:
                    continue
                if (
                    root["spent_cents"] + root["reserved_cents"] + need
                    > root["spending_ceiling_cents"]
                ):
                    continue
                account_limit = self.cfg.budgets["account_cents"]
                total = self.store.one(
                    "SELECT COALESCE(SUM(spent_cents+reserved_cents),0) n FROM work_items"
                )["n"]
                if total + need > account_limit:
                    continue
                if job["kind"] in {"plan", "critique"}:
                    spent = self.store.one(
                        "SELECT COALESCE(SUM(a.cost_cents),0) n FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.work_item_id=? AND j.kind IN ('plan','critique')",
                        (root["id"],),
                    )["n"]
                    if spent + need > self.cfg.budgets["planning_cents"]:
                        continue
                attempt = new_id("att")
                deadline = min(
                    datetime.fromisoformat(snapshot["deadline"]),
                    datetime.now(UTC) + timedelta(seconds=spec["timeout_seconds"]),
                )
                inputs = {
                    "root_version": root["plan_version"],
                    "base": snapshot["base"],
                    "candidate": json.loads(root["candidate_commits"]),
                    "policy_hash": _digest(snapshot["policy"]),
                    "route": vars(route) if route else None,
                    "commands": snapshot["bindings"],
                    "role": role,
                    "project_id": self.cfg.project_id,
                    "lease_seconds": self.cfg.lease_seconds(worker_id),
                    "environment": self.cfg.environments.get(environment)
                    if self.cfg.project_id
                    else None,
                }
                self.store.execute(
                    "UPDATE jobs SET status='leased',worker_id=?,lease_until=? WHERE id=?",
                    (
                        worker_id,
                        min(
                            deadline,
                            datetime.now(UTC)
                            + timedelta(seconds=inputs["lease_seconds"]),
                        ).isoformat(),
                        job["id"],
                    ),
                )
                self.store.execute(
                    "UPDATE work_items SET reserved_cents=reserved_cents+?,status='running',presentation_stage=? WHERE id=?",
                    (
                        need,
                        "Plan"
                        if job["kind"] in {"plan", "critique"}
                        else "Build"
                        if job["kind"] == "implement"
                        else "Check",
                        root["id"],
                    ),
                )
                self.store.execute(
                    """INSERT INTO attempts(id,job_id,worker_id,status,started_at,heartbeat_at,deadline,config_hash,prompt_hash,route_id,inputs_json)
                    VALUES (?,?,?,'leased',?,?,?,?,?,?,?)""",
                    (
                        attempt,
                        job["id"],
                        worker_id,
                        now_iso(),
                        now_iso(),
                        deadline.isoformat(),
                        self.cfg.digest,
                        snapshot["policy"]["assets"].get(role, {}).get("sha256", ""),
                        route.id if route else None,
                        json.dumps(inputs),
                    ),
                )
                self.store.execute(
                    "INSERT INTO workers(id,capabilities,last_heartbeat,busy) VALUES (?,?,?,1) ON CONFLICT(id) DO UPDATE SET capabilities=excluded.capabilities,last_heartbeat=excluded.last_heartbeat,busy=1",
                    (worker_id, json.dumps(capabilities), now_iso()),
                )
                self.store.audit(
                    worker_id,
                    "attempt.claim",
                    {
                        "attempt_id": attempt,
                        "job_id": job["id"],
                        "reservation_cents": need,
                    },
                )
                return {
                    "job": job,
                    "spec": spec,
                    "attempt_id": attempt,
                    "work_item": dict(root),
                    "snapshot": snapshot,
                    "inputs": inputs,
                    "prompt": snapshot["policy"]["assets"]
                    .get(role, {})
                    .get("body", ""),
                }, blocked
        return None, blocked

    @atomic
    def release_unstarted_claim(self, attempt_id, worker_id, reason):
        """Fence untouched local dispatch; never reset a started CLI or its costs."""
        attempt = self.store.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        if (
            not attempt
            or attempt["worker_id"] != worker_id
            or attempt["status"] != "leased"
            or attempt["workspace"] is not None
            or attempt["pid"] is not None
        ):
            return False
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (attempt["job_id"],))
        if job["status"] != "leased" or job["worker_id"] != worker_id:
            return False
        reservation = json.loads(job["spec_json"])["budget_cents"]
        self.store.execute(
            "UPDATE attempts SET status='not_started',ended_at=?,cost_cents=0,error=? WHERE id=?",
            (now_iso(), reason, attempt_id),
        )
        self.store.execute(
            "UPDATE jobs SET status='ready',worker_id=NULL,lease_until=NULL WHERE id=?",
            (job["id"],),
        )
        self.store.execute(
            "UPDATE work_items SET reserved_cents=MAX(reserved_cents-?,0) WHERE id=?",
            (reservation, job["work_item_id"]),
        )
        self.store.execute("UPDATE workers SET busy=0 WHERE id=?", (worker_id,))
        self.store.execute(
            "INSERT INTO costs(id,root_id,attempt_id,source,amount_cents,currency,status,created_at) VALUES (?,?,?,?,0,'USD','not_started',?)",
            (
                new_id("cost"),
                job["work_item_id"],
                attempt_id,
                "workflow-startup",
                now_iso(),
            ),
        )
        self.store.audit(
            worker_id,
            "attempt.not_started",
            {"attempt_id": attempt_id, "job_id": job["id"], "reason": reason},
        )
        return True

    @atomic
    def resume_claim(self, attempt_id, worker_id, capabilities):
        """Reacquire dispatch identity; active CLI work is never implicitly replayed."""
        attempt = self.store.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        if not attempt or attempt["worker_id"] != worker_id:
            return None
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (attempt["job_id"],))
        root = self.store.one(
            "SELECT * FROM work_items WHERE id=?", (job["work_item_id"],)
        )
        inputs = json.loads(attempt["inputs_json"])
        snapshot = json.loads(root["snapshot_json"])
        spec = json.loads(job["spec_json"])
        if attempt["status"] in {"leased", "running", "cancelling"}:
            if attempt["status"] != "leased" or attempt["pid"] or attempt["workspace"]:
                return None
            if not self.dispatch_allowed()[0] or root["status"] not in {
                "planning",
                "approved",
                "running",
            }:
                return None
            self._require_root_scope(root)
            if min(
                datetime.fromisoformat(attempt["deadline"]),
                datetime.fromisoformat(job["lease_until"]),
            ) <= datetime.now(UTC):
                return None
            if snapshot["policy"] != self._policy():
                raise ControllerError("resumed dispatch policy changed; replan")
            if job["kind"] not in {"plan", "critique"} and not self._approval_current(
                root
            ):
                raise ControllerError("resumed dispatch approval changed; replan")
            required = {"native"}
            if inputs.get("route"):
                required.add(inputs["route"]["harness"])
            if not required <= set(capabilities):
                return None
        role = spec.get("role")
        return {
            "job": dict(job),
            "spec": spec,
            "attempt_id": attempt_id,
            "work_item": dict(root),
            "snapshot": snapshot,
            "inputs": inputs,
            "prompt": snapshot["policy"]["assets"].get(role, {}).get("body", ""),
        }

    @atomic
    def heartbeat(self, worker_id, attempt_id=None):
        self.store.execute(
            "UPDATE workers SET last_heartbeat=? WHERE id=?", (now_iso(), worker_id)
        )
        if not attempt_id:
            return True
        attempt = self.store.one(
            "SELECT a.*,j.lease_until FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE a.id=? AND a.worker_id=?",
            (attempt_id, worker_id),
        )
        now = datetime.now(UTC)
        if (
            not attempt
            or attempt["status"] not in {"leased", "running"}
            or not attempt["lease_until"]
        ):
            return False
        deadline = datetime.fromisoformat(attempt["deadline"])
        if datetime.fromisoformat(attempt["lease_until"]) <= now or deadline <= now:
            return False
        self.store.execute(
            "UPDATE attempts SET heartbeat_at=? WHERE id=?", (now_iso(), attempt_id)
        )
        self.store.execute(
            "UPDATE jobs SET lease_until=? WHERE id=?",
            (
                min(
                    deadline,
                    now
                    + timedelta(
                        seconds=json.loads(attempt["inputs_json"]).get(
                            "lease_seconds", 30
                        )
                    ),
                ).isoformat(),
                attempt["job_id"],
            ),
        )
        return True

    @atomic
    def cancel_job(self, job_id, actor):
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job or job["status"] not in {"ready", "blocked", "leased", "running"}:
            raise ControllerError("job is not cancellable")
        active = job["status"] in {"leased", "running"}
        self.store.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            ("cancelling" if active else "cancelled", job_id),
        )
        self.store.execute(
            "UPDATE attempts SET status='cancelling' WHERE job_id=? AND status IN ('leased','running')",
            (job_id,),
        )
        self.store.execute(
            "UPDATE work_items SET status='cancelled' WHERE id=?",
            (job["work_item_id"],),
        )
        self._packet(
            job["work_item_id"],
            "operator cancellation requested; active process must stop before reservation release",
        )
        self.store.audit(actor, "cancel", {"job_id": job_id})

    @atomic
    def recover(self, actor="operator"):
        """Fence expired work; never automatically replace an interrupted attempt."""
        count = 0
        for attempt in self.store.query(
            "SELECT a.*,j.lease_until FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE a.status IN ('leased','running','cancelling')"
        ):
            if min(
                datetime.fromisoformat(attempt["deadline"]),
                datetime.fromisoformat(attempt["lease_until"]),
            ) > datetime.now(UTC):
                continue
            if (
                attempt["status"] == "leased"
                and attempt["worker_id"] in self.cfg.workers
                and not self.store.get_op("execution-start:" + attempt["id"], "")
            ):
                if self.release_unstarted_claim(
                    attempt["id"],
                    attempt["worker_id"],
                    "remote delivery expired before authorized execution",
                ):
                    count += 1
                    continue
            # An OS-supervised live child cannot be safely replaced merely on missing heartbeat.
            # Stop the recorded owned process group, then preserve workspace for inspection.

            if attempt["pid"] and attempt["pid_identity"]:
                if process_identity(attempt["pid"]) == attempt["pid_identity"]:
                    try:
                        if os.getpgid(attempt["pid"]) == attempt["pid"]:
                            os.killpg(attempt["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            reservation = json.loads(
                self.store.one(
                    "SELECT spec_json FROM jobs WHERE id=?", (attempt["job_id"],)
                )[0]
            )["budget_cents"]
            self.finish_attempt(
                attempt["id"],
                False,
                "interrupted attempt fenced after deadline",
                reservation,
                None,
                error="worker interrupted; workspace retained",
                timed_out=True,
            )
            count += 1
        # Periodic service recovery runs every second; record only real fencing.
        if count or actor == "operator":
            self.store.audit(actor, "recover", {"fenced_attempts": count})
        self.scrub_retired_credentials()
        self.reconcile_continuations()
        return count

    def scrub_retired_credentials(self):
        """Only adapter-declared credential files in retired, owned attempt runtimes."""
        for attempt in self.store.query(
            "SELECT * FROM attempts WHERE status NOT IN ('leased','running','cancelling')"
        ):
            route = json.loads(attempt["inputs_json"] or "{}").get("route")
            if route:
                removed = adapter_for(route["harness"]).scrub_credentials(
                    self.cfg.state_dir / "runtime" / attempt["id"]
                )
                if removed:
                    self.store.audit(
                        "controller",
                        "credentials.scrub",
                        {"attempt": attempt["id"], "files": removed},
                    )

    def cleanup(self, actor="operator") -> int:

        self.scrub_retired_credentials()
        count = 0
        for attempt in self.store.query(
            "SELECT * FROM attempts WHERE cleanup_status IN ('archived','cleanup_failed') AND status NOT IN ('leased','running','cancelling')"
        ):
            if not attempt["archive_path"] or not attempt["workspace"]:
                continue
            artifacts.verify(self.store.resolve_path(attempt["archive_path"]))
            release_attempt_environment(self.store, attempt)
            self.store.execute(
                "UPDATE attempts SET cleanup_status='cleaned' WHERE id=?",
                (attempt["id"],),
            )
            shutil.rmtree(
                self.cfg.state_dir / "runtime" / attempt["id"], ignore_errors=True
            )
            self.store.audit(actor, "cleanup.complete", {"attempt_id": attempt["id"]})
            count += 1
        return count
