"""Real guardian lifetime/crash tests with dummy tokens, never account access."""

import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dark_factory.adapters.harness.credential_file import credential_file, scrub_file


def wait_removed(path):
    deadline = time.monotonic() + 5
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not path.exists()


def test_guardian_erases_after_worker_sigkill(tmp_path):
    script = """
import sys
from dark_factory.adapters.harness.credential_file import credential_file
with credential_file(sys.argv[1], 'codex-agent/auth.json', 'dummy-token', 30):
    print('ready', flush=True)
    sys.stdin.buffer.read(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path / "runtime")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(10)
        assert child.stdout.readline() == b"ready\n"
        auth = tmp_path / "runtime" / "codex-agent" / "auth.json"
        assert auth.read_text() == "dummy-token"
        assert stat.S_IMODE(auth.stat().st_mode) == 0o600
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        wait_removed(auth)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


def test_scrubbing_skips_live_guardian_and_context_erases_on_error(tmp_path):
    runtime = tmp_path / "runtime"
    with pytest.raises(RuntimeError, match="fixture failure"):
        with credential_file(runtime, "codex-agent/auth.json", "dummy", 10) as path:
            assert not scrub_file(runtime, "codex-agent/auth.json")
            assert path.exists()
            raise RuntimeError("fixture failure")
    assert not path.exists()


def test_guardian_expires_even_when_parent_remains_alive(tmp_path):
    with credential_file(
        tmp_path / "runtime", "codex-agent/auth.json", "dummy", 0.1
    ) as path:
        wait_removed(path)


def test_existing_file_and_symlink_are_not_overwritten_or_erased(tmp_path):
    runtime = tmp_path / "runtime"
    agent = runtime / "codex-agent"
    agent.mkdir(parents=True)
    target = agent / "auth.json"
    target.write_text("existing-owned-file")
    with pytest.raises(FileExistsError):
        with credential_file(runtime, "codex-agent/auth.json", "dummy", 10):
            pass
    assert target.read_text() == "existing-owned-file"
    outside = tmp_path / "outside"
    outside.write_text("must-remain")
    link = agent / "linked.json"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        scrub_file(runtime, "codex-agent/linked.json")
    assert outside.read_text() == "must-remain"


def test_codex_adapter_erases_tokens_when_worker_process_is_killed(tmp_path):
    script = """
import sys
from pathlib import Path
from test_codex import run, stream
tail = "while os.path.exists(os.path.join(os.environ['CODEX_HOME'], 'auth.json')): time.sleep(0.02)\\n"
run(Path(sys.argv[1]), stream()[:1], tail)
"""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": os.pathsep.join(
            [str(Path("src").resolve()), str(Path("tests").resolve())]
        ),
    }
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    auth = tmp_path / "runtime" / "codex-agent" / "auth.json"
    try:
        deadline = time.monotonic() + 5
        while (
            not auth.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert auth.exists()
        child.kill()
        child.wait(timeout=5)
        wait_removed(auth)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()
        child.stderr.close()


def test_broken_pipe_does_not_skip_guardian_reaping(tmp_path, monkeypatch):
    actual = subprocess.Popen
    processes = []

    class BrokenClose:
        def __init__(self, stream):
            self.stream = stream

        def write(self, value):
            return self.stream.write(value)

        def flush(self):
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

        def close(self):
            self.stream.close()
            raise BrokenPipeError("fixture closed pipe")

    def spawn(*args, **kwargs):
        proc = actual(*args, **kwargs)
        proc.stdin = BrokenClose(proc.stdin)
        processes.append(proc)
        return proc

    monkeypatch.setattr(
        "dark_factory.adapters.harness.credential_file.subprocess.Popen", spawn
    )
    with credential_file(
        tmp_path / "runtime", "codex-agent/auth.json", "dummy", 10
    ) as path:
        assert path.exists()
    assert not path.exists()
    assert all(proc.poll() is not None for proc in processes)


def test_large_credential_document_remains_bounded_and_private(tmp_path):
    with credential_file(
        tmp_path / "runtime", "codex-agent/auth.json", "dummy" * 10000, 10
    ) as path:
        assert path.stat().st_size == 50000
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.exists()
