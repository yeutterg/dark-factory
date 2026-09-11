"""Pi coding-agent adapter. Qualifies when the `pi` binary is on PATH."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterator, Optional

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult
from dark_factory.runner import RunnerError


class PiHarness:
    name = "pi"

    def __init__(self, binary: str = "pi") -> None:
        self.binary = binary
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._result = HarnessResult(ok=False, output="", usage={}, session_id=None, error="not started")

    @classmethod
    def available(cls, binary: str = "pi") -> bool:
        return shutil.which(binary) is not None

    def start(
        self,
        prompt: str,
        workspace: Path,
        extra: Optional[dict[str, Any]] = None,
    ) -> Iterator[HarnessEvent]:
        if not self.available(self.binary):
            raise RunnerError("pi binary not found; use harness=fake for bootstrap tests")
        argv = [self.binary, "--print", prompt]
        self._proc = subprocess.Popen(
            argv,
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        yield HarnessEvent("start", "pi started")
        assert self._proc.stdout is not None
        chunks: list[str] = []
        for raw in self._proc.stdout:
            text = raw.decode("utf-8", errors="replace")
            chunks.append(text)
            yield HarnessEvent("text", text)
        code = self._proc.wait()
        err = self._proc.stderr.read().decode("utf-8", errors="replace") if self._proc.stderr else ""
        output = "".join(chunks)
        self._result = HarnessResult(
            ok=code == 0,
            output=output,
            usage={},
            session_id=None,
            error=None if code == 0 else err or f"pi exited {code}",
        )

    def wait(self) -> HarnessResult:
        return self._result

    def cancel(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def attach_command(self, session_id: str) -> list[str]:
        return [self.binary, "resume", session_id]
