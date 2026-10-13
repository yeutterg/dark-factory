"""Owned transient credentials: a separate process erases them on parent EOF.

The guardian starts before the file exists. Tokens travel only over its private
stdin, never argv/environment/logs. A lock lets recovery distinguish a live
guardian from stale files after a host crash. This does not revoke provider tokens.
"""

from __future__ import annotations

import fcntl
import json
import os
import selectors
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def owned_path(runtime, relative):
    runtime = Path(runtime)
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("credential path must stay inside owned runtime")
    path = runtime / relative
    parents = [runtime.parent, runtime] + [
        runtime.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts))
    ]
    for parent in parents:
        if parent.is_symlink():
            raise ValueError("credential runtime cannot contain symlinks")
    if path.is_symlink():
        raise ValueError("credential file cannot be a symlink")
    return path


@contextmanager
def owner_lock(path):
    fd = os.open(str(path) + ".guard", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def scrub_file(runtime, relative):
    path = owned_path(runtime, relative)
    if not path.parent.exists():
        return False
    try:
        with owner_lock(path):
            existed = path.exists()
            path.unlink(missing_ok=True)
            return existed
    except BlockingIOError:
        return False  # Never remove a live guardian's credentials.


@contextmanager
def credential_file(runtime, relative, content, timeout_seconds):
    path = owned_path(runtime, relative)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        raise FileExistsError("credential file already exists in attempt runtime")
    guardian = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": os.defpath},
        start_new_session=True,
    )
    try:
        request = (
            json.dumps(
                {
                    "runtime": str(Path(runtime).resolve()),
                    "relative": relative,
                    "content": content,
                    "timeout": timeout_seconds,
                }
            ).encode()
            + b"\n"
        )
        if len(request) > 65536:
            raise ValueError("credential document exceeds size limit")
        # Bound the startup write too: a stalled guardian cannot block on a full pipe.
        os.set_blocking(guardian.stdin.fileno(), False)
        deadline = time.monotonic() + 5
        remaining = memoryview(request)
        with selectors.DefaultSelector() as writable:
            writable.register(guardian.stdin, selectors.EVENT_WRITE)
            while remaining:
                if not writable.select(max(0, deadline - time.monotonic())):
                    raise RuntimeError("credential guardian startup timed out")
                try:
                    remaining = remaining[
                        os.write(guardian.stdin.fileno(), remaining) :
                    ]
                except BlockingIOError:
                    continue
        with selectors.DefaultSelector() as ready:
            ready.register(guardian.stdout, selectors.EVENT_READ)
            if (
                not ready.select(max(0, deadline - time.monotonic()))
                or os.read(guardian.stdout.fileno(), 16) != b"ready\n"
            ):
                raise RuntimeError("credential guardian did not become ready")
        yield path
    finally:
        try:
            guardian.stdin.close()  # Also happens automatically on worker SIGKILL.
        except BrokenPipeError:
            pass  # A dead guardian must not prevent reaping and owner cleanup.
        try:
            guardian.wait(timeout=5)
        except subprocess.TimeoutExpired:
            guardian.kill()
            guardian.wait()
        guardian.stdout.close()
        # If the guardian itself failed, the owner can still erase its file.
        scrub_file(runtime, relative)


def guard():
    request = sys.stdin.buffer.readline(65537)
    if len(request) > 65536 or not request.endswith(b"\n"):
        raise ValueError("invalid guardian request")
    payload = json.loads(request)
    timeout = payload["timeout"]
    if type(timeout) not in {int, float} or not 0 < timeout < float("inf"):
        raise ValueError("invalid credential lifetime")
    path = owned_path(payload["runtime"], payload["relative"])
    with owner_lock(path):
        # Refuse to overwrite another file; create with private mode from the outset.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(payload["content"])
            sys.stdout.write("ready\n")
            sys.stdout.flush()
            with selectors.DefaultSelector() as lifetime:
                lifetime.register(sys.stdin, selectors.EVENT_READ)
                lifetime.select(timeout)  # Parent EOF or the original bounded lifetime.
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        guard()
    except Exception:
        sys.exit(1)  # Never print credential-bearing exceptions or inputs.
