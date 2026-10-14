"""Outbound project worker with durable handoff/result journal and no operator authority."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
from dark_factory.execution.packages import MAX_BYTES, identity


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
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(path)
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

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
        if item["phase"] == "settled":
            self._cleanup(item)
            journal.unlink()
            return True
        if item["phase"] == "claim":
            self._preflight(self.request("preflight", {}))
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
                if binding != expected or not os.environ.get(route["auth_env"]):
                    raise ValueError(
                        "worker account binding is absent or differs; no host-login fallback"
                    )
                if os.environ[route["auth_env"]] == self.token:
                    raise ValueError(
                        "inference credentials cannot equal the worker credential"
                    )
                env[route["auth_env"]] = os.environ[route["auth_env"]]
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
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            item["pid"] = proc.pid
            item["pid_identity"] = self._process_identity(proc.pid)
            self._write(journal, item)
            try:
                while proc.poll() is None:
                    try:
                        alive = self.request("heartbeat", {"attempt_id": aid})
                    except Exception:
                        alive = False
                    if not alive:
                        cancel.touch()
                    else:
                        lease.touch()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if proc.poll() is None:
                    cancel.touch()
        elif item["phase"] == "started" and not (directory / "result.json").exists():
            current = self._process_identity(item.get("pid"))
            if current and current == item.get("pid_identity"):
                try:
                    alive = self.request("heartbeat", {"attempt_id": aid})
                except Exception:
                    alive = False
                (directory / ("lease" if alive else "cancel")).touch()
                return False  # Verified existing executor, not permission to replay.
            (directory / "cancel").touch()
            if time.time() - item.get("started_at", 0) < 12:
                return False  # The start-intent/PID-publication gap must expire conservatively.
        if not (directory / "result.json").exists():
            # Retain all unique work. The controller conservatively charges unknown usage.
            result = {
                "attempt_id": aid,
                "claim_digest": assignment["claim_digest"],
                "archive": None,
                "receipt": {
                    "ok": False,
                    "output": "",
                    "cost": assignment["claim"]["spec"]["budget_cents"],
                    "error": "worker interrupted; private task resources retained",
                    "timed_out": False,
                    "cancelled": False,
                },
            }
            self._write(directory / "result.json", result)
        result = json.loads((directory / "result.json").read_text())
        ack = self.request("finish", result)
        if (
            not ack.get("accepted")
            or ack["attempt_id"] != aid
            or ack.get("result_digest") != identity(result)
        ):
            raise ValueError("controller did not verify settlement")
        self._write(directory / "ack.json", ack)
        item.update(phase="settled", cleanup=bool(result.get("archive")))
        self._write(journal, item)
        receipts = self.state / "receipts"
        receipts.mkdir(exist_ok=True, mode=0o700)
        self._write(receipts / (aid + ".json"), ack)
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

    @staticmethod
    def _process_identity(pid):
        if type(pid) is not int or pid < 1:
            return ""
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
        return result.stdout.strip() if result.returncode == 0 else ""

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
            if self.accounts.get(
                route["billing_account"]
            ) != expected or not os.environ.get(route["auth_env"]):
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
