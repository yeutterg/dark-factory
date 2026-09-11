"""Environment lifecycle protocol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol


@dataclass
class PreparedEnv:
    workspace: Path
    kind: str
    metadata: dict


class Environment(Protocol):
    name: str

    def prepare(self, work_item_id: str, repo: Optional[str]) -> PreparedEnv: ...

    def run(
        self, argv: list[str], cwd: Path, timeout_seconds: int
    ) -> tuple[int, str, str]: ...

    def stop(self, prepared: PreparedEnv) -> None: ...

    def release(self, prepared: PreparedEnv) -> None: ...
