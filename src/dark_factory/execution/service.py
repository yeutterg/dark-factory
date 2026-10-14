"""Project/worker-scoped assignment service; only controller settles results."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from dark_factory import artifacts
from dark_factory.controller import ControllerError
from dark_factory.execution.packages import identity, pack, unpack
from dark_factory.workflow import runtime_for


class ExecutionService:
    def __init__(self, controller):
        self.ctrl = controller
        self.cfg, self.store = controller.cfg, controller.store

    def authenticate(self, worker, token):
        import hmac

        item = self.cfg.raw.get("workers", {}).get(worker)
        if not item or not self.cfg.project_id:
            return False
        expected = os.environ.get(item["token_env"], "")
        forbidden = {self.cfg.worker_token, self.cfg.operator_token}
        other = [
            os.environ.get(v["token_env"], "")
            for k, v in self.cfg.raw["workers"].items()
            if k != worker
        ]
        return (
            len(expected) >= 32
            and expected not in forbidden
            and expected not in other
            and hmac.compare_digest(expected, token)
        )

    def _owned(self, worker, aid):
        row = self.store.one("SELECT * FROM attempts WHERE id=?", (aid,))
        if not row or row["worker_id"] != worker:
            raise ControllerError("attempt belongs to another worker")
        return row

    def preflight(self, worker):
        choices = self.cfg.raw["workers"][worker]["environments"]
        return {
            "version": 1,
            "project_id": self.cfg.project_id,
            "runner_code": self.ctrl._policy()["runner_code"],
            "execution": self.cfg.execution,
            "environments": {k: self.cfg.raw["environments"][k] for k in choices},
            "routes": [
                vars(r) for r in self.cfg.routes.values() if r.environment in choices
            ],
        }

    def claim(self, worker, request_id):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("bounded request_id required")
        key = "dispatch:" + worker + ":" + request_id
        with self.store.transaction():
            previous = self.store.get_op(key, "")
            if previous:
                row = self._owned(worker, previous)
                caps = [
                    "native",
                    *{r.harness for r in self.cfg.routes.values()},
                    *(
                        "environment:" + n
                        for n in self.cfg.raw["workers"][worker]["environments"]
                    ),
                ]
                if row["status"] != "leased" or not self.ctrl.resume_claim(
                    previous, worker, caps
                ):
                    raise ControllerError("replayed assignment is fenced")
                path = self.store.state_dir / "dispatch" / previous / "assignment.json"
                return json.loads(path.read_text())
            # Workflow installation is validated before the lease, never by the remote agent.
            runtime = runtime_for(self.cfg, self.store)
            runtime.validate()
            self.ctrl.recover("execution-supervisor")
            caps = [
                "native",
                *{r.harness for r in self.cfg.routes.values()},
                *(
                    "environment:" + n
                    for n in self.cfg.raw["workers"][worker]["environments"]
                ),
            ]
            claimed = self.ctrl.claim_job(worker, caps)
            if claimed is None:
                return None
            directory = self.store.state_dir / "dispatch" / claimed["attempt_id"]
            directory.mkdir(parents=True)

            def publish():
                assignment = self._assignment(claimed)
                (directory / "assignment.json").write_text(json.dumps(assignment))
                artifacts.sync_tree(directory)
                self.store.set_op(key, claimed["attempt_id"], "execution")

            runtime.run(claimed, publish)
            from datetime import datetime, timedelta, timezone

            row = self._owned(worker, claimed["attempt_id"])
            until = min(
                datetime.fromisoformat(row["deadline"]),
                datetime.now(timezone.utc)
                + timedelta(seconds=claimed["inputs"]["lease_seconds"]),
            )
            self.store.execute(
                "UPDATE jobs SET lease_until=? WHERE id=?",
                (until.isoformat(), row["job_id"]),
            )
            return json.loads((directory / "assignment.json").read_text())

    def _assignment(self, claimed):
        snapshot = claimed["snapshot"]
        source = self.store.resolve_path(snapshot["input_repo"])
        paths = {"inputs/source": source}
        references = {snapshot["input_repo"]: "inputs/source"}
        candidates = claimed["inputs"]["candidate"]
        for n, candidate in enumerate(candidates.values()):
            archive = self.store.resolve_path(candidate["archive"])
            artifacts.verify(archive)
            paths[f"archives/candidate-{n}"] = archive
            references[candidate["archive"]] = f"archives/candidate-{n}"
        # Transfer only this root's prior check/review records and required artifacts.
        jobs = self.ctrl._jobs(claimed["work_item"]["id"])
        attempts = [
            dict(row)
            for row in self.store.query(
                "SELECT * FROM attempts WHERE job_id IN (SELECT id FROM jobs WHERE work_item_id=?)",
                (claimed["work_item"]["id"],),
            )
            if row["job_id"] in {j["id"] for j in jobs}
        ]
        for row in attempts:
            if row["archive_path"] and row["archive_path"] not in references:
                archive = self.store.resolve_path(row["archive_path"])
                artifacts.verify(archive)
                name = "archives/" + row["id"]
                paths[name] = archive
                references[row["archive_path"]] = name
        for cid in snapshot["source"]["checks"]:
            binding = snapshot["bindings"][cid]
            for path, digest in binding["files"].items():
                local = self.store.resolve_path(path)
                if artifacts.digest(local) != digest:
                    raise ControllerError("trusted command binding changed")
                # Executables must be installed compatibly at the worker; do not ship host binaries.
                if path != binding["argv"][0] and local.is_file():
                    name = "trusted/" + identity(path)
                    paths[name] = local
                    references[path] = name
        return {
            "version": 1,
            "project_id": self.cfg.project_id,
            "claim": claimed,
            "claim_digest": identity(claimed),
            "references": references,
            "package": pack(paths),
            "jobs": [dict(j) for j in jobs],
            "attempts": attempts,
            "execution": self.cfg.execution,
            "max_output_bytes": self.cfg.max_output_bytes,
        }

    def heartbeat(self, worker, aid):
        row = self._owned(worker, aid)
        return row["status"] in {"leased", "running"} and self.ctrl.heartbeat(
            worker, aid
        )

    def start(self, worker, aid, execution_id):
        import re

        from dark_factory.controller import _digest

        if not isinstance(execution_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", execution_id
        ):
            raise ValueError("execution_id must be a durable UUID")
        with self.store.transaction():
            row = self._owned(worker, aid)
            previous = self.store.get_op("execution-start:" + aid, "")
            if previous and previous != execution_id:
                raise ControllerError("another executor owns this start intent")
            if not self.heartbeat(worker, aid):
                return False
            job = self.store.one("SELECT * FROM jobs WHERE id=?", (row["job_id"],))
            root = self.store.one(
                "SELECT * FROM work_items WHERE id=?", (job["work_item_id"],)
            )
            snapshot = json.loads(root["snapshot_json"])
            self.ctrl._require_root_scope(root)
            if (
                snapshot["policy"] != self.ctrl._policy()
                or job["kind"] not in {"plan", "critique"}
                and root["approved_digest"]
                != _digest(self.ctrl._approval_payload(root["id"]))
            ):
                raise ControllerError("execution authority changed before start")
            if not self.heartbeat(worker, aid):
                return False
            self.store.set_op("execution-start:" + aid, execution_id, "execution")
            self.store.execute(
                "UPDATE attempts SET status='running' WHERE id=?", (aid,)
            )
            return True

    def finish(self, worker, data):
        ack = self._finish(worker, data)
        # Settlement is committed before dropping reproducible transport copies.
        path = (
            self.store.state_dir / "dispatch" / data["attempt_id"] / "assignment.json"
        )
        value = json.loads(path.read_text())
        if "package" in value:
            value["package_sha256"] = value.pop("package")["sha256"]
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value))
            temporary.replace(path)
            artifacts.sync_tree(path.parent)
        return ack

    def _finish(self, worker, data):
        aid = data["attempt_id"]
        with self.store.transaction():
            row = self._owned(worker, aid)
            request = identity(data)
            key = "result:" + aid
            previous = self.store.get_op(key, "")
            if previous:
                if previous != request:
                    raise ControllerError("conflicting duplicate result")
                return {"accepted": True, "attempt_id": aid, "result_digest": request}
            if row["status"] not in {"leased", "running", "cancelling"}:
                raise ControllerError("attempt is fenced")
            from datetime import datetime, timezone

            job = self.store.one(
                "SELECT lease_until FROM jobs WHERE id=?", (row["job_id"],)
            )
            if datetime.fromisoformat(job["lease_until"]) <= datetime.now(timezone.utc):
                raise ControllerError("attempt lease expired before result submission")
            # Bound verified artifact ingestion by the original attempt deadline.
            # It cannot authorize execution replay or extend the approved deadline.
            self.store.execute(
                "UPDATE jobs SET lease_until=? WHERE id=?",
                (row["deadline"], row["job_id"]),
            )
            assignment = json.loads(
                (
                    self.store.state_dir / "dispatch" / aid / "assignment.json"
                ).read_text()
            )
            if data["claim_digest"] != assignment["claim_digest"]:
                raise ControllerError("assignment identity changed")
            receipt = data["receipt"]
            if receipt.get("ok") and not self.store.get_op(
                "execution-start:" + aid, ""
            ):
                raise ControllerError("result has no authorized execution start")
            if (
                set(receipt)
                != {"ok", "output", "cost", "error", "timed_out", "cancelled"}
                or any(
                    type(receipt[k]) is not bool
                    for k in ("ok", "timed_out", "cancelled")
                )
                or type(receipt["cost"]) is not int
                or receipt["cost"] < 0
                or not isinstance(receipt["output"], str)
                or receipt["error"] is not None
                and not isinstance(receipt["error"], str)
            ):
                raise ValueError("invalid execution receipt")
            destination = self.store.state_dir / "archives" / aid
            if data.get("archive"):
                with tempfile.TemporaryDirectory(
                    prefix="factory-result-", dir=self.store.state_dir
                ) as temporary:
                    unpack(data["archive"], Path(temporary) / "package")
                    archive = Path(temporary) / "package" / "archive"
                    manifest = artifacts.verify(archive)
                    result = json.loads((archive / "result.json").read_text())
                    if (
                        manifest.get("attempt_id") != aid
                        or manifest.get("job_id") != row["job_id"]
                        or result.get("inputs") != json.loads(row["inputs_json"])
                    ):
                        raise ControllerError("result is not bound to the assignment")
                    if destination.exists():
                        artifacts.verify(destination)
                        if artifacts.digest(
                            destination / "manifest.json"
                        ) != artifacts.digest(archive / "manifest.json"):
                            raise ControllerError("conflicting staged archive")
                    else:
                        shutil.move(str(archive), destination)
                    artifacts.sync_tree(destination)
            self.ctrl.finish_attempt(
                aid,
                receipt["ok"],
                receipt["output"],
                receipt["cost"],
                str(destination) if data.get("archive") else None,
                receipt["error"],
                receipt["timed_out"],
                receipt["cancelled"],
            )
            self.store.set_op(key, request, "execution")
            self.store.set_op("workflow-settled:" + aid, "remote-settled", "execution")
            self.ctrl.reconcile_continuations()
            return {"accepted": True, "attempt_id": aid, "result_digest": request}
