"""Deterministic role fixtures; executes no model calls."""

from __future__ import annotations

import json
from pathlib import Path

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult


class FakeHarness:
    name = "fake"

    def __init__(self, scripted=None):
        self.scripted = scripted or {}
        self._cancelled = False
        self._last = HarnessResult(False, "", {}, None)

    def start(self, prompt, workspace, extra=None):
        role = (extra or {}).get("role", "coder")
        self._cancelled = False
        yield HarnessEvent("start", f"fixture role={role}")
        if self._cancelled:
            return
        body = self.scripted.get(role)
        if body is None:
            if role == "planner":
                body = json.dumps(
                    {
                        "why": "Complete the selected fixture outcome",
                        "what": "Return ready from message()",
                        "assumptions": ["Single repository; no deployment"],
                        "needs_input": [],
                        "acceptance": [
                            {
                                "id": "AC-01",
                                "description": "message() returns ready",
                                "checks": (extra or {}).get(
                                    "checks", ["fixture-check"]
                                ),
                                "manual_gates": [],
                            },
                            {
                                "id": "AC-02",
                                "description": "Canonical verification passes",
                                "checks": (extra or {}).get(
                                    "checks", ["fixture-check"]
                                ),
                                "manual_gates": [],
                            },
                        ],
                        "tasks": [
                            {
                                "description": "Update the message implementation",
                                "checks": (extra or {}).get(
                                    "checks", ["fixture-check"]
                                ),
                            }
                        ],
                        "test_instructions": [
                            "Run the canonical verification command",
                            "Inspect the exact candidate diff",
                        ],
                    }
                )
            elif role == "critic":
                body = json.dumps(
                    {
                        "passed": True,
                        "findings": [],
                        "summary": "Plan matches the bounded fixture requirements",
                    }
                )
            elif role == "reviewer":
                body = json.dumps(
                    {
                        "passed": True,
                        "findings": [],
                        "summary": "Candidate diff and check evidence match the fixture plan",
                        "criteria": [
                            {
                                "id": c["id"],
                                "status": "needs_manual"
                                if c["manual_gates"]
                                else "satisfied",
                                "evidence": ["check:" + name for name in c["checks"]],
                            }
                            for c in (extra or {}).get("criteria", [])
                        ],
                    }
                )
            else:
                (Path(workspace) / "message.py").write_text(
                    'def message():\n    return "ready"\n'
                )
                body = "Updated message.py in the isolated fixture repository"
        yield HarnessEvent("text", body)
        self._last = HarnessResult(
            True, body, {"input": 10, "output": 20}, "fake-session", cost_cents=1
        )

    def wait(self):
        if self._cancelled:
            return HarnessResult(False, "", {}, None, error="cancelled")
        return self._last

    def cancel(self):
        self._cancelled = True

    def attach_command(self, session_id):
        return ["echo", "fake sessions are not interactive"]
