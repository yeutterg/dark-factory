"""Intent-first external writes with reconciliation before retry."""

from __future__ import annotations

import json

from dark_factory.control.base import ControllerCore, ControllerError, _digest
from dark_factory.store import new_id, now_iso


class ExternalMixin(ControllerCore):
    """Record intent before a code-host write and reconcile ambiguity."""

    def external_action(self, wid, operation, payload, actor):
        """Record intent before a write; reconcile the same identity before retry."""
        if operation not in {"create_pr", "push_branch"}:
            raise ControllerError(
                "bootstrap supports only draft PR creation and the branch it needs"
            )
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
            if action["status"] == "abandoned":
                self.store.execute(
                    "UPDATE pending_actions SET status='ambiguous' WHERE id=?",
                    (identity,),
                )
            observed = self.runner.run_json(
                cmd.command,
                "reconcile_" + operation,
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

    def abandon_action(self, wid, reason, actor):
        """Operator exit for a pending or ambiguous external write on one root.

        The action is closed, never deleted. A later identical request finds
        it and reconciles with the code host before any retry, so abandoning
        cannot cause a duplicate write.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ControllerError("a reason is required")
        with self.store.transaction(), self.decision():
            action = self.unresolved_action(wid)
            if not action:
                raise ControllerError("no unresolved external action for this root")
            self.store.execute(
                "UPDATE pending_actions SET status='abandoned' WHERE id=?",
                (action["id"],),
            )
            self.store.audit(
                actor,
                "external.abandoned",
                {"id": action["id"], "root": wid, "reason": reason.strip()},
            )
        return {"abandoned": action["id"]}

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
        if evidence.get("merged") and evidence.get("merge_commit_sha"):
            self.store.execute(
                "UPDATE work_items SET merge_commit_sha=?,merged_at=? WHERE id=?",
                (
                    evidence["merge_commit_sha"],
                    evidence.get("merged_at") or now_iso(),
                    wid,
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
