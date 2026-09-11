"""GitHub PR/check JSON commands. Credentials remain on the controller."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

from dark_factory.contracts import ContractError, validate_request
from dark_factory.runner import CommandRunner


def api(path, method="GET", payload=None):
    argv = ["gh", "api", "--method", method, path]
    if payload is not None:
        argv += ["--input", "-"]
    runner = CommandRunner(Path.cwd())
    import os

    code, out, err = runner._run(
        argv,
        json.dumps(payload).encode() if payload is not None else b"",
        60,
        Path.cwd(),
        dict(os.environ),
    )
    if code:
        raise ContractError(
            f"GitHub request failed ({code}): {err.decode(errors='replace')[-1000:]}"
        )
    return json.loads(out) if out.strip() else {}


def repository(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value
    ):
        raise ContractError("repo must be owner/name")
    return value


def handle(request):
    validate_request(request)
    op = request["operation"]
    payload = request["input"]
    repo = repository(payload.get("repo"))
    result = {
        "contract_version": 1,
        "request_id": request["request_id"],
        "operation": op,
        "ok": True,
    }
    if op == "inspect_pr":
        number = payload.get("number")
        if type(number) is not int or number <= 0:
            raise ContractError("positive PR number required")
        pr = api(f"repos/{repo}/pulls/{number}")
        sha = pr["head"]["sha"]
        checks = api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
        statuses = api(f"repos/{repo}/commits/{sha}/status?per_page=100")
        if checks.get("total_count", 0) > 100 or statuses.get("total_count", 0) > 100:
            raise ContractError(
                "check pagination exceeds bootstrap limit; required evidence incomplete"
            )
        result["evidence"] = {
            "external_id": f"github:{repo}:pr:{number}",
            "url": pr["html_url"],
            "head_sha": sha,
            "base_sha": pr["base"]["sha"],
            "base_ref": pr["base"]["ref"],
            "state": pr["state"],
            "merged": pr["merged"],
            "mergeable": pr["mergeable"],
            "checks": checks["check_runs"],
            "statuses": statuses["statuses"],
            "required_check_policy": "unknown; GitHub branch/ruleset requirements must be checked by the human merger",
        }
    elif op in {"create_pr", "reconcile_create_pr"}:
        head = payload.get("head")
        base = payload.get("base")
        expected = payload.get("head_sha")
        if not all(
            isinstance(v, str) and v and not v.startswith("-")
            for v in (head, base, expected)
        ):
            raise ContractError("head, base and exact head_sha required")
        if not re.fullmatch("[0-9a-f]{40}", expected):
            raise ContractError("exact head SHA required")
        observed = api(f"repos/{repo}/commits/{quote(head, safe='')}")["sha"]
        if observed != expected:
            raise ContractError("head branch changed; refusing external write")
        marker = f"<!-- dark-factory:{request['request_id']} -->"
        owner = repo.split("/")[0]
        prs = api(
            f"repos/{repo}/pulls?state=all&head={quote(owner + ':' + head, safe='')}&base={quote(base, safe='')}&per_page=100"
        )
        matches = [pr for pr in prs if marker in (pr.get("body") or "")]
        if len(matches) > 1:
            raise ContractError("ambiguous duplicate action marker")
        if matches:
            pr = matches[0]
            if pr["head"]["sha"] != expected:
                raise ContractError("existing PR revision changed")
            result["evidence"] = {
                "observed": True,
                "url": pr["html_url"],
                "number": pr["number"],
                "head_sha": expected,
                "reconciled": True,
            }
        elif op == "reconcile_create_pr":
            result["evidence"] = {
                "observed": False,
                "safe_to_retry": len(prs) < 100,
                "head_sha": expected,
            }
        else:
            if (
                not isinstance(payload.get("title"), str)
                or not payload["title"].strip()
            ):
                raise ContractError("title required")
            if len(prs) >= 100:
                raise ContractError("PR reconciliation pagination incomplete")
            pr = api(
                f"repos/{repo}/pulls",
                "POST",
                {
                    "head": head,
                    "base": base,
                    "title": payload["title"],
                    "body": payload.get("body", "") + "\n\n" + marker,
                    "draft": True,
                },
            )
            confirmed = api(f"repos/{repo}/pulls/{pr['number']}")
            if confirmed["head"]["sha"] != expected or marker not in (
                confirmed.get("body") or ""
            ):
                raise ContractError("PR write read-back did not match intention")
            result["evidence"] = {
                "observed": True,
                "url": confirmed["html_url"],
                "number": confirmed["number"],
                "head_sha": expected,
                "draft": True,
            }
    else:
        raise ContractError(f"unsupported code-host operation {op!r}")
    return result


def main():
    try:
        print(json.dumps(handle(json.load(sys.stdin))))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
