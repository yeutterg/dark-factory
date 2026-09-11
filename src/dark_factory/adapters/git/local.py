"""Owned Git inputs and candidate capture; no code-host privileges."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.defpath,
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Dark Factory",
        "GIT_AUTHOR_EMAIL": "factory@localhost",
        "GIT_COMMITTER_NAME": "Dark Factory",
        "GIT_COMMITTER_EMAIL": "factory@localhost",
    }
    proc = subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-2000:])
    return proc.stdout.decode().strip()


def pin(source: Path, revision: str) -> str:
    commit = git(source, "rev-parse", "--verify", revision + "^{commit}")
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("expected exact Git commit")
    # Submodules require a pinned recursive input contract beyond bootstrap.
    if "160000 " in git(source, "ls-tree", "-r", commit):
        raise ValueError("submodules are unsupported in bootstrap")
    return commit


def clone(source: Path, destination: Path, commit: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(
        destination.parent,
        "clone",
        "--no-local",
        "--no-checkout",
        "--",
        str(source),
        str(destination),
    )
    git(destination, "checkout", "--detach", commit)
    git(destination, "remote", "remove", "origin")
    if (destination / ".gitmodules").exists():
        raise ValueError("submodule sources are unsupported")


def within(path: str, allowed: list[str]) -> bool:
    return any(
        path == prefix or path.startswith(prefix.rstrip("/") + "/")
        for prefix in allowed
    )


def capture(
    workspace: Path, base: str, allowed: list[str], protected: list[str]
) -> dict:
    # Capture all files, including ignored files: unknown generated output is not silently lost.
    git(workspace, "add", "--all", "--force", ".")
    names = git(workspace, "diff", "--cached", "--name-only", "-z", base).split("\0")
    names = [name for name in names if name]
    flags = [
        name
        for name in names
        if not within(name, allowed)
        or within(name, protected)
        or within(name, [".github", ".gitmodules", ".pi", ".agents"])
    ]
    # Symlinks are preserved in archives but cannot become accepted candidate input.
    for entry in git(workspace, "ls-files", "--stage").splitlines():
        if entry.startswith("120000 "):
            flags.append(entry.split("\t", 1)[-1])
    git(workspace, "commit", "--allow-empty", "-m", "Factory candidate")
    commit = git(workspace, "rev-parse", "HEAD")
    diff = git(workspace, "diff", "--binary", base, commit)
    return {
        "base": base,
        "commit": commit,
        "changed_paths": names,
        "scope_violations": sorted(set(flags)),
        "diff": diff,
        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
    }


def fixture(root: Path) -> dict:
    """A real Git repo for the explicit fake-harness demonstration only."""
    source = root / "fixture-source"
    if not source.exists():
        source.mkdir(parents=True)
        git(source, "init", "-b", "main")
        (source / "message.py").write_text('def message():\n    return "pending"\n')
        (source / "verify.py").write_text(
            'from message import message\nassert message() == "ready", "message must be ready"\n'
        )
        (source / "AGENTS.md").write_text(
            "Keep the message API and verification contract.\n"
        )
        git(source, "add", ".")
        git(source, "commit", "-m", "Qualification fixture")
    return {
        "path": str(source),
        "revision": "HEAD",
        "checks": ["fixture-check"],
        "allowed_paths": ["message.py"],
        "protected_paths": ["verify.py", "AGENTS.md"],
        "manual_acceptance": ["Inspect candidate diff"],
    }
