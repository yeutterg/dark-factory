"""Shared lifecycle for harness CLIs that stream JSON lines on stdout."""

from __future__ import annotations

import queue
import shutil
import threading
from typing import Callable, Iterator

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult


class JsonLines:
    """Split streamed stdout bytes into complete, non-blank lines."""

    def __init__(self, handle: Callable[[bytes], None]) -> None:
        self.handle = handle
        self.pending = bytearray()

    def __call__(self, name: str, data: bytes) -> None:
        if name != "stdout":
            return
        self.pending.extend(data)
        while b"\n" in self.pending:
            line, _, rest = self.pending.partition(b"\n")
            self.pending[:] = rest
            if line.strip():
                self.handle(line)

    @property
    def incomplete(self) -> bool:
        return bool(self.pending.strip())

    def flush(self) -> None:
        """Treat an unterminated final line as complete."""
        if self.incomplete:
            self("stdout", b"\n")


class StreamingCliHarness:
    """Run one supervised CLI per attempt on a background thread.

    Subclasses build argv/env and interpret events. The supervised run executes
    off-thread so the worker can keep heartbeating, observe cancellation and
    enforce its own deadline while the CLI is silent.
    """

    name = ""

    def __init__(self, binary: str) -> None:
        self.binary = binary
        self._cancel = threading.Event()
        self._result = HarnessResult(False, "", {}, None, error="not started")

    @classmethod
    def available(cls, binary: str) -> bool:
        return shutil.which(binary) is not None

    def wait(self) -> HarnessResult:
        return self._result

    def cancel(self) -> None:
        self._cancel.set()

    def _stream(
        self,
        run: Callable[[Callable[[HarnessEvent], None]], None],
        *,
        stop_on_exit: bool = False,
    ) -> Iterator[HarnessEvent]:
        """Yield events emitted by run(emit), with heartbeats while it is quiet.

        run must set self._result before returning. With stop_on_exit, closing
        the generator early also cancels the CLI and bounds the join.
        """
        events: queue.Queue = queue.Queue()

        def target():
            try:
                run(events.put)
            finally:
                events.put(None)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    event = events.get(timeout=0.1)
                except queue.Empty:
                    yield HarnessEvent("heartbeat")
                    continue
                if event is None:
                    break
                yield event
            if not stop_on_exit:
                thread.join()
        finally:
            if stop_on_exit:
                self._cancel.set()
                thread.join(timeout=5)
