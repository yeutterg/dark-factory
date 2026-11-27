"""Every directory the factory creates has an owner and a removal rule.

Owner: an attempt (workspaces, runtime, dispatch) or a root (inputs). A resource is
removed when its owner is finished and its unique work is archived and verified, or
when it is older than the retention window; resources whose owner is gone are
removed outright. Unique work is never removed while its owner is active. The sweep
runs at startup and periodically, so a crash or an abandoned build cannot leave
disk behind.
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dark_factory import artifacts
from dark_factory.control.base import ControllerCore
from dark_factory.execution.cleanup import release_attempt_environment
from dark_factory.models import ACTIVE_ATTEMPT, TERMINAL_ROOT, sql_set

ATTEMPT_AREAS = ("workspaces", "runtime", "dispatch")
# High-volume records no later decision reads. Decisions (approve, reject, accept,
# manual_gate, external.*, release.*, improvement.*) are never pruned.
NOISY_AUDIT = (
    "attempt.finish",
    "attempt.not_started",
    "recover",
    "worker.error",
    "gate.status",
    "gate.error",
    "resources.swept",
    "resources.retained",
    "credentials.scrub",
    "decider.unavailable",
    "task_progress.rejected",
    "continuation.reconcile",
    "tasks.reconcile",
)
SWEEP_INTERVAL_SECONDS = 600
STATE_SIZE_CACHE_SECONDS = 60


def tree_size(path: Path) -> int:
    """Bytes under a path, without following symbolic links."""
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                total += tree_size(Path(entry.path))
            else:
                total += entry.stat(follow_symlinks=False).st_size
    except OSError:
        pass
    return total


class ResourcesMixin(ControllerCore):
    """Disk accounting, the budget that refuses new work, and the sweep."""

    _state_size: tuple[float, int] = (0.0, 0)

    # ---- accounting ------------------------------------------------------------

    def disk_usage(self):
        state = self.cfg.state_dir
        usage = shutil.disk_usage(state)
        areas = {
            name: tree_size(state / name)
            for name in (
                "workspaces",
                "runtime",
                "dispatch",
                "inputs",
                "archives",
                "artifacts",
                "work-items",
            )
        }
        roots = {}
        for row in self.store.query(
            "SELECT id,title,status FROM work_items ORDER BY created_at,rowid"
        ):
            size = tree_size(state / "inputs" / row["id"])
            for attempt in self.store.query(
                "SELECT a.id FROM attempts a JOIN jobs j ON a.job_id=j.id "
                "WHERE j.work_item_id=?",
                (row["id"],),
            ):
                for area in ("archives", "workspaces", "runtime"):
                    size += tree_size(state / area / attempt["id"])
            if size:
                roots[row["id"]] = {
                    "title": row["title"],
                    "status": row["status"],
                    "bytes": size,
                }
        return {
            "state_bytes": sum(areas.values()),
            "free_bytes": usage.free,
            "areas": areas,
            "roots": roots,
            "floor_bytes": self.cfg.disk["min_free_gb"] * 1024**3,
            "budget_bytes": self.cfg.disk["max_state_gb"] * 1024**3,
        }

    def _cached_state_size(self):
        taken, size = self._state_size
        if time.monotonic() - taken > STATE_SIZE_CACHE_SECONDS:
            size = tree_size(self.cfg.state_dir)
            self._state_size = (time.monotonic(), size)
        return size

    def disk_blocker(self):
        """Why new work is refused for lack of disk, or None. Never fills the disk."""
        settings = self.cfg.disk
        free = shutil.disk_usage(self.cfg.state_dir).free
        if free < settings["min_free_gb"] * 1024**3:
            return "disk_low"
        budget = settings["max_state_gb"]
        if budget and self._cached_state_size() > budget * 1024**3:
            return "state_over_budget"
        return None

    # ---- the sweep -------------------------------------------------------------

    def _verified(self, attempt):
        if not attempt["archive_path"]:
            return False
        try:
            artifacts.verify(self.store.resolve_path(attempt["archive_path"]))
        except (OSError, ValueError, KeyError):
            return False
        return True

    def _root_finished(self, attempt):
        row = self.store.one(
            "SELECT w.status FROM work_items w JOIN jobs j ON j.work_item_id=w.id "
            "WHERE j.id=?",
            (attempt["job_id"],),
        )
        return row is None or row["status"] in TERMINAL_ROOT

    def _finished_before(self, attempt, cutoff):
        stamp = attempt["ended_at"]
        return bool(stamp) and datetime.fromisoformat(stamp) < cutoff

    def sweep_resources(self, actor="controller"):
        """Remove what no active owner needs. Returns what was freed and what stayed."""
        state = self.cfg.state_dir
        cutoff = datetime.now(UTC) - timedelta(days=self.cfg.disk["keep_finished_days"])
        self.scrub_retired_credentials()
        freed: dict[str, int] = {}
        kept: list[str] = []

        def remove(area, path):
            size = tree_size(path)
            shutil.rmtree(path, ignore_errors=True)
            freed[area] = freed.get(area, 0) + size

        for area in ATTEMPT_AREAS:
            base = state / area
            if not base.is_dir():
                continue
            for entry in sorted(base.iterdir()):
                attempt = self.store.one(
                    "SELECT * FROM attempts WHERE id=?", (entry.name,)
                )
                if attempt and attempt["status"] in ACTIVE_ATTEMPT:
                    continue
                if attempt is not None:
                    if not (
                        self._verified(attempt)
                        or (
                            self._finished_before(attempt, cutoff)
                            and self._root_finished(attempt)
                        )
                    ):
                        # Unarchived work is the only copy: never removed while its
                        # root could still be resumed or inspected by a person.
                        kept.append(f"{area}/{entry.name}")
                        continue
                    if area == "workspaces" and attempt["workspace"]:
                        try:
                            release_attempt_environment(self.store, attempt)
                        except Exception as exc:
                            self.store.audit(
                                actor,
                                "resources.retained",
                                {"path": f"{area}/{entry.name}", "why": str(exc)[:300]},
                            )
                            continue
                elif time.time() - entry.stat().st_mtime < 3600:
                    continue  # Too new to be sure it is orphaned.
                remove(area, entry)

        inputs = state / "inputs"
        if inputs.is_dir():
            for root_dir in sorted(inputs.iterdir()):
                root = self.store.one(
                    "SELECT status,plan_version FROM work_items WHERE id=?",
                    (root_dir.name,),
                )
                for version in sorted(root_dir.iterdir()):
                    stale = (
                        root is None
                        or root["status"] in TERMINAL_ROOT
                        or version.name != str(root["plan_version"])
                    )
                    if stale:
                        remove("inputs", version)
                if not any(root_dir.iterdir()):
                    root_dir.rmdir()

        # Archives of finished roots keep their evidence; the bulky checkout goes.
        for attempt in self.store.query(
            "SELECT a.* FROM attempts a JOIN jobs j ON a.job_id=j.id "
            "JOIN work_items w ON w.id=j.work_item_id "
            f"WHERE w.status IN {sql_set(TERMINAL_ROOT)} AND a.archive_path IS NOT NULL "
            "AND COALESCE(a.cleanup_status,'')<>'archive_pruned'"
        ):
            if not self._finished_before(attempt, cutoff):
                continue
            checkout = self.store.resolve_path(attempt["archive_path"]) / "workspace"
            if checkout.is_dir():
                remove("archives", checkout)
            self.store.execute(
                "UPDATE attempts SET cleanup_status='archive_pruned' WHERE id=?",
                (attempt["id"],),
            )

        pruned = self.prune_audit()
        leftover = self.sweep_external_worktrees()
        if leftover:
            freed["external"] = leftover
        total = sum(freed.values())
        if total:
            self.store.audit(
                actor, "resources.swept", {"freed_bytes": total, "by_area": freed}
            )
        return {
            "freed_bytes": total,
            "by_area": freed,
            "kept": kept,
            "audit_pruned": pruned,
        }

    def prune_audit(self):
        """Drop old noise from the audit trail; decisions stay for good."""
        cutoff = (
            datetime.now(UTC) - timedelta(days=self.cfg.disk["keep_audit_days"])
        ).isoformat()
        marks = ",".join("?" for _ in NOISY_AUDIT)
        cursor = self.store.execute(
            f"DELETE FROM audit WHERE at<? AND action IN ({marks})",
            (cutoff, *NOISY_AUDIT),
        )
        return cursor.rowcount

    def sweep_if_due(self):
        """At startup and then at most every few minutes."""
        last = float(self.store.get_op("resources:last", "0") or 0)
        if time.time() - last < SWEEP_INTERVAL_SECONDS:
            return None
        self.store.set_op("resources:last", str(time.time()), "controller")
        return self.sweep_resources()
