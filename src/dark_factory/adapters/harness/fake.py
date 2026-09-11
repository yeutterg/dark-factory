"""In-process harness for tests and bootstrap demos."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Optional

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult


class FakeHarness:
    name = "fake"

    def __init__(self, scripted: Optional[dict[str, str]] = None) -> None:
        self.scripted = scripted or {}
        self._last = HarnessResult(ok=True, output="", usage={}, session_id="fake-session")
        self._cancelled = False
        self._prompt = ""

    def start(
        self,
        prompt: str,
        workspace: Path,
        extra: Optional[dict[str, Any]] = None,
    ) -> Iterator[HarnessEvent]:
        self._cancelled = False
        self._prompt = prompt
        role = (extra or {}).get("role", "coder")
        yield HarnessEvent("start", f"fake harness role={role}")
        if self._cancelled:
            return
        body = self.scripted.get(role, self._default_output(role, workspace))
        Path(workspace).mkdir(parents=True, exist_ok=True)
        if role == "coder":
            (Path(workspace) / "CHANGELOG.md").write_text("factory implemented the selected work\n", encoding="utf-8")
        yield HarnessEvent("text", body)
        self._last = HarnessResult(
            ok=True,
            output=body,
            usage={"input_tokens": 10, "output_tokens": 20},
            session_id="fake-session",
            cost_cents=1,
        )

    def wait(self) -> HarnessResult:
        if self._cancelled:
            return HarnessResult(ok=False, output="", usage={}, session_id=None, error="cancelled")
        return self._last

    def cancel(self) -> None:
        self._cancelled = True

    def attach_command(self, session_id: str) -> list[str]:
        return ["echo", f"attach {session_id}"]

    def _default_output(self, role: str, workspace: Path) -> str:
        if role == "planner":
            return (
                "Why: selected work should be implemented with a bounded graph.\n"
                "What: add a documented change in the target repository.\n"
                "Assumption: single repository, no preview target.\n"
                "Needs your input: none for bootstrap demo.\n"
            )
        if role == "critic":
            return "Critique: summary matches the proposed tasks. No blocking issues."
        if role == "reviewer":
            return "Independent review: checks passed on the captured evidence. Remaining manual test: read CHANGELOG.md."
        return f"Implemented in {workspace}"
