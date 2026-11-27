"""Outbound project worker with durable handoff/result journal and no operator authority."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from dark_factory import artifacts
from dark_factory.execution.packages import MAX_BYTES, identity, pack
from dark_factory.runner import process_identity
from dark_factory.secrets import secret


def write_json_atomic(path, value):
    """Temp file, fsync, rename, fsync the directory: a reader never sees half a file."""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


STDERR_TAIL_BYTES = 2000
PREFLIGHT_SECONDS = 300
MAX_RETAINED_FENCED_ATTEMPTS = 20


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("worker credentials cannot follow redirects")


class WorkerClient:
    def __init__(self, url, project, worker, token, state, accounts=None, ca_file=None):
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid controller URL")
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError(
                "remote workers require HTTPS or an explicit loopback tunnel"
            )
        if len(token) < 32:
            raise ValueError("worker token must be private and at least 32 characters")
        for value in (project, worker):
            if not value or any(
                c
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for c in value
            ):
                raise ValueError("invalid project/worker identity")
        self.url, self.project, self.worker, self.token = (
            url.rstrip("/"),
            project,
            worker,
            token,
        )
        self.state = Path(state).resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (self.state / "factory.sqlite").exists():
            raise ValueError("worker state cannot be the supervisor state directory")
        owner = self.state / "owner.json"
        expected = {"project": project, "worker": worker, "controller": self.url}
        if owner.exists() and json.loads(owner.read_text()) != expected:
            raise ValueError("worker state belongs to a different deployment")
        if not owner.exists():
            with owner.open("x") as output:
                json.dump(expected, output)
        self.accounts = accounts or {}
        self._preflighted: tuple[str, float] | None = None
        self._checkpoint_upload: threading.Thread | None = None
        import ssl

        context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
        self.opener = build_opener(
            ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context)
        )

    def request(self, operation, payload):
        body = json.dumps(payload).encode()
        if len(body) > MAX_BYTES * 2:
            raise ValueError("request exceeds limit")
        request = Request(
            self.url + f"/v1/execution/{self.project}/{self.worker}/{operation}",
            body,
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
            },
        )
        with self.opener.open(request, timeout=120) as response:
            raw = response.read(MAX_BYTES * 2 + 1)
        if len(raw) > MAX_BYTES * 2:
            raise ValueError("response exceeds limit")
        result = json.loads(raw)
        if not result.get("ok"):
            raise RuntimeError("execution request rejected")
        return result["result"]

    def _write(self, path, value):
        write_json_atomic(path, value)

    def run_once(self):
        import fcntl

        with (self.state / "worker.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._run_once()

    def _run_once(self):
        journal = self.state / "dispatch.json"
        if not journal.exists():
            self._write(journal, {"request_id": uuid.uuid4().hex, "phase": "claim"})
        item = json.loads(journal.read_text())
        if item["phase"] == "fenced":
            self._retire_fenced(journal, item)
            return True
        if item["phase"] == "settled":
            self._cleanup(item)
            journal.unlink()
            return True
        if item["phase"] == "claim":
            info = self.request("preflight", {})
            # Hashing source and probing images and harnesses is not repeated on
            # every idle poll; it is repeated when the controller's answer
            # changes and at least every few minutes.
            key = identity(info)
            now = time.monotonic()
            if (
                not self._preflighted
                or self._preflighted[0] != key
                or now - self._preflighted[1] > PREFLIGHT_SECONDS
            ):
                self._preflight(info)
                self._preflighted = (key, now)
            assignment = self.request("claim", {"request_id": item["request_id"]})
            if assignment is None:
                journal.unlink()
                return False
            if assignment["project_id"] != self.project:
                raise ValueError("controller returned another project's assignment")
            import re

            if not isinstance(
                assignment["claim"]["attempt_id"], str
            ) or not re.fullmatch(
                r"att_[a-f0-9]{12,64}", assignment["claim"]["attempt_id"]
            ):
                raise ValueError("invalid attempt identity")
            directory = self.state / assignment["claim"]["attempt_id"]
            if directory.parent != self.state or directory.is_symlink():
                raise ValueError("invalid owned attempt path")
            directory.mkdir(exist_ok=True, mode=0o700)
            owner = directory / "owner.json"
            expected = {
                "project": self.project,
                "worker": self.worker,
                "attempt": assignment["claim"]["attempt_id"],
            }
            if owner.exists() and json.loads(owner.read_text()) != expected:
                raise ValueError("attempt directory belongs to another execution")
            if not owner.exists():
                self._write(owner, expected)
            self._write(directory / "assignment.json", assignment)
            item.update(
                phase="execute", directory=str(directory), execution_id=uuid.uuid4().hex
            )
            self._write(journal, item)
        directory = Path(item["directory"])
        assignment = json.loads((directory / "assignment.json").read_text())
        aid = assignment["claim"]["attempt_id"]
        if item["phase"] == "cleanup":
            return self._retry_cleanup(journal, item, assignment)
        route = assignment["claim"]["inputs"].get("route")
        if item["phase"] == "execute":
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "en_US.UTF-8",
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            }
            if route and route["harness"] != "fake":
                binding = self.accounts.get(route["billing_account"])
                expected = {
                    "provider": route["provider"],
                    "account_kind": route["account_kind"],
                    "auth_env": route["auth_env"],
                }
                if binding != expected or not secret(route["auth_env"]):
                    raise ValueError(
                        "worker account binding is absent or differs; no host-login fallback"
                    )
                if secret(route["auth_env"]) == self.token:
                    raise ValueError(
                        "inference credentials cannot equal the worker credential"
                    )
                env[route["auth_env"]] = secret(route["auth_env"])
            # Persist start intent before effect. A crashed worker must never blindly rerun it.
            if not self.request(
                "start", {"attempt_id": aid, "execution_id": item["execution_id"]}
            ):
                raise ValueError("attempt is fenced before execution")
            item["phase"] = "started"
            item["started_at"] = time.time()
            self._write(journal, item)
            cancel = directory / "cancel"
            lease = directory / "lease"
            lease.touch()
            log_file = (directory / "executor.log").open("ab")
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "dark_factory.execution.executor",
                    str(directory / "assignment.json"),
                    str(directory / "task"),
                    "--cancel-file",
                    str(cancel),
                    "--lease-file",
                    str(lease),
                    "--lease-seconds",
                    str(assignment["claim"]["inputs"].get("lease_seconds", 30)),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                start_new_session=True,
            )
            log_file.close()
            item["pid"] = proc.pid
            item["pid_identity"] = process_identity(proc.pid)
            self._write(journal, item)
            try:
                while proc.poll() is None:
                    alive = self._heartbeat(aid)
                    if alive is False:
                        cancel.touch()
                    elif alive:
                        lease.touch()
                        self._schedule_checkpoint_upload(directory, aid)
                    # None: the controller could not be reached. The executor
                    # stops by itself if the lease file ages past the lease.
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if proc.poll() is None:
                    cancel.touch()
        elif item["phase"] == "started" and not (directory / "result.json").exists():
            current = process_identity(item.get("pid"))
            if current and current == item.get("pid_identity"):
                alive = self._heartbeat(aid)
                if alive is not None:
                    (directory / ("lease" if alive else "cancel")).touch()
                return False  # Verified existing executor, not permission to replay.
            (directory / "cancel").touch()
            if time.time() - item.get("started_at", 0) < 12:
                return False  # The start-intent/PID-publication gap must expire conservatively.
        result = self._load_result(directory, assignment)
        self._drain_checkpoint_upload(directory, aid)
        ack = self.request("finish", result)
        if ack.get("terminal") and ack.get("disposition") == "fenced":
            item.update(phase="fenced", terminal_ack=ack)
            self._write(journal, item)
            self._retire_fenced(journal, item)
            return True
        if (
            not ack.get("accepted")
            or ack["attempt_id"] != aid
            or ack.get("result_digest") != identity(result)
        ):
            raise ValueError("controller did not verify settlement")
        self._write(directory / "ack.json", ack)
        cleanup_status = result["receipt"]["cleanup_status"]
        item.update(
            phase="cleanup"
            if result.get("archive") and cleanup_status == "cleanup_failed"
            else "settled",
            cleanup=bool(result.get("archive")) and cleanup_status == "cleaned",
            result_digest=ack["result_digest"],
        )
        self._write(journal, item)
        receipts = self.state / "receipts"
        receipts.mkdir(exist_ok=True, mode=0o700)
        self._write(receipts / (aid + ".json"), ack)
        if item["phase"] == "cleanup":
            return self._retry_cleanup(journal, item, assignment)
        self._cleanup(item)
        journal.unlink()
        return True

    def _upload_checkpoints(self, directory, aid):
        """Upload complete checkpoint archives; retry transport failures later."""
        base = directory / "task" / "checkpoints" / aid
        if not base.is_dir():
            return
        sent = directory / "checkpoint-sent"
        sent.mkdir(exist_ok=True, mode=0o700)
        for checkpoint in sorted(base.iterdir()):
            if checkpoint.name.startswith(".") or not checkpoint.is_dir():
                continue
            marker = sent / checkpoint.name
            if marker.exists():
                continue
            try:
                ack = self.request(
                    "checkpoint",
                    {
                        "attempt_id": aid,
                        "checkpoint_id": checkpoint.name,
                        "checkpoint": pack({"checkpoint": checkpoint}),
                    },
                )
            except Exception:
                return
            if (
                not ack.get("accepted")
                or ack.get("attempt_id") != aid
                or ack.get("checkpoint_id") != checkpoint.name
            ):
                raise ValueError("controller did not verify checkpoint")
            self._write(marker, ack)

    def _schedule_checkpoint_upload(self, directory, aid):
        """Transfer milestones off the heartbeat path."""
        if self._checkpoint_upload and self._checkpoint_upload.is_alive():
            return
        self._checkpoint_upload = threading.Thread(
            target=self._upload_checkpoints, args=(directory, aid), daemon=True
        )
        self._checkpoint_upload.start()

    def _drain_checkpoint_upload(self, directory, aid):
        self._schedule_checkpoint_upload(directory, aid)
        while self._checkpoint_upload and self._checkpoint_upload.is_alive():
            alive = self._heartbeat(aid)
            if alive is False:
                break
            self._checkpoint_upload.join(timeout=1)

    def _retire_fenced(self, journal, item):
        """Preserve a late result locally and let this worker claim fresh work."""
        directory = Path(item["directory"])
        aid = directory.name
        expected = {"project": self.project, "worker": self.worker, "attempt": aid}
        if (
            directory.parent != self.state
            or not aid.startswith("att_")
            or directory.is_symlink()
            or directory.exists()
            and (
                not (directory / "owner.json").is_file()
                or json.loads((directory / "owner.json").read_text()) != expected
            )
        ):
            raise ValueError("refusing to retain an unowned fenced attempt")
        retained = self.state / "retired" / aid
        retained.parent.mkdir(exist_ok=True, mode=0o700)
        if directory.exists() and not retained.exists():
            directory.replace(retained)
        receipts = self.state / "receipts"
        receipts.mkdir(exist_ok=True, mode=0o700)
        self._write(
            receipts / (aid + ".fenced.json"),
            {
                **item.get("terminal_ack", {}),
                "retained": str(retained),
            },
        )
        journal.unlink(missing_ok=True)
        self._prune_retired()

    def _prune_retired(self):
        """Bound inspected late results; receipts remain as durable dispositions."""
        import shutil

        retired = self.state / "retired"
        if not retired.is_dir():
            return
        owned = sorted(
            (
                path
                for path in retired.iterdir()
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime,
        )
        for path in owned[:-MAX_RETAINED_FENCED_ATTEMPTS]:
            receipt = self.state / "receipts" / (path.name + ".fenced.json")
            owner = path / "owner.json"
            expected = {
                "project": self.project,
                "worker": self.worker,
                "attempt": path.name,
            }
            if (
                receipt.is_file()
                and owner.is_file()
                and json.loads(owner.read_text()) == expected
            ):
                shutil.rmtree(path)

    def _heartbeat(self, aid):
        """True renewed, False refused by the controller, None unknown.

        Only an explicit refusal cancels. A transport error says nothing about
        the lease, which keeps its own clock.
        """
        try:
            return bool(self.request("heartbeat", {"attempt_id": aid}))
        except Exception:
            return None

    def _load_result(self, directory, assignment):
        """The executor's result, or a conservative interrupted receipt.

        A missing result and an unreadable one are handled the same way: the
        unreadable file is kept as ``result.corrupt`` for inspection and is not
        read again, so a damaged file cannot make settlement retry forever.
        """
        aid = assignment["claim"]["attempt_id"]
        path = directory / "result.json"
        reason = "worker interrupted; private task resources retained"
        if path.exists():
            try:
                result = json.loads(path.read_text())
                if (
                    not isinstance(result, dict)
                    or result.get("attempt_id") != aid
                    or result.get("claim_digest") != assignment["claim_digest"]
                    or not isinstance(result.get("receipt"), dict)
                ):
                    raise ValueError("unexpected result shape")
                return result
            except (OSError, ValueError) as exc:
                path.replace(directory / "result.corrupt")
                reason += f"; executor result was unreadable ({str(exc)[:120]})"
        log = directory / "executor.log"
        if log.exists():
            tail = log.read_bytes()[-STDERR_TAIL_BYTES:].decode(errors="replace")
            if tail.strip():
                reason += "; executor stderr: " + tail.strip()
        result = {
            "attempt_id": aid,
            "claim_digest": assignment["claim_digest"],
            "archive": None,
            "receipt": {
                "ok": False,
                "output": "",
                # The controller conservatively charges unknown usage.
                "cost": assignment["claim"]["spec"]["budget_cents"],
                "error": reason,
                "timed_out": False,
                "cancelled": False,
                "cleanup_status": "archive_failed",
            },
        }
        self._write(path, result)
        return result

    def serve(self, max_jobs=0, sleep=time.sleep, log=None):
        """Run until stopped. Transport and controller errors back off, then retry.

        The journal makes every step safe to repeat, so an error never needs to
        end the worker; it waits (1s, 2s, 4s ... capped at 60s) and tries again.
        """
        log = log or (
            lambda event: print(json.dumps(event), file=sys.stderr, flush=True)
        )
        count = failures = 0
        while not max_jobs or count < max_jobs:
            try:
                ran = self.run_once()
                failures = 0
            except Exception as exc:
                failures += 1
                if max_jobs and failures >= 5:
                    raise
                delay = min(60, 2 ** (failures - 1))
                log({"worker_error": str(exc)[:500], "retry_in_seconds": delay})
                sleep(delay)
                continue
            if ran:
                count += 1
            elif max_jobs:
                break
            else:
                sleep(1)
        return count

    def _retry_cleanup(self, journal, item, assignment):
        """Retry release only; never replay the agent or mutate its result."""
        from dark_factory.execution.cleanup import retry_replica_cleanup

        directory = Path(item["directory"])
        aid = assignment["claim"]["attempt_id"]
        try:
            retry_replica_cleanup(directory / "task", aid)
            ack = self.request(
                "cleanup",
                {
                    "attempt_id": aid,
                    "result_digest": item["result_digest"],
                },
            )
        except Exception as exc:
            item["cleanup_error"] = str(exc)
            self._write(journal, item)
            return False
        if (
            not ack.get("accepted")
            or ack.get("attempt_id") != aid
            or ack.get("result_digest") != item["result_digest"]
        ):
            raise ValueError("controller did not verify cleanup settlement")
        item.update(phase="settled", cleanup=True)
        item.pop("cleanup_error", None)
        self._write(journal, item)
        self._cleanup(item)
        journal.unlink()
        return True

    def _cleanup(self, item):
        import shutil

        directory = Path(item["directory"])
        if (
            directory.parent != self.state
            or directory.is_symlink()
            or not directory.name.startswith("att_")
        ):
            raise ValueError("refusing cleanup outside owned attempt")
        if item.get("cleanup") and directory.exists():
            expected = {
                "project": self.project,
                "worker": self.worker,
                "attempt": directory.name,
            }
            if (
                not (directory / "owner.json").is_file()
                or json.loads((directory / "owner.json").read_text()) != expected
            ):
                raise ValueError("refusing cleanup without matching ownership evidence")
            shutil.rmtree(directory)

    def _preflight(self, info):
        import platform

        root = Path(__file__).resolve().parents[1]
        actual = {
            str(p.relative_to(root)): artifacts.digest(p)
            for p in root.rglob("*.py")
            if "node_modules" not in p.parts
        }
        if (
            info["version"] != 1
            or info["project_id"] != self.project
            or actual != info["runner_code"]
        ):
            raise ValueError("worker/controller code or project binding differs")
        for recipe in info["environments"].values():
            if recipe["kind"] == "macos-sandbox" and platform.system() != "Darwin":
                raise ValueError("macOS sandbox requires a macOS execution worker")
            if recipe["kind"] == "oci":
                if os.getuid() == 0:
                    raise ValueError("OCI workers require a non-root host account")
                from dark_factory.adapters.env.registry import docker_command

                probe = subprocess.run(
                    [
                        *docker_command(recipe),
                        "image",
                        "inspect",
                        recipe["image"],
                    ],
                    capture_output=True,
                    timeout=10,
                )
                if probe.returncode:
                    raise ValueError(
                        "pinned OCI image is not installed; no implicit image pull"
                    )
                if recipe.get("network"):
                    from dark_factory.adapters.env.registry import project_network

                    project_network(recipe, self.project)
            for path, digest in recipe.get("driver_files", {}).items():
                if artifacts.digest(Path(path)) != digest:
                    raise ValueError("external sandbox driver changed")
        for route in info["routes"]:
            if route["harness"] == "fake":
                continue
            expected = {
                "provider": route["provider"],
                "account_kind": route["account_kind"],
                "auth_env": route["auth_env"],
            }
            if self.accounts.get(route["billing_account"]) != expected or not secret(
                route["auth_env"]
            ):
                raise ValueError(
                    "worker needs explicitly bound project inference credentials"
                )
            import tempfile
            from types import SimpleNamespace

            from dark_factory.adapters.env.native import process_environment
            from dark_factory.adapters.env.registry import environment_for
            from dark_factory.adapters.harness.registry import adapter_for

            recipe = info["environments"][route["environment"]]
            adapter = adapter_for(route["harness"])
            execution = dict(info["execution"])
            if route["harness"] in recipe.get("harness_binaries", {}):
                execution[adapter.binary_key] = recipe["harness_binaries"][
                    route["harness"]
                ]
            cfg = SimpleNamespace(
                execution=execution, source_path=self.state / "worker.toml"
            )
            # VM driver preflight must not implicitly provision infrastructure.
            # Its version probe happens inside the original approved task at execution.
            if recipe["kind"] != "command":
                with tempfile.TemporaryDirectory(
                    prefix="probe-", dir=self.state
                ) as temporary:
                    root = Path(temporary)
                    probe = environment_for(root / "workspaces", recipe, route)
                    prepared = probe.prepare("version")
                    runtime = root / "runtime"
                    try:
                        prefix = (
                            probe.prefix(
                                prepared,
                                runtime,
                                network=False,
                                writable=[],
                                protected=[],
                            )
                            if recipe["kind"] == "oci"
                            else []
                        )
                        adapter.open(cfg, route, prefix, process_environment(runtime))
                    finally:
                        probe.stop(prepared)
                        probe.release(prepared)
