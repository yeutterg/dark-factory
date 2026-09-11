"""Human review packets."""

from __future__ import annotations

from typing import Any

from dark_factory.config import FactoryConfig
from dark_factory.models import PlanGraph


def plan_packet(title: str, body: str, graph: PlanGraph, cfg: FactoryConfig) -> dict[str, Any]:
    return {
        "kind": "plan",
        "summary": {
            "Why": "Complete the selected work with a bounded, approved job graph.",
            "What": title,
            "Assumption": "Bootstrap is single-repository and stops on failure without fallback.",
            "Needs your input": "Approve or request changes before implementation.",
        },
        "repos": cfg.repositories,
        "coding_tier": list(cfg.routes),
        "spending_ceiling_cents": graph.spending_ceiling_cents,
        "permitted_previews": graph.permitted_previews,
        "protected_paths": graph.protected_paths,
        "decision": "Approve this plan, graph, acceptance criteria, and limits.",
        "acceptance": graph.acceptance,
        "jobs": [
            {
                "id": j.job_id,
                "kind": j.kind,
                "depends_on": j.depends_on,
                "repo": j.repo,
                "timeout_seconds": j.timeout_seconds,
            }
            for j in graph.jobs
        ],
        "source_excerpt": body[:2000],
    }


def result_packet(work_item: dict[str, Any], review_output: str, ok: bool) -> dict[str, Any]:
    if not ok:
        return {
            "kind": "result",
            "ready_to_merge": False,
            "What changed": "Work stopped before acceptance.",
            "What to review": "Failing evidence and saved archive.",
            "What to test": "Do not merge. Inspect the failure packet.",
            "review": review_output,
            "status": work_item.get("status"),
            "spent_cents": work_item.get("spent_cents"),
            "recommended_next_step": "Fix the failing check or re-plan; do not treat this as ready.",
        }
    return {
        "kind": "result",
        "ready_to_merge": False,
        "What changed": work_item.get("title"),
        "What to review": "Diff, independent review notes, and captured checks.",
        "What to test": [
            {
                "n": 1,
                "setup": "Open the archived workspace artifact",
                "action": "Confirm the documented change exists",
                "expected": "CHANGELOG.md or equivalent artifact is present",
            }
        ],
        "automated_checks": "See evidence records for the check job.",
        "manual_remaining": "Human merge remains an operator action in bootstrap.",
        "review": review_output,
        "spent_cents": work_item.get("spent_cents"),
    }
