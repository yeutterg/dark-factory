"""The required pull-request status: nothing merges without an approved plan.

Every open pull request in a gated repository gets one status, `dark-factory/gate`.
It is green only for the exact commit the factory built, tested and independently
reviewed under an approved plan. Anything else is red, with the way in: file or pick
an issue, get the plan approved, submit through the factory. A new push is a new
candidate, so the old evidence stops counting at once.
"""

from __future__ import annotations

import json

from dark_factory.control.base import ControllerCore
from dark_factory.store import new_id

CONTEXT = "dark-factory/gate"
HOW_TO = (
    "No approved plan covers this commit. Start from an issue: "
    "dark-factory work-start, get the plan approved, then work-submit."
)


class GateMixin(ControllerCore):
    def evaluate_pr(self, repo, pr):
        """(state, description) for one open pull request."""
        sha = pr["head_sha"]
        roots = self.store.query(
            "SELECT * FROM work_items WHERE repo=? AND candidate_commits<>'{}'", (repo,)
        )
        by_sha = None
        by_number = None
        for root in roots:
            candidate = json.loads(root["candidate_commits"]).get(repo, {})
            if candidate.get("commit") == sha:
                by_sha = root
            if self._created_pr(root["id"], repo) == pr["number"]:
                by_number = root
        root = by_sha or by_number
        if root is None:
            return "failure", HOW_TO
        if by_sha is None:
            return (
                "failure",
                "A commit was pushed after review. Submit it with work-submit so it "
                "is built, tested and reviewed.",
            )
        if root["status"] in {"awaiting_review", "accepted"} and self._approval_current(
            root
        ):
            return "success", f"Approved plan {root['id']}; built, tested and reviewed."
        if root["status"] in {"failed", "rejected", "cancelled"}:
            return "failure", f"Work {root['id']} is {root['status']}."
        return "pending", f"Work {root['id']} is {root['status']}; checks are running."

    def gate_tick(self):
        settings = self.cfg.gate
        if not settings["enabled"]:
            return []
        command = self.cfg.commands["code_host"]
        posted = []
        for repo in settings["repositories"] or self.cfg.repositories:
            listing = self.runner.run_json(
                command.command,
                "list_open_prs",
                {"repo": repo},
                new_id("req"),
                command.timeout_seconds,
            )
            for pr in listing["evidence"]["prs"]:
                state, description = self.evaluate_pr(repo, pr)
                key = f"gate:{repo}#{pr['number']}"
                marker = f"{state}:{pr['head_sha']}"
                if self.store.get_op(key, "") == marker:
                    continue
                self.runner.run_json(
                    command.command,
                    "set_status",
                    {
                        "repo": repo,
                        "sha": pr["head_sha"],
                        "state": state,
                        "context": CONTEXT,
                        "description": description,
                    },
                    new_id("req"),
                    command.timeout_seconds,
                )
                self.store.set_op(key, marker, "controller")
                self.store.audit(
                    "controller",
                    "gate.status",
                    {"repo": repo, "pr": pr["number"], "state": state},
                )
                posted.append((repo, pr["number"], state))
        return posted
