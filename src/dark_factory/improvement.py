"""Recurring-failure signals and the proposals built from them.

Signals are derived from structured records (failure kinds, interruption
categories, rejections), never from inference. Roots the loop itself created are
never evidence: the loop cannot justify itself.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from dark_factory.config import FactoryConfig
from dark_factory.store import Store

# The role whose guidance a failed step points at.
KIND_ROLES: dict[str, tuple[str, ...]] = {
    "plan": ("planner",),
    "critique": ("critic",),
    "implement": ("coder",),
    "check": ("coder",),
    "review": ("reviewer",),
}


def collect_signals(store: Store) -> list[dict[str, Any]]:
    """One row per (signal, distinct root), oldest evidence first."""
    rows: list[dict[str, Any]] = []

    def add(key, label, roles, root_id, title, attempt_id, detail, at):
        rows.append(
            {
                "signal": key,
                "label": label,
                "roles": tuple(roles),
                "root_id": root_id,
                "title": title,
                "attempt_id": attempt_id,
                "detail": str(detail)[:500],
                "at": at or "",
            }
        )

    for row in store.query(
        """SELECT w.id root_id,w.title,j.kind,a.id attempt_id,a.status,a.error,
                  a.result_json,a.ended_at
           FROM attempts a JOIN jobs j ON j.id=a.job_id
           JOIN work_items w ON w.id=j.work_item_id
           WHERE a.status IN ('failed','timed_out')
             AND w.origin NOT LIKE 'improvement:%'
           ORDER BY a.ended_at,a.rowid"""
    ):
        result = json.loads(row["result_json"] or "{}")
        kind, error = row["kind"], str(row["error"] or "")
        roles: tuple[str, ...] = KIND_ROLES.get(kind, ("planner",))
        if result.get("failure_kind") == "canonical-check-failure":
            key = "canonical-check-failure"
            label = "Implementation repeatedly fails canonical checks"
            roles = ("coder",)
        elif kind == "review" and "blocking independent findings" in error:
            key = "blocking-review-findings"
            label = "Independent review repeatedly finds blocking defects"
            roles = ("coder", "reviewer")
        elif row["status"] == "timed_out":
            key = f"timeout:{kind}"
            label = f"The {kind} stage repeatedly times out"
        else:
            key = f"failed-stage:{kind}"
            label = f"The {kind} stage repeatedly fails"
        add(
            key,
            label,
            roles,
            row["root_id"],
            row["title"],
            row["attempt_id"],
            error or result.get("failure_kind") or row["status"],
            row["ended_at"],
        )

    for row in store.query(
        """SELECT i.root_id,i.attempt_id,i.category,i.detail,i.at,w.title
           FROM interruptions i JOIN work_items w ON w.id=i.root_id
           WHERE w.origin NOT LIKE 'improvement:%' ORDER BY i.at,i.rowid"""
    ):
        add(
            f"interruption:{row['category']}",
            f"Runs repeatedly stop because of {row['category']} interruptions",
            (),
            row["root_id"],
            row["title"],
            row["attempt_id"],
            row["detail"],
            row["at"],
        )

    for row in store.query(
        "SELECT at,payload_json FROM audit WHERE action='reject' ORDER BY at,rowid"
    ):
        payload = json.loads(row["payload_json"])
        root = store.one(
            "SELECT title,origin FROM work_items WHERE id=?",
            (payload.get("work_item_id"),),
        )
        if not root or root["origin"].startswith("improvement:"):
            continue
        add(
            "human-rejection",
            "Humans repeatedly reject factory proposals or candidates",
            ("planner", "coder"),
            payload["work_item_id"],
            root["title"],
            None,
            payload.get("reason", "rejected"),
            row["at"],
        )
    return rows


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


def group_signals(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        grouped[row["signal"]].setdefault(row["root_id"], row)
    return grouped


def build_report(
    grouped: dict[str, dict[str, dict[str, Any]]],
    cfg: FactoryConfig,
    minimum_occurrences: int,
) -> dict[str, Any]:
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
                        k: v
                        for k, v in item.items()
                        if k not in {"label", "roles", "signal", "at"}
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
        "enabled": bool(cfg.improvement["enabled"]),
        "mode": "proposal-only",
        "minimum_distinct_roots": minimum_occurrences,
        "definition_digest": cfg.definition.get("digest"),
        "observed_signals": observed,
        "proposals": proposals,
        "external_actions": [],
        "note": "This report called no model and created no work item, branch, pull "
        "request or configuration change.",
    }


def analyze_improvements(
    store: Store, cfg: FactoryConfig, minimum_occurrences: int | None = None
) -> dict[str, Any]:
    """The read-only report: recomputed from records, creating nothing."""
    minimum = (
        cfg.improvement["threshold"]
        if minimum_occurrences is None
        else minimum_occurrences
    )
    if type(minimum) is not int or minimum < 2:
        raise ValueError("minimum occurrences must be an integer of at least 2")
    return build_report(group_signals(collect_signals(store)), cfg, minimum)


def stored_improvements(store: Store, cfg: FactoryConfig) -> dict[str, Any]:
    """The same report read from persisted signals; cheap enough for every status read."""
    rows = [
        {**dict(r), "roles": tuple(json.loads(r["roles"]))}
        for r in store.query("SELECT * FROM improvement_signals ORDER BY at,root_id")
    ]
    for row in rows:
        row["title"] = ""
    return build_report(group_signals(rows), cfg, cfg.improvement["threshold"])
