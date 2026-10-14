"""Attempt bookkeeping a worker reports through, never writes around.

The controller and the private replica used by remote executors both provide
these records, so the worker has one interface for either.
"""

from __future__ import annotations


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

        self.store.execute(
            "UPDATE attempts SET pid_identity=? WHERE id=?",
            (process_identity(pid), attempt_id),
        )
        self.store.execute(
            "UPDATE attempts SET pid=?,status='running' WHERE id=? AND status='leased'",
            (pid, attempt_id),
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
