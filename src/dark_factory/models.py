"""Normalized records for the factory controller."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

WORK_STATUSES = (
    "selected",
    "planning",
    "awaiting_approval",
    "approved",
    "running",
    "awaiting_review",
    "accepted",
    "failed",
    "cancelled",
    "rejected",
)

JOB_KINDS = (
    "plan",
    "critique",
    "implement",
    "check",
    "review",
    "command",
)

ATTEMPT_STATUSES = (
    "pending",
    "leased",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "archived",
)

PRESENTATION_STAGES = ("Plan", "Build", "Check", "Review", "Done")


def to_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError(type(obj))


@dataclass
class JobSpec:
    job_id: str
    kind: str
    role: Optional[str] = None
    command_id: Optional[str] = None
    repo: Optional[str] = None
    depends_on: list[str] = field(default_factory=list)
    budget_cents: int = 0
    timeout_seconds: int = 3600
    manual_gate: bool = False
    check_names: list[str] = field(default_factory=list)


@dataclass
class PlanGraph:
    version: int
    jobs: list[JobSpec]
    acceptance: list[str]
    spending_ceiling_cents: int
    permitted_routes: list[str]
    permitted_previews: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)


@dataclass
class WorkItem:
    id: str
    external_id: str
    url: str
    title: str
    body: str
    parent_id: Optional[str]
    repo: Optional[str]
    status: str
    is_root: bool
    iteration: Optional[str] = None
    plan_version: int = 0
    approved_digest: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    spending_ceiling_cents: int = 0
    reserved_cents: int = 0
    spent_cents: int = 0
    candidate_commits: dict[str, str] = field(default_factory=dict)
    presentation_stage: str = "Plan"


@dataclass
class Job:
    id: str
    work_item_id: str
    kind: str
    status: str
    spec_json: str
    depends_on: str
    lease_until: Optional[str] = None
    worker_id: Optional[str] = None
    reserved_cents: int = 0


@dataclass
class Attempt:
    id: str
    job_id: str
    worker_id: Optional[str]
    status: str
    workspace: Optional[str]
    route_id: Optional[str]
    started_at: Optional[str]
    ended_at: Optional[str]
    cost_cents: int = 0
    error: Optional[str] = None
    config_hash: Optional[str] = None
    prompt_hash: Optional[str] = None


@dataclass
class Evidence:
    id: str
    attempt_id: str
    kind: str
    path: Optional[str]
    digest: Optional[str]
    payload_json: str
    inputs_json: str
