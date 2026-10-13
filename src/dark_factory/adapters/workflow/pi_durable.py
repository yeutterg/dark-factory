"""Pinned Pi Durable dispatch adapter; the controller remains the only authority."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from dark_factory.runner import CommandRunner

ENGINE_DIR = Path(__file__).parent / "pi_engine"


class PiDurableRuntime:
    name = "pi-durable"

    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store

    def validate(self):
        """Check the installed runtime without leasing work or opening its database."""
        for package in ("pi-durable", "pi-ai", "chord"):
            manifest = (
                ENGINE_DIR
                / "node_modules"
                / "@earendil-works"
                / package
                / "package.json"
            )
            try:
                version = json.loads(manifest.read_text())["version"]
            except (OSError, ValueError, KeyError, TypeError):
                version = None
            if version != "1.0.3":
                raise RuntimeError(
                    "install the pinned workflow dependencies with npm ci"
                )
        binary = self.cfg.raw.get("workflow", {}).get("node_binary", "node")
        runner = CommandRunner(ENGINE_DIR, 4096)
        code, output, _ = runner._run(
            [binary, "--version"], b"", 10, ENGINE_DIR, self._environment()
        )
        try:
            version = tuple(
                int(part) for part in output.decode().strip().lstrip("v").split(".")
            )
        except ValueError:
            version = ()
        if code or len(version) != 3 or version < (22, 19, 0):
            raise RuntimeError("Pi Durable requires Node >=22.19.0")
        code, _, _ = runner._run(
            [
                binary,
                "--input-type=module",
                "-e",
                "await import('@earendil-works/pi-durable')",
            ],
            b"",
            10,
            ENGINE_DIR,
            self._environment(),
        )
        if code:
            raise RuntimeError(
                "installed Pi Durable dependencies cannot load; reinstall with npm ci"
            )
        return {
            "engine": self.name,
            "node_version": ".".join(map(str, version)),
            "package_version": "1.0.3",
        }

    @staticmethod
    def _environment():
        # No credentials, proxy overrides or executable Node hooks in the scheduler.
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "en_US.UTF-8",
            "NODE_NO_WARNINGS": "1",
        }

    def pending_attempts(self):
        directory = self.cfg.state_dir / "workflow"
        recorded = {
            path.parent.name
            for path in directory.glob("att_*/dispatch.sqlite")
            if not self.store.get_op("workflow-settled:" + path.parent.name, "")
        }
        # The controller lease precedes the engine checkpoint. Discovery is not
        # authority: resume_claim must still validate owner, policy and lease.
        unstarted = {
            row["id"]
            for row in self.store.query(
                "SELECT id FROM attempts WHERE status='leased' AND workspace IS NULL AND pid IS NULL"
            )
        }
        return sorted(recorded | unstarted)

    def run(self, claim, execute):
        self.validate()
        attempt_id = claim["attempt_id"]
        expected = json.dumps(claim["inputs"], sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(expected.encode()).hexdigest()
        directory = self.cfg.state_dir / "workflow"
        directory.mkdir(mode=0o700, exist_ok=True)
        with (directory / "owner.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("durable runtime already has an owner") from None
            self._drive(directory, attempt_id, digest, expected, execute, claim)
            row = self.store.one(
                "SELECT status FROM attempts WHERE id=?", (attempt_id,)
            )
            if row["status"] not in {"leased", "running", "cancelling"}:
                self.store.set_op(
                    "workflow-settled:" + attempt_id, row["status"], "controller"
                )

    def _drive(self, directory, attempt_id, digest, expected, execute, claim):
        binary = self.cfg.raw.get("workflow", {}).get("node_binary", "node")
        proc = subprocess.Popen(
            [binary, "--experimental-strip-types", str(ENGINE_DIR / "main.ts")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ENGINE_DIR,
            start_new_session=True,
            # The scheduler runs no inference; do not inherit any credentials or Node hooks.
            env=self._environment(),
        )
        buffer = bytearray()
        total = 0
        result = None
        executed = False
        deadline = time.monotonic() + claim["spec"]["timeout_seconds"] + 30

        def send(value):
            proc.stdin.write((json.dumps(value) + "\n").encode())
            proc.stdin.flush()

        try:
            # One task database per attempt prevents implicit resumption of unrelated authority.
            send(
                {
                    "version": 1,
                    "database": str(directory / attempt_id / "dispatch.sqlite"),
                    "attemptId": attempt_id,
                    "digest": digest,
                }
            )
            with selectors.DefaultSelector() as selector:
                for stream in (proc.stdout, proc.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map() or proc.poll() is None:
                    if time.monotonic() > deadline:
                        raise RuntimeError("durable runtime exceeded dispatch deadline")
                    for key, _ in selector.select(0.1):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(data)
                        if total > self.cfg.max_output_bytes:
                            raise RuntimeError("durable runtime exceeded output limit")
                        if key.fileobj is proc.stderr:
                            continue  # Never echo untrusted dependency diagnostics.
                        buffer.extend(data)
                        while b"\n" in buffer:
                            line, _, rest = buffer.partition(b"\n")
                            buffer[:] = rest
                            msg = json.loads(line)
                            if msg.get("version") != 1:
                                raise RuntimeError("invalid workflow protocol version")
                            if msg.get("type") == "error":
                                raise RuntimeError("durable dispatch failed")
                            if (
                                msg.get("attemptId") != attempt_id
                                or msg.get("digest") != digest
                            ):
                                raise RuntimeError(
                                    "workflow attempted to change dispatch identity"
                                )
                            if msg.get("type") == "result":
                                if result is not None:
                                    raise RuntimeError("duplicate workflow result")
                                result = msg
                                continue
                            if msg.get("type") != "request" or msg.get(
                                "operation"
                            ) not in {"observe", "execute"}:
                                raise RuntimeError("unapproved workflow operation")
                            row = self.store.one(
                                "SELECT * FROM attempts WHERE id=?", (attempt_id,)
                            )
                            if (
                                not row
                                or json.dumps(
                                    json.loads(row["inputs_json"]),
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                                != expected
                            ):
                                raise RuntimeError("controller dispatch inputs changed")
                            if (
                                msg["operation"] == "execute"
                                and row["status"] == "leased"
                            ):
                                if executed or row["workspace"] or row["pid"]:
                                    raise RuntimeError(
                                        "interrupted CLI cannot be replayed"
                                    )
                                executed = True
                                execute()
                                row = self.store.one(
                                    "SELECT * FROM attempts WHERE id=?", (attempt_id,)
                                )
                            send(
                                {"version": 1, "id": msg["id"], "status": row["status"]}
                            )
                if proc.wait() or buffer or result is None:
                    raise RuntimeError("workflow ended without a durable result")
                row = self.store.one(
                    "SELECT status FROM attempts WHERE id=?", (attempt_id,)
                )
                if result.get("outcome") != (
                    "completed" if row["status"] == "succeeded" else "failed"
                ):
                    raise RuntimeError(
                        "workflow result differs from controller evidence"
                    )
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()
