"""One operator command table shared by the CLI and the authenticated API.

Shared commands accept the same parameters and return the same JSON result
whether they run locally or through ``/operator``. Commands that need the local
process (state files, workers, servers) are listed in LOCAL_ONLY and are never
reachable through the API.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from dark_factory.controller import Controller, ControllerError


def _compact(result: Any) -> tuple[str | None, int]:
    return (None if result is None else json.dumps(result)), 0


def _pretty(result: Any) -> tuple[str | None, int]:
    return json.dumps(result, indent=2), 0


@dataclass(frozen=True)
class OperatorCommand:
    name: str
    handler: Callable[[Controller, dict[str, Any], str], Any]
    # Parameter names the command reads; the CLI builds them from its arguments.
    params: tuple[str, ...] = ()
    # How the CLI prints a result: (text or None, exit code).
    render: Callable[[Any], tuple[str | None, int]] = field(default=_compact)


def _plan(ctrl: Controller, p: dict[str, Any], actor: str) -> dict[str, Any]:
    graph = ctrl.start_planning(p.get("work_item_id"), actor)
    return {"jobs": [job.job_id for job in graph.jobs]}


COMMANDS: dict[str, OperatorCommand] = {
    command.name: command
    for command in (
        OperatorCommand("status", lambda c, p, a: c.status(), render=_pretty),
        OperatorCommand(
            "intake",
            lambda c, p, a: {"ids": c.intake_issues(p["issues"], a)},
            ("issues",),
        ),
        OperatorCommand("plan", _plan, ("work_item_id",)),
        OperatorCommand(
            "proposal",
            lambda c, p, a: c.proposal(p.get("work_item_id"), p.get("version")),
            ("work_item_id", "version"),
            render=_pretty,
        ),
        OperatorCommand(
            "approve",
            lambda c, p, a: {"digest": c.approve(p.get("work_item_id"), a)},
            ("work_item_id",),
        ),
        OperatorCommand(
            "reject",
            lambda c, p, a: c.reject(
                p.get("work_item_id"), a, p.get("reason", "rejected")
            ),
            ("work_item_id", "reason"),
        ),
        OperatorCommand(
            "review",
            lambda c, p, a: c.review_packet(p.get("work_item_id")),
            ("work_item_id",),
        ),
        OperatorCommand(
            "manual-gate",
            lambda c, p, a: c.record_manual_gate(
                p.get("work_item_id"), p["name"], p["candidate"], a, p["evidence"]
            ),
            ("work_item_id", "name", "candidate", "evidence"),
        ),
        OperatorCommand(
            "create-pr",
            lambda c, p, a: c.external_action(
                p.get("work_item_id"), "create_pr", p["payload"], a
            ),
            ("work_item_id", "payload"),
            render=_pretty,
        ),
        OperatorCommand(
            "accept",
            lambda c, p, a: c.accept(p.get("work_item_id"), a),
            ("work_item_id",),
        ),
        OperatorCommand("pause", lambda c, p, a: c.pause(a)),
        OperatorCommand("resume", lambda c, p, a: c.resume(a)),
        OperatorCommand("drain", lambda c, p, a: c.drain(a)),
        OperatorCommand("undrain", lambda c, p, a: c.undrain(a)),
        OperatorCommand(
            "cancel", lambda c, p, a: c.cancel_job(p["job_id"], a), ("job_id",)
        ),
        OperatorCommand("cleanup", lambda c, p, a: {"cleaned": c.cleanup(a)}),
        OperatorCommand("recover", lambda c, p, a: {"recovered": c.recover(a)}),
        OperatorCommand(
            "reload",
            lambda c, p, a: {"reloaded": c.reload(a)},
            render=lambda result: (None, 0 if result["reloaded"] else 2),
        ),
    )
}

# Commands that act on the local process or filesystem and therefore never
# travel through the operator API.
LOCAL_ONLY = frozenset(
    {
        "preflight",  # Reads configuration without opening controller state.
        "execution-worker",  # Separate outbound worker process.
        "adapter",  # JSON command adapters invoked by the controller.
        "restore",  # Writes a new state directory.
        "backup",  # Copies the owned state directory.
        "run-worker",  # Runs jobs in this process.
        "serve",  # Starts the API itself.
        "demo",  # Simulation loop in this process.
    }
)


def run(controller: Controller, name: str, params: dict[str, Any], actor: str) -> Any:
    command = COMMANDS.get(name)
    if command is None:
        raise ControllerError("unsupported operator command")
    return command.handler(controller, params, actor or "operator")
