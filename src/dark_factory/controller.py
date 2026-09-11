"""Controller: graphs, approvals, dispatch, packets."""

from __future__ import annotations

import hashlib
import json
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dark_factory.config import FactoryConfig, load_prompt
from dark_factory.models import JOB_KINDS, JobSpec, PlanGraph
from dark_factory.packets import plan_packet, result_packet
from dark_factory.runner import CommandRunner
from dark_factory.store import Store, new_id, now_iso


class ControllerError(RuntimeError):
    pass


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def validate_graph(graph: PlanGraph, cfg: FactoryConfig) -> None:
    ids = [j.job_id for j in graph.jobs]
    if len(ids) != len(set(ids)):
        raise ControllerError("duplicate job ids")
    known = set(ids)
    for job in graph.jobs:
        if job.kind not in JOB_KINDS:
            raise ControllerError(f"unknown job kind {job.kind}")
        for dep in job.depends_on:
            if dep not in known:
                raise ControllerError(f"job {job.job_id} depends on unknown {dep}")
            if dep == job.job_id:
                raise ControllerError("self-dependency")
        if job.repo and job.repo not in cfg.repositories:
            raise ControllerError(f"job {job.job_id} repo {job.repo} is not in the profile")
        if job.command_id and job.command_id not in cfg.commands:
            raise ControllerError(f"job {job.job_id} references unknown command {job.command_id}")
    if _cycles(graph.jobs):
        raise ControllerError("job graph contains a cycle")
    for route in graph.permitted_routes:
        if route not in cfg.routes:
            raise ControllerError(f"unknown route {route}")
    if graph.spending_ceiling_cents < 0:
        raise ControllerError("spending ceiling cannot be negative")


def _cycles(jobs: list[JobSpec]) -> bool:
    deps = {j.job_id: list(j.depends_on) for j in jobs}
    visiting: set[str] = set()
    seen: set[str] = set()

    def walk(node: str) -> bool:
        if node in visiting:
            return True
        if node in seen:
            return False
        visiting.add(node)
        for dep in deps.get(node, []):
            if walk(dep):
                return True
        visiting.remove(node)
        seen.add(node)
        return False

    return any(walk(j) for j in deps)


def default_bootstrap_graph(root_id: str, repo: str, ceiling: int, route_ids: list[str]) -> PlanGraph:
    jobs = [
        JobSpec("plan", "plan", role="planner", repo=repo, budget_cents=50, timeout_seconds=600),
        JobSpec("critique", "critique", role="critic", repo=repo, depends_on=["plan"], budget_cents=50, timeout_seconds=600),
        JobSpec("implement", "implement", role="coder", repo=repo, depends_on=["critique"], budget_cents=200, timeout_seconds=1800),
        JobSpec("check", "check", command_id=None, repo=repo, depends_on=["implement"], check_names=["workspace-artifact"], timeout_seconds=300),
        JobSpec("review", "review", role="reviewer", repo=repo, depends_on=["check"], budget_cents=50, timeout_seconds=600),
    ]
    return PlanGraph(
        version=1,
        jobs=jobs,
        acceptance=["Workspace contains the implemented change", "Independent review recorded", "Costs captured"],
        spending_ceiling_cents=ceiling,
        permitted_routes=route_ids,
    )


def window_open(cfg: FactoryConfig, at: Optional[datetime] = None) -> bool:
    if not cfg.windows:
        return True
    at = at or datetime.now(timezone.utc)
    date_key = at.date().isoformat()
    if date_key in cfg.date_exceptions:
        return False
    for window in cfg.windows:
        tz = zoneinfo.ZoneInfo(window.timezone)
        local = at.astimezone(tz)
        day = local.strftime("%a")
        if day not in window.days:
            continue
        start_h, start_m = [int(x) for x in window.start.split(":")]
        end_h, end_m = [int(x) for x in window.end.split(":")]
        minutes = local.hour * 60 + local.minute
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start <= minutes < end:
            return True
    return False


