"""Deterministic, proposal-only factory improvement analysis."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from dark_factory.config import FactoryConfig
from dark_factory.store import Store


def _targets(cfg: FactoryConfig, roles: tuple[str, ...]) -> list[str]:
    agents = cfg.definition.get("agents", [])
    paths = [
        agent["prompt"]
        for agent in agents
        if agent.get("role") in roles and agent.get("prompt")
    ]
    if paths:
        return paths
    return [f"{role} prompt or skill" for role in roles]


def analyze_improvements(
    store: Store, cfg: FactoryConfig, minimum_occurrences: int = 3
) -> dict[str, Any]:
    """Group recurring failures across roots without inference or external writes."""

    if type(minimum_occurrences) is not int or minimum_occurrences < 2:
        raise ValueError("minimum occurrences must be an integer of at least 2")
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    for row in store.query(
        """SELECT w.id root_id,w.title,j.kind,a.id attempt_id,a.status,a.error,
                  a.result_json
           FROM attempts a JOIN jobs j ON j.id=a.job_id
           JOIN work_items w ON w.id=j.work_item_id
           WHERE a.status IN ('failed','timed_out') ORDER BY a.ended_at,a.id"""
    ):
        result = json.loads(row["result_json"] or "{}")
        roles: tuple[str, ...]
        if result.get("failure_kind") == "canonical-check-failure":
            key = "canonical-check-failure"
            label = "Implementation repeatedly fails canonical checks"
            roles = ("coder",)
        elif row["kind"] == "review" and "blocking independent findings" in str(
            row["error"] or ""
        ):
            key = "blocking-review-findings"
            label = "Independent review repeatedly finds blocking defects"
            roles = ("coder", "reviewer")
        elif row["status"] == "timed_out":
            key = f"timeout:{row['kind']}"
            label = f"The {row['kind']} stage repeatedly times out"
            roles = ("coder",) if row["kind"] == "implement" else ("planner",)
        else:
            key = f"failed-stage:{row['kind']}"
            label = f"The {row['kind']} stage repeatedly fails"
            roles = ("coder",) if row["kind"] == "implement" else ("planner",)
        grouped[key].setdefault(
            row["root_id"],
            {
                "root_id": row["root_id"],
                "title": row["title"],
                "attempt_id": row["attempt_id"],
                "detail": str(
                    row["error"] or result.get("failure_kind") or row["status"]
                )[:500],
                "label": label,
                "roles": roles,
            },
        )

    for row in store.query(
        "SELECT id,root_id,attempt_id,category,detail FROM interruptions ORDER BY at,id"
    ):
        key = f"interruption:{row['category']}"
        root = store.one("SELECT title FROM work_items WHERE id=?", (row["root_id"],))
        grouped[key].setdefault(
            row["root_id"],
            {
                "root_id": row["root_id"],
                "title": root["title"] if root else "",
                "attempt_id": row["attempt_id"],
                "detail": row["detail"][:500],
                "label": f"Runs repeatedly stop because of {row['category']} interruptions",
                "roles": (),
            },
        )

    for row in store.query(
        "SELECT at,payload_json FROM audit WHERE action='reject' ORDER BY at,id"
    ):
        payload = json.loads(row["payload_json"])
        root_id = payload.get("work_item_id")
        if not root_id:
            continue
        root = store.one("SELECT title FROM work_items WHERE id=?", (root_id,))
        grouped["human-rejection"].setdefault(
            root_id,
            {
                "root_id": root_id,
                "title": root["title"] if root else "",
                "attempt_id": None,
                "detail": str(payload.get("reason", "rejected"))[:500],
                "label": "Humans repeatedly reject factory proposals or candidates",
                "roles": ("planner", "coder"),
            },
        )

    observed = []
    proposals = []
    for key, roots in sorted(grouped.items()):
        evidence = list(roots.values())
        exemplar = evidence[0]
        observed.append(
            {
                "signal": key,
                "description": exemplar["label"],
                "distinct_roots": len(evidence),
            }
        )
        if len(evidence) < minimum_occurrences:
            continue
        roles = tuple(exemplar["roles"])
        targets = _targets(cfg, roles) if roles else ["runner or route binding"]
        title = f"Improve factory: {exemplar['label']}"
        body = (
            f"Dark Factory observed `{key}` in {len(evidence)} distinct root outcomes. "
            "Review the linked evidence and propose one narrowly scoped factory-definition "
            "or skill change. Do not change application behavior, permissions, budgets, or "
            "the running controller as part of this recommendation."
        )
        proposals.append(
            {
                "signal": key,
                "title": title,
                "summary": body,
                "suggested_targets": targets,
                "evidence": [
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"label", "roles"}
                    }
                    for item in evidence
                ],
                "work_item": {
                    "title": title,
                    "body": body,
                    "acceptance": [
                        "The change addresses only the cited recurring failure.",
                        "The definition validates and existing canonical checks pass.",
                        "A human reviews and explicitly selects any resulting implementation work.",
                    ],
                },
            }
        )

    return {
        "enabled": True,
        "mode": "proposal-only",
        "minimum_distinct_roots": minimum_occurrences,
        "definition_digest": cfg.definition.get("digest"),
        "observed_signals": observed,
        "proposals": proposals,
        "external_actions": [],
        "note": "No model was called and no work item, branch, pull request, or configuration change was created.",
    }
