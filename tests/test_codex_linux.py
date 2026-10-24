"""Installed Codex-in-Bubblewrap qualification without external inference."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from dark_factory.adapters.env.linux_bwrap import sandbox_command
from dark_factory.adapters.env.native import process_environment
from dark_factory.adapters.harness.codex import inline_table

CODEX = shutil.which("codex")
pytestmark = pytest.mark.skipif(
    platform.system() != "Linux"
    or not CODEX
    or os.environ.get("DARK_FACTORY_CODEX_LINUX_QUALIFICATION") != "1",
    reason="explicit privileged container qualification with installed Codex required",
)


def test_installed_codex_sandbox_isolates_worker_container(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    private = tmp_path / "worker-private"
    for path in (workspace / "src", workspace / ".git", runtime / "tmp", private):
        path.mkdir(parents=True)
    (workspace / "public.txt").write_text("PUBLIC_FIXTURE")
    agent = runtime / "codex-agent"
    agent.mkdir()
    credential = agent / "auth.json"
    credential.write_text("DUMMY_CREDENTIAL_FIXTURE")
    (private / "token").write_text("WORKER_PRIVATE_FIXTURE")

    filesystem = {
        ":root": "deny",
        ":minimal": "read",
        ":workspace_roots": "read",
        str(workspace / "src"): "write",
        str(workspace / ".git"): "read",
        str(runtime / "tmp"): "write",
        str(agent / "tmp"): "read",
        str(credential): "deny",
    }
    base = [
        str(CODEX),
        "sandbox",
        "--cd",
        str(workspace),
        "-P",
        "factory",
        "-c",
        "permissions.factory.filesystem=" + inline_table(filesystem),
        "-c",
        "permissions.factory.network.enabled=false",
    ]
    env = {**process_environment(runtime), "CODEX_HOME": str(agent)}

    policy = {
        "workspace": str(workspace),
        "runtime": str(runtime),
        "network": False,
        "writable": ["src"],
        "protected": [".git"],
        "immutable_runtime_files": [],
        "self_sandbox_binaries": [str(CODEX)],
    }
    production = [str(CODEX), "exec", "--help"]
    assert sandbox_command(policy, production) == production

    def run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [*base, *command],
            cwd=workspace,
            env=env,
            capture_output=True,
            timeout=20,
            check=False,
        )

    public = run(["/bin/cat", str(workspace / "public.txt")])
    assert public.returncode == 0 and public.stdout == b"PUBLIC_FIXTURE"
    writable = run(["/usr/bin/touch", str(workspace / "src" / "codex-write")])
    assert writable.returncode == 0
    assert (workspace / "src" / "codex-write").is_file()

    for denied, marker in (
        (credential, b"DUMMY_CREDENTIAL_FIXTURE"),
        (private / "token", b"WORKER_PRIVATE_FIXTURE"),
    ):
        result = run(["/bin/cat", str(denied)])
        assert result.returncode != 0
        assert marker not in result.stdout + result.stderr
    protected = run(["/usr/bin/touch", str(workspace / ".git" / "blocked")])
    assert protected.returncode != 0
    assert not (workspace / ".git" / "blocked").exists()
    process_status = run(["/bin/cat", "/proc/self/status"])
    assert process_status.returncode != 0
    assert process_status.stdout == b""