class Controller:
    def __init__(self, cfg: FactoryConfig, store: Store, packaged_prompts: Path) -> None:
        self.cfg = cfg
        self.store = store
        self.packaged_prompts = packaged_prompts
        self.runner = CommandRunner(cfg.source_path.parent, cfg.max_output_bytes)
        store.save_config_revision(cfg.digest, Path(cfg.source_path).read_text(encoding="utf-8"), True)

    def intake_issues(self, issues: list[dict[str, Any]], actor: str = "operator") -> list[str]:
        cmd = self.cfg.commands["tracking"]
        result = self.runner.run_json(
            cmd.command,
            "list_selected_issues",
            {"issues": issues},
            new_id("req"),
            cmd.timeout_seconds,
        )
        ids = []
        for item in result.get("items", []):
            wid = new_id("wi")
            self.store.execute(
                """INSERT INTO work_items(id, external_id, url, title, body, parent_id, repo, status, is_root, iteration)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    wid,
                    item["external_id"],
                    item.get("url") or "",
                    item.get("title") or "",
                    item.get("body") or "",
                    item.get("parent_id"),
                    item.get("repo"),
                    "selected",
                    1 if item.get("is_root", True) else 0,
                    item.get("iteration"),
                ),
            )
            ids.append(wid)
        self.store.audit(actor, "intake", {"ids": ids})
        return ids

    def start_planning(self, work_item_id: str, actor: str = "operator") -> PlanGraph:
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
        if not row:
            raise ControllerError("work item not found")
        repo = row["repo"] or self.cfg.repositories[0]
        graph = default_bootstrap_graph(
            work_item_id,
            repo,
            int(self.cfg.raw.get("budgets", {}).get("root_cents", 1000)),
            list(self.cfg.routes),
        )
        validate_graph(graph, self.cfg)
        self._replace_jobs(work_item_id, graph)
        packet = plan_packet(row["title"], row["body"], graph, self.cfg)
        self.store.execute(
            "UPDATE work_items SET status=?, plan_version=?, spending_ceiling_cents=?, plan_json=?, presentation_stage=? WHERE id=?",
            ("planning", graph.version, graph.spending_ceiling_cents, json.dumps(packet), "Plan", work_item_id),
        )
        self.store.audit(actor, "plan.start", {"work_item_id": work_item_id})
        return graph

    def record_job_output(self, job_id: str, output: str, cost_cents: int, ok: bool) -> None:
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            raise ControllerError("job not found")
        status = "succeeded" if ok else "failed"
        self.store.execute("UPDATE jobs SET status=?, worker_id=NULL, lease_until=NULL WHERE id=?", (status, job_id))
        if job["kind"] == "plan" and ok:
            self.store.execute(
                "UPDATE work_items SET packet_json=? WHERE id=?",
                (json.dumps({"kind": "plan", "body": output}), job["work_item_id"]),
            )
        if job["kind"] == "critique" and ok:
            self.store.execute("UPDATE work_items SET critique_json=? WHERE id=?", (output, job["work_item_id"]))
        if job["kind"] == "review" and ok:
            root = self.store.one("SELECT * FROM work_items WHERE id=?", (job["work_item_id"],))
            packet = result_packet(dict(root), output, ok=True)
            self.store.execute(
                "UPDATE work_items SET status='awaiting_review', packet_json=?, presentation_stage=? WHERE id=?",
                (json.dumps(packet), "Review", job["work_item_id"]),
            )
        if not ok:
            packet = result_packet(dict(self.store.one("SELECT * FROM work_items WHERE id=?", (job["work_item_id"],))), output, ok=False)
            self.store.execute(
                "UPDATE work_items SET status='failed', packet_json=?, presentation_stage=? WHERE id=?",
                (json.dumps(packet), "Review", job["work_item_id"]),
            )
        self._add_cost(job["work_item_id"], cost_cents)

    def approve(self, work_item_id: str, actor: str) -> str:
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
        if not row:
            raise ControllerError("work item not found")
        if row["status"] not in {"awaiting_approval", "planning"}:
            raise ControllerError(f"cannot approve from status {row['status']}")
        jobs = [dict(j) for j in self.store.query("SELECT * FROM jobs WHERE work_item_id=?", (work_item_id,))]
        payload = {
            "work_item_id": work_item_id,
            "plan": row["plan_json"],
            "critique": row["critique_json"],
            "jobs": jobs,
            "ceiling": row["spending_ceiling_cents"],
            "routes": sorted(self.cfg.routes),
            "commands": {k: v.command for k, v in self.cfg.commands.items()},
        }
        digest = _digest(payload)
        self.store.execute(
            "UPDATE work_items SET status=?, approved_digest=?, approved_by=?, approved_at=? WHERE id=?",
            ("approved", digest, actor, now_iso(), work_item_id),
        )
        # Unlock implementation jobs (those depending on critique).
        self.store.execute(
            "UPDATE jobs SET status='ready' WHERE work_item_id=? AND kind IN ('implement','check','review') AND status='blocked'",
            (work_item_id,),
        )
        self.store.audit(actor, "approve", {"work_item_id": work_item_id, "digest": digest})
        return digest

    def reject(self, work_item_id: str, actor: str, reason: str) -> None:
        self.store.execute("UPDATE work_items SET status='rejected' WHERE id=?", (work_item_id,))
        self.store.audit(actor, "reject", {"work_item_id": work_item_id, "reason": reason})

    def pause(self, actor: str) -> None:
        self.store.set_op("pause", "on", actor)

    def resume(self, actor: str) -> None:
        self.store.set_op("pause", "off", actor)

    def drain(self, actor: str) -> None:
        self.store.set_op("drain", "on", actor)

    def cancel_job(self, job_id: str, actor: str) -> None:
        self.store.execute("UPDATE jobs SET status='cancelled', worker_id=NULL, lease_until=NULL WHERE id=?", (job_id,))
        self.store.execute(
            "UPDATE attempts SET status='cancelled', ended_at=? WHERE job_id=? AND status IN ('pending','leased','running')",
            (now_iso(), job_id),
        )
        self.store.audit(actor, "cancel", {"job_id": job_id})

    def dispatch_allowed(self) -> tuple[bool, str]:
        if self.store.get_op("pause") == "on":
            return False, "paused"
        if self.store.get_op("drain") == "on":
            return False, "draining"
        if not window_open(self.cfg):
            return False, "window_closed"
        return True, "ok"

    def claim_job(self, worker_id: str, capabilities: list[str]) -> Optional[dict[str, Any]]:
        allowed, reason = self.dispatch_allowed()
        if not allowed:
            return None
        self.store.execute(
            "INSERT INTO workers(id, capabilities, last_heartbeat, busy) VALUES (?,?,?,1) ON CONFLICT(id) DO UPDATE SET last_heartbeat=excluded.last_heartbeat, busy=1, capabilities=excluded.capabilities",
            (worker_id, json.dumps(capabilities), now_iso()),
        )
        jobs = self.store.query("SELECT * FROM jobs WHERE status IN ('ready','queued') ORDER BY id")
        for job in jobs:
            if job["kind"] in {"implement", "check", "review"}:
                root = self.store.one("SELECT approved_digest FROM work_items WHERE id=?", (job["work_item_id"],))
                if not root or not root["approved_digest"]:
                    continue
            if not self._deps_satisfied(job["work_item_id"], job["depends_on"]):
                continue
            spec = json.loads(job["spec_json"])
            need = spec.get("budget_cents") or 0
            root = self.store.one("SELECT * FROM work_items WHERE id=?", (job["work_item_id"],))
            if root["reserved_cents"] + root["spent_cents"] + need > root["spending_ceiling_cents"]:
                continue
            lease = now_iso()
            self.store.execute(
                "UPDATE jobs SET status='leased', worker_id=?, lease_until=? WHERE id=?",
                (worker_id, lease, job["id"]),
            )
            self.store.execute(
                "UPDATE work_items SET reserved_cents=reserved_cents+?, status='running', presentation_stage=? WHERE id=?",
                (need, "Build" if job["kind"] == "implement" else "Check", job["work_item_id"]),
            )
            attempt_id = new_id("att")
            prompt_text, prompt_hash = "", ""
            role = spec.get("role")
            if role:
                prompt_text, prompt_hash = load_prompt(self.cfg, role, self.packaged_prompts)
            self.store.execute(
                "INSERT INTO attempts(id, job_id, worker_id, status, started_at, config_hash, prompt_hash) VALUES (?,?,?,?,?,?,?)",
                (attempt_id, job["id"], worker_id, "leased", now_iso(), self.cfg.digest, prompt_hash),
            )
            return {
                "job": dict(job),
                "attempt_id": attempt_id,
                "spec": spec,
                "prompt": prompt_text,
                "work_item": dict(root),
            }
        return None

    def heartbeat(self, worker_id: str) -> None:
        self.store.execute("UPDATE workers SET last_heartbeat=? WHERE id=?", (now_iso(), worker_id))

    def finish_attempt(
        self,
        attempt_id: str,
        ok: bool,
        output: str,
        cost_cents: int,
        archive_path: Optional[str],
        error: Optional[str] = None,
        timed_out: bool = False,
        cancelled: bool = False,
    ) -> None:
        attempt = self.store.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        if not attempt:
            raise ControllerError("attempt not found")
        status = "succeeded" if ok else "failed"
        if timed_out:
            status = "timed_out"
        if cancelled:
            status = "cancelled"
        self.store.execute(
            "UPDATE attempts SET status=?, ended_at=?, cost_cents=?, error=?, archive_path=? WHERE id=?",
            (status, now_iso(), cost_cents, error, archive_path, attempt_id),
        )
        job_status = "succeeded" if ok else status
        self.store.execute("UPDATE jobs SET status=?, worker_id=NULL, lease_until=NULL WHERE id=?", (job_status, attempt["job_id"]))
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (attempt["job_id"],))
        spec = json.loads(job["spec_json"])
        reserved = spec.get("budget_cents") or 0
        self.store.execute(
            "UPDATE work_items SET reserved_cents=MAX(reserved_cents-?,0), spent_cents=spent_cents+? WHERE id=?",
            (reserved, cost_cents, job["work_item_id"]),
        )
        self.store.execute(
            "INSERT INTO evidence(id, attempt_id, kind, path, digest, payload_json, inputs_json) VALUES (?,?,?,?,?,?,?)",
            (
                new_id("ev"),
                attempt_id,
                "output",
                archive_path,
                None,
                json.dumps({"ok": ok, "output": output, "error": error}),
                json.dumps({"config_hash": attempt["config_hash"], "prompt_hash": attempt["prompt_hash"]}),
            ),
        )
        self.record_job_output(job["id"], output, 0, ok and not timed_out and not cancelled)
        if job["kind"] == "plan" and ok:
            self.store.execute("UPDATE jobs SET status='ready' WHERE work_item_id=? AND kind='critique'", (job["work_item_id"],))
        if job["kind"] == "critique" and ok:
            self.store.execute("UPDATE work_items SET status='awaiting_approval' WHERE id=?", (job["work_item_id"],))

    def accept(self, work_item_id: str, actor: str) -> None:
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
        if not row or row["status"] != "awaiting_review":
            raise ControllerError("work item is not awaiting review")
        self.store.execute(
            "UPDATE work_items SET status='accepted', presentation_stage='Done' WHERE id=?",
            (work_item_id,),
        )
        self.store.audit(actor, "accept", {"work_item_id": work_item_id})

    def status(self) -> dict[str, Any]:
        items = [dict(r) for r in self.store.query("SELECT id, title, status, presentation_stage, spent_cents FROM work_items")]
        jobs = [dict(r) for r in self.store.query("SELECT id, work_item_id, kind, status FROM jobs")]
        pause = self.store.get_op("pause")
        drain = self.store.get_op("drain")
        allowed, reason = self.dispatch_allowed()
        return {
            "project": self.cfg.project_name,
            "dispatch": {"allowed": allowed, "reason": reason, "pause": pause, "drain": drain},
            "work_items": items,
            "jobs": jobs,
        }

    def _replace_jobs(self, work_item_id: str, graph: PlanGraph) -> None:
        self.store.execute("DELETE FROM jobs WHERE work_item_id=?", (work_item_id,))
        for spec in graph.jobs:
            initial = "queued" if spec.kind in {"plan", "critique"} else "blocked"
            if spec.kind == "plan":
                initial = "ready"
            self.store.execute(
                "INSERT INTO jobs(id, work_item_id, kind, status, spec_json, depends_on, reserved_cents) VALUES (?,?,?,?,?,?,?)",
                (
                    spec.job_id + "_" + work_item_id[-8:],
                    work_item_id,
                    spec.kind,
                    initial,
                    json.dumps(
                        {
                            "logical_id": spec.job_id,
                            "role": spec.role,
                            "command_id": spec.command_id,
                            "repo": spec.repo,
                            "budget_cents": spec.budget_cents,
                            "timeout_seconds": spec.timeout_seconds,
                            "check_names": spec.check_names,
                            "manual_gate": spec.manual_gate,
                        }
                    ),
                    json.dumps(spec.depends_on),
                    spec.budget_cents,
                ),
            )

    def _deps_satisfied(self, work_item_id: str, depends_on_json: str) -> bool:
        deps = json.loads(depends_on_json)
        if not deps:
            return True
        succeeded = []
        for row in self.store.query("SELECT spec_json, status FROM jobs WHERE work_item_id=?", (work_item_id,)):
            spec = json.loads(row["spec_json"])
            if row["status"] == "succeeded":
                succeeded.append(spec.get("logical_id"))
        return all(d in succeeded for d in deps)

    def _add_cost(self, root_id: str, cents: int) -> None:
        if cents <= 0:
            return
        self.store.execute(
            "INSERT INTO costs(id, root_id, attempt_id, source, amount_cents, currency, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_id("cost"), root_id, None, "harness", cents, "USD", "reported", now_iso()),
        )
