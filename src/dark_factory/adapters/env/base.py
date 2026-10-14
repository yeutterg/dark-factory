"""Environment lifecycle protocol, implemented by the native, OCI and command backends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass
class PreparedEnv:
    workspace: Path
    kind: str
    metadata: dict


class Environment(Protocol):
    name: str

    def prepare(
        self, attempt_id: str, source: dict[str, Any] | None = None
    ) -> PreparedEnv:
        """Create the attempt's owned workspace, cloning source at its commit."""
        ...

    def prefix(
        self,
        prepared: PreparedEnv,
        runtime: Path,
        *,
        network: bool,
        writable: list[str],
        protected: list[str],
        private_paths: list[Path] | None = None,
        readable_files: list[Path] | None = None,
        immutable_runtime_files: tuple[str, ...] = (),
    ) -> list[str]:
        """argv prefix that runs a command under this backend's isolation policy."""
        ...

    def stop(self, prepared: PreparedEnv) -> None: ...

    def release(self, prepared: PreparedEnv) -> None:
        """Remove owned resources; callers archive unique work first."""
        ...
