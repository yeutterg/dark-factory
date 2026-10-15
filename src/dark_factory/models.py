"""Normalized records for the factory controller."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class WorkStatus(StrEnum):
    SELECTED = "selected"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    ACCEPTED = "accepted"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    INTERRUPTED = "interrupted"  # A step stopped; a person decides to resume or stop.


class JobStatus(StrEnum):
    STANDBY = "standby"
    BLOCKED = "blocked"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"  # Its attempt stopped; it may continue from saved work.
    SUPERSEDED = "superseded"
    SUPERSEDED_BY_REPAIR = "superseded_by_repair"
    NOT_NEEDED = "not_needed"


class AttemptStatus(StrEnum):
    LEASED = "leased"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    NOT_STARTED = "not_started"


JOB_KINDS = (
    "plan",
    "critique",
    "implement",
    "check",
    "review",
    "command",
)
PLANNING_KINDS = frozenset({"plan", "critique"})
EVIDENCE_KINDS = frozenset({"check", "review"})

ACTIVE_ATTEMPT = frozenset(
    {AttemptStatus.LEASED, AttemptStatus.RUNNING, AttemptStatus.CANCELLING}
)
DISPATCHABLE_ROOT = frozenset(
    {WorkStatus.PLANNING, WorkStatus.APPROVED, WorkStatus.RUNNING}
)
TERMINAL_ROOT = frozenset(
    {WorkStatus.ACCEPTED, WorkStatus.FAILED, WorkStatus.REJECTED, WorkStatus.CANCELLED}
)
INACTIVE_REPAIR = frozenset({JobStatus.STANDBY, JobStatus.NOT_NEEDED})

PRESENTATION_STAGES = ("Plan", "Build", "Check", "Review", "Done")


def sql_set(values) -> str:
    """Render a fixed status set as a SQL list. Only enum members are accepted."""
    if not all(isinstance(v, StrEnum) for v in values):
        raise TypeError("sql_set accepts status enum members only")
    return "(" + ",".join(f"'{v}'" for v in sorted(values)) + ")"


@dataclass
class JobSpec:
    job_id: str
    kind: str
    role: str | None = None
    command_id: str | None = None
    repo: str | None = None
    depends_on: list[str] = field(default_factory=list)
    budget_cents: int = 0
    timeout_seconds: int = 3600
    manual_gate: bool = False
    check_names: list[str] = field(default_factory=list)
    repair: bool = False


@dataclass
class PlanGraph:
    version: int
    jobs: list[JobSpec]
    acceptance: list[str]
    spending_ceiling_cents: int
    permitted_routes: list[str]
    permitted_previews: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
