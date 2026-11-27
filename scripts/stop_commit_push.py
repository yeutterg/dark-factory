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
ROOT_FILES = {
    "README.md",
    "AGENTS.md",
    "LICENSE",
    "pyproject.toml",
    ".gitignore",
    "Dockerfile",
    "compose.yaml",
    "compose.live.yaml",
    "compose.live-hardened.yaml",
}
DIRECTORIES = {
    "src",
    "tests",
    "docs",
    "specs",
    "examples",
    "scripts",
    "deploy",
    "factory",
    ".specify",
    ".github",
}
SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    rb"|(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    rb"|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})"
    # This project's own credentials: Together keys, JWT-shaped tokens, and the
    # token fields of a Codex or Claude login file.
    rb"|tgp_v1_[A-Za-z0-9_-]{20,}"
    rb"|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    rb"|\"(?:access|refresh|id)_token\"\s*:\s*\"[^\"]{20,}\""
)
DEFAULT_BRANCHES = {"main", "master"}


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
        or ".local." in part
        for part in path.parts
    ):
        return False
    # A profile is a .toml file; only the package metadata and the examples
    # directory may carry one into a checkpoint.
    if path.suffix.lower() == ".toml" and not (
        name == "pyproject.toml" or path.parts[0] == "examples"
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


def default_branch(repo):
    """The remote's default branch when it is known locally, else empty."""
    try:
        ref = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    except RuntimeError:
        return ""
    return ref.decode().strip().removeprefix("refs/remotes/origin/")


def scan_unpushed(repo):
    """Every commit this push would publish, not only today's edits."""
    patch = git(repo, "log", "-p", "--format=", "HEAD", "--not", "--remotes=origin")
    if SECRET.search(patch):
        raise RuntimeError(
            "possible credential in a commit that would be pushed; inspect before publishing"
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
    if branch in DEFAULT_BRANCHES or branch == default_branch(repo):
        raise RuntimeError(
            "stop hook never pushes the default branch; work on a feature branch"
        )
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
        worktree_changes = set()
        for args in (
            ("diff", "--no-renames", "--name-only", "-z"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
        ):
            worktree_changes.update(
                os.fsdecode(n) for n in git(repo, *args).split(b"\0") if n
            )
        staged = {
            os.fsdecode(n)
            for n in git(
                repo, "diff", "--cached", "--no-renames", "--name-only", "-z"
            ).split(b"\0")
            if n
        }
        names = worktree_changes | staged
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
        to_stage = sorted(worktree_changes.intersection(selected))
        if to_stage:
            git(repo, "add", "-A", "--", *to_stage)
        if git(repo, "diff", "--cached", "--name-only"):
            git(repo, "diff", "--cached", "--check")
            git(repo, "commit", "-m", "chore: checkpoint Dark Factory session")
        scan_unpushed(repo)
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
