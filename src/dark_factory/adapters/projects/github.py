"""Read explicitly selected GitHub issues; fixture input is labeled as such."""

from __future__ import annotations

import json
import sys

from dark_factory.adapters.git.github import api, repository
from dark_factory.contracts import ContractError, validate_request


def handle(request):
    validate_request(request)
    op = request["operation"]
    payload = request["input"]
    if op != "list_selected_issues":
        raise ContractError(f"unsupported tracking operation {op!r}")
    issues = payload.get("issues")
    if not isinstance(issues, list) or not issues:
        raise ContractError("explicit selected issues required")
    items = []
    observations = []
    for selected in issues:
        repo = repository(selected.get("repo"))
        number = selected.get("number")
        if type(number) is not int or number <= 0:
            raise ContractError("positive issue number required")
        # An explicit title/body constitutes caller-provided fixture data. With only
        # repo and number the adapter must obtain a real observation from GitHub.
        fixture = "title" in selected
        issue = selected if fixture else api(f"repos/{repo}/issues/{number}")
        if "pull_request" in issue:
            raise ContractError("selected tracker item is a PR, not an issue")
        items.append(
            {
                "external_id": f"github:{repo}:issue:{number}",
                "url": issue.get("html_url", issue.get("url", "")),
                "title": issue.get("title", ""),
                "body": issue.get("body") or "",
                "repo": repo,
                "parent_id": selected.get("parent_id"),
                "is_root": selected.get("is_root", True),
                "iteration": selected.get("iteration"),
            }
        )
        observations.append(
            {
                "external_id": items[-1]["external_id"],
                "source": "provided-fixture" if fixture else "github-api",
            }
        )
    return {
        "contract_version": 1,
        "request_id": request["request_id"],
        "ok": True,
        "operation": op,
        "evidence": {"count": len(items), "observations": observations},
        "items": items,
    }


def main():
    try:
        print(json.dumps(handle(json.load(sys.stdin))))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
