"""Project/worker-scoped assignment service; only controller settles results."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from dark_factory import artifacts
from dark_factory.controller import ControllerError
from dark_factory.execution.packages import identity, pack, unpack
from dark_factory.models import ACTIVE_ATTEMPT
from dark_factory.secrets import secret
from dark_factory.store import now_iso


class ExecutionService:
    def __init__(self, controller):
        self.ctrl = controller
        self.cfg, self.store = controller.cfg, controller.store

    def authenticate(self, worker, token):
        import hmac

        item = self.cfg.workers.get(worker)
        if not item:
            return False
        expected = secret(item["token_env"])
        forbidden = {self.cfg.operator_token}
        other = [
            secret(v["token_env"]) for k, v in self.cfg.workers.items() if k != worker
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

    def _capabilities(self, worker):
        settings = self.cfg.workers[worker]
        return [
            "native",
            *sorted({r.harness for r in self.cfg.routes.values()}),
            *sorted("environment:" + name for name in settings["environments"]),
            *("label:" + label for label in settings.get("labels", [])),
        ]

    def preflight(self, worker):
        choices = self.cfg.workers[worker]["environments"]
        return {
            "version": 1,
            "project_id": self.cfg.project_id,
            "runner_code": self.ctrl._policy()["runner_code"],
            "execution": self.cfg.execution,
            "environments": {k: self.cfg.environments[k] for k in choices},
            "routes": [
                vars(r) for r in self.cfg.routes.values() if r.environment in choices
            ],
            "worker": {
                key: self.cfg.workers[worker].get(key, default)
                for key, default in {
                    "labels": [],
                    "exclusive_labels": [],
                    "max_parallel": 1,
                    "max_load": 0.8,
                    "min_free_gb": 2,
                }.items()
            },
        }

    def _capacity_available(self, worker, capacity):
        settings = self.cfg.workers[worker]
        # Direct in-process callers and pre-capacity protocol clients retain the
        # controller-side slot guard. New runner clients always report pressure.
        if capacity is None:
            return True
        if not isinstance(capacity, dict):
            return False
        try:
            load = float(capacity["load"])
            free_gb = float(capacity["free_gb"])
            slots = int(capacity["slots"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            0 <= load <= settings.get("max_load", 0.8)
            and free_gb >= settings.get("min_free_gb", 2)
            and 1 <= slots <= settings.get("max_parallel", 1)
        )

    def claim(self, worker, request_id, capacity=None):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("bounded request_id required")
        key = "dispatch:" + worker + ":" + request_id
        if not self.store.get_op(key, ""):
            # Housekeeping can wait on the code host or the decision agent, so it runs
            # before the claim's write transaction opens, never inside it.
            self.ctrl.recover("execution-supervisor")
            self.ctrl.settle_interruptions("execution-supervisor")
            self.ctrl.completeness_tick()
        with self.store.transaction():
            caps = self._capabilities(worker)
            if not self._capacity_available(worker, capacity):
                return None
            # An authenticated worker polling for work is online even when no
            # assignment is eligible. Preserve busy while an active lease owns
            # it; _claim changes the flag atomically when it assigns work.
            self.store.execute(
                """INSERT INTO workers(id,capabilities,last_heartbeat,busy,telemetry_json)
                VALUES (?,?,?,0,?) ON CONFLICT(id) DO UPDATE SET
                capabilities=excluded.capabilities,
                last_heartbeat=excluded.last_heartbeat,
                telemetry_json=excluded.telemetry_json""",
                (worker, json.dumps(caps), now_iso(), json.dumps(capacity or {})),
            )
            previous = self.store.get_op(key, "")
            if previous:
                row = self._owned(worker, previous)
                if row["status"] != "leased" or not self.ctrl.resume_claim(
                    previous, worker, caps
                ):
                    raise ControllerError("replayed assignment is fenced")
                path = self.store.state_dir / "dispatch" / previous / "assignment.json"
                return json.loads(path.read_text())
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

            try:
                publish()
            except ValueError as exc:
                # A tree that cannot be packaged is a recorded failure of this
                # attempt, not a claim that waits for its lease to expire.
                shutil.rmtree(directory, ignore_errors=True)
                self.ctrl.finish_attempt(
                    claimed["attempt_id"],
                    False,
                    "",
                    0,
                    None,
                    error=f"package refused at pack time: {exc}",
                )
                raise ControllerError(f"package refused at pack time: {exc}") from exc
            from datetime import datetime, timedelta

            row = self._owned(worker, claimed["attempt_id"])
            until = min(
                datetime.fromisoformat(row["deadline"]),
                datetime.now(UTC)
                + timedelta(seconds=claimed["inputs"]["lease_seconds"]),
            )
            self.ctrl.set_lease(row["job_id"], until)
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
        resume = claimed["inputs"].get("resume") or {}
        if resume.get("archive") and resume["archive"] not in references:
            archive = self.store.resolve_path(resume["archive"])
            artifacts.verify(archive)
            paths["archives/resume"] = archive
            references[resume["archive"]] = "archives/resume"
        # Transfer this root's small attempt records. Historical workspace archives
        # stay on the controller unless the candidate or selected resume needs one.
        jobs = self.ctrl.jobs(claimed["work_item"]["id"])
        attempts = [
            dict(row)
            for row in self.store.query(
                "SELECT * FROM attempts WHERE job_id IN (SELECT id FROM jobs WHERE work_item_id=?)",
                (claimed["work_item"]["id"],),
            )
            if row["job_id"] in {j["id"] for j in jobs}
        ]
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
        try:
            package = pack(paths)
        except ValueError as exc:
            raise ValueError(
                "required remote inputs exceed the transport limit: "
                + ", ".join(sorted(paths))
            ) from exc
        return {
            "version": 1,
            "project_id": self.cfg.project_id,
            "claim": claimed,
            "claim_digest": identity(claimed),
            "references": references,
            "package": package,
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
                snapshot["policy"] != self.ctrl._policy(root["repo"])
                or job["kind"] not in {"plan", "critique"}
                and not self.ctrl._approval_current(root)
            ):
                raise ControllerError("execution authority changed before start")
            if not self.heartbeat(worker, aid):
                return False
            self.store.set_op("execution-start:" + aid, execution_id, "execution")
            self.ctrl.mark_running(aid)
            return True

    def task_progress(self, worker, data):
        self._owned(worker, data["attempt_id"])
        return self.ctrl.record_task_progress(
            data["attempt_id"],
            data["task_id"],
            data["kind"],
            data.get("payload") or {},
            worker,
            data["event_id"],
        )

    def task_instructions(self, worker, aid):
        self._owned(worker, aid)
        return self.ctrl.pending_task_instructions(aid, worker)

    def task_ack(self, worker, data):
        self._owned(worker, data["attempt_id"])
        return self.ctrl.acknowledge_task_instruction(
            data["attempt_id"], data["instruction_id"], worker
        )

    def checkpoint(self, worker, data):
        """Ingest one verified, approval-bound workspace checkpoint."""
        aid = data.get("attempt_id")
        checkpoint_id = data.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not re.fullmatch(
            r"cp_[a-f0-9]{12}", checkpoint_id
        ):
            raise ValueError("invalid checkpoint identity")
        row = self._owned(worker, aid)
        if row["status"] not in ACTIVE_ATTEMPT:
            raise ControllerError("attempt is fenced")
        job_lease = self.store.one(
            "SELECT lease_until FROM jobs WHERE id=?", (row["job_id"],)
        )["lease_until"]
        if not job_lease or datetime.fromisoformat(job_lease) <= datetime.now(UTC):
            raise ControllerError("attempt is fenced")
        with tempfile.TemporaryDirectory(
            prefix="factory-checkpoint-", dir=self.store.state_dir
        ) as temporary:
            package = Path(temporary) / "package"
            unpack(data.get("checkpoint"), package)
            source = package / "checkpoint"
            metadata = artifacts.verify(source)
            job = self.store.one("SELECT * FROM jobs WHERE id=?", (row["job_id"],))
            root = self.store.one(
                "SELECT * FROM work_items WHERE id=?", (job["work_item_id"],)
            )
            inputs = json.loads(row["inputs_json"] or "{}")
            expected = {
                "checkpoint_id": checkpoint_id,
                "root_id": root["id"],
                "job_id": job["id"],
                "attempt_id": aid,
                "plan_digest": root["approved_digest"],
                "candidate_digest": identity(inputs.get("candidate", {})),
            }
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise ControllerError("checkpoint approval binding changed")
            if (
                not isinstance(metadata.get("sequence"), int)
                or not 1 <= metadata["sequence"] <= 5
            ):
                raise ValueError("checkpoint sequence exceeds limit")
            manifest_digest = artifacts.digest(source / "manifest.json")
            destination = self.store.state_dir / "checkpoints" / aid / checkpoint_id
            with self.store.transaction():
                row = self._owned(worker, aid)
                if row["status"] not in ACTIVE_ATTEMPT:
                    raise ControllerError("attempt is fenced")
                existing = self.store.one(
                    "SELECT * FROM attempt_checkpoints WHERE id=?", (checkpoint_id,)
                )
                if existing:
                    if existing["manifest_digest"] != manifest_digest:
                        raise ControllerError("conflicting checkpoint replay")
                    return {
                        "accepted": True,
                        "attempt_id": aid,
                        "checkpoint_id": checkpoint_id,
                        "manifest_digest": manifest_digest,
                    }
                if self.store.one(
                    "SELECT COUNT(*) n FROM attempt_checkpoints WHERE attempt_id=?",
                    (aid,),
                )["n"] >= 5 or self.store.one(
                    "SELECT id FROM attempt_checkpoints WHERE attempt_id=? AND sequence=?",
                    (aid, metadata["sequence"]),
                ):
                    raise ControllerError("checkpoint limit or sequence already used")
                lease = self.store.one(
                    "SELECT lease_until FROM jobs WHERE id=?", (row["job_id"],)
                )["lease_until"]
                if not lease or datetime.fromisoformat(lease) <= datetime.now(UTC):
                    raise ControllerError("attempt is fenced")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), destination)
                detail = json.loads((destination / "checkpoint.json").read_text())
                self.store.execute(
                    """INSERT INTO attempt_checkpoints
                    (id,root_id,job_id,attempt_id,sequence,path,manifest_digest,
                     workspace_digest,plan_digest,candidate_digest,detail_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        checkpoint_id,
                        root["id"],
                        job["id"],
                        aid,
                        metadata["sequence"],
                        str(destination),
                        manifest_digest,
                        metadata["workspace_digest"],
                        metadata["plan_digest"],
                        metadata["candidate_digest"],
                        json.dumps(detail),
                        now_iso(),
                    ),
                )
            artifacts.sync_tree(destination.parent)
        return {
            "accepted": True,
            "attempt_id": aid,
            "checkpoint_id": checkpoint_id,
            "manifest_digest": manifest_digest,
        }

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

    def finish_remote(self, worker, data):
        """Translate a permanent fence into a terminal worker disposition."""
        try:
            return self.finish(worker, data)
        except ControllerError as exc:
            if str(exc) == "attempt lease expired before result submission":
                self.ctrl.recover("execution-supervisor")
                if (
                    self._owned(worker, data.get("attempt_id"))["status"]
                    not in ACTIVE_ATTEMPT
                ):
                    exc = ControllerError("attempt is fenced")
            if str(exc) != "attempt is fenced":
                raise
            return {
                "accepted": False,
                "terminal": True,
                "disposition": "fenced",
                "attempt_id": data.get("attempt_id"),
                "result_digest": identity(data),
            }

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
            if row["status"] not in ACTIVE_ATTEMPT:
                raise ControllerError("attempt is fenced")
            from datetime import datetime

            job = self.store.one(
                "SELECT lease_until FROM jobs WHERE id=?", (row["job_id"],)
            )
            if datetime.fromisoformat(job["lease_until"]) <= datetime.now(UTC):
                raise ControllerError("attempt lease expired before result submission")
            # Bound verified artifact ingestion by the original attempt deadline.
            # It cannot authorize execution replay or extend the approved deadline.
            self.ctrl.set_lease(row["job_id"], row["deadline"])
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
                != {
                    "ok",
                    "output",
                    "cost",
                    "error",
                    "timed_out",
                    "cancelled",
                    "cleanup_status",
                }
                or any(
                    type(receipt[k]) is not bool
                    for k in ("ok", "timed_out", "cancelled")
                )
                or type(receipt["cost"]) is not int
                or receipt["cost"] < 0
                or not isinstance(receipt["output"], str)
                or receipt["error"] is not None
                and not isinstance(receipt["error"], str)
                or receipt["cleanup_status"]
                not in {"cleaned", "cleanup_failed", "archive_failed"}
                or bool(data.get("archive"))
                != (receipt["cleanup_status"] in {"cleaned", "cleanup_failed"})
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
                    for index, update in enumerate(result.get("task_updates", [])):
                        if not isinstance(update, dict):
                            continue
                        kind = {
                            "progress": "task_progress",
                            "done": "task_reported_done",
                            "blocked": "task_blocked",
                        }.get(update.get("state"))
                        if not kind or not isinstance(update.get("task_id"), str):
                            continue
                        try:
                            self.ctrl.record_task_progress(
                                aid,
                                update["task_id"],
                                kind,
                                update,
                                worker,
                                f"{aid}:report:{index}",
                            )
                        except ControllerError as exc:
                            self.store.audit(
                                worker,
                                "task_progress.rejected",
                                {
                                    "attempt": aid,
                                    "task": update["task_id"],
                                    "error": str(exc)[:500],
                                },
                            )
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
            self.ctrl.record_cleanup(aid, receipt["cleanup_status"])
            self.store.set_op(key, request, "execution")
            self.ctrl.reconcile_continuations()
            return {"accepted": True, "attempt_id": aid, "result_digest": request}

    def cleanup(self, worker, aid, result_digest):
        """Acknowledge a post-settlement release retry by the owning worker."""
        with self.store.transaction():
            row = self._owned(worker, aid)
            expected = self.store.get_op("result:" + aid, "")
            if not expected or result_digest != expected:
                raise ControllerError("cleanup is not bound to the settled result")
            if row["cleanup_status"] == "cleaned":
                return {
                    "accepted": True,
                    "attempt_id": aid,
                    "result_digest": expected,
                }
            if row["cleanup_status"] != "cleanup_failed" or not row["archive_path"]:
                raise ControllerError("attempt cleanup is not retryable")
            artifacts.verify(self.store.resolve_path(row["archive_path"]))
            self.ctrl.record_cleanup(aid, "cleaned")
            self.store.audit(
                worker,
                "cleanup.remote_confirmed",
                {"attempt_id": aid, "result_digest": expected},
            )
            return {
                "accepted": True,
                "attempt_id": aid,
                "result_digest": expected,
            }
