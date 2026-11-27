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

    def dispatch_allowed(self):
        if self.store.get_op("pause") == "on":
            return False, "paused"
        if self.store.get_op("drain") == "on":
            return False, "draining"
        if not window_open(self.cfg):
            return False, "window_closed"
        blocker = self.disk_blocker()
        if blocker:
            return False, blocker
        return True, "ok"

    def _budget_blocker(self, root, job, spec, route):
        """The cap that stops this step from being dispatched, or None.

        One rule for dispatch and for the operator's explanation of why work waits.
        """
        need = spec["budget_cents"]
        if route and route.max_cents and need > route.max_cents:
            return f"the step needs {need}¢ but its route allows {route.max_cents}¢"
        if (
            root["spent_cents"] + root["reserved_cents"] + need
            > root["spending_ceiling_cents"]
        ):
            return "remaining root allowance cannot fund the next required task"
        if root["origin"].startswith("improvement:"):
            # A dedicated allowance, inside the account allowance checked below.
            used = self.store.one(
                "SELECT COALESCE(SUM(spent_cents+reserved_cents),0) n FROM work_items "
                "WHERE origin LIKE 'improvement:%'"
            )["n"]
            if used + need > self.cfg.improvement["budget_cents"]:
                return "the improvement allowance cannot fund the next step"
        total = self.store.one(
            "SELECT COALESCE(SUM(spent_cents+reserved_cents),0) n FROM work_items"
        )["n"]
        if total + need > self.cfg.budgets["account_cents"]:
            return "the account allowance cannot fund the next required task"
        if job["kind"] in {"plan", "critique"}:
            spent = self.store.one(
                "SELECT COALESCE(SUM(a.cost_cents),0) n FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.work_item_id=? AND j.kind IN ('plan','critique')",
                (root["id"],),
            )["n"]
            if spent + need > self.cfg.budgets["planning_cents"]:
                return "the planning allowance cannot fund another planning step"
        return None

    def _local_runner_blocker(self, worker_id):
        """A machine with no spare capacity does not take another stage."""
        from dark_factory.runners import capacity_blocker

        running = self.store.one(
            f"SELECT COUNT(*) n FROM attempts WHERE worker_id=? AND status IN {sql_set(ACTIVE_ATTEMPT)}",
            (worker_id,),
        )["n"]
        return capacity_blocker(self.cfg.runners["local"], running, self.cfg.state_dir)

    def unresolved_action(self, wid):
        """The pending or ambiguous external action that holds this root, if any."""
        return self.store.one(
            "SELECT id FROM pending_actions WHERE status IN ('pending','ambiguous') "
            "AND json_extract(payload_json,'$.root')=?",
            (wid,),
        )

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
        blocked: list[str] = []
        if not self.dispatch_allowed()[0]:
            return None, blocked
        if self.store.one(
            f"SELECT id FROM attempts WHERE status IN {sql_set(ACTIVE_ATTEMPT)}"
        ):
            return None, blocked
        for root in self.store.query(
            f"SELECT * FROM work_items WHERE status IN {sql_set(DISPATCHABLE_ROOT)} ORDER BY created_at,rowid"
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
            if self.unresolved_action(root["id"]):
                # Held by its own unfinished external write; other roots run.
                continue
            if root["candidate_commits"] != "{}":
                try:
                    self.verify_candidate(root)
                except (ControllerError, OSError, ValueError, KeyError) as exc:
                    self._fail_root(
                        root["id"], f"candidate evidence is not intact: {exc}"
                    )
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
                    required = {"native", route.harness}
                else:
                    required = {"native"}
                environment = (
                    route.environment
                    if route
                    else self.cfg.routes[self.cfg.role_routes["coder"]].environment
                )
                required.add("environment:" + environment)
                if job["kind"] == "check":
                    pipeline = snapshot.get("pipeline") or {}
                    wanted = {
                        "label:" + label
                        for stage in pipeline.get("change", {}).values()
                        for label in stage["labels"]
                    }
                    required |= wanted
                    if pipeline and self._local_runner_blocker(worker_id):
                        continue
                external = self._external_dispatch(root, job, spec)
                if external == "wait":
                    continue
                if external:
                    # The agent is outside; no harness binary is needed to run its work.
                    required.discard(route.harness if route else "")
                if not required.issubset(set(capabilities)):
                    continue
                need = spec["budget_cents"]
                if self._budget_blocker(root, job, spec, route):
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
                    "environment": self.cfg.environments.get(environment),
                }
                if external:
                    inputs["external"] = external
                if external:
                    self._external_consume(root["id"], spec)
                # A resumed step continues the interrupted attempt's saved work.
                resume = self._resume_input(job["id"], attempt)
                if resume:
                    inputs["resume"] = resume
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
                    """UPDATE task_instructions SET attempt_id=?
                    WHERE root_id=? AND job_id=? AND attempt_id IS NULL AND state='queued'""",
                    (attempt, root["id"], job["id"]),
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
                    "task_list": self.tasks_view(root["id"]),
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
        # An unstarted claim never used the saved work it was given; the next
        # claim of this step must still find it.
        self.store.execute(
            "UPDATE interruptions SET resumed_by_attempt=NULL WHERE resumed_by_attempt=?",
            (attempt_id,),
        )
        self.store.execute(
            "INSERT INTO costs(id,root_id,attempt_id,source,amount_cents,currency,status,created_at) VALUES (?,?,?,?,0,'USD','not_started',?)",
            (
                new_id("cost"),
                job["work_item_id"],
                attempt_id,
                "unstarted-claim",
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
        if attempt["status"] in ACTIVE_ATTEMPT:
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
        if not job or job["status"] not in {
            "ready",
            "blocked",
            "leased",
            "running",
            "interrupted",
        }:
            raise ControllerError("job is not cancellable")
        active = job["status"] in {"leased", "running"}
        self.store.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            ("cancelling" if active else "cancelled", job_id),
        )
        # Cancelling stops the whole root: every live attempt of it is signalled,
        # not only the one the operator named.
        for other in self.store.query(
            "SELECT a.id,a.job_id FROM attempts a JOIN jobs j ON a.job_id=j.id "
            "WHERE j.work_item_id=? AND a.status IN ('leased','running')",
            (job["work_item_id"],),
        ):
            self.store.execute(
                "UPDATE attempts SET status='cancelling' WHERE id=?", (other["id"],)
            )
            self.store.execute(
                "UPDATE jobs SET status='cancelling' WHERE id=?", (other["job_id"],)
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
        self.release_orphaned_supervisor_calls(actor)
        try:
            self.sweep_if_due()
        except Exception as exc:  # Cleanup must never stop recovery.
            self.store.audit("controller", "resources.error", {"error": str(exc)[:500]})
        try:
            self.gate_tick()
        except Exception as exc:  # The gate must never stop recovery.
            self.store.audit("controller", "gate.error", {"error": str(exc)[:500]})
        try:
            self.external_tick()
        except Exception as exc:  # Publishing must never stop recovery.
            self.store.audit("controller", "external.error", {"error": str(exc)[:500]})
        try:
            self.improvement_tick()
        except Exception as exc:  # The loop must never stop recovery.
            self.store.audit(
                "controller", "improvement.error", {"error": str(exc)[:500]}
            )
        self.scrub_retired_credentials()
        self.reconcile_continuations()
        return count

    def stop_active_processes(self, reason, grace_seconds=5.0):
        """Stop the process groups of running attempts without cancelling their roots.

        Used when the controller itself is stopping. The attempts are left active
        in the records: after a restart, recovery fences them as interrupted and
        they continue from their saved work within the approved limits.
        """
        import time

        stopped = []
        for attempt in self.store.query(
            "SELECT id,pid,pid_identity FROM attempts WHERE status IN ('leased','running') "
            "AND pid IS NOT NULL AND pid_identity IS NOT NULL"
        ):
            if process_identity(attempt["pid"]) != attempt["pid_identity"]:
                continue
            try:
                if os.getpgid(attempt["pid"]) == attempt["pid"]:
                    os.killpg(attempt["pid"], signal.SIGTERM)
                    stopped.append(attempt)
            except ProcessLookupError:
                pass
        end = time.monotonic() + grace_seconds
        while stopped and time.monotonic() < end:
            stopped = [
                a for a in stopped if process_identity(a["pid"]) == a["pid_identity"]
            ]
            time.sleep(0.05)
        for attempt in stopped:
            try:
                os.killpg(attempt["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.store.audit(
            "controller",
            "shutdown",
            {"reason": reason, "stopped_attempts": [a["id"] for a in stopped]},
        )

    def scrub_retired_credentials(self):
        """Only adapter-declared credential files in retired, owned attempt runtimes.

        Incremental: an attempt is scrubbed once it has retired, then marked, so the
        steady-state cost is one cheap query rather than a walk of every attempt.
        """
        for attempt in self.store.query(
            "SELECT id,inputs_json FROM attempts WHERE credentials_scrubbed=0 "
            "AND status NOT IN ('leased','running','cancelling') LIMIT 200"
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
            self.store.execute(
                "UPDATE attempts SET credentials_scrubbed=1 WHERE id=?",
                (attempt["id"],),
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
