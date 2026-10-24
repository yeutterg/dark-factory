"""Verified durable archives and restorable state backups."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import ExitStack
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            h.update(data)
    return h.hexdigest()


# One entry per path: a changed file replaces its previous identity and digest.
_DIGESTS: dict[str, tuple[tuple, str]] = {}


def digest_cached(path: Path) -> str:
    """Digest of a trusted input, re-hashed only when its file identity changes.

    ctime is part of the key: any content write updates it and it cannot be set
    back with utime. Archive verification always uses digest() instead.
    """
    path = Path(path).resolve()
    stat = path.stat()
    identity = (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )
    cached = _DIGESTS.get(str(path))
    if cached and cached[0] == identity:
        return cached[1]
    value = digest(path)
    _DIGESTS[str(path)] = (identity, value)
    return value


def manifest(root: Path) -> dict:
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if name == "manifest.json":
            continue
        if path.is_symlink():
            entries[name] = {"link": str(path.readlink())}
        elif path.is_file():
            entries[name] = {"sha256": digest(path), "size": path.stat().st_size}
    return entries


def sync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for path in [p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()] + [
        root
    ]:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def seal(root: Path, metadata: dict) -> str:
    payload = {**metadata, "files": manifest(root)}
    (root / "manifest.json").write_text(json.dumps(payload, sort_keys=True, indent=2))
    sync_tree(root)
    verify(root)
    return digest(root / "manifest.json")


def verify(root: Path) -> dict:
    payload = json.loads((root / "manifest.json").read_text())
    if payload["files"] != manifest(root):
        raise ValueError("archive content failed verification")
    return payload


def archive(
    workspace: Path,
    destination: Path,
    metadata: dict,
    files: dict[str, str],
    ephemeral_paths=(),
    unchanged=None,
) -> str:
    """Copy unique work and evidence, then seal and verify the archive.

    unchanged is (commit, git_fingerprint) taken when the workspace was
    prepared. A workspace still exactly at that state, with no file, ref,
    object, index-flag or stash changes, holds nothing unique, so only the
    commit is recorded; the pinned input snapshot already preserves it.
    """
    destination.mkdir(parents=True, exist_ok=False)
    from dark_factory.adapters.git.local import git

    if unchanged and workspace_matches(workspace, *unchanged):
        unchanged_commit = unchanged[0]
        for name, body in files.items():
            if Path(name).name != name:
                raise ValueError("invalid artifact filename")
            (destination / name).write_text(body)
        return seal(destination, {**metadata, "workspace_commit": unchanged_commit})

    for name in ephemeral_paths:
        if (
            Path(name).is_absolute()
            or ".." in Path(name).parts
            or name in {".", ".git", ""}
        ):
            raise ValueError("unsafe ephemeral output path")
        if git(workspace, "ls-files", "--", name):
            raise ValueError("cannot discard tracked files as ephemeral output")

    def ignore(directory, names):
        return [
            name
            for name in names
            if str((Path(directory) / name).relative_to(workspace)) in ephemeral_paths
        ]

    shutil.copytree(
        workspace,
        destination / "workspace",
        symlinks=True,
        ignore=ignore if ephemeral_paths else None,
    )
    metadata = {**metadata, "discarded_ephemeral_paths": list(ephemeral_paths)}
    for name, body in files.items():
        if Path(name).name != name:
            raise ValueError("invalid artifact filename")
        (destination / name).write_text(body)
    return seal(destination, metadata)


def git_fingerprint(workspace: Path) -> str:
    """Digest of repository state that git status does not show.

    Covers every ref plus the owned Git directory's file identities. The latter
    catches reflogs, config, hooks and other Git-only state that can change while
    HEAD, refs, object counts and the worktree all return to their baseline.
    """
    from dark_factory.adapters.git.local import git

    root = workspace.resolve()
    dot_git = workspace / ".git"
    git_dir = Path(git(workspace, "rev-parse", "--absolute-git-dir")).resolve()
    if not git_dir.is_relative_to(root) or dot_git.is_symlink():
        raise ValueError("archive optimization requires an owned Git directory")
    # Run every Git command before sampling filesystem identities: read-only Git
    # commands may refresh index metadata, which must not make an unchanged
    # workspace differ from its own baseline.
    parts = [
        git(workspace, "rev-parse", "HEAD"),
        git(workspace, "for-each-ref", "--format=%(refname) %(objectname)"),
        git(workspace, "count-objects", "-v"),
        git(workspace, "ls-files", "-v"),
        git(workspace, "stash", "list"),
        git(
            workspace,
            "status",
            "--porcelain=v1",
            "--ignored",
            "--untracked-files=all",
        ),
    ]
    metadata: list[tuple[Any, ...]] = []
    for path in sorted(git_dir.rglob("*")):
        relative = str(path.relative_to(git_dir))
        if path.is_dir() and not path.is_symlink():
            # Child path identities already capture membership. Directory
            # mtimes are noisy when Git atomically refreshes its index.
            metadata.append((relative, "dir"))
            continue
        stat = path.lstat()
        if relative == "index" or relative.startswith("sharedindex."):
            # Git may atomically replace an unchanged index during a read. Its
            # content, unlike its inode and timestamps, remains stable.
            metadata.append((relative, "file", stat.st_size, digest(path)))
            continue
        metadata.append(
            (
                relative,
                "link" if path.is_symlink() else "file",
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                str(path.readlink()) if path.is_symlink() else "",
            )
        )
    parts.append(json.dumps(metadata, separators=(",", ":")))
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def workspace_matches(workspace: Path, commit: str, fingerprint: str) -> bool:
    from dark_factory.adapters.git.local import git

    try:
        return (
            git(workspace, "rev-parse", "HEAD") == commit
            and not git(
                workspace,
                "status",
                "--porcelain=v1",
                "--ignored",
                "--untracked-files=all",
            )
            and git_fingerprint(workspace) == fingerprint
        )
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        return False


def backup(store, destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists() or destination.is_relative_to(store.state_dir.resolve()):
        raise ValueError("backup needs a new directory outside controller state")
    with store.transaction(), ExitStack() as owned:
        owner_path = store.state_dir / "workflow" / "owner.lock"
        if owner_path.exists():
            owner = owned.enter_context(owner_path.open("a+"))
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("drain the workflow runtime before backup") from None
        if store.one(
            "SELECT id FROM attempts WHERE status IN ('leased','running','cancelling')"
        ):
            raise ValueError("drain active work before backup")
        destination.mkdir(parents=True)
        # Use a separate read connection: backup on the write-transaction connection can deadlock.
        source = sqlite3.connect(store.db_path)
        target = sqlite3.connect(destination / "factory.sqlite")
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        for directory in (
            "archives",
            "artifacts",
            "workspaces",
            "inputs",
            "runtime",
            "workflow",
            "dispatch",
            "work-items",
        ):
            path = store.state_dir / directory
            if path.exists():
                shutil.copytree(
                    path,
                    destination / directory,
                    symlinks=True,
                    ignore=shutil.ignore_patterns("auth.json")
                    if directory == "runtime"
                    else None,
                )
        seal(
            destination,
            {
                "kind": "controller-backup",
                "schema_version": 2,
                "origin": str(store.state_dir.resolve()),
            },
        )


def restore(source: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError(
            "restore destination must not exist; never overwrite live state"
        )
    metadata = verify(source)
    if (
        metadata.get("kind") != "controller-backup"
        or metadata.get("schema_version") != 2
    ):
        raise ValueError("incompatible backup")
    shutil.copytree(source, destination, symlinks=True)
    conn = sqlite3.connect(destination / "factory.sqlite")
    try:
        previous = conn.execute(
            "SELECT value FROM meta WHERE key='restore_origins'"
        ).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('restore_origins',?)",
            (
                json.dumps(
                    list(
                        dict.fromkeys(
                            (json.loads(previous[0]) if previous else [])
                            + [metadata["origin"]]
                        )
                    )
                ),
            ),
        )
        conn.commit()
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("restored SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("restored SQLite foreign key check failed")
    finally:
        conn.close()
