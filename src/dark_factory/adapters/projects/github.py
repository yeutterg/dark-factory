"""GitHub tracking command adapter (JSON stdin/stdout)."""

from __future__ import annotations

import json
import sys
from typing import Any

from dark_factory.contracts import CONTRACT_VERSION, ContractError, validate_result


def handle(request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("operation")
    payload = request.get("input") or {}
    if op == "list_selected_issues":
        issues = payload.get("issues") or []
        items = []
        for issue in issues:
            items.append(
                {
                    "external_id": f"github:{issue.get('repo')}:{issue.get('number')}",
                    "url": issue.get("url") or "",
                    "title": issue.get("title") or "",
                    "body": issue.get("body") or "",
                    "repo": issue.get("repo"),
                    "parent_id": issue.get("parent_id"),
                    "is_root": bool(issue.get("is_root", True)),
                    "iteration": issue.get("iteration"),
                }
            )
        return {
            "ok": True,
            "operation": op,
            "evidence": {"count": len(items), "source": "github-tracking"},
            "items": items,
        }
    if op == "write_status":
        if not payload.get("external_id") or not payload.get("status"):
            return {
                "ok": False,
                "operation": op,
                "evidence": {},
                "error": "external_id and status are required",
            }
        return {
            "ok": True,
            "operation": op,
            "evidence": {
                "external_id": payload["external_id"],
                "status": payload["status"],
                "written": True,
            },
        }
    raise ContractError(f"unsupported tracking operation {op!r}")


def main() -> None:
    request = json.loads(sys.stdin.read())
    try:
        result = handle(request)
        if result.get("ok"):
            validate_result(result, request["operation"])
        result.setdefault("contract_version", CONTRACT_VERSION)
        sys.stdout.write(json.dumps(result))
    except Exception as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
