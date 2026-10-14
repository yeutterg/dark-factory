"""Intent-first external writes with reconciliation before retry."""

from __future__ import annotations

import json

from dark_factory.control.base import ControllerCore, ControllerError, _digest
from dark_factory.store import now_iso


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
