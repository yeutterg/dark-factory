"""Harness lifecycle protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from os import PathLike
from typing import Any, Iterator, Optional, Protocol


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
    session_id: Optional[str]
    cost_cents: int = 0
    error: Optional[str] = None


class Harness(Protocol):
    name: str

    def start(
        self,
        prompt: str,
        workspace: PathLike,
        extra: Optional[dict[str, Any]] = None,
    ) -> Iterator[HarnessEvent]:
        ...

    def wait(self) -> HarnessResult:
        ...

    def cancel(self) -> None:
        ...

    def attach_command(self, session_id: str) -> list[str]:
        ...
