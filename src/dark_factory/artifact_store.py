"""Narrow local-first store for verified release artifacts."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, release_id: str) -> Path:
        return self.root / release_id

    def reset(self, release_id: str) -> Path:
        path = self.path(release_id)
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, mode=0o700)
        return path

    def put(self, release_id: str, name: str, data: bytes) -> dict:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("artifact name must be repository-relative")
        target = self.path(release_id) / relative
        if target.exists() or target.is_symlink():
            raise ValueError("duplicate artifact path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {
            "name": relative.as_posix(),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def prune(self, release_id: str) -> None:
        shutil.rmtree(self.path(release_id), ignore_errors=True)
