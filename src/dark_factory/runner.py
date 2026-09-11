"""Run named commands with argument arrays, limits, and JSON contracts."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
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
        code, stdout, stderr = self._run(
            argv, stdin, timeout_seconds, self.workdir, merged
        )
        if code != 0:
            err = stderr.decode("utf-8", errors="replace")[-2000:]
            raise RunnerError(f"command exited {code}: {err}")
        text = stdout.decode("utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ContractError("invalid JSON on stdout") from exc
        return validate_result(data, operation, request_id)

    def run_plain(
        self,
        argv: list[str],
        timeout_seconds: int,
        cwd: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
    ) -> tuple[int, str, str]:
        code, stdout, stderr = self._run(
            argv,
            b"",
            timeout_seconds,
            cwd or self.workdir,
            {**os.environ, **(env or {})},
        )
        return (
            code,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    def _run(
        self,
        argv,
        stdin,
        timeout_seconds,
        cwd,
        env,
        *,
        should_stop=None,
        on_output=None,
        on_start=None,
    ):
        if not argv or not all(isinstance(x, str) for x in argv):
            raise RunnerError("argv must be a non-empty list of strings")
        if timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise RunnerError("time and output limits must be positive")
        # A file for stdin avoids deadlock while draining both output pipes.
        with (
            tempfile.TemporaryFile() as request,
            selectors.DefaultSelector() as selector,
        ):
            request.write(stdin)
            request.seek(0)
            proc = subprocess.Popen(
                argv,
                stdin=request,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
            chunks = {"stdout": bytearray(), "stderr": bytearray()}
            deadline = time.monotonic() + timeout_seconds
            total = 0
            try:
                if on_start:
                    on_start(proc.pid)
                for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map() or proc.poll() is None:
                    if should_stop and should_stop():
                        raise RunnerError("command cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RunnerError(f"command timed out after {timeout_seconds}s")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(data)
                        if total > self.max_output_bytes:
                            raise RunnerError("stdout/stderr exceeded output limit")
                        chunks[key.data].extend(data)
                        if on_output:
                            on_output(key.data, data)
                return proc.wait(), bytes(chunks["stdout"]), bytes(chunks["stderr"])
            finally:
                # Descendants must not survive a timeout, cancellation or parent exit.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                proc.stdout.close()
                proc.stderr.close()
