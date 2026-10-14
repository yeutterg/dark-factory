"""Harness lifecycle protocol."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from os import PathLike
from typing import Any, Protocol


@dataclass
class HarnessEvent:
    type: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessResult:
    ok: bool
    output: str
    usage: dict[str, Any]
    session_id: str | None
    cost_cents: int = 0
    error: str | None = None


class Harness(Protocol):
    name: str

    def start(
        self,
        prompt: str,
        workspace: PathLike,
        extra: dict[str, Any] | None = None,
    ) -> Iterator[HarnessEvent]: ...

    def wait(self) -> HarnessResult: ...

    def cancel(self) -> None: ...

    def attach_command(self, session_id: str) -> list[str]: ...
