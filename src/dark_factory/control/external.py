"""Intent-first external writes with reconciliation before retry."""

from __future__ import annotations

import json

from dark_factory.control.base import ControllerCore, ControllerError, _digest
from dark_factory.store import new_id, now_iso


class ExternalMixin(ControllerCore):
    """Record intent before a code-host write and reconcile ambiguity."""

    def external_action(self, wid, operation, payload, actor):
        """Record intent before a write; reconcile the same identity before retry."""
        if operation != "create_pr":
            raise ControllerError("bootstrap only supports draft PR creation")
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if not root or root["status"] != "awaiting_review":
            raise ControllerError("a checked and reviewed candidate is required")
        candidate = json.loads(root["candidate_commits"])[root["repo"]]
        if (
            payload.get("repo") != root["repo"]
            or payload.get("head_sha") != candidate["commit"]
        ):
            raise ControllerError("PR payload must name the exact reviewed candidate")
        if not self._approval_current(root):
            raise ControllerError("approved inputs changed")
        identity = _digest({"root": wid, "operation": operation, "payload": payload})
        action = self.store.one(
            "SELECT * FROM pending_actions WHERE idempotency_key=?", (identity,)
        )
        cmd = self.cfg.commands["code_host"]
        if action and action["status"] == "succeeded":
            return json.loads(action["payload_json"])["result"]
        if action:
            observed = self.runner.run_json(
                cmd.command,
                "reconcile_create_pr",
                payload,
                identity,
                cmd.timeout_seconds,
            )
            if not observed["ok"]:
                raise ControllerError("external action reconciliation failed")
            if observed["evidence"].get("observed"):
                result = observed
            elif observed["evidence"].get("safe_to_retry"):
                result = None
            else:
                raise ControllerError("external action remains ambiguous; no retry")
        else:
            self.store.execute(
                "INSERT INTO pending_actions(id,kind,idempotency_key,payload_json,status,created_at) VALUES (?,?,?,?,?,?)",
                (
                    identity,
                    operation,
                    identity,
                    json.dumps({"root": wid, "input": payload}),
                    "pending",
                    now_iso(),
                ),
            )
            self.store.audit(
                actor,
                "external.intent",
                {"id": identity, "operation": operation, "root": wid},
            )
            result = None
        if result is None:
            try:
                result = self.runner.run_json(
                    cmd.command, operation, payload, identity, cmd.timeout_seconds
                )
                if not result["ok"] or not result["evidence"].get("observed"):
                    raise ControllerError(
                        "external write did not return confirmed evidence"
                    )
            except Exception:
                self.store.execute(
                    "UPDATE pending_actions SET status='ambiguous' WHERE id=?",
                    (identity,),
                )
                raise
        self.store.execute(
            "UPDATE pending_actions SET status='succeeded',payload_json=? WHERE id=?",
            (json.dumps({"root": wid, "input": payload, "result": result}), identity),
        )
        self.store.audit(
            actor,
            "external.confirmed",
            {"id": identity, "evidence": result["evidence"]},
        )
        return result

    def _created_pr(self, wid, repo):
        """The draft PR this factory opened for the root, if any."""
        for action in self.store.query(
            "SELECT payload_json FROM pending_actions WHERE kind='create_pr' AND status='succeeded' ORDER BY created_at DESC"
        ):
            payload = json.loads(action["payload_json"])
            if payload.get("root") == wid and payload["input"].get("repo") == repo:
                return payload["result"]["evidence"].get("number")
        return None

    def read_pr_evidence(self, wid, number, actor):
        """Read a PR's checks and reviews; only the exact candidate's evidence counts."""
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if not root or root["status"] != "awaiting_review":
            raise ControllerError("work item is not awaiting review")
        candidate = json.loads(root["candidate_commits"])[root["repo"]]["commit"]
        if number is None:
            number = self._created_pr(wid, root["repo"])
        if type(number) is not int or number <= 0:
            raise ControllerError("name the pull request that holds this candidate")
        cmd = self.cfg.commands["code_host"]
        observed = self.runner.run_json(
            cmd.command,
            "inspect_pr",
            {"repo": root["repo"], "number": number},
            new_id("req"),
            cmd.timeout_seconds,
        )
        evidence = observed.get("evidence", {})
        if not observed["ok"] or not isinstance(evidence, dict):
            raise ControllerError("pull request could not be read")
        if evidence.get("head_sha") != candidate:
            raise ControllerError(
                "pull request head is not the reviewed candidate; push the exact "
                f"candidate {candidate[:12]} or replan"
            )
        for key in ("checks", "statuses", "reviews", "review_comments"):
            if not isinstance(evidence.get(key, []), list):
                raise ControllerError(f"pull request {key} must be a list")
        with self.store.transaction(), self.decision():
            return self._record_pr_evidence(wid, candidate, number, evidence, actor)

    def _record_pr_evidence(self, wid, candidate, number, evidence, actor):
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        current = json.loads(root["candidate_commits"]).get(root["repo"], {})
        if root["status"] != "awaiting_review" or current.get("commit") != candidate:
            raise ControllerError("the candidate changed while the PR was read")
        self.store.execute(
            "INSERT OR REPLACE INTO external_reviews(root_id,candidate,pr_number,url,actor,at,payload_json) VALUES (?,?,?,?,?,?,?)",
            (
                wid,
                candidate,
                number,
                str(evidence.get("url", "")),
                actor,
                now_iso(),
                json.dumps(evidence),
            ),
        )
        external = self._external(wid, root)
        self.store.audit(
            actor,
            "external.read",
            {
                "root": wid,
                "candidate": candidate,
                "pr": number,
                "required": external["required"],
            },
        )
        self._packet(wid)
        self.reconcile_continuations()
        return external
