"""Run named commands with argument arrays, limits, and JSON contracts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from dark_factory.contracts import ContractError, make_request, validate_result


class RunnerError(RuntimeError):
    pass


class CommandRunner:
    def __init__(self, workdir: Path, max_output_bytes: int = 2_000_000) -> None:
        self.workdir = Path(workdir)
        self.max_output_bytes = max_output_bytes

    def run_json(
        self,
        argv: list[str],
        operation: str,
        payload: dict[str, Any],
        request_id: str,
        timeout_seconds: int,
        env: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        if not argv or not all(isinstance(x, str) for x in argv):
            raise RunnerError("argv must be a non-empty list of strings")
        request = make_request(operation, payload, request_id)
        stdin = json.dumps(request).encode("utf-8")
        merged = os.environ.copy()
        if env:
            merged.update(env)
        try:
            proc = subprocess.run(
                argv,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                cwd=self.workdir,
                env=merged,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"command timed out after {timeout_seconds}s") from exc
        if len(proc.stdout) > self.max_output_bytes:
            raise RunnerError("stdout exceeded output limit")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")[-2000:]
            raise RunnerError(f"command exited {proc.returncode}: {err}")
        text = proc.stdout.decode("utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ContractError("invalid JSON on stdout") from exc
        return validate_result(data, operation)

    def run_plain(
        self,
        argv: list[str],
        timeout_seconds: int,
        cwd: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
    ) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                cwd=str(cwd or self.workdir),
                env={**os.environ, **(env or {})},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"command timed out after {timeout_seconds}s") from exc
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )
