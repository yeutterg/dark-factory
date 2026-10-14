from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from dark_factory import artifacts
from dark_factory.adapters.env.native import process_environment
from dark_factory.adapters.env.registry import (
    CommandEnvironment,
    OciEnvironment,
    project_network,
)
from dark_factory.adapters.git.local import fixture
from dark_factory.runner import CommandRunner


def recipe(image="sha256:" + "a" * 64):
    return {"kind": "oci", "image": image, "cpu": 1, "memory_mb": 128, "pids": 32}


def test_container_policy_has_explicit_bounds_and_no_host_socket(tmp_path):
    env = OciEnvironment(tmp_path / "workspaces", recipe())
    prepared = env.prepare("task")
    (prepared.workspace / "src").mkdir()
    prefix = env.prefix(
        prepared,
        tmp_path / "runtime",
        network=False,
        writable=["src"],
        protected=["REPO"],
    )
    assert "--read-only" in prefix and "--pull=never" in prefix
    assert prefix[prefix.index("--network") + 1] == "none"
    assert prefix[prefix.index("--memory") + 1] == "128m"
    assert "--privileged" not in prefix and not any(
        "docker.sock" in arg for arg in prefix if arg.startswith("type=bind,")
    )
    with pytest.raises(ValueError, match="escapes"):
        env.prefix(
            prepared,
            tmp_path / "runtime",
            network=False,
            writable=["../other"],
            protected=[],
        )


def test_container_whole_workspace_write_does_not_duplicate_mount(tmp_path):
    env = OciEnvironment(tmp_path / "workspaces", recipe())
    prepared = env.prepare("task")
    prefix = env.prefix(
        prepared, tmp_path / "runtime", network=False, writable=["."], protected=[]
    )
    mounts = [
        arg
        for arg in prefix
        if arg.startswith("type=bind,source=" + str(prepared.workspace) + ",")
    ]
    assert len(mounts) == 1 and not mounts[0].endswith("readonly")


def test_project_network_rejects_shared_external_or_host_gateway(monkeypatch):
    base = {
        "Internal": True,
        "Labels": {"com.dark-factory.project": "project-one"},
        "Driver": "bridge",
        "EnableIPv6": False,
        "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
    }
    for data in (
        {**base, "Internal": False},
        {**base, "Labels": {}},
        {**base, "Options": {}},
    ):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, d=data, **k: subprocess.CompletedProcess(
                a, 0, json.dumps([d]), ""
            ),
        )
        with pytest.raises(RuntimeError):
            project_network({"network": "project-one-gateway"}, "project-one")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps([base]), ""),
    )
    assert (
        project_network({"network": "project-one-gateway"}, "project-one")
        == "project-one-gateway"
    )


def test_named_sandbox_contract_runs_stop_and_release(tmp_path):
    driver = Path("tests/fixtures/sandbox_driver.py").resolve()
    env = CommandEnvironment(
        tmp_path / "workspaces",
        {
            "kind": "command",
            "argv": [sys.executable, str(driver)],
            "driver_files": {str(driver): artifacts.digest(driver)},
        },
    )
    prepared = env.prepare("task")
    runtime = tmp_path / "runtime"
    prefix = env.prefix(prepared, runtime, network=False, writable=[], protected=[])
    code, stdout, _ = CommandRunner(prepared.workspace)._run(
        prefix + [sys.executable, "-c", "print('ok')"],
        b"",
        10,
        prepared.workspace,
        process_environment(runtime),
    )
    assert code == 0 and stdout.strip() == b"ok"
    env.stop(prepared)
    env.release(prepared)
    events = [
        json.loads(line)
        for line in (runtime / "driver-events.jsonl").read_text().splitlines()
    ]
    assert [e["operation"] for e in events] == ["execute", "stop", "release"]
    assert not prepared.workspace.exists()


def test_declared_build_outputs_not_archived_but_tracked_source_preserved(tmp_path):
    repo = Path(fixture(tmp_path / "repo")["path"])
    (repo / "build").mkdir()
    (repo / "build" / "cache").write_text("reproducible")
    destination = tmp_path / "archive"
    artifacts.archive(repo, destination, {}, {}, ephemeral_paths=["build"])
    assert not (destination / "workspace" / "build").exists()
    assert (destination / "workspace" / "message.py").exists()
    assert artifacts.verify(destination)["discarded_ephemeral_paths"] == ["build"]
    with pytest.raises(ValueError, match="tracked"):
        artifacts.archive(
            repo, tmp_path / "invalid", {}, {}, ephemeral_paths=["message.py"]
        )


def installed_image():
    if not shutil.which("docker"):
        pytest.skip("Docker unavailable")
    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "python:3.12-slim-bookworm",
            "--format",
            "{{.Id}}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        pytest.skip("cached qualification image absent; never pull implicitly")
    return result.stdout.strip()


def test_installed_oci_denies_private_files_and_protected_writes(tmp_path):
    # Docker-on-macOS commonly shares /Users, not pytest's /private/var/folders.
    root = Path("state").resolve()
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="oci-qualification-", dir=root) as temporary:
        _installed_oci_isolation(Path(temporary))


def _installed_oci_isolation(tmp_path):
    selected = recipe(installed_image())
    selected["daemon_host"] = subprocess.run(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    env = OciEnvironment(tmp_path / "workspaces", selected)
    prepared = env.prepare("task")
    runtime = tmp_path / "runtime"
    (prepared.workspace / "src").mkdir()
    (prepared.workspace / "protected").write_text("approved")
    private = tmp_path / "other-project-key"
    private.write_text("must-not-read")
    prefix = env.prefix(
        prepared, runtime, network=False, writable=["src"], protected=["protected"]
    )
    script = f"""from pathlib import Path
Path({str(prepared.workspace / "src" / "new.py")!r}).write_text('ok')
for path in [{str(private)!r}, {str(prepared.workspace / "protected")!r}]:
    try:
        if path.endswith('protected'): Path(path).write_text('tamper')
        else: Path(path).read_text()
    except OSError: pass
    else: raise AssertionError(path)
assert not Path('/var/run/docker.sock').exists()
print('isolated')
"""
    try:
        code, out, err = CommandRunner(prepared.workspace)._run(
            prefix + ["python3", "-c", script],
            b"",
            30,
            prepared.workspace,
            process_environment(runtime),
        )
        assert code == 0, err.decode()
        assert out.strip() == b"isolated"
        assert (prepared.workspace / "protected").read_text() == "approved"
    finally:
        env.stop(prepared)
        env.release(prepared)
