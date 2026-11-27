from __future__ import annotations

from pathlib import Path

import pytest

from dark_factory.adapters.env.linux_bwrap import sandbox_command


def policy(tmp_path: Path) -> dict:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    (workspace / "src").mkdir()
    (workspace / ".git").mkdir()
    (runtime / "agent").mkdir()
    (runtime / "agent" / "settings.json").write_text("{}")
    return {
        "workspace": str(workspace),
        "runtime": str(runtime),
        "network": True,
        "writable": ["src"],
        "protected": [".git"],
        "immutable_runtime_files": ["agent/settings.json"],
    }


def test_bwrap_driver_mounts_only_owned_work_and_runtime(tmp_path: Path) -> None:
    item = policy(tmp_path)
    argv = sandbox_command(item, ["/bin/echo", "ok"])
    workspace = item["workspace"]
    runtime = item["runtime"]
    triples = [argv[index : index + 3] for index in range(len(argv) - 2)]
    assert argv[:4] == [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-ipc",
    ]
    assert "--unshare-net" not in argv
    assert ["--ro-bind", workspace, workspace] in triples
    assert ["--bind", f"{workspace}/src", f"{workspace}/src"] in triples
    assert ["--bind", runtime, runtime] in triples
    assert argv[-3:] == ["--", "/bin/echo", "ok"]


def test_bwrap_driver_rejects_path_escape(tmp_path: Path) -> None:
    item = policy(tmp_path)
    item["writable"] = ["../private"]
    with pytest.raises(ValueError, match="escapes"):
        sandbox_command(item, ["/bin/true"])


def test_bwrap_driver_denies_network_by_default(tmp_path: Path) -> None:
    item = policy(tmp_path)
    item["network"] = False
    assert "--unshare-net" in sandbox_command(item, ["/bin/true"])


def test_bwrap_driver_sizes_scratch_space_from_the_environment_limits(
    tmp_path: Path,
) -> None:
    item = policy(tmp_path)
    item["limits"] = {"memory_mb": 1024, "pids": 64, "cpu": 1}
    argv = sandbox_command(item, ["/bin/true"])
    at = argv.index("--size")
    assert argv[at : at + 3] == ["--size", str(256 * 1024 * 1024), "--tmpfs"]


def test_bwrap_driver_bounds_processes_before_it_executes(
    tmp_path: Path, monkeypatch
) -> None:
    import resource

    from dark_factory.adapters.env import linux_bwrap

    item = policy(tmp_path)
    item["limits"] = {"memory_mb": 512, "pids": 77, "cpu": 1}
    seen = {}
    monkeypatch.setattr(resource, "setrlimit", lambda r, v: seen.update({r: v}))
    monkeypatch.setattr(linux_bwrap.os, "execvpe", lambda *a: seen.update(exec=a[0]))
    linux_bwrap.main(
        ["--factory-policy", __import__("json").dumps(item), "--", "/bin/true"]
    )
    assert seen[resource.RLIMIT_NPROC] == (77, 77)
    assert seen[resource.RLIMIT_CORE] == (0, 0)
    assert "exec" in seen


def test_bwrap_driver_rejects_nonsense_limits(tmp_path: Path) -> None:
    item = policy(tmp_path)
    item["limits"] = {"pids": 0}
    with pytest.raises(ValueError, match="positive"):
        sandbox_command(item, ["/bin/true"])
