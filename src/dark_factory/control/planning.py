"""Intake, proposals and the human approval decisions on them."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dark_factory.adapters.git.local import clone, fixture, pin
from dark_factory.control.base import ControllerCore, ControllerError, _digest, atomic
from dark_factory.control.graph import default_bootstrap_graph, validate_graph
from dark_factory.models import (
    PlanGraph,
)
from dark_factory.store import new_id, now_iso


class PlanningMixin(ControllerCore):
    """Select complete roots, plan them and record human decisions."""

    def discover_issues(self) -> dict[str, Any]:
        """Read bounded tracker candidates without selecting or mutating them."""
        label = self.cfg.labels.get("selected")
        if not label:
            raise ControllerError("selected tracker label is not configured")
        cmd = self.cfg.commands["tracking"]
        result = self.runner.run_json(
            cmd.command,
            "list_candidate_issues",
            {"repositories": self.cfg.repositories, "label": label, "limit": 50},
            new_id("req"),
            cmd.timeout_seconds,
        )
        if not result["ok"] or not isinstance(result.get("items"), list):
            raise ControllerError(result.get("error", "tracking returned no items"))
        return {"items": result["items"], "evidence": result["evidence"]}

    def intake_issues(
        self, issues: list[dict[str, Any]], actor="operator"
    ) -> list[str]:
        cmd = self.cfg.commands["tracking"]
        result = self.runner.run_json(
            cmd.command,
            "list_selected_issues",
            {"issues": issues},
            new_id("req"),
            cmd.timeout_seconds,
        )
        if not result["ok"] or not isinstance(result.get("items"), list):
            raise ControllerError(result.get("error", "tracking returned no items"))
        # Caller-supplied issue fields are labeled fixtures; they cannot establish
        # root scope when any route can spend or produce live evidence.
        fixtures = {
            observation.get("external_id")
            for observation in result.get("evidence", {}).get("observations", [])
            if isinstance(observation, dict)
            and observation.get("source") == "provided-fixture"
        }
        simulation_only = all(
            route.evidence_kind == "simulation" for route in self.cfg.routes.values()
        )
        ids = []
        with self.store.transaction():
            for item in result["items"]:
                if (
                    "parent_id" not in item
                    or item.get("parent_id") is not None
                    or item.get("is_root") is not True
                ):
                    raise ControllerError(
                        "select the root outcome, not a child issue; "
                        f"parent: {item.get('parent_id') or 'resolve in the tracker'}"
                    )
                child_count = item.get("required_child_count")
                if type(child_count) is not int or child_count < 0:
                    raise ControllerError(
                        "tracking result needs a non-negative required_child_count"
                    )
                if child_count:
                    raise ControllerError(
                        "bootstrap cannot execute roots with required child issues; "
                        "preserve the complete root for child-graph execution, do not select a child instead"
                    )
                if item.get("repo") not in self.cfg.repositories:
                    raise ControllerError("intake repository is not configured")
                if (
                    not isinstance(item.get("external_id"), str)
                    or not item["external_id"]
                ):
                    raise ControllerError(
                        "tracking result needs a provider-qualified external_id"
                    )
                if item["external_id"] in fixtures and not simulation_only:
                    raise ControllerError(
                        "provided issue fixtures cannot establish root scope for live "
                        "routes; select the issue by repository and number"
                    )
                existing = self.store.one(
                    "SELECT id FROM work_items WHERE external_id=?",
                    (item["external_id"],),
                )
                if existing:
                    ids.append(existing["id"])
                    continue
                wid = new_id("wi")
                self.store.execute(
                    """INSERT INTO work_items
                    (id,external_id,url,title,body,parent_id,repo,status,is_root,iteration,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        wid,
                        item["external_id"],
                        item.get("url", ""),
                        item.get("title", ""),
                        item.get("body", ""),
                        item.get("parent_id"),
                        item["repo"],
                        "selected",
                        int(item.get("is_root", True)),
                        item.get("iteration"),
                        now_iso(),
                    ),
                )
                ids.append(wid)
            self.store.audit(
                actor, "intake", {"ids": ids, "evidence": result.get("evidence", {})}
            )
        return ids

    def _require_root_scope(self, row):
        if row["parent_id"] is not None or row["is_root"] != 1:
            raise ControllerError("select the root outcome, not a child issue")
        if self.store.one(
            "SELECT id FROM work_items WHERE parent_id IN (?,?) LIMIT 1",
            (row["id"], row["external_id"]),
        ):
            raise ControllerError(
                "bootstrap cannot execute roots with required child issues; preserve the complete root"
            )

    @atomic
    def start_planning(self, work_item_id: str, actor="operator") -> PlanGraph:

        row = self.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
        if not row:
            raise ControllerError("work item not found")
        self._require_root_scope(row)
        if self.store.one(
            "SELECT a.id FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.work_item_id=? AND a.status IN ('leased','running')",
            (work_item_id,),
        ):
            raise ControllerError("stop active attempts before replanning")
        if row["status"] not in {
            "selected",
            "failed",
            "rejected",
            "cancelled",
            "awaiting_approval",
        }:
            raise ControllerError("work item is not eligible for a new proposal")
        if row["reserved_cents"]:
            raise ControllerError(
                "reconcile outstanding reservations before replanning"
            )
        source = self.cfg.sources.get(row["repo"])
        simulation = any(
            route.evidence_kind == "simulation" for route in self.cfg.routes.values()
        )
        if source is None:
            if not simulation:
                raise ControllerError("source repository configuration required")
            source = fixture(self.cfg.state_dir)
        version = row["plan_version"] + 1
        base = pin(Path(source["path"]), source["revision"])
        inputs = self.cfg.state_dir / "inputs" / work_item_id / str(version)
        inputs.mkdir(parents=True, exist_ok=False)
        clone(Path(source["path"]), inputs / "repo", base)
        bindings = self._bindings()
        if simulation and source["checks"] == ["fixture-check"]:
            bindings["fixture-check"] = {
                "argv": [sys.executable, "verify.py"],
                "files": {},
                "timeout_seconds": 30,
            }
        ceiling = self.cfg.budgets["root_cents"]
        graph = default_bootstrap_graph(
            work_item_id,
            row["repo"],
            ceiling,
            list(self.cfg.routes),
            self.cfg.max_repairs,
        )
        for job in graph.jobs:
            if job.kind == "implement":
                job.check_names = list(source["checks"])
            if job.kind == "check":
                job.check_names = list(source["checks"])
        validate_graph(graph, self.cfg)
        snapshot = {
            "revision_feedback": [
                {
                    "at": event["at"],
                    "actor": event["actor"],
                    "decision": json.loads(event["payload_json"]),
                }
                for event in self.store.query(
                    "SELECT * FROM audit WHERE action='reject' AND json_extract(payload_json,'$.work_item_id')=? ORDER BY at,id",
                    (work_item_id,),
                )
            ]
            + [
                {"attempt": attempt["id"], "findings": attempt["error"]}
                for attempt in self.store.query(
                    "SELECT a.id,a.error FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.work_item_id=? AND j.kind IN ('critique','review') AND a.status='failed' ORDER BY a.started_at,a.id",
                    (work_item_id,),
                )
            ],
            "source": dict(source),
            "base": base,
            "input_repo": str(inputs / "repo"),
            "bindings": bindings,
            "policy": self._policy(),
            "simulation": simulation,
            "manual_acceptance": source.get("manual_acceptance", []),
            "external_acceptance": source.get("external_acceptance", []),
            "deadline": (
                datetime.now(UTC) + timedelta(seconds=self.cfg.max_seconds)
            ).isoformat(),
        }
        self.store.execute(
            "UPDATE jobs SET status='superseded' WHERE work_item_id=? AND status IN ('blocked','ready','queued')",
            (work_item_id,),
        )
        for index, spec in enumerate(graph.jobs):
            self.store.execute(
                """INSERT INTO jobs(id,work_item_id,kind,status,spec_json,depends_on,
                reserved_cents,plan_version,logical_id,position)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"{index}_{spec.job_id}_{work_item_id}_v{version}",
                    work_item_id,
                    spec.kind,
                    "standby"
                    if spec.repair
                    else "ready"
                    if spec.kind in {"plan", "critique"}
                    else "blocked",
                    json.dumps(
                        {**vars(spec), "logical_id": spec.job_id, "version": version}
                    ),
                    json.dumps(spec.depends_on),
                    spec.budget_cents,
                    version,
                    spec.job_id,
                    index,
                ),
            )
        packet = {
            "kind": "plan",
            "simulation": simulation,
            "selected_work": {"title": row["title"], "body": row["body"]},
            "repos": [row["repo"]],
            "base_commit": base,
            "acceptance": graph.acceptance,
            "spending_ceiling_cents": ceiling,
            "permitted_routes": list(self.cfg.routes),
            "commands": bindings,
            "allowed_paths": source["allowed_paths"],
            "protected_paths": source.get("protected_paths", []),
            "manual_acceptance": snapshot["manual_acceptance"],
            "external_acceptance": snapshot["external_acceptance"],
            "repair_policy": {
                "max_repairs": self.cfg.max_repairs,
                "shared_scope_budget_deadline": True,
            },
            "decision": "Review the plan and critique, then approve this exact proposal.",
        }
        self.store.execute(
            """UPDATE work_items SET status='planning',plan_version=?,spending_ceiling_cents=?,
            snapshot_json=?,plan_json=?,packet_json=?,critique_json=NULL,approved_digest=NULL,approved_by=NULL,
            approved_at=NULL,candidate_commits='{}',presentation_stage='Plan' WHERE id=?""",
            (
                version,
                ceiling,
                json.dumps(snapshot),
                json.dumps(packet),
                json.dumps(packet),
                work_item_id,
            ),
        )
        self.store.execute(
            "INSERT INTO proposals(root_id,version,payload_json) VALUES (?,?,?)",
            (work_item_id, version, json.dumps(packet)),
        )
        self.store.audit(
            actor,
            "plan.start",
            {"work_item_id": work_item_id, "version": version, "base": base},
        )
        return graph

    @atomic
    def approve(self, work_item_id, actor):
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
        if not row or row["status"] != "awaiting_approval":
            raise ControllerError("cannot approve before planning and critique succeed")
        self._require_root_scope(row)
        self._require_human(actor, row)
        if any(
            j["status"] != "succeeded"
            for j in self.jobs(work_item_id)
            if j["kind"] in {"plan", "critique"}
        ):
            raise ControllerError("planning and critique must succeed before approval")
        if not json.loads(row["critique_json"])["passed"]:
            raise ControllerError("blocking critique requires a new proposal")
        snapshot = json.loads(row["snapshot_json"])
        if snapshot["policy"] != self._policy():
            raise ControllerError("planning inputs changed; replan before approval")
        if snapshot["source"].get("require_live_evidence") and snapshot["simulation"]:
            raise ControllerError(
                "live evidence required; simulation cannot qualify this outcome"
            )
        payload = self._approval_payload(work_item_id)
        digest = _digest(payload)
        self.store.execute(
            "UPDATE work_items SET status='approved',approved_digest=?,approved_by=?,approved_at=? WHERE id=?",
            (digest, actor, now_iso(), work_item_id),
        )
        self.store.execute(
            "UPDATE proposals SET payload_json=?,approved_digest=?,actor=?,approved_at=? WHERE root_id=? AND version=?",
            (
                json.dumps(payload),
                digest,
                actor,
                now_iso(),
                work_item_id,
                row["plan_version"],
            ),
        )
        for job in self.jobs(work_item_id):
            if job["status"] == "blocked":
                self.store.execute(
                    "UPDATE jobs SET status='ready' WHERE id=?", (job["id"],)
                )
        self.store.audit(
            actor, "approve", {"work_item_id": work_item_id, "digest": digest}
        )
        self.reconcile_continuations()
        return digest

    @atomic
    def reject(self, work_item_id, actor, reason):
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
        if not row or row["status"] not in {"awaiting_approval", "awaiting_review"}:
            raise ControllerError(
                "only a pending proposal or reviewed candidate can be rejected"
            )
        self._require_human(actor, row)
        if not isinstance(reason, str) or not reason.strip():
            raise ControllerError("rejection reason is required")
        self.store.execute(
            "UPDATE work_items SET status='rejected' WHERE id=?", (work_item_id,)
        )
        self.store.audit(
            actor, "reject", {"work_item_id": work_item_id, "reason": reason}
        )
        if row["status"] == "awaiting_review":
            self._packet(work_item_id, reason)
        self.reconcile_continuations()

    def _require_human(self, actor, row):
        if not isinstance(actor, str) or not actor.strip() or actor != actor.strip():
            raise ControllerError("named human identity is required")
        if actor.lower().startswith(("agent", "worker", "model")) or self.store.one(
            "SELECT id FROM workers WHERE id=?", (actor,)
        ):
            raise ControllerError("worker identities cannot make human decisions")
        humans = self.cfg.humans
        simulation = json.loads(row["snapshot_json"])["simulation"]
        if (humans or not simulation) and actor not in humans:
            raise ControllerError(
                "human identity is not configured in operators.humans"
            )

    def proposal(self, wid, version=None):
        if version is None:
            root = self.store.one(
                "SELECT plan_version FROM work_items WHERE id=?", (wid,)
            )
            if not root:
                raise ControllerError("work item not found")
            version = root["plan_version"]
        row = self.store.one(
            "SELECT payload_json FROM proposals WHERE root_id=? AND version=?",
            (wid, version),
        )
        if not row:
            raise ControllerError("proposal version not found")
        return json.loads(row["payload_json"])
