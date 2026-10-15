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


def candidate_issues(repo, label, limit):
    """Read one bounded issue page with hierarchy in a single GraphQL call."""
    owner, name = repo.split("/")
    result = api(
        "graphql",
        "POST",
        {
            "query": """query($owner:String!,$name:String!,$labels:[String!]!,$first:Int!) {
              repository(owner:$owner,name:$name) {
                issues(first:$first,states:OPEN,labels:$labels,orderBy:{field:UPDATED_AT,direction:DESC}) {
                  nodes {
                    number title url
                    parent { number repository { nameWithOwner } }
                    subIssues { totalCount }
                  }
                  pageInfo { hasNextPage }
                }
              }
            }""",
            "variables": {
                "owner": owner,
                "name": name,
                "labels": [label],
                "first": limit,
            },
        },
    )
    try:
        if result.get("errors"):
            raise ValueError("partial GraphQL response")
        connection = result["data"]["repository"]["issues"]
        items = []
        for issue in connection["nodes"]:
            number = issue["number"]
            parent = issue["parent"]
            child_count = issue["subIssues"]["totalCount"]
            if type(number) is not int or number <= 0:
                raise ValueError("invalid issue number")
            if type(child_count) is not int or child_count < 0:
                raise ValueError("invalid child count")
            parent_id = None
            if parent is not None:
                parent_repo = repository(parent["repository"]["nameWithOwner"])
                parent_number = parent["number"]
                if type(parent_number) is not int or parent_number <= 0:
                    raise ValueError("invalid parent number")
                parent_id = f"github:{parent_repo}:issue:{parent_number}"
            items.append(
                {
                    "repo": repo,
                    "number": number,
                    "title": issue.get("title", ""),
                    "url": issue.get("url", ""),
                    "is_root": parent_id is None,
                    "parent_id": parent_id,
                    "required_child_count": child_count,
                }
            )
        has_next = connection["pageInfo"]["hasNextPage"]
        if type(has_next) is not bool:
            raise ValueError("invalid page information")
        return items, has_next
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(
            "GitHub candidate hierarchy is incomplete; cannot list selectable roots"
        ) from exc


def handle(request):
    validate_request(request)
    op = request["operation"]
    payload = request["input"]
    if op == "list_candidate_issues":
        repositories = payload.get("repositories")
        label = payload.get("label")
        limit = payload.get("limit", 50)
        if (
            not isinstance(repositories, list)
            or not repositories
            or not isinstance(label, str)
            or not label
            or type(limit) is not int
            or limit < 1
            or limit > 50
        ):
            raise ContractError("repositories, label and a limit from 1 to 50 required")
        items = []
        observations = []
        for value in repositories:
            remaining = limit - len(items)
            if remaining <= 0:
                break
            repo = repository(value)
            visible, has_next = candidate_issues(repo, label, remaining)
            items.extend(visible)
            observations.append(
                {
                    "repo": repo,
                    "returned": len(visible),
                    "truncated": has_next,
                }
            )
        return {
            "contract_version": 1,
            "request_id": request["request_id"],
            "ok": True,
            "operation": op,
            "evidence": {
                "label": label,
                "count": len(items),
                "truncated": len(items) == limit
                or any(item["truncated"] for item in observations),
                "observations": observations,
            },
            "items": items,
        }
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
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
