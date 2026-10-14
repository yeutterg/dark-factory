"""Candidate-bound evidence, continuation views and human acceptance."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from dark_factory import artifacts
from dark_factory.adapters.git.local import pin
from dark_factory.continuation import describe
from dark_factory.control.base import ControllerError, atomic, decided
from dark_factory.models import (
    TERMINAL_ROOT,
    sql_set,
)
from dark_factory.store import now_iso


class ReviewMixin:
    """Compute completion from candidate-bound facts and record acceptance."""

    def _packet(self, wid, error=None):
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        records = []
        for job in self.jobs(wid):
            for attempt in self.store.query(
                "SELECT * FROM attempts WHERE job_id=?", (job["id"],)
            ):
                records.append(
                    {
                        "kind": job["kind"],
                        "attempt": attempt["id"],
                        "status": attempt["status"],
                        "archive": attempt["archive_path"],
                        "workspace": attempt["workspace"]
                        if attempt["cleanup_status"] != "cleaned"
                        else None,
                        "cost_cents": attempt["cost_cents"],
                        "error": attempt["error"],
                        "cleanup": attempt["cleanup_status"],
                        "recovery_runtime": str(
                            self.cfg.state_dir / "runtime" / attempt["id"]
                        )
                        if attempt["cleanup_status"] == "archive_failed"
                        else None,
                    }
                )
        snapshot = json.loads(row["snapshot_json"])
        plan = json.loads(row["plan_json"])
        candidate_map = json.loads(row["candidate_commits"])
        candidate_sha = candidate_map.get(row["repo"], {}).get("commit")
        completed_gates = {
            gate["name"]
            for gate in self.store.query(
                "SELECT name FROM manual_gates WHERE root_id=? AND candidate=? AND passed=1",
                (wid, candidate_sha),
            )
        }
        for candidate in candidate_map.values():
            candidate["archive"] = str(self.store.resolve_path(candidate["archive"]))
            candidate["diff"] = str(Path(candidate["archive"]) / "diff.patch")
        assumptions = list(plan.get("proposal", {}).get("assumptions", []))
        implementation_assumptions_recorded = False
        if candidate_sha:
            result_path = Path(candidate_map[row["repo"]]["archive"]) / "result.json"
            if result_path.is_file():
                implementation_result = json.loads(result_path.read_text())
                assumptions += implementation_result.get("assumptions", [])
                implementation_assumptions_recorded = implementation_result.get(
                    "assumptions_recorded", False
                )
        packet = {
            "kind": "result",
            "simulation": snapshot["simulation"],
            "ready_to_merge": False,
            "status": row["status"],
            "exec_summary": {
                "outcome": row["status"],
                "changes": plan.get("proposal", {}).get("what", row["title"]),
                "assumptions": list(dict.fromkeys(assumptions)),
                "implementation_assumptions_recorded": implementation_assumptions_recorded,
                "repairs": [
                    dict(a)
                    for a in self.store.query(
                        "SELECT at,payload_json FROM audit WHERE action='repair.activate' AND json_extract(payload_json,'$.root')=? ORDER BY at,id",
                        (wid,),
                    )
                ],
                "candidate": candidate_map,
                "next_action": self.continuation(wid)["next_action"],
                "remaining_manual_steps": [
                    name
                    for name in snapshot["manual_acceptance"]
                    if name not in completed_gates
                ],
            },
            "What changed": row["title"],
            "What to review": "Exact candidate diff, canonical checks and fresh independent review in the archives.",
            "What to test": [
                {
                    "n": i + 1,
                    "setup": f"Use a separate checkout of candidate {candidate_sha}; execution profile: {self.cfg.execution.get('profile', 'native')}. See the verified implementation archive.",
                    "action": step,
                    "expected": plan.get("proposal", {}).get("acceptance", []),
                }
                for i, step in enumerate(
                    plan.get("proposal", {}).get("test_instructions", [])
                )
            ],
            "approved_plan": {
                "root": wid,
                "version": row["plan_version"],
                "digest": row["approved_digest"],
                "command": [
                    "dark-factory",
                    "--config",
                    str(self.cfg.source_path),
                    "proposal",
                    wid,
                    "--version",
                    str(row["plan_version"]),
                ],
            },
            "candidate": candidate_map,
            "acceptance_coverage": self.coverage(wid),
            "continuation": self.continuation(wid),
            "ready_for_human_review": self.continuation(wid)["next_action"]["kind"]
            == "human_review",
            "manual_attestations": [
                dict(gate)
                for gate in self.store.query(
                    "SELECT name,actor,at,candidate,passed,evidence FROM manual_gates WHERE root_id=? AND candidate=? ORDER BY name",
                    (wid, candidate_sha),
                )
            ],
            "attempts": records,
            "spent_cents": row["spent_cents"],
            "reserved_cents": row["reserved_cents"],
            "manual_remaining": [
                name
                for name in snapshot["manual_acceptance"]
                if name not in completed_gates
            ],
            "missing_evidence": [
                missing
                for criterion in self.coverage(wid)
                for missing in criterion["missing_evidence"]
            ]
            + [
                "manual:" + name
                for name in snapshot["manual_acceptance"]
                if name not in completed_gates
            ],
            "error": error,
            "recommended_next_step": "Accepted candidate; retain the review and recovery artifacts."
            if row["status"] == "accepted"
            else "Review exact candidate and record manual gates; merge remains a human action."
            if row["status"] == "awaiting_review"
            else "Inspect saved work and failure evidence, reconcile cleanup, then replan.",
        }
        self.store.execute(
            "UPDATE work_items SET packet_json=? WHERE id=?", (json.dumps(packet), wid)
        )
        return packet

    @decided
    def coverage(self, wid):
        """Compute completion from candidate-bound facts, never a model's overall verdict."""
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        candidate = json.loads(row["candidate_commits"])
        proposal = json.loads(row["plan_json"]).get("proposal", {})
        snapshot = json.loads(row["snapshot_json"])
        checks, assessments = set(), {}
        for job in self.jobs(wid):
            if job["kind"] not in {"check", "review"} or job["status"] != "succeeded":
                continue
            attempt = self.store.one(
                "SELECT * FROM attempts WHERE job_id=? AND status='succeeded'",
                (job["id"],),
            )
            if (
                not attempt
                or json.loads(attempt["inputs_json"])["candidate"] != candidate
            ):
                continue
            result = json.loads(attempt["result_json"])
            if job["kind"] == "check":
                sha = candidate.get(row["repo"], {}).get("commit")
                checks.update(
                    c["command"]
                    for c in result.get("checks", [])
                    if c.get("exit_code") == 0 and c.get("candidate") == sha
                )
            else:
                evidence = self.store.one(
                    "SELECT payload_json FROM evidence WHERE attempt_id=?",
                    (attempt["id"],),
                )
                if evidence:
                    try:
                        review = self._parse_role(
                            "review",
                            json.loads(evidence[0])["output"],
                            snapshot,
                            proposal,
                        )
                        if review["passed"]:
                            assessments = {c["id"]: c for c in review["criteria"]}
                    except (ValueError, KeyError, TypeError):
                        pass
        sha = candidate.get(row["repo"], {}).get("commit")
        manual = {
            g["name"]
            for g in self.store.query(
                "SELECT name FROM manual_gates WHERE root_id=? AND candidate=? AND passed=1",
                (wid, sha),
            )
        }
        coverage = []
        for criterion in proposal.get("acceptance", []):
            missing = ["check:" + c for c in criterion["checks"] if c not in checks]
            missing += [
                "manual:" + g for g in criterion["manual_gates"] if g not in manual
            ]
            if criterion["id"] not in assessments:
                missing.append("independent-review:" + criterion["id"])
            coverage.append(
                {
                    **criterion,
                    "status": "verified" if not missing else "incomplete",
                    "missing_evidence": missing,
                }
            )
        return coverage

    @decided
    def continuation(self, wid):

        row = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if not row:
            raise ControllerError("work item not found")
        allowed, reason = self.dispatch_allowed()
        automated_work_pending = row["status"] in {"planning", "approved", "running"}
        if allowed or not automated_work_pending:
            reason = ""
        if row["snapshot_json"]:
            snapshot = json.loads(row["snapshot_json"])
            if row["approved_digest"] and not self._approval_current(row):
                reason = "approved inputs changed; a revised proposal is required"
            elif automated_work_pending and datetime.fromisoformat(
                snapshot["deadline"]
            ) <= datetime.now(timezone.utc):
                reason = "root deadline expired"
            elif automated_work_pending and (
                row["spent_cents"] + row["reserved_cents"]
                >= row["spending_ceiling_cents"]
            ):
                reason = "root allowance exhausted"
        if (
            automated_work_pending
            and not reason
            and self.store.get_op("workflow_unavailable", "")
        ):
            reason = "Workflow runtime unavailable: " + self.store.get_op(
                "workflow_unavailable", ""
            )
        if not reason and row["status"] in {"approved", "running"}:
            succeeded = {
                j["logical_id"] for j in self.jobs(wid) if j["status"] == "succeeded"
            }
            eligible = [
                j
                for j in self.required_jobs(wid)
                if j["status"] == "ready"
                and set(json.loads(j["depends_on"])) <= succeeded
            ]
            if (
                eligible
                and min(json.loads(j["spec_json"])["budget_cents"] for j in eligible)
                + row["spent_cents"]
                + row["reserved_cents"]
                > row["spending_ceiling_cents"]
            ):
                reason = "remaining root allowance cannot fund the next required task"
        view = describe(
            row,
            self.required_jobs(wid),
            self.coverage(wid) if row["snapshot_json"] else [],
            reason if (reason or not allowed) else "",
        )
        if reason and row["status"] == "awaiting_review":
            view["next_action"] = {
                "kind": "blocked",
                "reason": reason,
                "next_step": "Revalidate the exact candidate and approved authority.",
            }
        return view

    @atomic
    def reconcile_continuations(self):
        """Repair dropped status handoffs, never create new work or retry failed attempts."""
        # A finished root's view cannot change once it has been persisted at that status.
        for row in self.store.query(
            f"""SELECT w.* FROM work_items w LEFT JOIN ops o ON o.key='continuation:'||w.id
            WHERE w.status NOT IN {sql_set(TERMINAL_ROOT)}
            OR o.value IS NULL OR json_extract(o.value,'$.root_status') IS NOT w.status
            ORDER BY w.created_at,w.id"""
        ):
            if row["status"] in {"approved", "running"} and self._approval_current(row):
                jobs = self.jobs(row["id"])
                succeeded = {
                    j["logical_id"] for j in jobs if j["status"] == "succeeded"
                }
                for job in jobs:
                    if (
                        job["status"] == "blocked"
                        and set(json.loads(job["depends_on"])) <= succeeded
                    ):
                        self.store.execute(
                            "UPDATE jobs SET status='ready' WHERE id=?", (job["id"],)
                        )
                        self.store.audit(
                            "controller",
                            "continuation.reconcile",
                            {"root": row["id"], "job": job["id"]},
                        )
            view = {**self.continuation(row["id"]), "root_status": row["status"]}
            value = json.dumps(view, sort_keys=True)
            key = "continuation:" + row["id"]
            if self.store.get_op(key, "") != value:
                self.store.set_op(key, value, "controller")

    @atomic
    def record_manual_gate(self, wid, name, candidate, actor, evidence, passed=True):
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if not row or row["status"] != "awaiting_review":
            raise ControllerError("work item is not awaiting review")
        snapshot = json.loads(row["snapshot_json"])
        commits = json.loads(row["candidate_commits"])
        if (
            name not in snapshot["manual_acceptance"]
            or candidate != commits[row["repo"]]["commit"]
        ):
            raise ControllerError("manual gate or candidate mismatch")
        self._require_human(actor, row)
        if (
            not isinstance(evidence, str)
            or not evidence.strip()
            or type(passed) is not bool
        ):
            raise ControllerError("evidence and a boolean decision required")
        self.store.execute(
            "INSERT OR REPLACE INTO manual_gates(root_id,candidate,name,actor,at,passed,evidence) VALUES (?,?,?,?,?,?,?)",
            (wid, candidate, name, actor, now_iso(), int(passed), evidence),
        )
        self.store.audit(
            actor,
            "manual_gate",
            {"root": wid, "candidate": candidate, "name": name, "passed": passed},
        )
        self._packet(wid)
        self.reconcile_continuations()

    @atomic
    def accept(self, work_item_id, actor):

        row = self.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
        if not row or row["status"] != "awaiting_review":
            raise ControllerError("work item is not awaiting review")
        self._require_root_scope(row)
        if not self._approval_current(row):
            raise ControllerError("approved inputs changed; cannot accept")
        if any(j["status"] != "succeeded" for j in self.required_jobs(work_item_id)):
            raise ControllerError("required jobs have not succeeded")
        candidate = json.loads(row["candidate_commits"])[row["repo"]]
        snapshot = json.loads(row["snapshot_json"])
        if not snapshot["simulation"]:
            current = pin(
                Path(snapshot["source"]["path"]), snapshot["source"]["revision"]
            )
            if current not in {snapshot["base"], candidate["commit"]}:
                raise ControllerError(
                    "source target changed; revalidate against the new target before acceptance"
                )
        for job in self.required_jobs(work_item_id):
            att = self.store.one(
                "SELECT * FROM attempts WHERE job_id=? AND status='succeeded'",
                (job["id"],),
            )
            artifacts.verify(self.store.resolve_path(att["archive_path"]))
            inputs = json.loads(att["inputs_json"])
            if job["kind"] in {"check", "review"} and inputs["candidate"] != json.loads(
                row["candidate_commits"]
            ):
                raise ControllerError("candidate evidence is stale")
        for name in json.loads(row["snapshot_json"])["manual_acceptance"]:
            gate = self.store.one(
                "SELECT passed FROM manual_gates WHERE root_id=? AND candidate=? AND name=?",
                (work_item_id, candidate["commit"], name),
            )
            if not gate or not gate["passed"]:
                raise ControllerError(f"manual gate missing: {name}")
        coverage = self.coverage(work_item_id)
        if not coverage or any(c["status"] != "verified" for c in coverage):
            raise ControllerError("approved acceptance criteria remain incomplete")
        if snapshot["source"].get("require_live_evidence") and snapshot["simulation"]:
            raise ControllerError(
                "live evidence required; simulation cannot qualify this outcome"
            )
        self._require_human(actor, row)
        self.store.execute(
            "UPDATE work_items SET status='accepted',presentation_stage='Done',accepted_at=? WHERE id=?",
            (now_iso(), work_item_id),
        )
        self._packet(work_item_id)
        self.store.audit(
            actor,
            "accept",
            {"work_item_id": work_item_id, "candidate": candidate["commit"]},
        )

    def review_packet(self, wid):
        row = self.store.one("SELECT packet_json FROM work_items WHERE id=?", (wid,))
        if not row:
            raise ControllerError("work item not found")
        return json.loads(row["packet_json"] or "{}")

    @decided
    def status(self):
        allowed, reason = self.dispatch_allowed()
        return {
            "project": self.cfg.project_name,
            "dispatch": {
                "allowed": allowed,
                "reason": reason,
                "pause": self.store.get_op("pause"),
                "drain": self.store.get_op("drain"),
            },
            "work_items": [
                dict(r)
                for r in self.store.query(
                    "SELECT id,title,status,presentation_stage,spent_cents,reserved_cents FROM work_items"
                )
            ],
            "jobs": [
                dict(r)
                for r in self.store.query(
                    "SELECT id,work_item_id,kind,status FROM jobs"
                )
            ],
            "continuations": [
                self.continuation(r["id"])
                for r in self.store.query("SELECT id FROM work_items")
            ],
        }
