"""Credential-free, JSON-safe controller view for local and remote TUIs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from dark_factory.controller import Controller

TERMINAL = frozenset({"accepted", "cancelled", "failed", "rejected"})
NEEDS_HUMAN = frozenset(
    {"selected", "awaiting_approval", "awaiting_review", "rejected"}
)


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _age(value: str | None) -> str:
    if not value:
        return "—"
    try:
        then = datetime.fromisoformat(value)
        if then.tzinfo is None:
            then = then.replace(tzinfo=UTC)
        seconds = max(0, int((datetime.now(UTC) - then).total_seconds()))
    except ValueError:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _recent(value: str | None, seconds: int) -> bool:
    if not value:
        return False
    try:
        then = datetime.fromisoformat(value)
        if then.tzinfo is None:
            then = then.replace(tzinfo=UTC)
        return (datetime.now(UTC) - then).total_seconds() <= seconds
    except ValueError:
        return False


def _external_number(external_id: str) -> str:
    marker = ":issue:"
    return external_id.rsplit(marker, 1)[-1] if marker in external_id else external_id


def _accepted_today(value: str | None, timezone: ZoneInfo) -> bool:
    if not value:
        return False
    try:
        accepted = datetime.fromisoformat(value)
        if accepted.tzinfo is None:
            accepted = accepted.replace(tzinfo=UTC)
        return accepted.astimezone(timezone).date() == datetime.now(timezone).date()
    except ValueError:
        return False


def _safe_route(route: Any) -> dict[str, Any]:
    return {
        "id": route.id,
        "role": route.role,
        "model": route.model,
        "provider": route.provider,
        "harness": route.harness,
        "harness_version": route.harness_version,
        "billing_account": route.billing_account,
        "region": route.region,
        "environment": route.environment,
        "account_kind": route.account_kind,
        "auth_reference": route.auth_env,
    }


def _candidate_diff(
    controller: Controller, row: Any, candidates: dict[str, Any]
) -> str:
    candidate = candidates.get(row["repo"], {})
    archive = candidate.get("archive") if isinstance(candidate, dict) else None
    if not isinstance(archive, str) or not archive:
        return ""
    path = controller.store.resolve_path(archive) / "diff.patch"
    try:
        if not path.is_file() or path.is_symlink():
            return ""
        limit = min(controller.cfg.max_output_bytes, 200_000)
        with path.open("rb") as source:
            data = source.read(limit + 1)
    except OSError:
        return ""
    suffix = "\n\n[diff truncated by operator projection]" if len(data) > limit else ""
    return data[:limit].decode("utf-8", errors="replace") + suffix


def _work_item(
    controller: Controller, row: Any, include_detail: bool = False
) -> dict[str, Any]:
    jobs = controller.jobs(row["id"])
    continuation = controller.continuation(row["id"])
    work = continuation["work"]
    active = next((item for item in work if item["state"] == "running"), None)
    queued = next((item for item in work if item["state"] == "queued"), None)
    current = active or queued
    complete = sum(item["state"] == "verified" for item in work)
    total = len(work)
    packet = _json(row["packet_json"], {}) if include_detail else {}
    plan = _json(row["plan_json"], {}) if include_detail else {}
    critique = _json(row["critique_json"], {}) if include_detail else {}
    candidates = _json(row["candidate_commits"], {}) if include_detail else {}
    diff_patch = _candidate_diff(controller, row, candidates) if include_detail else ""
    attempts = (
        [
            dict(attempt)
            for attempt in controller.store.query(
                """SELECT a.* FROM attempts a JOIN jobs j ON j.id=a.job_id
                WHERE j.work_item_id=? ORDER BY a.started_at,a.id""",
                (row["id"],),
            )
        ]
        if include_detail
        else []
    )
    evidence = (
        [
            dict(item)
            for item in controller.store.query(
                """SELECT e.id,e.attempt_id,e.kind,e.path,e.digest,e.payload_json,e.inputs_json
                FROM evidence e JOIN attempts a ON a.id=e.attempt_id
                JOIN jobs j ON j.id=a.job_id WHERE j.work_item_id=? ORDER BY e.id""",
                (row["id"],),
            )
        ]
        if include_detail
        else []
    )
    audits = (
        [
            dict(item)
            for item in controller.store.query(
                """SELECT at,actor,action,payload_json FROM audit
                WHERE json_extract(payload_json,'$.root')=?
                   OR json_extract(payload_json,'$.work_item_id')=?
                ORDER BY at,id""",
                (row["id"], row["id"]),
            )
        ]
        if include_detail
        else []
    )
    status = row["status"]
    next_action = continuation["next_action"]
    return {
        "id": row["id"],
        "number": _external_number(row["external_id"]),
        "external_id": row["external_id"],
        "url": row["url"],
        "title": row["title"],
        "body": row["body"],
        "repo": row["repo"],
        "status": status,
        "stage": row["presentation_stage"],
        "spent_cents": row["spent_cents"],
        "reserved_cents": row["reserved_cents"],
        "cap_cents": row["spending_ceiling_cents"],
        "created_at": row["created_at"],
        "accepted_at": row["accepted_at"],
        "age": _age(row["created_at"]),
        "jobs_complete": complete,
        "jobs_total": total,
        "current_job": current["kind"] if current else "",
        "progress_visible": status not in TERMINAL
        and status not in {"approved", "awaiting_approval", "awaiting_review"},
        "lane": "done"
        if status == "accepted"
        else "needs_you"
        if status in NEEDS_HUMAN
        or next_action["kind"] in {"human_approval", "human_review", "blocked"}
        else "running",
        "next_action": next_action,
        "work": work,
        "jobs": jobs if include_detail else [],
        "attempts": attempts,
        "evidence": evidence,
        "audit": audits,
        "packet": packet,
        "plan": plan,
        "critique": critique,
        "candidates": candidates,
        "diff_patch": diff_patch,
    }


def controller_projection(
    controller: Controller, detail_id: str | None = None
) -> dict[str, Any]:
    """Return one consistent, secret-free snapshot of operator-visible facts."""

    with controller.decision():
        allowed, dispatch_reason = controller.dispatch_allowed()
        rows = controller.store.query(
            "SELECT * FROM work_items WHERE is_root=1 ORDER BY created_at,id"
        )
        items = [
            _work_item(controller, row, include_detail=row["id"] == detail_id)
            for row in rows
        ]
        workers = []
        registered = controller.cfg.workers
        observed = {
            row["id"]: row
            for row in controller.store.query(
                "SELECT id,capabilities,last_heartbeat,busy FROM workers ORDER BY id"
            )
        }
        for worker_id in sorted(set(registered) | set(observed)):
            configured = registered.get(worker_id, {})
            row = observed.get(worker_id)
            capabilities = _json(row["capabilities"], []) if row else []
            lease_seconds = configured.get("lease_seconds", 30)
            last_heartbeat = row["last_heartbeat"] if row else None
            busy = bool(row["busy"]) if row else False
            fresh = _recent(last_heartbeat, max(30, lease_seconds * 2))
            active = controller.store.one(
                """SELECT a.id attempt_id,a.status,j.kind,j.work_item_id,a.started_at,
                a.deadline,a.route_id,a.workspace FROM attempts a JOIN jobs j ON j.id=a.job_id
                WHERE a.worker_id=? AND a.status IN ('leased','running','cancelling')
                ORDER BY a.started_at DESC LIMIT 1""",
                (worker_id,),
            )
            workers.append(
                {
                    "id": worker_id,
                    "status": "busy"
                    if fresh and busy
                    else "ready"
                    if fresh
                    else "offline",
                    "last_heartbeat": last_heartbeat,
                    "age": _age(last_heartbeat),
                    "capabilities": capabilities,
                    "environments": configured.get("environments", []),
                    "lease_seconds": lease_seconds,
                    "active": dict(active) if active else None,
                }
            )
        routes = [_safe_route(route) for route in controller.cfg.routes.values()]
        timezone = ZoneInfo(
            controller.cfg.windows[0].timezone if controller.cfg.windows else "UTC"
        )
        return {
            "schema": 1,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "project": {
                "id": controller.cfg.project_id,
                "name": controller.cfg.project_name,
                "repositories": controller.cfg.repositories,
                "config": str(controller.cfg.source_path),
                "digest": controller.cfg.digest,
            },
            "dispatch": {
                "allowed": allowed,
                "reason": dispatch_reason,
                "pause": controller.store.get_op("pause"),
                "drain": controller.store.get_op("drain"),
            },
            "items": items,
            "done_today": sum(
                item["status"] == "accepted"
                and _accepted_today(item["accepted_at"], timezone)
                for item in items
            ),
            "workers": workers,
            "settings": {
                "routes": routes,
                "role_routes": controller.cfg.role_routes,
                "environments": controller.cfg.environments,
                "workers": controller.cfg.workers,
                "repositories": controller.cfg.repositories,
                "budgets": controller.cfg.budgets,
                "windows": [window.__dict__ for window in controller.cfg.windows],
                "people": controller.cfg.humans,
                "workflow": controller.cfg.workflow_engine,
            },
        }
