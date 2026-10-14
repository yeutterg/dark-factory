"""Verified durable archives and restorable state backups."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import ExitStack
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            h.update(data)
    return h.hexdigest()


_DIGESTS: dict[tuple, str] = {}


def digest_cached(path: Path) -> str:
    """Digest of a trusted input, re-hashed only when its file identity changes.

    ctime is part of the key: any content write updates it and it cannot be set
    back with utime. Archive verification always uses digest() instead.
    """
    path = Path(path).resolve()
    stat = path.stat()
    key = (
        str(path),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )
    value = _DIGESTS.get(key)
    if value is None:
        value = _DIGESTS[key] = digest(path)
    return value


def manifest(root: Path) -> dict:
    entries = {}
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
    unchanged_commit=None,
) -> str:
    """Copy unique work and evidence, then seal and verify the archive.

    With unchanged_commit, a workspace that is exactly that commit (no tracked,
    untracked or ignored changes) holds nothing unique, so only the commit is
    recorded; the pinned input snapshot already preserves it.
    """
    destination.mkdir(parents=True, exist_ok=False)
    from dark_factory.adapters.git.local import git

    if unchanged_commit and workspace_matches(workspace, unchanged_commit):
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


def workspace_matches(workspace: Path, commit: str) -> bool:
    from dark_factory.adapters.git.local import git

    try:
        return git(workspace, "rev-parse", "HEAD") == commit and not git(
            workspace,
            "status",
            "--porcelain=v1",
            "--ignored",
            "--untracked-files=all",
        )
    except RuntimeError:
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
