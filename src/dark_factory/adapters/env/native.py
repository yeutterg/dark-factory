"""Disposable native workspace on the controller/worker host."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from dark_factory.adapters.env.base import PreparedEnv


class NativeEnvironment:
    name = "native"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(self, work_item_id: str, repo: Optional[str]) -> PreparedEnv:
        workspace = self.root / work_item_id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)
        (workspace / "REPO").write_text(repo or "local", encoding="utf-8")
        return PreparedEnv(workspace=workspace, kind="native", metadata={"repo": repo})

    def run(self, argv: list[str], cwd: Path, timeout_seconds: int) -> tuple[int, str, str]:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )

    def stop(self, prepared: PreparedEnv) -> None:
        return None

    def release(self, prepared: PreparedEnv) -> None:
        if prepared.workspace.exists() and prepared.workspace.is_dir():
            shutil.rmtree(prepared.workspace, ignore_errors=False)
