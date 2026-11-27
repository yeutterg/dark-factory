"""Attempt bookkeeping a worker reports through, never writes around.

The controller and the private replica used by remote executors both provide
these records, so the worker has one interface for either.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from dark_factory import artifacts
from dark_factory.execution.packages import identity
from dark_factory.store import new_id, now_iso

MAX_CHECKPOINTS_PER_ATTEMPT = 5


class AttemptRecords:
    """Requires ``self.store``."""

    def attempt_status(self, attempt_id):
        row = self.store.one("SELECT status FROM attempts WHERE id=?", (attempt_id,))
        return row["status"] if row else None

    def record_workspace(self, attempt_id, workspace):
        self.store.execute(
            "UPDATE attempts SET workspace=? WHERE id=?", (str(workspace), attempt_id)
        )

    def record_process(self, attempt_id, pid):
        from dark_factory.runner import process_identity

        # Pid and identity change together: a check runs several commands in
        # turn, and recovery must only ever signal the process that is recorded.
        self.store.execute(
            "UPDATE attempts SET pid=?,pid_identity=?,status='running' "
            "WHERE id=? AND status IN ('leased','running')",
            (pid, process_identity(pid), attempt_id),
        )

    def mark_running(self, attempt_id):
        self.store.execute(
            "UPDATE attempts SET status='running' WHERE id=?", (attempt_id,)
        )

    def set_lease(self, job_id, until):
        value = until if isinstance(until, str) else until.isoformat()
        self.store.execute("UPDATE jobs SET lease_until=? WHERE id=?", (value, job_id))

    def record_archive(self, attempt_id, archive):
        self.store.execute(
            "UPDATE attempts SET archive_path=?,cleanup_status='archived' WHERE id=?",
            (str(archive), attempt_id),
        )

    def record_cleanup(self, attempt_id, status):
        if status not in {"archive_failed", "cleaned", "cleanup_failed"}:
            raise ValueError(f"unknown cleanup status {status!r}")
        self.store.execute(
            "UPDATE attempts SET cleanup_status=? WHERE id=?", (status, attempt_id)
        )

    def record_checkpoint(self, attempt_id, workspace, detail):
        """Seal changed implementation work so another worker can continue it."""
        attempt = self.store.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        if not attempt or attempt["status"] not in {"leased", "running"}:
            return None
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (attempt["job_id"],))
        if not job or job["kind"] != "implement":
            return None
        root = self.store.one(
            "SELECT * FROM work_items WHERE id=?", (job["work_item_id"],)
        )
        if not root or not root["approved_digest"]:
            return None
        workspace = Path(workspace)
        ephemeral = json.loads(root["snapshot_json"])["source"].get(
            "ephemeral_paths", []
        )
        workspace_digest = artifacts.workspace_fingerprint(workspace, ephemeral)
        existing = self.store.one(
            "SELECT * FROM attempt_checkpoints WHERE attempt_id=? AND workspace_digest=?",
            (attempt_id, workspace_digest),
        )
        if existing:
            return dict(existing)
        count = self.store.one(
            "SELECT COUNT(*) n FROM attempt_checkpoints WHERE attempt_id=?",
            (attempt_id,),
        )["n"]
        if count >= MAX_CHECKPOINTS_PER_ATTEMPT:
            return None
        checkpoint_id = new_id("cp")
        parent = self.store.state_dir / "checkpoints" / attempt_id
        parent.mkdir(parents=True, exist_ok=True)
        temporary = parent / ("." + checkpoint_id)
        destination = parent / checkpoint_id
        inputs = json.loads(attempt["inputs_json"] or "{}")
        candidate_digest = identity(inputs.get("candidate", {}))
        bounded_detail = {
            "kind": str(detail.get("kind", "progress"))[:80],
            "summary": str(detail.get("summary", ""))[:500],
            "task_id": str(detail.get("task_id", ""))[:120],
        }
        try:
            artifacts.archive(
                workspace,
                temporary,
                {
                    "checkpoint_id": checkpoint_id,
                    "root_id": root["id"],
                    "job_id": job["id"],
                    "attempt_id": attempt_id,
                    "plan_digest": root["approved_digest"],
                    "candidate_digest": candidate_digest,
                    "workspace_digest": workspace_digest,
                    "sequence": count + 1,
                },
                {"checkpoint.json": json.dumps(bounded_detail, sort_keys=True)},
                ephemeral_paths=ephemeral,
            )
            temporary.replace(destination)
            artifacts.sync_tree(parent)
            manifest_digest = artifacts.digest(destination / "manifest.json")
            self.store.execute(
                """INSERT INTO attempt_checkpoints
                (id,root_id,job_id,attempt_id,sequence,path,manifest_digest,
                 workspace_digest,plan_digest,candidate_digest,detail_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    checkpoint_id,
                    root["id"],
                    job["id"],
                    attempt_id,
                    count + 1,
                    str(destination),
                    manifest_digest,
                    workspace_digest,
                    root["approved_digest"],
                    candidate_digest,
                    json.dumps(bounded_detail),
                    now_iso(),
                ),
            )
            return dict(
                self.store.one(
                    "SELECT * FROM attempt_checkpoints WHERE id=?", (checkpoint_id,)
                )
            )
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
