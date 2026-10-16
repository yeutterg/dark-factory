"""Read explicitly selected GitHub issues; fixture input is labeled as such.

Candidate discovery reads labeled issues and, when the command is given
``--project owner/number``, open issues on those GitHub Projects with their
status and theme. ``--theme-field`` names the project field used as the theme.
"""

from __future__ import annotations

import argparse
import json
import re
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


def _candidate(repo, issue, project=None):
    """Normalize one GraphQL issue node; partial hierarchy is never a root."""
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
    return {
        "external_id": f"github:{repo}:issue:{number}",
        "repo": repo,
        "number": number,
        "title": issue.get("title", ""),
        "url": issue.get("url", ""),
        "is_root": parent_id is None,
        "parent_id": parent_id,
        "required_child_count": child_count,
        "project": project,
    }


def project_reference(value):
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)/([0-9]+)", value or "")
    if not match or int(match[2]) <= 0:
        raise ContractError("project must be owner/number")
    return match[1], int(match[2])


def _field_text(value):
    if not isinstance(value, dict):
        return None
    for key in ("name", "text", "title"):
        if isinstance(value.get(key), str) and value[key]:
            return value[key]
    return None


PROJECT_QUERY = """query($owner:String!,$number:Int!,$theme:String!) {
  repositoryOwner(login:$owner) {
    ... on ProjectV2Owner {
      projectV2(number:$number) {
        title url
        items(first:100) {
          nodes {
            status: fieldValueByName(name:"Status") { ...value }
            theme: fieldValueByName(name:$theme) { ...value }
            content {
              ... on Issue {
                number title url state
                repository { nameWithOwner }
                parent { number repository { nameWithOwner } }
                subIssues { totalCount }
              }
            }
          }
          pageInfo { hasNextPage }
        }
      }
    }
  }
}
fragment value on ProjectV2ItemFieldValue {
  ... on ProjectV2ItemFieldSingleSelectValue { name }
  ... on ProjectV2ItemFieldTextValue { text }
  ... on ProjectV2ItemFieldIterationValue { title }
}"""


def project_issues(project, theme_field, repositories):
    """Open issues on one project board that belong to configured repositories."""
    owner, number = project_reference(project)
    result = api(
        "graphql",
        "POST",
        {
            "query": PROJECT_QUERY,
            "variables": {"owner": owner, "number": number, "theme": theme_field},
        },
    )
    try:
        if result.get("errors"):
            raise ValueError("partial GraphQL response")
        board = result["data"]["repositoryOwner"]["projectV2"]
        if board is None:
            raise ValueError("project not found")
        items = []
        for node in board["items"]["nodes"]:
            issue = node.get("content") or {}
            if "number" not in issue or issue.get("state") != "OPEN":
                continue  # Drafts, pull requests and closed issues are not work.
            repo = repository(issue["repository"]["nameWithOwner"])
            if repo not in repositories:
                continue
            items.append(
                _candidate(
                    repo,
                    issue,
                    {
                        "id": f"{owner}/{number}",
                        "title": board["title"],
                        "url": board["url"],
                        "status": _field_text(node.get("status")),
                        "theme": _field_text(node.get("theme")),
                    },
                )
            )
        has_next = board["items"]["pageInfo"]["hasNextPage"]
        if type(has_next) is not bool:
            raise ValueError("invalid page information")
        return items, has_next
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(
            f"GitHub project {project} is incomplete or unreadable; "
            "the token needs the read:project scope"
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
            items.append(_candidate(repo, issue))
        has_next = connection["pageInfo"]["hasNextPage"]
        if type(has_next) is not bool:
            raise ValueError("invalid page information")
        return items, has_next
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(
            "GitHub candidate hierarchy is incomplete; cannot list selectable roots"
        ) from exc


def handle(request, projects=(), theme_field="Theme"):
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
        repositories = [repository(value) for value in repositories]
        items = []
        observations = []
        seen = {}
        for project in projects:
            visible, has_next = project_issues(project, theme_field, repositories)
            added = 0
            for item in visible:
                key = (item["repo"], item["number"])
                if key in seen or len(items) >= limit:
                    has_next = has_next or key not in seen
                    continue
                seen[key] = item
                items.append(item)
                added += 1
            observations.append(
                {"project": project, "returned": added, "truncated": has_next}
            )
        for repo in repositories:
            remaining = limit - len(items)
            if remaining <= 0:
                break
            visible, has_next = candidate_issues(repo, label, remaining)
            added = 0
            for item in visible:
                if (item["repo"], item["number"]) not in seen:
                    seen[(item["repo"], item["number"])] = item
                    items.append(item)
                    added += 1
            observations.append(
                {
                    "repo": repo,
                    "returned": added,
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
                "projects": list(projects),
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dark_factory.adapters.projects.github")
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--theme-field", default="Theme")
    args = parser.parse_args(argv)
    try:
        for project in args.project:
            project_reference(project)
        print(json.dumps(handle(json.load(sys.stdin), args.project, args.theme_field)))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
