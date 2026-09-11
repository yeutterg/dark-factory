"""Worker: prepare → execute → capture → archive → clean."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from dark_factory.adapters.env.native import NativeEnvironment
from dark_factory.adapters.harness.fake import FakeHarness
from dark_factory.adapters.harness.pi import PiHarness
from dark_factory.config import FactoryConfig
from dark_factory.controller import Controller
from dark_factory.store import Store, new_id


class Worker:
    def __init__(self, cfg: FactoryConfig, store: Store, controller: Controller, worker_id: str = "local") -> None:
        self.cfg = cfg
        self.store = store
        self.controller = controller
        self.worker_id = worker_id
        self.env = NativeEnvironment(cfg.state_dir / "workspaces")

    def _harness(self, route_id: Optional[str] = None) -> Any:
        route = None
        if route_id and route_id in self.cfg.routes:
            route = self.cfg.routes[route_id]
        name = route.harness if route else next(iter(self.cfg.routes.values())).harness
        if name == "pi" and PiHarness.available():
            return PiHarness()
        return FakeHarness()

    def run_once(self) -> bool:
        claimed = self.controller.claim_job(self.worker_id, ["native", "fake"])
        if not claimed:
            return False
        spec = claimed["spec"]
        attempt_id = claimed["attempt_id"]
        job = claimed["job"]
        timeout = int(spec.get("timeout_seconds") or self.cfg.default_timeout_seconds)
        prepared = self.env.prepare(job["id"], spec.get("repo"))
        archive_dir = self.cfg.state_dir / "archives" / attempt_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        ok = False
        output = ""
        error = None
        timed_out = False
        cancelled = False
        cost = 0
        try:
            job_row = self.store.one("SELECT status FROM jobs WHERE id=?", (job["id"],))
            if job_row and job_row["status"] == "cancelled":
                cancelled = True
            elif spec.get("role"):
                harness = self._harness()
                start = time.time()
                extra = {"role": spec["role"], "job_kind": job["kind"]}
                chunks: list[str] = []
                for event in harness.start(claimed["prompt"], prepared.workspace, extra):
                    if time.time() - start > timeout:
                        harness.cancel()
                        timed_out = True
                        break
                    again = self.store.one("SELECT status FROM jobs WHERE id=?", (job["id"],))
                    if again and again["status"] == "cancelled":
                        harness.cancel()
                        cancelled = True
                        break
                    if event.text:
                        chunks.append(event.text)
                result = harness.wait()
                output = result.output or "\n".join(chunks)
                ok = bool(result.ok) and not timed_out and not cancelled
                cost = int(result.cost_cents or 0)
                error = result.error
            elif job["kind"] == "check":
                artifact = prepared.workspace / "CHANGELOG.md"
                # Implementation wrote into the same workspace for this attempt chain only
                # when jobs share nothing; bootstrap checks the archive of implement via sibling lookup.
                ok, output = self._run_check(job["work_item_id"], prepared.workspace)
            else:
                output = "unsupported job"
                ok = False
            (archive_dir / "output.txt").write_text(output, encoding="utf-8")
            if prepared.workspace.exists():
                for item in prepared.workspace.iterdir():
                    target = archive_dir / item.name
                    if item.is_dir():
                        shutil.copytree(item, target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, target)
            (archive_dir / "manifest.json").write_text(
                json.dumps({"attempt_id": attempt_id, "job_id": job["id"], "ok": ok}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 — capture and fail the attempt
            error = str(exc)
            ok = False
            (archive_dir / "error.txt").write_text(str(exc), encoding="utf-8")
        finally:
            self.env.stop(prepared)
            self.env.release(prepared)
        self.controller.finish_attempt(
            attempt_id,
            ok=ok,
            output=output,
            cost_cents=cost,
            archive_path=str(archive_dir),
            error=error,
            timed_out=timed_out,
            cancelled=cancelled,
        )
        return True

    def run_loop(self, max_jobs: int = 0) -> int:
        n = 0
        while True:
            self.controller.heartbeat(self.worker_id)
            ran = self.run_once()
            if not ran:
                break
            n += 1
            if max_jobs and n >= max_jobs:
                break
        return n

    def _run_check(self, work_item_id: str, workspace: Path) -> tuple[bool, str]:
        archives = self.cfg.state_dir / "archives"
        found = False
        for att in self.store.query(
            """SELECT archive_path FROM attempts a JOIN jobs j ON a.job_id=j.id
               WHERE j.work_item_id=? AND j.kind='implement' ORDER BY a.started_at DESC""",
            (work_item_id,),
        ):
            if not att["archive_path"]:
                continue
            path = Path(att["archive_path"]) / "CHANGELOG.md"
            if path.is_file():
                found = True
                return True, f"found artifact {path}"
        if (workspace / "CHANGELOG.md").is_file():
            return True, "found workspace artifact"
        return False, "missing required workspace artifact"
