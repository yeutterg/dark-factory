"""Controller-owned, hook-disabled bare mirrors for remote repository sources."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class MirrorError(RuntimeError):
    pass


def _run(argv: list[str], timeout: int = 120) -> str:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": os.devnull,
    }
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, env=env
    )
    if result.returncode:
        raise MirrorError(result.stderr.strip()[:500] or "git mirror operation failed")
    return result.stdout


def sync_source(source: dict) -> Path:
    path = Path(source["path"])
    remote = source.get("remote")
    if not remote:
        return path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if not (path / "HEAD").is_file():
            raise MirrorError(f"mirror path is not a bare repository: {path}")
        _run(
            [
                "git",
                "-C",
                str(path),
                "fetch",
                "--prune",
                "origin",
                "+refs/heads/*:refs/heads/*",
            ]
        )
    else:
        temporary = Path(tempfile.mkdtemp(prefix="mirror-", dir=path.parent))
        try:
            _run(["git", "clone", "--mirror", remote, str(temporary / "repo.git")])
            (temporary / "repo.git").replace(path)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    trusted = source.get("trusted_adoption_commit")
    if trusted:
        branch = source.get("target_branch", "main")
        result = subprocess.run(
            ["git", "-C", str(path), "merge-base", "--is-ancestor", trusted, branch]
        )
        if result.returncode:
            raise MirrorError(
                "remote branch does not descend from trusted adoption commit"
            )
    return path


def sync_all(cfg) -> None:
    for source in cfg.sources.values():
        sync_source(source)
