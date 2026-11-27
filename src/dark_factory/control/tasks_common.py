"""Shared constants for the task list modules."""

from __future__ import annotations

import hashlib
import re

TASK_STATES = frozenset(
    {"inbox", "queued", "active", "waiting", "reported_done", "done", "cancelled"}
)
OWNER_KINDS = frozenset({"agent", "controller", "human", "external"})
VERIFICATION_STATES = frozenset(
    {"not_required", "pending", "passed", "failed", "stale"}
)
PROGRESS_KINDS = frozenset(
    {
        "task_started",
        "task_progress",
        "task_reported_done",
        "task_blocked",
        "task_proposed",
    }
)
TASK_LINE = re.compile(
    r"^- \[(?P<checked>[ xX])\] (?P<title>.*?)"
    r"(?: <!-- task:(?P<id>[A-Za-z0-9_-]+) owner:(?P<owner>[a-z]+) -->)?$"
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
