"""Shared controller primitives: errors, transactions and stable digests."""

from __future__ import annotations

import hashlib
import json
import threading
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dark_factory.config import FactoryConfig
    from dark_factory.runner import CommandRunner
    from dark_factory.store import Store


class ControllerError(RuntimeError):
    pass


class ControllerCore:
    """State and cross-module calls shared by every controller responsibility."""

    cfg: FactoryConfig
    store: Store
    runner: CommandRunner
    packaged_prompts: Path
    _decision_scope: threading.local

    if TYPE_CHECKING:

        def decision(self) -> Any: ...
        def _policy(self) -> dict: ...
        def _bindings(self) -> dict: ...
        def _approval_current(self, row) -> bool: ...
        def _approval_payload(self, wid) -> dict: ...
        def jobs(self, wid) -> list[dict]: ...
        def _external(self, wid, row=None) -> dict: ...
        def _packet(self, wid, error=None) -> dict: ...
        def reconcile_continuations(self) -> None: ...
        def _require_human(self, actor, row) -> None: ...
        def _fail_root(self, wid, error) -> None: ...
        def _record_interruption(
            self, root, job, attempt_id, error, timed_out
        ) -> None: ...
        def _resume_input(self, job_id, attempt_id) -> dict | None: ...
        def seed_plan_tasks(
            self, wid: str, proposal: dict, actor: str = "controller"
        ) -> int: ...
        def tasks_view(self, wid: str) -> dict: ...
        def _refresh_task_document(self, wid: str, actor: str) -> None: ...
        def record_manual_gate(
            self,
            wid: str,
            name: str,
            candidate: str,
            actor: str,
            evidence: str,
            passed: bool = True,
        ) -> None: ...


def atomic(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.store.transaction(), self.decision():
            return method(self, *args, **kwargs)

    return wrapped


def decided(method):
    """Evaluate a read-only view against one consistent policy snapshot."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.decision():
            return method(self, *args, **kwargs)

    return wrapped


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
