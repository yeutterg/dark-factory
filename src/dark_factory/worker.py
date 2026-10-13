"""Execute a pinned attempt, archive unique work, then release owned resources."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from dark_factory import artifacts
from dark_factory.adapters.env.native import NativeEnvironment, process_environment
from dark_factory.adapters.git.local import capture, git
from dark_factory.adapters.harness.registry import (
    adapter_for,
    capabilities,
    credential_for,
)
from dark_factory.runner import CommandRunner
from dark_factory.workflow import runtime_for


class Worker:
    def __init__(self, cfg, store, controller, worker_id="local"):
        self.cfg, self.store, self.controller, self.worker_id = (
            cfg,
            store,
            controller,
            worker_id,
        )
        self.env = NativeEnvironment(cfg.state_dir / "workspaces")

    def _harness(self, route=None):
        return adapter_for(route["harness"] if route else "fake").open(self.cfg, route)

    def _context(self, claimed, workspace):
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
                for job in self.controller._jobs(root["id"])
                for attempt in self.store.query(
                    "SELECT error,result_json FROM attempts WHERE job_id=? AND status='failed'",
                    (job["id"],),
                )
            ]
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
            for job in self.controller._jobs(root["id"]):
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
        common["selected_skills"] = claimed["snapshot"]["policy"]["assets"]["skills"]
        return (
            claimed["prompt"]
            + "\n\nFactory factual brief (permissions are enforced by code):\n"
            + json.dumps(common, indent=2)
        )

    def run_once(self):
        self.controller.reconcile_continuations()
        runtime = runtime_for(self.cfg, self.store)
        caps = capabilities(self.cfg)
        for aid in runtime.pending_attempts():
            resumed = self.controller.resume_claim(aid, self.worker_id, caps)
            if resumed:
                active = (
                    self.store.one("SELECT status FROM attempts WHERE id=?", (aid,))[0]
                    == "leased"
                )
                runtime.run(resumed, lambda: self._execute(resumed))
                if active:
                    self.controller.reconcile_continuations()
                    return True
        claimed = self.controller.claim_job(self.worker_id, caps)
        if not claimed:
            return False
        try:
            runtime.run(claimed, lambda: self._execute(claimed))
        except Exception as exc:
            # A runtime failure cannot trigger another harness or duplicate a completed CLI.
            row = self.store.one(
                "SELECT status FROM attempts WHERE id=?", (claimed["attempt_id"],)
            )
            if row["status"] in {"leased", "running", "cancelling"}:
                self.controller.finish_attempt(
                    claimed["attempt_id"],
                    False,
                    "workflow runtime failed",
                    claimed["spec"]["budget_cents"],
                    None,
                    error=str(exc),
                )
            self.store.audit(
                self.worker_id,
                "workflow.error",
                {"attempt": claimed["attempt_id"], "error": str(exc)},
            )
            raise
        finally:
            self.controller.reconcile_continuations()
        return True

    def _execute(self, claimed):
        attempt_id = claimed["attempt_id"]
        job = claimed["job"]
        spec = claimed["spec"]
        snapshot = claimed["snapshot"]
        runtime = self.cfg.state_dir / "runtime" / attempt_id
        runtime.mkdir(parents=True, mode=0o700)
        archive = self.cfg.state_dir / "archives" / attempt_id
        prepared = None
        output = ""
        error = None
        ok = False
        timed_out = False
        cancelled = False
        cost = 0
        result = {"ok": False, "inputs": claimed["inputs"], "usage": {}, "checks": []}
        events = []
        archived = False

        last_heartbeat = 0.0

        def stopped():
            nonlocal last_heartbeat
            if time.monotonic() - last_heartbeat >= 5:
                last_heartbeat = time.monotonic()
                if not self.controller.heartbeat(self.worker_id, attempt_id):
                    return True
            row = self.store.one(
                "SELECT status FROM attempts WHERE id=?", (attempt_id,)
            )
            return not row or row["status"] not in {"leased", "running"}

        def on_start(pid):
            import subprocess

            identity = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            self.store.execute(
                "UPDATE attempts SET pid_identity=? WHERE id=?", (identity, attempt_id)
            )
            self.store.execute(
                "UPDATE attempts SET pid=?,status='running' WHERE id=? AND status='leased'",
                (pid, attempt_id),
            )

        try:
            source = {
                "path": str(self.store.resolve_path(snapshot["input_repo"])),
                "commit": snapshot["base"],
            }
            candidate = claimed["inputs"]["candidate"].get(claimed["work_item"]["repo"])
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
            prepared = self.env.prepare(attempt_id, source)
            self.store.execute(
                "UPDATE attempts SET workspace=? WHERE id=?",
                (str(prepared.workspace), attempt_id),
            )
            env = process_environment(runtime)
            live = self.cfg.execution.get("profile") == "macos-sandbox"
            role = spec.get("role")
            prefix = []
            readable_files = []
            if role:
                adapter = adapter_for(claimed["inputs"]["route"]["harness"])
                if adapter.binary_key:
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
                    private_paths=[
                        self.cfg.source_path,
                        Path(snapshot["source"]["path"]),
                    ],
                )
            if stopped():
                raise RuntimeError("attempt cancelled")
            if role:
                route = claimed["inputs"]["route"]
                if route["harness"] != "fake":
                    secret = _route_secret(route)
                    if not secret:
                        raise RuntimeError(
                            f"route credential unavailable: {route['auth_env']}"
                        )
                    env[route["auth_env"]] = secret
                harness = self._harness(route)
                prompt = self._context(claimed, prepared.workspace)
                (runtime / "prompt.txt").write_text(prompt)
                extra = {
                    "role": role,
                    "route": route,
                    "runtime": str(runtime),
                    "env": env,
                    "prefix": prefix,
                    "timeout_seconds": spec["timeout_seconds"],
                    "max_output_bytes": self.cfg.max_output_bytes,
                    "budget_cents": spec["budget_cents"],
                    "on_start": on_start,
                    "checks": snapshot["source"]["checks"],
                    "criteria": json.loads(claimed["work_item"]["plan_json"])
                    .get("proposal", {})
                    .get("acceptance", []),
                }
                started = time.monotonic()
                for event in harness.start(prompt, prepared.workspace, extra):
                    self.controller.heartbeat(self.worker_id, attempt_id)
                    if stopped():
                        cancelled = True
                        harness.cancel()
                    if time.monotonic() - started >= spec["timeout_seconds"]:
                        timed_out = True
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
                            {"type": event.type, "text": event.text, "data": event.data}
                        )
                answer = harness.wait()
                output = answer.output
                ok = answer.ok
                cost = answer.cost_cents
                error = answer.error
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
                    result["candidate"] = capture(
                        prepared.workspace,
                        snapshot["base"],
                        snapshot["source"]["allowed_paths"],
                        snapshot["source"].get("protected_paths", []),
                    )
                    if result["candidate"]["scope_violations"]:
                        ok = False
                        error = "protected or out-of-scope changes: " + str(
                            result["candidate"]["scope_violations"]
                        )
            elif job["kind"] == "check":
                ok = True
                for cid in snapshot["source"]["checks"]:
                    binding = snapshot["bindings"][cid]
                    for path, expected in binding["files"].items():
                        if artifacts.digest(Path(path)) != expected:
                            raise RuntimeError("trusted command binding changed")
                    argv = [
                        arg.replace("{workspace}", str(prepared.workspace)).replace(
                            "{trusted}",
                            str(self.store.resolve_path(snapshot["input_repo"])),
                        )
                        for arg in binding["argv"]
                    ]
                    code, stdout, stderr = CommandRunner(
                        prepared.workspace, self.cfg.max_output_bytes
                    )._run(
                        prefix + argv,
                        b"",
                        min(spec["timeout_seconds"], binding["timeout_seconds"]),
                        prepared.workspace,
                        env,
                        should_stop=stopped,
                        on_start=on_start,
                    )
                    record = {
                        "command": cid,
                        "argv": binding["argv"],
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
                if not ok:
                    error = "canonical verification failed"
                # Check recipes may generate files, but may not change the candidate's tracked source.
                if git(prepared.workspace, "diff", "HEAD", "--"):
                    ok = False
                    error = "verification command modified candidate source"
                    result["failure_kind"] = "integrity"
                elif not ok:
                    result["failure_kind"] = "canonical-check-failure"
            else:
                raise RuntimeError("unsupported bootstrap job")
            ok = ok and not cancelled and not timed_out
        except Exception as exc:
            error = str(exc)
            ok = False
            result["failure_kind"] = "execution"
            timed_out = timed_out or "timed out" in error
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
                    )
                    archived = True
                    self.store.execute(
                        "UPDATE attempts SET archive_path=?,cleanup_status='archived' WHERE id=?",
                        (str(archive), attempt_id),
                    )
                except Exception as exc:
                    error = f"archive failed; workspace retained: {exc}"
                    ok = False
                    self.store.execute(
                        "UPDATE attempts SET cleanup_status='archive_failed' WHERE id=?",
                        (attempt_id,),
                    )
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
                        self.store.execute(
                            "UPDATE attempts SET cleanup_status='cleaned' WHERE id=?",
                            (attempt_id,),
                        )
                    except Exception as exc:
                        self.store.execute(
                            "UPDATE attempts SET cleanup_status='cleanup_failed' WHERE id=?",
                            (attempt_id,),
                        )
                        self.store.audit(
                            self.worker_id,
                            "cleanup.failed",
                            {"attempt_id": attempt_id, "error": str(exc)},
                        )
            if archived:
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


def _route_secret(route: dict) -> str:
    return credential_for(route)
