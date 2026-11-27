"""Execute a pinned attempt, archive unique work, then release owned resources."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from dark_factory import artifacts
from dark_factory.adapters.env.native import NativeEnvironment, process_environment
from dark_factory.adapters.env.registry import environment_for
from dark_factory.adapters.git.local import capture, git
from dark_factory.adapters.harness.external import ExternalHarness
from dark_factory.adapters.harness.registry import (
    adapter_for,
    capabilities,
    credential_for,
)
from dark_factory.execution.policy import recipe_for
from dark_factory.models import ACTIVE_ATTEMPT
from dark_factory.redaction import redact
from dark_factory.runner import CommandRunner, RunnerTimeout
from dark_factory.store import new_id

HEARTBEAT_SECONDS = 5.0


class LeaseKeeper:
    """Renews one attempt's lease from claim to release, whatever the harness does.

    A refusal (cancelled, fenced, past deadline) is recorded and read by the
    worker; a transient error is not a refusal, and the lease itself bounds it.
    """

    def __init__(self, controller, worker_id, attempt_id, interval):
        self.controller, self.worker_id, self.attempt_id = (
            controller,
            worker_id,
            attempt_id,
        )
        self.interval = max(0.05, interval)
        self.refused = False
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _beat(self):
        try:
            if not self.controller.heartbeat(self.worker_id, self.attempt_id):
                self.refused = True
        except Exception:
            pass

    def _loop(self):
        while not self._halt.wait(self.interval) and not self.refused:
            self._beat()

    def start(self):
        self._beat()
        self._thread.start()

    def stop(self):
        self._halt.set()
        self._thread.join(timeout=5)


class Worker:
    def __init__(self, cfg, store, controller, worker_id="local"):
        self.cfg, self.store, self.controller, self.worker_id = (
            cfg,
            store,
            controller,
            worker_id,
        )
        self.env = NativeEnvironment(cfg.state_dir / "workspaces")
        self._base_cfg = cfg

    def _harness(self, route=None):
        return adapter_for(route["harness"] if route else "fake").open(
            self.cfg,
            route,
            getattr(self, "_harness_prefix", None),
            getattr(self, "_harness_env", None),
        )

    def _context(self, claimed, workspace, restored=False):
        root = claimed["work_item"]
        snapshot = claimed["snapshot"]
        kind = claimed["job"]["kind"]
        common = {
            "repository": root["repo"],
            "base_commit": snapshot["base"],
            "allowed_paths": snapshot["source"]["allowed_paths"],
            "protected_paths": snapshot["source"].get("protected_paths", []),
            "checks": snapshot["source"]["checks"],
            "manual_gates": snapshot["manual_acceptance"],
            "routes": snapshot["policy"]["routes"],
            "spending_ceiling_cents": root["spending_ceiling_cents"],
            "revision_feedback": snapshot.get("revision_feedback", []),
            "task_list": claimed.get("task_list", {}),
        }
        if kind == "plan":
            common["selected_work"] = {"title": root["title"], "body": root["body"]}
        else:
            common["approved_requirements"] = json.loads(root["plan_json"])
            if kind == "review":
                common["approved_requirements"].pop("selected_work", None)
        if claimed["spec"].get("repair"):
            common["targeted_repair"] = [
                {
                    "kind": job["kind"],
                    "error": attempt["error"],
                    "result": json.loads(attempt["result_json"] or "{}").get(
                        "checks", []
                    ),
                }
                for job in self.controller.jobs(root["id"])
                for attempt in self.store.query(
                    "SELECT error,result_json FROM attempts WHERE job_id=? AND status='failed'",
                    (job["id"],),
                )
            ]
        resume = claimed["inputs"].get("resume")
        if resume:
            # Say only what is true: the workspace is restored for an
            # implementation step that had an archived checkout.
            common["resumed_after_interruption"] = {
                "previous_attempt": resume["attempt"],
                "what_happened": resume["detail"][:500],
                "work_restored": restored,
                "instructions": "Your previous attempt at this step was interrupted. "
                + (
                    "Its work is already in the workspace"
                    + (
                        " and your earlier conversation is loaded"
                        if resume.get("session_id")
                        and (claimed["inputs"].get("route") or {}).get("harness")
                        == "pi"
                        else ""
                    )
                    + ". Check what is done, continue from there and do not redo "
                    "finished work."
                    if restored
                    else "None of its work could be restored; start the step again "
                    "from the beginning."
                ),
            }
        if kind in {"check", "review"}:
            common["candidate"] = json.loads(root["candidate_commits"])
        if kind == "review":
            candidate = common["candidate"][root["repo"]]
            candidate_result = json.loads(
                (
                    self.store.resolve_path(candidate["archive"]) / "result.json"
                ).read_text()
            )
            common["declared_candidate_assumptions"] = candidate_result.get(
                "assumptions", []
            )
            common["exact_diff"] = (
                self.store.resolve_path(candidate["archive"]) / "diff.patch"
            ).read_text()
            common["check_evidence"] = []
            for job in self.controller.jobs(root["id"]):
                if job["kind"] == "check" and job["status"] == "succeeded":
                    att = self.store.one(
                        "SELECT result_json,inputs_json FROM attempts WHERE job_id=? AND status='succeeded'",
                        (job["id"],),
                    )
                    if (
                        att
                        and json.loads(att["inputs_json"])["candidate"]
                        == common["candidate"]
                    ):
                        common["check_evidence"].append(json.loads(att["result_json"]))
        # Preserve repository instructions as labeled source material; never merge
        # project-local executable extensions or settings into the harness environment.
        instructions = {}
        for path in workspace.rglob("AGENTS.md"):
            if ".git" not in path.parts and not path.is_symlink():
                instructions[str(path.relative_to(workspace))] = path.read_text()
        common["repository_instructions_source_material"] = instructions
        assets = claimed["snapshot"]["policy"]["assets"]
        role = claimed["spec"].get("role")
        common["selected_skills"] = assets["skills"] + assets.get(
            "role_skills", {}
        ).get(role, [])
        return (
            claimed["prompt"]
            + "\n\nFactory factual brief (permissions are enforced by code):\n"
            + json.dumps(common, indent=2)
        )

    def _unstarted_claims(self):
        """Attempts this worker leased but never started (no workspace, no process)."""
        return [
            row["id"]
            for row in self.store.query(
                """SELECT id FROM attempts WHERE status='leased' AND worker_id=?
                   AND workspace IS NULL AND pid IS NULL ORDER BY started_at,rowid""",
                (self.worker_id,),
            )
        ]

    def run_once(self):
        self.controller.settle_interruptions(self.worker_id)
        self.controller.completeness_tick()
        self.controller.reconcile_continuations()
        pending = self._unstarted_claims()
        if not pending and (
            not self.controller.dispatch_allowed()[0]
            or not self.store.one("SELECT id FROM jobs WHERE status='ready' LIMIT 1")
        ):
            return False
        caps = capabilities(self.cfg)
        # Discovery is not authority: resume_claim revalidates owner, policy,
        # approval, lease and deadline before the claim runs again.
        for aid in pending:
            resumed = self.controller.resume_claim(aid, self.worker_id, caps)
            if resumed:
                self._run_claim(resumed)
                self.controller.reconcile_continuations()
                return True
        claimed = self.controller.claim_job(self.worker_id, caps)
        if not claimed:
            return False
        self._run_claim(claimed)
        return True

    def _run_claim(self, claimed):
        try:
            self._execute(claimed)
        except Exception as exc:
            # An unexpected worker error must not replay a CLI or leave the lease open.
            if self.controller.attempt_status(claimed["attempt_id"]) in ACTIVE_ATTEMPT:
                self.controller.finish_attempt(
                    claimed["attempt_id"],
                    False,
                    "worker error",
                    claimed["spec"]["budget_cents"],
                    None,
                    error=str(exc),
                )
            self.store.audit(
                self.worker_id,
                "worker.error",
                {"attempt": claimed["attempt_id"], "error": str(exc)},
            )
            raise
        finally:
            self.controller.reconcile_continuations()

    def _execute(self, claimed):
        """Run one claim while a dedicated thread keeps its lease alive."""
        keeper = LeaseKeeper(
            self.controller,
            self.worker_id,
            claimed["attempt_id"],
            min(HEARTBEAT_SECONDS, claimed["inputs"].get("lease_seconds", 30) / 3),
        )
        keeper.start()
        try:
            return self._run(claimed, keeper)
        finally:
            keeper.stop()

    def _run(self, claimed, keeper):
        self.cfg = self._base_cfg
        attempt_id = claimed["attempt_id"]
        job = claimed["job"]
        spec = claimed["spec"]
        snapshot = claimed["snapshot"]
        runtime = self.cfg.state_dir / "runtime" / attempt_id
        route = claimed["inputs"].get("route")
        recipe = claimed["inputs"].get("environment") or recipe_for(self.cfg, route)
        if route and route["harness"] in recipe.get("harness_binaries", {}):
            self.cfg = copy.copy(self.cfg)
            self.cfg.execution = {
                **self.cfg.execution,
                adapter_for(route["harness"]).binary_key: recipe["harness_binaries"][
                    route["harness"]
                ],
            }
        self.env = environment_for(self.cfg.state_dir / "workspaces", recipe, route)
        runtime.mkdir(parents=True, mode=0o700)
        archive = self.cfg.state_dir / "archives" / attempt_id
        prepared = None
        baseline = None
        output = ""
        error = None
        ok = False
        timed_out = False
        cancelled = False
        cost = 0
        result = {"ok": False, "inputs": claimed["inputs"], "usage": {}, "checks": []}
        events = []
        last_checkpoint_digest = None
        redactions: list[str] = []
        archived = False
        cleaned = False

        deadline = datetime.fromisoformat(
            self.store.one("SELECT deadline FROM attempts WHERE id=?", (attempt_id,))[
                "deadline"
            ]
        )

        def remaining():
            return (deadline - datetime.now(UTC)).total_seconds()

        def stopped():
            # Read-only: the lease is renewed by the keeper thread, not here.
            return keeper.refused or self.controller.attempt_status(attempt_id) not in {
                "leased",
                "running",
            }

        def on_start(pid):
            self.controller.record_process(attempt_id, pid)

        try:
            source = {
                "path": str(self.store.resolve_path(snapshot["input_repo"])),
                "commit": snapshot["base"],
            }
            candidate = claimed["inputs"]["candidate"].get(claimed["work_item"]["repo"])
            resume = claimed["inputs"].get("resume")
            resume_archive = (
                self.store.resolve_path(resume["archive"])
                if resume and resume.get("archive")
                else None
            )
            if resume_archive:
                artifacts.verify(resume_archive)
            if job["kind"] in {"check", "review"} or spec.get("repair"):
                if not candidate:
                    raise RuntimeError("candidate input missing")
                artifacts.verify(self.store.resolve_path(candidate["archive"]))
                source = {
                    "path": str(
                        self.store.resolve_path(candidate["archive"]) / "workspace"
                    ),
                    "commit": candidate["commit"],
                }
            restored = False
            if (
                job["kind"] == "implement"
                and resume_archive
                and (resume_archive / "workspace").is_dir()
            ):
                # Continue from the interrupted attempt's checkout, uncommitted work included.
                source = {"path": str(resume_archive / "workspace"), "restore": True}
                restored = True
            prepared = self.env.prepare(attempt_id, source)
            self.controller.record_workspace(attempt_id, prepared.workspace)
            if job["kind"] == "implement":
                last_checkpoint_digest = artifacts.git_fingerprint(prepared.workspace)
            if job["kind"] in {"plan", "critique", "review"}:
                baseline = (
                    source["commit"],
                    artifacts.git_fingerprint(prepared.workspace),
                )
            env = process_environment(runtime)
            live = recipe["kind"] != "native"
            role = spec.get("role")
            prefix = []
            readable_files = []
            immutable_runtime_files = ()
            if role:
                adapter = adapter_for(claimed["inputs"]["route"]["harness"])
                immutable_runtime_files = adapter.immutable_runtime_files
                if adapter.binary_key and recipe["kind"] == "macos-sandbox":
                    binary = adapter.binary(self.cfg)
                    readable_files = [Path(shutil.which(binary) or binary).resolve()]
            if live:
                prefix = self.env.prefix(
                    prepared,
                    runtime,
                    network=bool(role),
                    writable=snapshot["source"]["allowed_paths"]
                    if role == "coder"
                    else (["."] if not role else []),
                    protected=snapshot["source"].get("protected_paths", []),
                    readable_files=readable_files,
                    immutable_runtime_files=immutable_runtime_files,
                    private_paths=[
                        self.cfg.source_path,
                        Path(snapshot["source"]["path"]),
                    ],
                )
            if stopped():
                raise RuntimeError("attempt cancelled")
            if role:
                route = claimed["inputs"]["route"]
                if route["harness"] != "fake" and not claimed["inputs"].get("external"):
                    secret = _route_secret(route, self.cfg)
                    redactions.append(secret)
                    if not secret:
                        raise RuntimeError(
                            f"route credential unavailable: {route['auth_env']}"
                        )
                    env[route["auth_env"]] = secret
                # Version probes run in the same execution backend, without credential-file mounts.
                probe_runtime = runtime / "version-probe"
                version_prefix = (
                    self.env.prefix(
                        prepared,
                        probe_runtime,
                        network=False,
                        writable=[],
                        protected=[],
                    )
                    if recipe["kind"] in {"oci", "command"}
                    else []
                )
                self._harness_prefix = version_prefix
                self._harness_env = process_environment(probe_runtime)
                external = claimed["inputs"].get("external")
                harness = (
                    ExternalHarness(external) if external else self._harness(route)
                )
                prompt = self._context(claimed, prepared.workspace, restored)
                (runtime / "prompt.txt").write_text(prompt)
                extra = {
                    "role": role,
                    "route": route,
                    "runtime": str(runtime),
                    "env": env,
                    "prefix": prefix,
                    "timeout_seconds": max(
                        1, min(spec["timeout_seconds"], int(remaining()))
                    ),
                    "max_output_bytes": self.cfg.max_output_bytes,
                    "budget_cents": spec["budget_cents"],
                    "resume_session": _resume_session(resume_archive, resume),
                    "on_start": on_start,
                    "checks": snapshot["source"]["checks"],
                    "criteria": json.loads(claimed["work_item"]["plan_json"])
                    .get("proposal", {})
                    .get("acceptance", []),
                    "writable": snapshot["source"]["allowed_paths"],
                    "protected": snapshot["source"].get("protected_paths", []),
                }
                for event in harness.start(prompt, prepared.workspace, extra):
                    if remaining() <= 0:
                        # The attempt's own deadline, not an operator cancel.
                        timed_out = True
                        harness.cancel()
                    elif stopped():
                        cancelled = True
                        harness.cancel()
                    if event.type != "heartbeat":
                        if event.type == "session":
                            self.store.audit(
                                self.worker_id,
                                "harness.session",
                                {
                                    "attempt": attempt_id,
                                    "session": event.data.get("session_id"),
                                },
                            )
                        events.append(
                            {
                                "type": event.type,
                                "text": redact(event.text, redactions),
                                "data": event.data,
                            }
                        )
                        if job["kind"] == "implement":
                            current_digest = artifacts.git_fingerprint(
                                prepared.workspace
                            )
                            if current_digest != last_checkpoint_digest:
                                try:
                                    saved = self.controller.record_checkpoint(
                                        attempt_id,
                                        prepared.workspace,
                                        {
                                            "kind": event.type,
                                            "summary": redact(event.text, redactions),
                                            "task_id": event.data.get("task_id", ""),
                                        },
                                    )
                                    if saved:
                                        last_checkpoint_digest = current_digest
                                except Exception as exc:
                                    events.append(
                                        {
                                            "type": "checkpoint_rejected",
                                            "text": str(exc)[:500],
                                            "data": {},
                                        }
                                    )
                        if (
                            event.type
                            in {
                                "task_started",
                                "task_progress",
                                "task_reported_done",
                                "task_blocked",
                                "task_proposed",
                            }
                            and isinstance(event.data.get("task_id"), str)
                            and hasattr(self.controller, "record_task_progress")
                        ):
                            try:
                                self.controller.record_task_progress(
                                    attempt_id,
                                    event.data["task_id"],
                                    event.type,
                                    event.data,
                                    self.worker_id,
                                    str(event.data.get("event_id") or new_id("evt")),
                                )
                            except Exception as exc:
                                # One bad event from the agent is recorded and
                                # skipped; it does not end the attempt.
                                events.append(
                                    {
                                        "type": "task_event_rejected",
                                        "text": str(exc)[:500],
                                        "data": {"kind": event.type},
                                    }
                                )
                answer = harness.wait()
                output = redact(answer.output, redactions)
                ok = answer.ok
                cost = answer.cost_cents
                error = (
                    redact(answer.error, redactions) if answer.error else answer.error
                )
                if not ok:
                    result["failure_kind"] = "harness"
                result["usage"] = answer.usage
                result["session_id"] = answer.session_id
                result["cost_status"] = answer.usage.get(
                    "cost_status",
                    "reported" if answer.usage.get("reported_cost") else "estimated",
                )
                result["prompt_sha256"] = artifacts.digest(runtime / "prompt.txt")
                if job["kind"] == "implement":
                    try:
                        report = json.loads(output)
                    except ValueError:
                        report = {}
                    assumptions = (
                        report.get("assumptions", [])
                        if isinstance(report, dict)
                        else []
                    )
                    if not isinstance(assumptions, list) or not all(
                        isinstance(a, str) and a.strip() for a in assumptions
                    ):
                        raise RuntimeError(
                            "implementation assumptions must be a list of strings"
                        )
                    result["assumptions"] = assumptions
                    result["assumptions_recorded"] = (
                        isinstance(report, dict) and "assumptions" in report
                    )
                    task_updates = (
                        report.get("task_updates", [])
                        if isinstance(report, dict)
                        else []
                    )
                    if (
                        not isinstance(task_updates, list)
                        or len(task_updates) > 50
                        or not all(
                            isinstance(update, dict)
                            and isinstance(update.get("task_id"), str)
                            and update.get("state") in {"progress", "done", "blocked"}
                            and isinstance(update.get("summary", ""), str)
                            and (
                                update.get("state") != "blocked"
                                or isinstance(update.get("reason"), str)
                                and update["reason"].strip()
                            )
                            for update in task_updates
                        )
                    ):
                        raise RuntimeError("implementation task_updates are invalid")
                    result["task_updates"] = task_updates
                    if hasattr(self.controller, "record_task_progress"):
                        for index, update in enumerate(task_updates):
                            kind = {
                                "progress": "task_progress",
                                "done": "task_reported_done",
                                "blocked": "task_blocked",
                            }[update["state"]]
                            try:
                                self.controller.record_task_progress(
                                    attempt_id,
                                    update["task_id"],
                                    kind,
                                    update,
                                    self.worker_id,
                                    f"{attempt_id}:report:{index}",
                                )
                            except Exception as exc:
                                self.store.audit(
                                    self.worker_id,
                                    "task_progress.rejected",
                                    {
                                        "attempt": attempt_id,
                                        "task": update["task_id"],
                                        "error": str(exc)[:500],
                                    },
                                )
                    result["candidate"] = capture(
                        prepared.workspace,
                        snapshot["base"],
                        snapshot["source"]["allowed_paths"],
                        snapshot["source"].get("protected_paths", []),
                        snapshot["source"].get("diff_limits"),
                    )
                    if result["candidate"]["scope_violations"]:
                        ok = False
                        error = "protected or out-of-scope changes: " + str(
                            result["candidate"]["scope_violations"]
                        )
            elif job["kind"] == "check":
                ok = True
                result["stages"] = []
                out_dir = runtime / "stage-out"
                out_dir.mkdir(exist_ok=True)
                pipeline = snapshot.get("pipeline") or {}
                for stage_name in ("build", "test"):
                    stage = pipeline.get("change", {}).get(stage_name)
                    if not stage:
                        continue
                    if stage["not_applicable"]:
                        result["stages"].append(
                            {
                                "stage": stage_name,
                                "status": "not_applicable",
                                "reason": stage["not_applicable"],
                            }
                        )
                        continue
                    for argv in stage["steps"]:
                        left = remaining()
                        if left <= 0:
                            raise RunnerTimeout("check step ran out of time")
                        began = time.monotonic()
                        code, stdout, stderr = CommandRunner(
                            prepared.workspace, self.cfg.max_output_bytes
                        ).supervise(
                            prefix + argv,
                            b"",
                            max(
                                1,
                                min(
                                    spec["timeout_seconds"],
                                    stage["timeout_seconds"],
                                    int(left),
                                ),
                            ),
                            prepared.workspace,
                            {
                                **env,
                                "FACTORY_STAGE": stage_name,
                                "FACTORY_KIND": "change",
                                "FACTORY_COMMIT": candidate["commit"],
                                "FACTORY_OUT": str(out_dir),
                            },
                            should_stop=stopped,
                            on_start=on_start,
                        )
                        result["stages"].append(
                            {
                                "stage": stage_name,
                                "status": "failed" if code else "passed",
                                "argv": argv,
                                "exit_code": code,
                                "runner": stage["runner"],
                                "seconds": round(time.monotonic() - began, 3),
                                "candidate": candidate["commit"],
                                "pipeline_digest": pipeline.get("digest"),
                                "stdout": stdout.decode(errors="replace"),
                                "stderr": stderr.decode(errors="replace"),
                            }
                        )
                        if code:
                            ok = False
                            result["failure_kind"] = "pipeline-stage-failure"
                            error = f"{stage_name} stage failed: {' '.join(argv)}"
                            break
                    if not ok:
                        break
                for cid in snapshot["source"]["checks"] if ok else []:
                    binding = snapshot["bindings"][cid]
                    for path, expected in binding["files"].items():
                        if path == binding["argv"][0] and cid in recipe.get(
                            "command_binaries", {}
                        ):
                            continue  # The approved OCI image digest pins the target executable instead.
                        if artifacts.digest(self.store.resolve_path(path)) != expected:
                            raise RuntimeError("trusted command binding changed")
                    argv = [
                        arg.replace("{workspace}", str(prepared.workspace)).replace(
                            "{trusted}",
                            str(self.store.resolve_path(snapshot["input_repo"])),
                        )
                        for arg in [
                            str(self.store.resolve_path(a))
                            if a in binding["files"] and a != binding["argv"][0]
                            else a
                            for a in binding["argv"]
                        ]
                    ]
                    if cid in recipe.get("command_binaries", {}):
                        argv[0] = recipe["command_binaries"][cid]
                    # All of a check's commands share the one attempt deadline.
                    left = remaining()
                    if left <= 0:
                        raise RunnerTimeout("check step ran out of time")
                    code, stdout, stderr = CommandRunner(
                        prepared.workspace, self.cfg.max_output_bytes
                    ).supervise(
                        prefix + argv,
                        b"",
                        max(
                            1,
                            min(
                                spec["timeout_seconds"],
                                binding["timeout_seconds"],
                                int(left),
                            ),
                        ),
                        prepared.workspace,
                        env,
                        should_stop=stopped,
                        on_start=on_start,
                    )
                    record = {
                        "command": cid,
                        "argv": binding["argv"],
                        "execution_argv": argv,
                        "exit_code": code,
                        "stdout": stdout.decode(errors="replace"),
                        "stderr": stderr.decode(errors="replace"),
                        "candidate": candidate["commit"],
                        "binding": binding,
                    }
                    result["checks"].append(record)
                    if code:
                        ok = False
                output = json.dumps(result["checks"], indent=2)
                if not ok and "failure_kind" not in result:
                    error = "canonical verification failed"
                # Check recipes may generate files, but may not change the candidate's tracked source.
                if git(prepared.workspace, "diff", "HEAD", "--"):
                    ok = False
                    error = "verification command modified candidate source"
                    result["failure_kind"] = "integrity"
                elif not ok and "failure_kind" not in result:
                    result["failure_kind"] = "canonical-check-failure"
            else:
                raise RuntimeError("unsupported bootstrap job")
            ok = ok and not cancelled and not timed_out
        except Exception as exc:
            error = str(exc)
            ok = False
            result["failure_kind"] = "execution"
            timed_out = timed_out or isinstance(
                exc, (RunnerTimeout, TimeoutError, subprocess.TimeoutExpired)
            )
            cancelled = cancelled or stopped()
        finally:
            if prepared:
                self.env.stop(prepared)
                result.update(ok=ok, error=error, output=output)
                try:
                    files = {
                        "output.txt": output,
                        "result.json": json.dumps(result, indent=2),
                        "events.jsonl": "\n".join(json.dumps(e) for e in events),
                    }
                    if "candidate" in result:
                        files["diff.patch"] = result["candidate"]["diff"]
                    session_paths = (
                        adapter_for(claimed["inputs"]["route"]["harness"]).session_paths
                        if spec.get("role")
                        else ()
                    )
                    for session_path in session_paths:
                        session_root = runtime / session_path
                        if session_root.exists():
                            for session in session_root.rglob("*.jsonl"):
                                files[f"session-{len(files)}.jsonl"] = (
                                    session.read_text()
                                )
                    # Preserve role output and sessions even if the durable archive fails.
                    for name, body in files.items():
                        (runtime / name).write_text(body)
                    artifacts.archive(
                        prepared.workspace,
                        archive,
                        {"attempt_id": attempt_id, "job_id": job["id"], "ok": ok},
                        files,
                        ephemeral_paths=snapshot["source"].get("ephemeral_paths", []),
                        # Only implementation output becomes a candidate source.
                        unchanged=baseline,
                    )
                    archived = True
                    self.controller.record_archive(attempt_id, archive)
                except Exception as exc:
                    error = f"archive failed; workspace retained: {exc}"
                    ok = False
                    self.controller.record_cleanup(attempt_id, "archive_failed")
                    self.store.audit(
                        self.worker_id,
                        "archive.failed",
                        {
                            "attempt_id": attempt_id,
                            "workspace": str(prepared.workspace),
                            "error": error,
                        },
                    )
                if archived:
                    try:
                        self.env.release(prepared)
                        self.controller.record_cleanup(attempt_id, "cleaned")
                        cleaned = True
                    except Exception as exc:
                        self.controller.record_cleanup(attempt_id, "cleanup_failed")
                        self.store.audit(
                            self.worker_id,
                            "cleanup.failed",
                            {"attempt_id": attempt_id, "error": str(exc)},
                        )
            if archived and cleaned:
                shutil.rmtree(runtime, ignore_errors=True)
        self.controller.finish_attempt(
            attempt_id,
            ok,
            output,
            cost,
            str(archive) if archived else None,
            error,
            timed_out,
            cancelled,
        )
        return True

    def run_loop(self, max_jobs=0):
        count = 0
        while not max_jobs or count < max_jobs:
            if not self.run_once():
                break
            count += 1
        return count


def _resume_session(archive, resume):
    """The interrupted attempt's harness session, complete lines only, or None."""
    if not archive or not resume:
        return None
    sessions = sorted(archive.glob("session-*.jsonl"))
    wanted = resume.get("session_id")
    for path in reversed(sessions):
        lines = []
        for line in path.read_text().splitlines():
            try:
                parsed = json.loads(line)
            except ValueError:
                break  # A crash can cut the last line; keep what is whole.
            if not lines:
                header = parsed
            lines.append(line)  # Byte for byte: the harness owns this format.
        if lines and (not wanted or header.get("id") == wanted):
            return "\n".join(lines) + "\n"
    return None


def _route_secret(route: dict, cfg=None) -> str:
    return credential_for(route, cfg)
