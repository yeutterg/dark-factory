"""Private attempt replica: reuse execution, never scheduling/approval authority."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from dark_factory import artifacts
from dark_factory.execution.packages import identity, pack, unpack
from dark_factory.store import Store
from dark_factory.worker import Worker


def install_assignment(assignment, root):
    claim = assignment["claim"]
    if (
        assignment["version"] != 1
        or identity(claim) != assignment["claim_digest"]
        or claim["inputs"]["project_id"] != assignment["project_id"]
    ):
        raise ValueError("assignment identity mismatch")
    unpack(assignment["package"], root)
    store = Store(root)
    store.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES ('execution_references',?)",
        (json.dumps(assignment["references"]),),
    )

    def insert(table, row):
        fields = list(row)
        store.execute(
            f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            tuple(row[k] for k in fields),
        )

    try:
        insert("work_items", claim["work_item"])
        for row in assignment["jobs"]:
            insert("jobs", row)
        for row in assignment["attempts"]:
            insert("attempts", row)
        for original, relative in assignment["references"].items():
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("unsafe input reference")
            path = root / relative
            if relative.startswith("archives/"):
                artifacts.verify(path)
        cfg = SimpleNamespace(
            state_dir=root,
            source_path=root / "assignment.json",
            execution=assignment["execution"],
            raw={},
            project_id=assignment["project_id"],
            max_output_bytes=assignment["max_output_bytes"],
        )
        (root / "assignment.json").write_text(json.dumps(assignment))
        return cfg, store
    except BaseException:
        store.close()
        raise


class AttemptController:
    def __init__(self, store, claim, alive):
        self.store, self.claim, self.alive = store, claim, alive
        self.receipt = None

    def _jobs(self, wid):
        return self.store.query(
            "SELECT * FROM jobs WHERE work_item_id=? ORDER BY rowid", (wid,)
        )

    def heartbeat(self, worker, aid):
        return self.alive()

    def finish_attempt(
        self, aid, ok, output, cost, archive, error, timed_out, cancelled
    ):
        self.receipt = {
            "ok": ok,
            "output": output,
            "cost": cost,
            "error": error,
            "timed_out": timed_out,
            "cancelled": cancelled,
        }


def execute_assignment(assignment, root, alive=lambda: True):
    cfg, store = install_assignment(assignment, root)
    claim = assignment["claim"]
    controller = AttemptController(store, claim, alive)
    try:
        Worker(
            cfg, store, controller, claim["job"]["worker_id"] or "execution"
        )._execute(claim)
        archive = root / "archives" / claim["attempt_id"]
        return {
            "attempt_id": claim["attempt_id"],
            "claim_digest": assignment["claim_digest"],
            "receipt": controller.receipt,
            "archive": pack({"archive": archive}) if archive.exists() else None,
        }
    finally:
        store.close()


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("assignment", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--cancel-file", type=Path)
    parser.add_argument("--lease-file", type=Path)
    parser.add_argument("--lease-seconds", type=int, default=30)
    args = parser.parse_args()
    assignment = json.loads(args.assignment.read_text())
    import time

    def alive():
        return (not args.cancel_file or not args.cancel_file.exists()) and (
            not args.lease_file
            or args.lease_file.exists()
            and time.time() - args.lease_file.stat().st_mtime < args.lease_seconds
        )

    result = execute_assignment(assignment, args.directory, alive)
    (args.directory.parent / "result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
