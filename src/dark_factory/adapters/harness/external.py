"""An outside coding agent's work, presented to the worker as a harness.

The agent works in a worktree the factory owns. When it submits, the ordinary worker
takes that tree (or the supplied plan) as the role's result and then does what it
does for any harness: captures the diff, applies scope, protected-path and diff-size
rules, archives it, and settles it. Nothing the agent says can skip those steps.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult


def tree_fingerprint(root: Path) -> str:
    """A digest of a tree's files, links and executable bits, ignoring .git."""
    import hashlib
    import os

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            digest.update(f"L {relative} -> {os.readlink(path)}\n".encode())
        elif path.is_file():
            mode = "x" if os.access(path, os.X_OK) else "-"
            digest.update(f"F {relative} {mode} ".encode())
            digest.update(
                hashlib.sha256(path.read_bytes()).hexdigest().encode() + b"\n"
            )
    return digest.hexdigest()


def replace_tree(source: Path, destination: Path) -> None:
    """Make destination's files (not its .git) exactly source's files (not its .git)."""
    for entry in destination.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in source.iterdir():
        if entry.name == ".git":
            continue
        target = destination / entry.name
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target, follow_symlinks=False)


class ExternalHarness:
    name = "external"

    def __init__(self, external: dict) -> None:
        self.external = external
        self._last = HarnessResult(False, "", {}, None, error="not started")

    def start(self, prompt, workspace, extra=None):
        role = (extra or {}).get("role")
        actor = self.external["actor"]
        yield HarnessEvent("start", f"external agent {actor} as {role}")
        if role == "planner":
            output = self.external["proposal_json"]
        elif role == "coder":
            frozen = Path(self.external["worktree"])
            # The captured submission, not the agent's editable worktree: it must still
            # be exactly what was submitted when the plan was approved.
            if tree_fingerprint(frozen) != self.external["submission"]["digest"]:
                raise ValueError(
                    "the captured submission changed after it was submitted"
                )
            replace_tree(frozen, Path(workspace))
            output = json.dumps(
                {
                    "summary": f"Candidate submitted by {actor}",
                    "assumptions": self.external.get("assumptions", []),
                    "task_updates": [],
                }
            )
        else:
            raise ValueError("an external agent cannot take this role")
        self._last = HarnessResult(
            True, output, {"cost_status": "reported", "reported_cost": 0}, None
        )

    def wait(self) -> HarnessResult:
        return self._last

    def cancel(self) -> None:
        pass
