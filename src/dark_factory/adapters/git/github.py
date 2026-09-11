"""GitHub code-host command adapter (JSON stdin/stdout)."""

from __future__ import annotations

import json
import sys
from typing import Any

from dark_factory.contracts import CONTRACT_VERSION, ContractError, validate_result


def handle(request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("operation")
    payload = request.get("input") or {}
    if op == "inspect_pr":
        pr = payload.get("pr") or "unknown"
        checks = payload.get("checks") or []
        return {
            "ok": True,
            "operation": op,
            "evidence": {"pr": pr, "checks": checks, "mergeable": payload.get("mergeable", False)},
            "observations": [f"inspected {pr}"],
        }
    if op == "create_pr":
        repo = payload.get("repo")
        title = payload.get("title")
        if not repo or not title:
            return {"ok": False, "operation": op, "evidence": {}, "error": "repo and title required"}
        return {
            "ok": True,
            "operation": op,
            "evidence": {
                "repo": repo,
                "title": title,
                "url": payload.get("url") or f"https://github.com/{repo}/pull/0",
            },
        }
    raise ContractError(f"unsupported code-host operation {op!r}")


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
