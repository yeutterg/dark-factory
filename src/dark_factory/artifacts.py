"""Verified durable archives and restorable state backups."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            h.update(data)
    return h.hexdigest()


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
    workspace: Path, destination: Path, metadata: dict, files: dict[str, str]
) -> str:
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(workspace, destination / "workspace", symlinks=True)
    for name, body in files.items():
        if Path(name).name != name:
            raise ValueError("invalid artifact filename")
        (destination / name).write_text(body)
    return seal(destination, metadata)


def backup(store, destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists() or destination.is_relative_to(store.state_dir.resolve()):
        raise ValueError("backup needs a new directory outside controller state")
    with store.transaction():
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
        for directory in ("archives", "artifacts", "workspaces", "inputs", "runtime"):
            path = store.state_dir / directory
            if path.exists():
                shutil.copytree(path, destination / directory, symlinks=True)
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
