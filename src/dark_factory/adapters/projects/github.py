"""Read explicitly selected GitHub issues; fixture input is labeled as such."""

from __future__ import annotations

import json
import sys

from dark_factory.adapters.git.github import api, repository
from dark_factory.contracts import ContractError, validate_request


def relationships(repo, number):
    """Read hierarchy explicitly; missing/partial API data is not a root."""
    owner, name = repo.split("/")
    result = api(
        "graphql",
        "POST",
        {
            "query": """query($owner:String!,$name:String!,$number:Int!) {
              repository(owner:$owner,name:$name) {
                issue(number:$number) {
                  parent { number url repository { nameWithOwner } }
                  subIssues { totalCount }
                }
              }
            }""",
            "variables": {"owner": owner, "name": name, "number": number},
        },
    )
    try:
        if result.get("errors"):
            raise ValueError("partial GraphQL response")
        issue = result["data"]["repository"]["issue"]
        parent = issue["parent"]
        count = issue["subIssues"]["totalCount"]
        if type(count) is not int or count < 0:
            raise ValueError("invalid child count")
        parent_id = None
        if parent is not None:
            parent_repo = repository(parent["repository"]["nameWithOwner"])
            parent_number = parent["number"]
            if type(parent_number) is not int or parent_number <= 0:
                raise ValueError("invalid parent number")
            parent_id = f"github:{parent_repo}:issue:{parent_number}"
        return parent_id, count
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(
            "GitHub issue relationships are incomplete; cannot establish root scope"
        ) from exc


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
        if fixture:
            parent_id = selected.get("parent_id")
            is_root = selected.get("is_root", parent_id is None)
            child_count = selected.get("required_child_count", 0)
        else:
            parent_id, child_count = relationships(repo, number)
            is_root = parent_id is None
        items.append(
            {
                "external_id": f"github:{repo}:issue:{number}",
                "url": issue.get("html_url", issue.get("url", "")),
                "title": issue.get("title", ""),
                "body": issue.get("body") or "",
                "repo": repo,
                "parent_id": parent_id,
                "is_root": is_root,
                "required_child_count": child_count,
                "iteration": selected.get("iteration"),
            }
        )
        observations.append(
            {
                "external_id": items[-1]["external_id"],
                "source": "provided-fixture" if fixture else "github-api",
                "parent_id": parent_id,
                "required_child_count": child_count,
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
