"""An in-memory code host that obeys the contract. For demos, tests and adapter authors."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from dark_factory.contracts import ContractError, validate_request

BRANCH = re.compile(r"dark-factory/[a-z0-9][a-z0-9._/-]{2,100}")
SHA = re.compile(r"[0-9a-f]{40}")


class FakeCodeHost:
    def __init__(self) -> None:
        self.branches: dict[tuple[str, str], str] = {}
        self.prs: dict[str, list[dict[str, Any]]] = {}
        self.statuses: list[dict[str, Any]] = []
        self.runs: dict[int, dict[str, Any]] = {}

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        validate_request(request)
        op, payload = request["operation"], request["input"]
        repo = payload.get("repo")
        if not isinstance(repo, str) or "/" not in repo:
            raise ContractError("repo must be owner/name")
        method = getattr(self, "op_" + op, None)
        if method is None:
            raise ContractError(f"unsupported code-host operation {op!r}")
        return {
            "contract_version": 1,
            "request_id": request["request_id"],
            "operation": op,
            "ok": True,
            "evidence": method(repo, payload, request["request_id"]),
        }

    # branches
    def op_push_branch(self, repo, p, rid):
        branch, sha = p.get("branch"), p.get("head_sha")
        if (
            not isinstance(branch, str)
            or not BRANCH.fullmatch(branch)
            or ".." in branch
        ):
            raise ContractError("branch must be a new dark-factory/ branch")
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise ContractError("exact head SHA required")
        existing = self.branches.get((repo, branch))
        if existing and existing != sha:
            raise ContractError(
                "branch exists at a different commit; refusing to overwrite"
            )
        self.branches[(repo, branch)] = sha
        return {"observed": True, "branch": branch, "head_sha": sha}

    def op_reconcile_push_branch(self, repo, p, rid):
        branch, sha = p.get("branch"), p.get("head_sha")
        if not isinstance(branch, str) or not BRANCH.fullmatch(branch):
            raise ContractError("branch must be a new dark-factory/ branch")
        existing = self.branches.get((repo, branch))
        if existing and existing != sha:
            raise ContractError(
                "branch exists at a different commit; refusing to overwrite"
            )
        if existing:
            return {"observed": True, "branch": branch, "head_sha": sha}
        return {"observed": False, "safe_to_retry": True, "head_sha": sha}

    # pull requests
    def _find(self, repo, head):
        return next(
            (pr for pr in self.prs.get(repo, []) if pr["head_ref"] == head), None
        )

    def op_create_pr(self, repo, p, rid):
        head, sha = p.get("head"), p.get("head_sha")
        if not all(isinstance(v, str) and v for v in (head, p.get("base"), sha)):
            raise ContractError("head, base and exact head_sha required")
        if self.branches.get((repo, head)) != sha:
            raise ContractError("head branch changed; refusing external write")
        found = self._find(repo, head)
        if found:
            return {**self._evidence(found), "reconciled": True}
        if not isinstance(p.get("title"), str) or not p["title"].strip():
            raise ContractError("title required")
        pr = {
            "number": len(self.prs.get(repo, [])) + 1,
            "head_sha": sha,
            "head_ref": head,
            "base": p["base"],
            "title": p["title"],
            "url": f"https://fake.test/{repo}/pull/{len(self.prs.get(repo, [])) + 1}",
            "body": p.get("body", ""),
            "draft": True,
        }
        self.prs.setdefault(repo, []).append(pr)
        return self._evidence(pr)

    def op_reconcile_create_pr(self, repo, p, rid):
        found = self._find(repo, p.get("head"))
        if found:
            return {**self._evidence(found), "reconciled": True}
        return {"observed": False, "safe_to_retry": True, "head_sha": p.get("head_sha")}

    @staticmethod
    def _evidence(pr):
        return {
            "observed": True,
            "url": pr["url"],
            "number": pr["number"],
            "head_sha": pr["head_sha"],
            "draft": pr.get("draft", True),
        }

    def op_inspect_pr(self, repo, p, rid):
        number = p.get("number")
        if type(number) is not int or number <= 0:
            raise ContractError("positive PR number required")
        pr = next((x for x in self.prs.get(repo, []) if x["number"] == number), None)
        if pr is None:
            raise ContractError("pull request not found")
        return {
            "head_sha": pr["head_sha"],
            "url": pr["url"],
            "checks": [],
            "statuses": [s for s in self.statuses if s["sha"] == pr["head_sha"]],
            "reviews": [],
            "review_comments": [],
            "merged": pr.get("merged", False),
            "merge_commit_sha": pr.get("merge_commit_sha"),
            "merged_at": pr.get("merged_at"),
        }

    def op_list_open_prs(self, repo, p, rid):
        return {"prs": [dict(pr) for pr in self.prs.get(repo, [])]}

    def op_set_status(self, repo, p, rid):
        if not isinstance(p.get("sha"), str) or not SHA.fullmatch(p["sha"]):
            raise ContractError("exact commit SHA required")
        if p.get("state") not in {"success", "pending", "failure", "error"}:
            raise ContractError("invalid status state")
        if not str(p.get("context", "")).startswith("dark-factory/"):
            raise ContractError("status context must start with dark-factory/")
        self.statuses.append(
            {
                "repo": repo,
                "sha": p["sha"],
                "state": p["state"],
                "context": p["context"],
            }
        )
        return {"observed": True, "sha": p["sha"], "state": p["state"]}

    # hosted runs
    def op_dispatch_workflow(self, repo, p, rid):
        inputs = p.get("inputs")
        if not isinstance(inputs, dict) or not all(
            isinstance(v, str) for v in inputs.values()
        ):
            raise ContractError("workflow inputs must be strings")
        if not isinstance(p.get("workflow"), str) or not p["workflow"].endswith(
            (".yml", ".yaml")
        ):
            raise ContractError("workflow must be a workflow file name")
        run_id = len(self.runs) + 41
        self.runs[run_id] = {
            "request_id": inputs.get("request_id"),
            "commit": inputs.get("commit", ""),
            "status": "completed",
            "conclusion": "success",
            "head_sha": p.get("ref", ""),
        }
        return {"observed": True, "request_id": inputs.get("request_id")}

    def op_find_run(self, repo, p, rid):
        if not isinstance(p.get("commit"), str) or not SHA.fullmatch(p["commit"]):
            raise ContractError("exact commit required")
        matches = [
            i
            for i, r in self.runs.items()
            if r["request_id"] == p.get("request_id") and r["commit"] == p["commit"]
        ]
        return {"run_id": matches[0] if len(matches) == 1 else None}

    def op_get_run(self, repo, p, rid):
        run = self.runs[int(p["run_id"])]
        return {
            "status": run["status"],
            "conclusion": run["conclusion"],
            "url": f"https://fake.test/{repo}/runs/{p['run_id']}",
            "head_sha": run["head_sha"],
        }

    def op_list_run_artifacts(self, repo, p, rid):
        return {"artifacts": []}

    def op_download_run_artifact(self, repo, p, rid):
        expected = str(p.get("expected_digest", "")).removeprefix("sha256:")
        data = b"fake hosted artifact"
        if hashlib.sha256(data).hexdigest() != expected:
            raise ContractError("downloaded artifact digest did not match code host")
        destination = Path(str(p.get("destination_path", "")))
        if not destination.is_absolute() or destination.exists():
            raise ContractError("destination must be a new absolute path")
        destination.mkdir(parents=True)
        target = destination / "package.bin"
        target.write_bytes(data)
        return {
            "files": [
                {
                    "name": f"{destination.name}/package.bin",
                    "size": len(data),
                    "sha256": expected,
                }
            ],
            "archive_sha256": expected,
        }
