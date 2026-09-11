#!/usr/bin/env python3
"""Repository-scoped Codex stop hook. No model calls or factory operations."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REMOTES = {
    "https://github.com/yeutterg/dark-factory.git",
    "git@github.com:yeutterg/dark-factory.git",
}
ROOT_FILES = {"README.md", "AGENTS.md", "LICENSE", "pyproject.toml", ".gitignore"}
DIRECTORIES = {
    "src",
    "tests",
    "docs",
    "specs",
    "examples",
    "scripts",
    ".specify",
    ".github",
}
SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})"
)


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, timeout=60
    )
    if result.returncode:
        raise RuntimeError(
            "git " + args[0] + " failed; inspect the repository or remote and retry"
        )
    return result.stdout


def allowed(name):
    path = Path(name)
    if any(
        part
        in {
            "state",
            "artifacts",
            "runtime",
            "dist",
            "build",
            "node_modules",
            "__pycache__",
        }
        or part.startswith(".env")
        for part in path.parts
    ):
        return False
    if path.suffix.lower() in {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pem",
        ".key",
        ".p8",
        ".log",
        ".zip",
        ".whl",
        ".pyc",
    }:
        return False
    return (
        name in ROOT_FILES
        or path.parts[0] in DIRECTORIES
        or name == ".codex/hooks.json"
    )


def checkpoint(repo, remotes=REMOTES):
    repo = Path(repo).resolve()
    # Check both fetch and push URLs, including local pushurl overrides.
    for args in (
        ("remote", "get-url", "--all", "origin"),
        ("remote", "get-url", "--push", "--all", "origin"),
    ):
        urls = git(repo, *args).decode().splitlines()
        if len(urls) != 1 or urls[0] not in remotes:
            raise RuntimeError("stop hook requires the Dark Factory origin")
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").decode().strip()
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").decode().strip())
    with (git_dir / "factory-stop.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another stop checkpoint is running") from None
        for marker in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
        ):
            if (git_dir / marker).exists():
                raise RuntimeError(
                    "finish the active Git operation before checkpointing"
                )
        names = set()
        for args in (
            ("diff", "--name-only", "-z"),
            ("diff", "--cached", "--name-only", "-z"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
        ):
            names.update(os.fsdecode(n) for n in git(repo, *args).split(b"\0") if n)
        staged = {
            os.fsdecode(n)
            for n in git(repo, "diff", "--cached", "--name-only", "-z").split(b"\0")
            if n
        }
        if any(not allowed(n) for n in staged):
            raise RuntimeError(
                "staged files outside development scope need explicit review"
            )
        selected = sorted(n for n in names if allowed(n))
        for name in selected:
            path = repo / name
            if path.is_symlink():
                raise RuntimeError("review symlinks before checkpointing")
            if path.is_file() and SECRET.search(path.read_bytes()):
                raise RuntimeError(
                    "possible credential in a development file; inspect before publishing"
                )
        if selected:
            git(repo, "add", "-A", "--", *selected)
        if git(repo, "diff", "--cached", "--name-only"):
            git(repo, "diff", "--cached", "--check")
            git(repo, "commit", "-m", "chore: checkpoint Dark Factory session")
        sha = git(repo, "rev-parse", "HEAD").decode().strip()
        # Always retry a prior failed push, even when the working tree is clean.
        git(
            repo,
            "push",
            "--no-follow-tags",
            "--set-upstream",
            "origin",
            "HEAD:refs/heads/" + branch,
        )
        remote = (
            git(repo, "ls-remote", "--heads", "origin", "refs/heads/" + branch)
            .decode()
            .split()
        )
        if not remote or remote[0] != sha:
            raise RuntimeError("remote branch did not match the checkpoint")
        return {
            "systemMessage": "Dark Factory checkpoint pushed: "
            + sha[:12]
            + "; local-only files excluded: "
            + str(len(names - set(selected)))
        }


def main():
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
        cwd = payload.get("cwd", os.getcwd())
        repo = git(cwd, "rev-parse", "--show-toplevel").decode().strip()
        print(json.dumps(checkpoint(repo)))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("Dark Factory checkpoint failed: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
