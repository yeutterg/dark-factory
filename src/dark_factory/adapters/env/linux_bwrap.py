"""Synchronous Bubblewrap driver for a containerized Linux execution worker."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from pathlib import Path


def _owned_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError(f"{name} must be an absolute path")
    path = Path(value).resolve()
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"{name} must be an existing real directory")
    return path


def _relative_path(root: Path, value: object, name: str) -> Path:
    if not isinstance(value, str) or not value or value == ".":
        raise ValueError(f"invalid {name}")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{name} escapes the workspace")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"{name} escapes the workspace")
    return path


def sandbox_command(policy: dict, command: list[str]) -> list[str]:
    workspace = _owned_path(policy.get("workspace"), "workspace")
    runtime = _owned_path(policy.get("runtime"), "runtime")
    if (
        workspace == runtime
        or workspace.is_relative_to(runtime)
        or runtime.is_relative_to(workspace)
    ):
        raise ValueError("workspace and runtime must be separate")
    if not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("sandbox command is required")

    self_sandbox = policy.get("self_sandbox_binaries", [])
    if not isinstance(self_sandbox, list) or not all(
        isinstance(item, str) and item.startswith("/") for item in self_sandbox
    ):
        raise ValueError("self-sandbox binaries must be absolute paths")
    if command[0] in self_sandbox:
        binary = Path(command[0])
        binary_target = binary.resolve()
        mode = binary_target.stat()
        if (
            binary.name != "codex"
            or not binary_target.is_file()
            or mode.st_uid != 0
            or stat.S_IMODE(mode.st_mode) & 0o022
            or not os.access(binary_target, os.X_OK)
        ):
            raise ValueError(
                "self-sandbox Codex binary must be root-owned and immutable"
            )
        if command[1:] == ["--version"] or command[1:2] == ["exec"]:
            # Codex is a trusted pinned broker. Its own Linux Bubblewrap process is
            # the model-tool boundary; nesting it inside this Bubblewrap boundary
            # fails closed because the inner process cannot create its user map.
            return command
        raise ValueError("unsupported self-sandbox command")

    writable = policy.get("writable", [])
    protected = policy.get("protected", [])
    immutable = policy.get("immutable_runtime_files", [])
    if not all(isinstance(items, list) for items in (writable, protected, immutable)):
        raise ValueError("sandbox path policy must use lists")

    argv = [
        shutil.which("bwrap") or "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup-try",
    ]
    if policy.get("network") is False:
        argv.append("--unshare-net")
    elif policy.get("network") is not True:
        raise ValueError("network policy must be boolean")

    for system_path in ("/usr", "/etc"):
        if Path(system_path).is_dir():
            argv += ["--ro-bind", system_path, system_path]
    for link_target, source in (
        ("/bin", "usr/bin"),
        ("/sbin", "usr/sbin"),
        ("/lib", "usr/lib"),
        ("/lib64", "usr/lib64"),
    ):
        argv += ["--symlink", source, link_target]
    argv += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/run",
        "--bind",
        str(runtime),
        str(runtime),
    ]

    workspace_mode = "--bind" if "." in writable else "--ro-bind"
    argv += [workspace_mode, str(workspace), str(workspace)]
    for value in writable:
        if value == ".":
            continue
        item_path = _relative_path(workspace, value, "writable path")
        if not item_path.exists():
            raise ValueError("writable path must exist")
        argv += ["--bind", str(item_path), str(item_path)]
    for value in dict.fromkeys(
        [*protected, ".git", ".pi", ".claude", ".mcp.json", ".agents", ".github"]
    ):
        item_path = _relative_path(workspace, value, "protected path")
        if item_path.exists():
            argv += ["--ro-bind", str(item_path), str(item_path)]
    for value in immutable:
        item_path = _relative_path(runtime, value, "immutable runtime path")
        if not item_path.is_file():
            raise ValueError("immutable runtime input must exist")
        argv += ["--ro-bind", str(item_path), str(item_path)]
    argv += ["--cap-drop", "ALL", "--chdir", str(workspace), "--", *command]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-policy", required=True)
    parser.add_argument("--operation", choices=("stop", "release"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    policy = json.loads(args.factory_policy)
    if not isinstance(policy, dict):
        raise ValueError("factory policy must be an object")
    if args.operation:
        # The driver owns no daemon resource: Bubblewrap replaces this process and
        # dies with it. CommandRunner synchronously terminates the whole process group.
        return 0
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    invocation = sandbox_command(policy, command)
    os.execvpe(invocation[0], invocation, os.environ)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
