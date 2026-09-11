"""Controller: graphs, approvals, dispatch, packets."""

from __future__ import annotations

import hashlib
import json
import zoneinfo
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from dark_factory.config import FactoryConfig, load_prompt
from dark_factory.models import JOB_KINDS, JobSpec, PlanGraph
from dark_factory.runner import CommandRunner
from dark_factory.store import Store, new_id, now_iso


class ControllerError(RuntimeError):
    pass


def atomic(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.store.transaction():
            return method(self, *args, **kwargs)

    return wrapped


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def validate_graph(graph: PlanGraph, cfg: FactoryConfig) -> None:
    ids = [j.job_id for j in graph.jobs]
    if len(ids) != len(set(ids)):
        raise ControllerError("duplicate job ids")
    known = set(ids)
    for job in graph.jobs:
        if type(job.budget_cents) is not int or job.budget_cents < 0:
            raise ControllerError("job budget must be a non-negative integer")
        if type(job.timeout_seconds) is not int or job.timeout_seconds <= 0:
            raise ControllerError("job timeout must be a positive integer")
        if job.kind == "implement" and not job.check_names and not job.manual_gate:
            raise ControllerError(
                "implementation needs meaningful checks or a manual acceptance gate"
            )
        if job.kind not in JOB_KINDS:
            raise ControllerError(f"unknown job kind {job.kind}")
        for dep in job.depends_on:
            if dep not in known:
                raise ControllerError(f"job {job.job_id} depends on unknown {dep}")
            if dep == job.job_id:
                raise ControllerError("self-dependency")
        if job.repo and job.repo not in cfg.repositories:
            raise ControllerError(
                f"job {job.job_id} repo {job.repo} is not in the profile"
            )
        if job.command_id and job.command_id not in cfg.commands:
            raise ControllerError(
                f"job {job.job_id} references unknown command {job.command_id}"
            )
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


def default_bootstrap_graph(
    root_id: str, repo: str, ceiling: int, route_ids: list[str]
) -> PlanGraph:
    jobs = [
        JobSpec(
            "plan",
            "plan",
            role="planner",
            repo=repo,
            budget_cents=50,
            timeout_seconds=600,
        ),
        JobSpec(
            "critique",
            "critique",
            role="critic",
            repo=repo,
            depends_on=["plan"],
            budget_cents=50,
            timeout_seconds=600,
        ),
        JobSpec(
            "implement",
            "implement",
            role="coder",
            repo=repo,
            depends_on=["critique"],
            budget_cents=200,
            timeout_seconds=1800,
        ),
        JobSpec(
            "check",
            "check",
            command_id=None,
            repo=repo,
            depends_on=["implement"],
            check_names=["workspace-artifact"],
            timeout_seconds=300,
        ),
        JobSpec(
            "review",
            "review",
            role="reviewer",
            repo=repo,
            depends_on=["check"],
            budget_cents=50,
            timeout_seconds=600,
        ),
    ]
    return PlanGraph(
        version=1,
        jobs=jobs,
        acceptance=[
            "Workspace contains the implemented change",
            "Independent review recorded",
            "Costs captured",
        ],
        spending_ceiling_cents=ceiling,
        permitted_routes=route_ids,
    )


def window_open(cfg: FactoryConfig, at: Optional[datetime] = None) -> bool:
    at = at or datetime.now(timezone.utc)
    if not cfg.windows:
        return at.date().isoformat() not in cfg.date_exceptions
    for window in cfg.windows:
        local = at.astimezone(zoneinfo.ZoneInfo(window.timezone))
        if local.date().isoformat() in cfg.date_exceptions:
            continue
        start_h, start_m = map(int, window.start.split(":"))
        end_h, end_m = map(int, window.end.split(":"))
        minutes = local.hour * 60 + local.minute
        start, end = start_h * 60 + start_m, end_h * 60 + end_m
        anchor = local
        if start > end and minutes < end:
            anchor = local - timedelta(days=1)
        if anchor.date().isoformat() in cfg.date_exceptions:
            continue
        inside = (
            start <= minutes < end if start < end else minutes >= start or minutes < end
        )
        if anchor.strftime("%a") in window.days and inside:
            return True
    return False


class Controller:
    """One-process bootstrap controller; only this object mutates execution state."""

    def __init__(
        self, cfg: FactoryConfig, store: Store, packaged_prompts: Path
    ) -> None:
        self.cfg, self.store, self.packaged_prompts = cfg, store, packaged_prompts
        self.runner = CommandRunner(cfg.source_path.parent, cfg.max_output_bytes)
        self._save_config()

    def _save_config(self):
        import copy

        raw = copy.deepcopy(self.cfg.raw)
        # A literal dev token is intentionally not a recoverable credential snapshot.
        for key in ("worker_token", "operator_token"):
            if key in raw.get("runtime", {}):
                raw["runtime"][key] = "<redacted>"
        self.store.save_config_revision(self.cfg.digest, json.dumps(raw), True)

    def reload(self, actor="operator") -> bool:
        from dark_factory.config import ConfigError, load_config

        try:
            candidate = load_config(self.cfg.source_path)
            if candidate.state_dir != self.cfg.state_dir:
                raise ConfigError("reload cannot move controller state")
        except (ConfigError, OSError) as exc:
            self.store.audit(actor, "config.reload_failed", {"error": str(exc)})
            return False
        self.cfg = candidate
        self.runner = CommandRunner(
            candidate.source_path.parent, candidate.max_output_bytes
        )
        self._save_config()
        self.store.audit(actor, "config.reload", {"digest": candidate.digest})
        return True

    def intake_issues(
        self, issues: list[dict[str, Any]], actor="operator"
    ) -> list[str]:
        cmd = self.cfg.commands["tracking"]
        result = self.runner.run_json(
            cmd.command,
            "list_selected_issues",
            {"issues": issues},
            new_id("req"),
            cmd.timeout_seconds,
        )
        if not result["ok"] or not isinstance(result.get("items"), list):
            raise ControllerError(result.get("error", "tracking returned no items"))
        ids = []
        with self.store.transaction():
            for item in result["items"]:
                if (
                    "parent_id" not in item
                    or item.get("parent_id") is not None
                    or item.get("is_root") is not True
                ):
                    raise ControllerError(
                        "select the root outcome, not a child issue; "
                        f"parent: {item.get('parent_id') or 'resolve in the tracker'}"
                    )
                child_count = item.get("required_child_count")
                if type(child_count) is not int or child_count < 0:
                    raise ControllerError(
                        "tracking result needs a non-negative required_child_count"
                    )
                if child_count:
                    raise ControllerError(
                        "bootstrap cannot execute roots with required child issues; "
                        "preserve the complete root for child-graph execution, do not select a child instead"
                    )
                if item.get("repo") not in self.cfg.repositories:
                    raise ControllerError("intake repository is not configured")
                if (
                    not isinstance(item.get("external_id"), str)
                    or not item["external_id"]
                ):
                    raise ControllerError(
                        "tracking result needs a provider-qualified external_id"
                    )
                existing = self.store.one(
                    "SELECT id FROM work_items WHERE external_id=?",
                    (item["external_id"],),
                )
                if existing:
                    ids.append(existing["id"])
                    continue
                wid = new_id("wi")
                self.store.execute(
                    """INSERT INTO work_items
                    (id,external_id,url,title,body,parent_id,repo,status,is_root,iteration,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        wid,
                        item["external_id"],
                        item.get("url", ""),
                        item.get("title", ""),
                        item.get("body", ""),
                        item.get("parent_id"),
                        item["repo"],
                        "selected",
                        int(item.get("is_root", True)),
                        item.get("iteration"),
                        now_iso(),
                    ),
                )
                ids.append(wid)
            self.store.audit(
                actor, "intake", {"ids": ids, "evidence": result.get("evidence", {})}
            )
        return ids

    def _assets(self) -> dict:
        assets = {}
        for role in ("planner", "critic", "coder", "reviewer"):
            body, sha = load_prompt(self.cfg, role, self.packaged_prompts)
            assets[role] = {"body": body, "sha256": sha}
        skills = []
        for name in self.cfg.skills:
            path = Path(name)
            if not path.is_dir() or not (path / "SKILL.md").is_file():
                raise ControllerError(f"selected skill not found: {path}")
            files = {}
            for file in sorted(path.rglob("*")):
                if file.is_symlink():
                    raise ControllerError("skill resources cannot be symlinks")
                if file.is_file():
                    from dark_factory.artifacts import digest

                    files[str(file.relative_to(path))] = digest(file)
            skills.append(
                {
                    "name": path.name,
                    "body": (path / "SKILL.md").read_text(),
                    "files": files,
                }
            )
        assets["skills"] = skills
        return assets

    def _bindings(self) -> dict:
        import shutil

        from dark_factory.artifacts import digest

        bindings = {}
        for cid, command in self.cfg.commands.items():
            argv = list(command.command)
            executable = shutil.which(argv[0])
            if not executable:
                raise ControllerError(f"command executable not found: {argv[0]}")
            argv[0] = str(Path(executable).resolve())
            files = {argv[0]: digest(Path(argv[0]))}
            for i, arg in enumerate(argv[1:], 1):
                path = (
                    Path(arg)
                    if Path(arg).is_absolute()
                    else self.cfg.source_path.parent / arg
                )
                if "{" not in arg and path.is_file():
                    argv[i] = str(path.resolve())
                    files[argv[i]] = digest(path)
            if "-m" in argv:
                import importlib.util

                index = argv.index("-m") + 1
                module = (
                    importlib.util.find_spec(argv[index]) if index < len(argv) else None
                )
                if module and module.origin and Path(module.origin).is_file():
                    files[str(Path(module.origin).resolve())] = digest(
                        Path(module.origin)
                    )
                else:
                    raise ControllerError("cannot pin module command")
            bindings[cid] = {
                "argv": argv,
                "files": files,
                "timeout_seconds": command.timeout_seconds,
            }
        return bindings

    def _policy(self) -> dict:
        # Availability can narrow at any time; the approved execution authority stays fixed.
        from dark_factory.artifacts import digest

        package = {
            str(p.relative_to(Path(__file__).parent)): digest(p)
            for p in sorted(Path(__file__).parent.rglob("*.py"))
        }
        return {
            "runner_code": package,
            "routes": {k: vars(v) for k, v in self.cfg.routes.items()},
            "commands": self._bindings(),
            "assets": self._assets(),
            "sources": self.cfg.sources,
            "execution": self.cfg.execution,
            "budgets": self.cfg.raw.get("budgets", {}),
            "geography": self.cfg.geography_policy,
        }

    def _require_root_scope(self, row):
        if row["parent_id"] is not None or row["is_root"] != 1:
            raise ControllerError("select the root outcome, not a child issue")
        if self.store.one(
            "SELECT id FROM work_items WHERE parent_id IN (?,?) LIMIT 1",
            (row["id"], row["external_id"]),
        ):
            raise ControllerError(
                "bootstrap cannot execute roots with required child issues; preserve the complete root"
            )

    @atomic
    def start_planning(self, work_item_id: str, actor="operator") -> PlanGraph:
        import sys

        from dark_factory.adapters.git.local import clone, fixture, pin

        row = self.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
        if not row:
            raise ControllerError("work item not found")
        self._require_root_scope(row)
        if self.store.one(
            "SELECT a.id FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.work_item_id=? AND a.status IN ('leased','running')",
            (work_item_id,),
        ):
            raise ControllerError("stop active attempts before replanning")
        if row["status"] not in {
            "selected",
            "failed",
            "rejected",
            "cancelled",
            "awaiting_approval",
        }:
            raise ControllerError("work item is not eligible for a new proposal")
        if row["reserved_cents"]:
            raise ControllerError(
                "reconcile outstanding reservations before replanning"
            )
        source = self.cfg.sources.get(row["repo"])
        simulation = any(
            route.evidence_kind == "simulation" for route in self.cfg.routes.values()
        )
        if source is None:
            if not simulation:
                raise ControllerError("source repository configuration required")
            source = fixture(self.cfg.state_dir)
        version = row["plan_version"] + 1
        base = pin(Path(source["path"]), source["revision"])
        inputs = self.cfg.state_dir / "inputs" / work_item_id / str(version)
        inputs.mkdir(parents=True, exist_ok=False)
        clone(Path(source["path"]), inputs / "repo", base)
        bindings = self._bindings()
        if simulation and source["checks"] == ["fixture-check"]:
            bindings["fixture-check"] = {
                "argv": [sys.executable, "verify.py"],
                "files": {},
                "timeout_seconds": 30,
            }
        ceiling = self.cfg.raw.get("budgets", {}).get("root_cents", 1000)
        graph = default_bootstrap_graph(
            work_item_id, row["repo"], ceiling, list(self.cfg.routes)
        )
        for job in graph.jobs:
            if job.kind == "implement":
                job.check_names = list(source["checks"])
            if job.kind == "check":
                job.check_names = list(source["checks"])
        validate_graph(graph, self.cfg)
        snapshot = {
            "source": dict(source),
            "base": base,
            "input_repo": str(inputs / "repo"),
            "bindings": bindings,
            "policy": self._policy(),
            "simulation": simulation,
            "manual_acceptance": source.get("manual_acceptance", []),
            "deadline": (
                datetime.now(timezone.utc)
                + timedelta(seconds=self.cfg.execution.get("max_seconds", 7200))
            ).isoformat(),
        }
        self.store.execute(
            "UPDATE jobs SET status='superseded' WHERE work_item_id=? AND status IN ('blocked','ready','queued')",
            (work_item_id,),
        )
        for index, spec in enumerate(graph.jobs):
            self.store.execute(
                """INSERT INTO jobs(id,work_item_id,kind,status,spec_json,depends_on,reserved_cents)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    f"{index}_{spec.job_id}_{work_item_id}_v{version}",
                    work_item_id,
                    spec.kind,
                    "ready" if spec.kind in {"plan", "critique"} else "blocked",
                    json.dumps(
                        {**vars(spec), "logical_id": spec.job_id, "version": version}
                    ),
                    json.dumps(spec.depends_on),
                    spec.budget_cents,
                ),
            )
        packet = {
            "kind": "plan",
            "simulation": simulation,
            "selected_work": {"title": row["title"], "body": row["body"]},
            "repos": [row["repo"]],
            "base_commit": base,
            "acceptance": graph.acceptance,
            "spending_ceiling_cents": ceiling,
            "permitted_routes": list(self.cfg.routes),
            "commands": bindings,
            "allowed_paths": source["allowed_paths"],
            "protected_paths": source.get("protected_paths", []),
            "decision": "Review the plan and critique, then approve this exact proposal.",
        }
        self.store.execute(
            """UPDATE work_items SET status='planning',plan_version=?,spending_ceiling_cents=?,
            snapshot_json=?,plan_json=?,packet_json=?,critique_json=NULL,approved_digest=NULL,approved_by=NULL,
            approved_at=NULL,candidate_commits='{}',presentation_stage='Plan' WHERE id=?""",
            (
                version,
                ceiling,
                json.dumps(snapshot),
                json.dumps(packet),
                json.dumps(packet),
                work_item_id,
            ),
        )
        self.store.execute(
            "INSERT INTO proposals(root_id,version,payload_json) VALUES (?,?,?)",
            (work_item_id, version, json.dumps(packet)),
        )
        self.store.audit(
            actor,
            "plan.start",
            {"work_item_id": work_item_id, "version": version, "base": base},
        )
        return graph

    def _jobs(self, wid):
        version = self.store.one(
            "SELECT plan_version FROM work_items WHERE id=?", (wid,)
        )["plan_version"]
        return [
            dict(j)
            for j in self.store.query(
                "SELECT * FROM jobs WHERE work_item_id=? ORDER BY id", (wid,)
            )
            if json.loads(j["spec_json"]).get("version", 1) == version
        ]

    def _approval_payload(self, wid):
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        return {
            "plan": json.loads(row["plan_json"]),
            "critique": row["critique_json"],
            "snapshot": json.loads(row["snapshot_json"]),
            "jobs": [
                {k: j[k] for k in ("id", "kind", "spec_json", "depends_on")}
                for j in self._jobs(wid)
            ],
            "ceiling": row["spending_ceiling_cents"],
            "policy": self._policy(),
        }

    @atomic
    def approve(self, work_item_id, actor):
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
        if not row or row["status"] != "awaiting_approval":
            raise ControllerError("cannot approve before planning and critique succeed")
        self._require_root_scope(row)
        if not actor.strip():
            raise ControllerError("approver is required")
        if any(
            j["status"] != "succeeded"
            for j in self._jobs(work_item_id)
            if j["kind"] in {"plan", "critique"}
        ):
            raise ControllerError("planning and critique must succeed before approval")
        if not json.loads(row["critique_json"])["passed"]:
            raise ControllerError("blocking critique requires a new proposal")
        snapshot = json.loads(row["snapshot_json"])
        if snapshot["policy"] != self._policy():
            raise ControllerError("planning inputs changed; replan before approval")
        if snapshot["source"].get("require_live_evidence") and snapshot["simulation"]:
            raise ControllerError(
                "live evidence required; simulation cannot qualify this outcome"
            )
        payload = self._approval_payload(work_item_id)
        digest = _digest(payload)
        self.store.execute(
            "UPDATE work_items SET status='approved',approved_digest=?,approved_by=?,approved_at=? WHERE id=?",
            (digest, actor, now_iso(), work_item_id),
        )
        self.store.execute(
            "UPDATE proposals SET payload_json=?,approved_digest=?,actor=?,approved_at=? WHERE root_id=? AND version=?",
            (
                json.dumps(payload),
                digest,
                actor,
                now_iso(),
                work_item_id,
                row["plan_version"],
            ),
        )
        for job in self._jobs(work_item_id):
            if job["status"] == "blocked":
                self.store.execute(
                    "UPDATE jobs SET status='ready' WHERE id=?", (job["id"],)
                )
        self.store.audit(
            actor, "approve", {"work_item_id": work_item_id, "digest": digest}
        )
        return digest

    @atomic
    def reject(self, work_item_id, actor, reason):
        row = self.store.one(
            "SELECT status FROM work_items WHERE id=?", (work_item_id,)
        )
        if not row or row["status"] != "awaiting_approval":
            raise ControllerError("only a pending proposal can be rejected")
        self.store.execute(
            "UPDATE work_items SET status='rejected' WHERE id=?", (work_item_id,)
        )
        self.store.audit(
            actor, "reject", {"work_item_id": work_item_id, "reason": reason}
        )

    def pause(self, actor):
        self.store.set_op("pause", "on", actor)

    def resume(self, actor):
        self.store.set_op("pause", "off", actor)

    def drain(self, actor):
        self.store.set_op("drain", "on", actor)

    def undrain(self, actor):
        self.store.set_op("drain", "off", actor)

    def dispatch_allowed(self):
        if self.store.get_op("pause") == "on":
            return False, "paused"
        if self.store.get_op("drain") == "on":
            return False, "draining"
        if not window_open(self.cfg):
            return False, "window_closed"
        if self.store.one(
            "SELECT id FROM pending_actions WHERE status IN ('pending','ambiguous')"
        ):
            return False, "external_action_needs_reconciliation"
        return True, "ok"

    @atomic
    def claim_job(self, worker_id, capabilities):
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or not isinstance(capabilities, list)
        ):
            raise ControllerError("invalid worker identity/capabilities")
        if (
            any(r.harness == "pi" for r in self.cfg.routes.values())
            and self.cfg.execution.get("profile") != "macos-sandbox"
        ):
            raise ControllerError("live execution is not qualified")
        if not self.dispatch_allowed()[0]:
            return None
        if self.store.one(
            "SELECT id FROM attempts WHERE status IN ('leased','running','cancelling')"
        ):
            return None
        for root in self.store.query(
            "SELECT * FROM work_items WHERE status IN ('planning','approved','running') ORDER BY created_at,id"
        ):
            try:
                self._require_root_scope(root)
            except ControllerError as exc:
                self._fail_root(root["id"], str(exc))
                continue
            snapshot = json.loads(root["snapshot_json"])
            if datetime.fromisoformat(snapshot["deadline"]) <= datetime.now(
                timezone.utc
            ):
                self._fail_root(root["id"], "root deadline expired")
                continue
            for job in self._jobs(root["id"]):
                if job["status"] != "ready":
                    continue
                if not all(
                    any(
                        j["status"] == "succeeded"
                        and json.loads(j["spec_json"])["logical_id"] == dep
                        for j in self._jobs(root["id"])
                    )
                    for dep in json.loads(job["depends_on"])
                ):
                    continue
                if job["kind"] not in {"plan", "critique"}:
                    if not root["approved_digest"]:
                        continue
                    if root["approved_digest"] != _digest(
                        self._approval_payload(root["id"])
                    ):
                        raise ControllerError(
                            "approved inputs changed; dispatch requires a new decision"
                        )
                elif snapshot["policy"] != self._policy():
                    raise ControllerError("planning inputs changed; replan")
                spec = json.loads(job["spec_json"])
                role = spec.get("role")
                route = None
                if role:
                    eligible = [
                        r
                        for r in self.cfg.routes.values()
                        if r.harness != "claude-code"
                        or role in {"planner", "critic", "reviewer"}
                    ]
                    if not eligible:
                        continue
                    candidates = [r for r in eligible if r.role == role]
                    route = (candidates or eligible)[0]
                    if (
                        route.harness in {"pi", "claude-code"}
                        and self.cfg.execution.get("profile") != "macos-sandbox"
                    ):
                        raise ControllerError("live execution is not qualified")
                    required = {"native", route.harness}
                else:
                    required = {"native"}
                if not required.issubset(set(capabilities)):
                    continue
                need = spec["budget_cents"]
                if route and route.max_cents and need > route.max_cents:
                    continue
                if (
                    root["spent_cents"] + root["reserved_cents"] + need
                    > root["spending_ceiling_cents"]
                ):
                    continue
                account_limit = self.cfg.raw.get("budgets", {}).get(
                    "account_cents", 10000
                )
                total = self.store.one(
                    "SELECT COALESCE(SUM(spent_cents+reserved_cents),0) n FROM work_items"
                )["n"]
                if total + need > account_limit:
                    continue
                if job["kind"] in {"plan", "critique"}:
                    spent = self.store.one(
                        "SELECT COALESCE(SUM(a.cost_cents),0) n FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.work_item_id=? AND j.kind IN ('plan','critique')",
                        (root["id"],),
                    )["n"]
                    if spent + need > self.cfg.raw.get("budgets", {}).get(
                        "planning_cents", 100
                    ):
                        continue
                attempt = new_id("att")
                deadline = min(
                    datetime.fromisoformat(snapshot["deadline"]),
                    datetime.now(timezone.utc)
                    + timedelta(seconds=spec["timeout_seconds"]),
                )
                inputs = {
                    "root_version": root["plan_version"],
                    "base": snapshot["base"],
                    "candidate": json.loads(root["candidate_commits"]),
                    "policy_hash": _digest(snapshot["policy"]),
                    "route": vars(route) if route else None,
                    "commands": snapshot["bindings"],
                    "role": role,
                }
                self.store.execute(
                    "UPDATE jobs SET status='leased',worker_id=?,lease_until=? WHERE id=?",
                    (
                        worker_id,
                        min(
                            deadline, datetime.now(timezone.utc) + timedelta(seconds=30)
                        ).isoformat(),
                        job["id"],
                    ),
                )
                self.store.execute(
                    "UPDATE work_items SET reserved_cents=reserved_cents+?,status='running',presentation_stage=? WHERE id=?",
                    (
                        need,
                        "Plan"
                        if job["kind"] in {"plan", "critique"}
                        else "Build"
                        if job["kind"] == "implement"
                        else "Check",
                        root["id"],
                    ),
                )
                self.store.execute(
                    """INSERT INTO attempts(id,job_id,worker_id,status,started_at,heartbeat_at,deadline,config_hash,prompt_hash,route_id,inputs_json)
                    VALUES (?,?,?,'leased',?,?,?,?,?,?,?)""",
                    (
                        attempt,
                        job["id"],
                        worker_id,
                        now_iso(),
                        now_iso(),
                        deadline.isoformat(),
                        self.cfg.digest,
                        snapshot["policy"]["assets"].get(role, {}).get("sha256", ""),
                        route.id if route else None,
                        json.dumps(inputs),
                    ),
                )
                self.store.execute(
                    "INSERT INTO workers(id,capabilities,last_heartbeat,busy) VALUES (?,?,?,1) ON CONFLICT(id) DO UPDATE SET capabilities=excluded.capabilities,last_heartbeat=excluded.last_heartbeat,busy=1",
                    (worker_id, json.dumps(capabilities), now_iso()),
                )
                self.store.audit(
                    worker_id,
                    "attempt.claim",
                    {
                        "attempt_id": attempt,
                        "job_id": job["id"],
                        "reservation_cents": need,
                    },
                )
                return {
                    "job": job,
                    "spec": spec,
                    "attempt_id": attempt,
                    "work_item": dict(root),
                    "snapshot": snapshot,
                    "inputs": inputs,
                    "prompt": snapshot["policy"]["assets"]
                    .get(role, {})
                    .get("body", ""),
                }
        return None

    @atomic
    def heartbeat(self, worker_id, attempt_id=None):
        self.store.execute(
            "UPDATE workers SET last_heartbeat=? WHERE id=?", (now_iso(), worker_id)
        )
        if not attempt_id:
            return True
        attempt = self.store.one(
            "SELECT a.*,j.lease_until FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE a.id=? AND a.worker_id=?",
            (attempt_id, worker_id),
        )
        now = datetime.now(timezone.utc)
        if (
            not attempt
            or attempt["status"] not in {"leased", "running"}
            or not attempt["lease_until"]
        ):
            return False
        deadline = datetime.fromisoformat(attempt["deadline"])
        if datetime.fromisoformat(attempt["lease_until"]) <= now or deadline <= now:
            return False
        self.store.execute(
            "UPDATE attempts SET heartbeat_at=? WHERE id=?", (now_iso(), attempt_id)
        )
        self.store.execute(
            "UPDATE jobs SET lease_until=? WHERE id=?",
            (min(deadline, now + timedelta(seconds=30)).isoformat(), attempt["job_id"]),
        )
        return True

    def _fail_root(self, wid, error):
        self.store.execute(
            "UPDATE work_items SET status='failed',presentation_stage='Review' WHERE id=?",
            (wid,),
        )
        self._packet(wid, error)

    def _packet(self, wid, error=None):
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        records = []
        for job in self._jobs(wid):
            for attempt in self.store.query(
                "SELECT * FROM attempts WHERE job_id=?", (job["id"],)
            ):
                records.append(
                    {
                        "kind": job["kind"],
                        "attempt": attempt["id"],
                        "status": attempt["status"],
                        "archive": attempt["archive_path"],
                        "workspace": attempt["workspace"]
                        if attempt["cleanup_status"] != "cleaned"
                        else None,
                        "cost_cents": attempt["cost_cents"],
                        "error": attempt["error"],
                        "cleanup": attempt["cleanup_status"],
                        "recovery_runtime": str(
                            self.cfg.state_dir / "runtime" / attempt["id"]
                        )
                        if attempt["cleanup_status"] == "archive_failed"
                        else None,
                    }
                )
        snapshot = json.loads(row["snapshot_json"])
        plan = json.loads(row["plan_json"])
        candidate_map = json.loads(row["candidate_commits"])
        candidate_sha = candidate_map.get(row["repo"], {}).get("commit")
        completed_gates = {
            gate["name"]
            for gate in self.store.query(
                "SELECT name FROM manual_gates WHERE root_id=? AND candidate=? AND passed=1",
                (wid, candidate_sha),
            )
        }
        for candidate in candidate_map.values():
            candidate["archive"] = str(self.store.resolve_path(candidate["archive"]))
            candidate["diff"] = str(Path(candidate["archive"]) / "diff.patch")
        packet = {
            "kind": "result",
            "simulation": snapshot["simulation"],
            "ready_to_merge": False,
            "status": row["status"],
            "What changed": row["title"],
            "What to review": "Exact candidate diff, canonical checks and fresh independent review in the archives.",
            "What to test": [
                {
                    "n": i + 1,
                    "setup": f"Use a separate checkout of candidate {candidate_sha}; execution profile: {self.cfg.execution.get('profile', 'native')}. See the verified implementation archive.",
                    "action": step,
                    "expected": plan.get("proposal", {}).get("acceptance", []),
                }
                for i, step in enumerate(
                    plan.get("proposal", {}).get("test_instructions", [])
                )
            ],
            "approved_plan": {
                "root": wid,
                "version": row["plan_version"],
                "digest": row["approved_digest"],
                "command": [
                    "dark-factory",
                    "--config",
                    str(self.cfg.source_path),
                    "proposal",
                    wid,
                    "--version",
                    str(row["plan_version"]),
                ],
            },
            "candidate": candidate_map,
            "acceptance_coverage": self.coverage(wid),
            "attempts": records,
            "spent_cents": row["spent_cents"],
            "reserved_cents": row["reserved_cents"],
            "manual_remaining": [
                name
                for name in snapshot["manual_acceptance"]
                if name not in completed_gates
            ],
            "missing_evidence": [
                missing
                for criterion in self.coverage(wid)
                for missing in criterion["missing_evidence"]
            ]
            + [
                "manual:" + name
                for name in snapshot["manual_acceptance"]
                if name not in completed_gates
            ],
            "error": error,
            "recommended_next_step": "Accepted candidate; retain the review and recovery artifacts."
            if row["status"] == "accepted"
            else "Review exact candidate and record manual gates; merge remains a human action."
            if row["status"] == "awaiting_review"
            else "Inspect saved work and failure evidence, reconcile cleanup, then replan.",
        }
        self.store.execute(
            "UPDATE work_items SET packet_json=? WHERE id=?", (json.dumps(packet), wid)
        )
        return packet

    @atomic
    def finish_attempt(
        self,
        attempt_id,
        ok,
        output,
        cost_cents,
        archive_path,
        error=None,
        timed_out=False,
        cancelled=False,
    ):
        from dark_factory.artifacts import digest, verify

        attempt = self.store.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        if not attempt:
            raise ControllerError("attempt not found")
        if attempt["status"] not in {"leased", "running", "cancelling"}:
            raise ControllerError(
                "attempt is no longer active; late or duplicate result rejected"
            )
        if type(cost_cents) is not int or cost_cents < 0:
            raise ControllerError("cost must be a non-negative integer")
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (attempt["job_id"],))
        root = self.store.one(
            "SELECT * FROM work_items WHERE id=?", (job["work_item_id"],)
        )
        snapshot = json.loads(root["snapshot_json"])
        result = {}
        archive_digest = None
        if archive_path:
            expected = self.cfg.state_dir / "archives" / attempt_id
            try:
                if Path(archive_path).resolve() != expected.resolve():
                    raise ValueError("unexpected archive path")
                manifest = verify(expected)
                if (
                    manifest["attempt_id"] != attempt_id
                    or manifest["job_id"] != job["id"]
                ):
                    raise ValueError("archive identity mismatch")
                result = json.loads((expected / "result.json").read_text())
                if result["inputs"] != json.loads(attempt["inputs_json"]):
                    raise ValueError("evidence inputs mismatch")
                if result.get("output") != output:
                    raise ValueError("output differs from archived evidence")
                archive_digest = digest(expected / "manifest.json")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                ok = False
                error = f"invalid durable archive: {exc}"
        elif ok:
            ok = False
            error = "missing durable archive"
        timed_out = (
            timed_out
            or datetime.fromisoformat(attempt["deadline"]) <= datetime.now(timezone.utc)
            or (
                job["lease_until"]
                and datetime.fromisoformat(job["lease_until"])
                <= datetime.now(timezone.utc)
            )
        )
        cancelled = (
            cancelled
            or attempt["status"] == "cancelling"
            or root["status"] == "cancelled"
        )
        ok = bool(ok and not timed_out and not cancelled and result.get("ok", False))
        if job["kind"] not in {"plan", "critique"} and root[
            "approved_digest"
        ] != _digest(self._approval_payload(root["id"])):
            ok = False
            error = "approved inputs changed during attempt"
        parsed = None
        if ok and job["kind"] in {"plan", "critique", "review"}:
            try:
                parsed = self._parse_role(
                    job["kind"],
                    output,
                    snapshot,
                    json.loads(root["plan_json"]).get("proposal", {}),
                )
                if job["kind"] != "plan" and not parsed["passed"]:
                    ok = False
                    error = "blocking independent findings: " + json.dumps(
                        parsed["findings"]
                    )
            except (ValueError, KeyError, TypeError) as exc:
                ok = False
                error = f"invalid role result: {exc}"
        if cost_cents > json.loads(job["spec_json"])["budget_cents"]:
            ok = False
            error = "attempt spending exceeded reserved limit"
        if ok and job["kind"] == "implement":
            candidate = result.get("candidate", {})
            if not candidate.get("commit") or candidate.get("scope_violations"):
                ok = False
                error = "candidate missing or changed protected/out-of-scope paths"
            else:
                self.store.execute(
                    "UPDATE work_items SET candidate_commits=? WHERE id=?",
                    (
                        json.dumps(
                            {
                                root["repo"]: {
                                    "commit": candidate["commit"],
                                    "base": candidate["base"],
                                    "archive": archive_path,
                                    "diff_sha256": candidate["diff_sha256"],
                                }
                            }
                        ),
                        root["id"],
                    ),
                )
        status = (
            "cancelled"
            if cancelled
            else "timed_out"
            if timed_out
            else "succeeded"
            if ok
            else "failed"
        )
        self.store.execute(
            "UPDATE attempts SET status=?,ended_at=?,cost_cents=?,error=?,archive_path=?,result_json=? WHERE id=?",
            (
                status,
                now_iso(),
                cost_cents,
                error,
                archive_path,
                json.dumps(result),
                attempt_id,
            ),
        )
        self.store.execute(
            "UPDATE jobs SET status=?,worker_id=NULL,lease_until=NULL WHERE id=?",
            (status, job["id"]),
        )
        reservation = json.loads(job["spec_json"])["budget_cents"]
        self.store.execute(
            "UPDATE work_items SET reserved_cents=MAX(reserved_cents-?,0),spent_cents=spent_cents+? WHERE id=?",
            (reservation, cost_cents, root["id"]),
        )
        route = json.loads(attempt["inputs_json"]).get("route")
        self.store.execute(
            "INSERT INTO costs(id,root_id,attempt_id,source,amount_cents,currency,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id("cost"),
                root["id"],
                attempt_id,
                route["billing_account"] if route else "local-check",
                cost_cents,
                "USD",
                "estimated"
                if snapshot["simulation"]
                else result.get("cost_status", "reported" if not route else "unknown"),
                now_iso(),
            ),
        )
        self.store.execute(
            "INSERT INTO evidence(id,attempt_id,kind,path,digest,payload_json,inputs_json) VALUES (?,?,?,?,?,?,?)",
            (
                new_id("ev"),
                attempt_id,
                job["kind"],
                archive_path,
                archive_digest,
                json.dumps({"ok": ok, "output": output, "error": error}),
                attempt["inputs_json"],
            ),
        )
        self.store.execute(
            "UPDATE workers SET busy=0 WHERE id=?", (attempt["worker_id"],)
        )
        if ok and job["kind"] == "plan":
            packet = json.loads(root["plan_json"])
            packet["proposal"] = parsed
            self.store.execute(
                "UPDATE work_items SET plan_json=?,packet_json=? WHERE id=?",
                (json.dumps(packet), json.dumps(packet), root["id"]),
            )
        elif ok and job["kind"] == "critique":
            packet = json.loads(root["plan_json"])
            packet["critique"] = parsed
            self.store.execute(
                "UPDATE work_items SET critique_json=?,packet_json=?,status='awaiting_approval',presentation_stage='Plan' WHERE id=?",
                (json.dumps(parsed), json.dumps(packet), root["id"]),
            )
        elif ok and job["kind"] == "review":
            self.store.execute(
                "UPDATE work_items SET status='awaiting_review',presentation_stage='Review' WHERE id=?",
                (root["id"],),
            )
            self._packet(root["id"])
        elif not ok:
            self._fail_root(root["id"], error or output or status)
            if cancelled:
                self.store.execute(
                    "UPDATE work_items SET status='cancelled' WHERE id=?", (root["id"],)
                )
        self.store.audit(
            attempt["worker_id"],
            "attempt.finish",
            {"attempt_id": attempt_id, "status": status, "archive": archive_path},
        )

    def _parse_role(self, kind, output, snapshot, proposal=None):
        text = output.strip()
        if text.startswith("```") and text.endswith("```"):
            text = "\n".join(text.splitlines()[1:-1])
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("expected JSON object")
        if kind == "plan":
            for key in ("why", "what"):
                if not isinstance(parsed.get(key), str) or not parsed[key]:
                    raise ValueError(f"{key} required")
            for key in ("acceptance", "tasks", "test_instructions"):
                if not isinstance(parsed.get(key), list) or not parsed[key]:
                    raise ValueError(f"{key} required")
            seen = set()
            for criterion in parsed["acceptance"]:
                if not isinstance(criterion, dict):
                    raise ValueError(
                        "acceptance requires identified criteria with evidence bindings"
                    )
                cid = criterion.get("id")
                if not isinstance(cid, str) or not cid or cid in seen:
                    raise ValueError("acceptance IDs must be unique non-empty strings")
                seen.add(cid)
                if (
                    not isinstance(criterion.get("description"), str)
                    or not criterion["description"].strip()
                ):
                    raise ValueError("acceptance description required")
                for key, allowed in (
                    ("checks", snapshot["source"]["checks"]),
                    ("manual_gates", snapshot["manual_acceptance"]),
                ):
                    values = criterion.get(key)
                    if not isinstance(values, list) or not all(
                        isinstance(v, str) and v in allowed for v in values
                    ):
                        raise ValueError(
                            "acceptance must bind configured checks and manual gates"
                        )
                if not criterion["checks"] and not criterion["manual_gates"]:
                    raise ValueError("acceptance criterion has no verification gate")
            for task in parsed["tasks"]:
                if not isinstance(task, dict):
                    raise ValueError("task must be an object")
                if (
                    not task.get("description")
                    or not task.get("checks")
                    or not set(task["checks"]).issubset(snapshot["source"]["checks"])
                ):
                    raise ValueError("each task needs configured meaningful checks")
            for item in parsed.get("needs_input", []):
                if not all(
                    key in item
                    for key in ("question", "recommendation", "consequence", "blocking")
                ):
                    raise ValueError(
                        "questions need recommendation, consequence and blocking status"
                    )
                if item["blocking"]:
                    raise ValueError(
                        "blocking question requires operator decision and replan"
                    )
        else:
            if (
                type(parsed.get("passed")) is not bool
                or not isinstance(parsed.get("findings"), list)
                or not parsed.get("summary")
            ):
                raise ValueError("review needs passed, findings and summary")
        if kind == "review":
            expected = {c["id"]: c for c in (proposal or {}).get("acceptance", [])}
            assessments = parsed.get("criteria")
            if not expected or not isinstance(assessments, list):
                raise ValueError(
                    "independent review must cover every approved criterion"
                )
            seen = set()
            for assessment in assessments:
                if not isinstance(assessment, dict):
                    raise ValueError("review criterion must be an object")
                cid = assessment.get("id")
                if cid not in expected or cid in seen:
                    raise ValueError(
                        "review criterion IDs must match approved requirements exactly"
                    )
                seen.add(cid)
                criterion = expected[cid]
                status = assessment.get("status")
                refs = assessment.get("evidence")
                required = {"check:" + name for name in criterion["checks"]}
                if (
                    not isinstance(refs, list)
                    or not all(isinstance(ref, str) and ref in required for ref in refs)
                    or not required.issubset(refs)
                ):
                    raise ValueError(
                        "review evidence must reference the criterion's canonical checks"
                    )
                if status not in {"satisfied", "needs_manual", "unverified", "failed"}:
                    raise ValueError("invalid criterion status")
                if status == "needs_manual" and not criterion["manual_gates"]:
                    raise ValueError("manual deferral needs an approved manual gate")
                if parsed["passed"] and status in {"unverified", "failed"}:
                    raise ValueError(
                        "successful review cannot leave approved criteria unverified"
                    )
            if seen != set(expected):
                raise ValueError("independent review omitted approved criteria")
        return parsed

    def coverage(self, wid):
        """Compute completion from candidate-bound facts, never a model's overall verdict."""
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        candidate = json.loads(row["candidate_commits"])
        proposal = json.loads(row["plan_json"]).get("proposal", {})
        snapshot = json.loads(row["snapshot_json"])
        checks, assessments = set(), {}
        for job in self._jobs(wid):
            if job["kind"] not in {"check", "review"} or job["status"] != "succeeded":
                continue
            attempt = self.store.one(
                "SELECT * FROM attempts WHERE job_id=? AND status='succeeded'",
                (job["id"],),
            )
            if (
                not attempt
                or json.loads(attempt["inputs_json"])["candidate"] != candidate
            ):
                continue
            result = json.loads(attempt["result_json"])
            if job["kind"] == "check":
                sha = candidate.get(row["repo"], {}).get("commit")
                checks.update(
                    c["command"]
                    for c in result.get("checks", [])
                    if c.get("exit_code") == 0 and c.get("candidate") == sha
                )
            else:
                evidence = self.store.one(
                    "SELECT payload_json FROM evidence WHERE attempt_id=?",
                    (attempt["id"],),
                )
                if evidence:
                    try:
                        review = self._parse_role(
                            "review",
                            json.loads(evidence[0])["output"],
                            snapshot,
                            proposal,
                        )
                        if review["passed"]:
                            assessments = {c["id"]: c for c in review["criteria"]}
                    except (ValueError, KeyError, TypeError):
                        pass
        sha = candidate.get(row["repo"], {}).get("commit")
        manual = {
            g["name"]
            for g in self.store.query(
                "SELECT name FROM manual_gates WHERE root_id=? AND candidate=? AND passed=1",
                (wid, sha),
            )
        }
        coverage = []
        for criterion in proposal.get("acceptance", []):
            missing = ["check:" + c for c in criterion["checks"] if c not in checks]
            missing += [
                "manual:" + g for g in criterion["manual_gates"] if g not in manual
            ]
            if criterion["id"] not in assessments:
                missing.append("independent-review:" + criterion["id"])
            coverage.append(
                {
                    **criterion,
                    "status": "verified" if not missing else "incomplete",
                    "missing_evidence": missing,
                }
            )
        return coverage

    @atomic
    def cancel_job(self, job_id, actor):
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job or job["status"] not in {"ready", "blocked", "leased", "running"}:
            raise ControllerError("job is not cancellable")
        active = job["status"] in {"leased", "running"}
        self.store.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            ("cancelling" if active else "cancelled", job_id),
        )
        self.store.execute(
            "UPDATE attempts SET status='cancelling' WHERE job_id=? AND status IN ('leased','running')",
            (job_id,),
        )
        self.store.execute(
            "UPDATE work_items SET status='cancelled' WHERE id=?",
            (job["work_item_id"],),
        )
        self._packet(
            job["work_item_id"],
            "operator cancellation requested; active process must stop before reservation release",
        )
        self.store.audit(actor, "cancel", {"job_id": job_id})

    @atomic
    def recover(self, actor="operator"):
        """Fence expired work; never automatically replace an interrupted attempt."""
        count = 0
        for attempt in self.store.query(
            "SELECT a.*,j.lease_until FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE a.status IN ('leased','running','cancelling')"
        ):
            if min(
                datetime.fromisoformat(attempt["deadline"]),
                datetime.fromisoformat(attempt["lease_until"]),
            ) > datetime.now(timezone.utc):
                continue
            # An OS-supervised live child cannot be safely replaced merely on missing heartbeat.
            # Stop the recorded owned process group, then preserve workspace for inspection.
            import os
            import signal

            if attempt["pid"] and attempt["pid_identity"]:
                import subprocess

                observed = subprocess.run(
                    ["/bin/ps", "-p", str(attempt["pid"]), "-o", "lstart="],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if observed == attempt["pid_identity"]:
                    try:
                        if os.getpgid(attempt["pid"]) == attempt["pid"]:
                            os.killpg(attempt["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            reservation = json.loads(
                self.store.one(
                    "SELECT spec_json FROM jobs WHERE id=?", (attempt["job_id"],)
                )[0]
            )["budget_cents"]
            self.finish_attempt(
                attempt["id"],
                False,
                "interrupted attempt fenced after deadline",
                reservation,
                None,
                error="worker interrupted; workspace retained",
                timed_out=True,
            )
            count += 1
        self.store.audit(actor, "recover", {"fenced_attempts": count})
        return count

    @atomic
    def record_manual_gate(self, wid, name, candidate, actor, evidence, passed=True):
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if not row or row["status"] != "awaiting_review":
            raise ControllerError("work item is not awaiting review")
        snapshot = json.loads(row["snapshot_json"])
        commits = json.loads(row["candidate_commits"])
        if (
            name not in snapshot["manual_acceptance"]
            or candidate != commits[row["repo"]]["commit"]
        ):
            raise ControllerError("manual gate or candidate mismatch")
        if not actor.strip() or not evidence.strip():
            raise ControllerError("actor and evidence required")
        self.store.execute(
            "INSERT OR REPLACE INTO manual_gates(root_id,candidate,name,actor,at,passed,evidence) VALUES (?,?,?,?,?,?,?)",
            (wid, candidate, name, actor, now_iso(), int(passed), evidence),
        )
        self.store.audit(
            actor,
            "manual_gate",
            {"root": wid, "candidate": candidate, "name": name, "passed": passed},
        )

    @atomic
    def accept(self, work_item_id, actor):
        from dark_factory.artifacts import verify

        row = self.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
        if not row or row["status"] != "awaiting_review":
            raise ControllerError("work item is not awaiting review")
        self._require_root_scope(row)
        if row["approved_digest"] != _digest(self._approval_payload(work_item_id)):
            raise ControllerError("approved inputs changed; cannot accept")
        if any(j["status"] != "succeeded" for j in self._jobs(work_item_id)):
            raise ControllerError("required jobs have not succeeded")
        candidate = json.loads(row["candidate_commits"])[row["repo"]]
        snapshot = json.loads(row["snapshot_json"])
        if not snapshot["simulation"]:
            from dark_factory.adapters.git.local import pin

            current = pin(
                Path(snapshot["source"]["path"]), snapshot["source"]["revision"]
            )
            if current not in {snapshot["base"], candidate["commit"]}:
                raise ControllerError(
                    "source target changed; revalidate against the new target before acceptance"
                )
        for job in self._jobs(work_item_id):
            att = self.store.one(
                "SELECT * FROM attempts WHERE job_id=? AND status='succeeded'",
                (job["id"],),
            )
            verify(self.store.resolve_path(att["archive_path"]))
            inputs = json.loads(att["inputs_json"])
            if job["kind"] in {"check", "review"} and inputs["candidate"] != json.loads(
                row["candidate_commits"]
            ):
                raise ControllerError("candidate evidence is stale")
        for name in json.loads(row["snapshot_json"])["manual_acceptance"]:
            gate = self.store.one(
                "SELECT passed FROM manual_gates WHERE root_id=? AND candidate=? AND name=?",
                (work_item_id, candidate["commit"], name),
            )
            if not gate or not gate["passed"]:
                raise ControllerError(f"manual gate missing: {name}")
        coverage = self.coverage(work_item_id)
        if not coverage or any(c["status"] != "verified" for c in coverage):
            raise ControllerError("approved acceptance criteria remain incomplete")
        if snapshot["source"].get("require_live_evidence") and snapshot["simulation"]:
            raise ControllerError(
                "live evidence required; simulation cannot qualify this outcome"
            )
        if not actor.strip():
            raise ControllerError("reviewer is required")
        self.store.execute(
            "UPDATE work_items SET status='accepted',presentation_stage='Done',accepted_at=? WHERE id=?",
            (now_iso(), work_item_id),
        )
        self._packet(work_item_id)
        self.store.audit(
            actor,
            "accept",
            {"work_item_id": work_item_id, "candidate": candidate["commit"]},
        )

    def cleanup(self, actor="operator") -> int:
        from dark_factory.adapters.env.base import PreparedEnv
        from dark_factory.adapters.env.native import NativeEnvironment
        from dark_factory.artifacts import verify

        count = 0
        environment = NativeEnvironment(self.cfg.state_dir / "workspaces")
        for attempt in self.store.query(
            "SELECT * FROM attempts WHERE cleanup_status IN ('archived','cleanup_failed') AND status NOT IN ('leased','running','cancelling')"
        ):
            if not attempt["archive_path"] or not attempt["workspace"]:
                continue
            verify(self.store.resolve_path(attempt["archive_path"]))
            workspace = self.store.resolve_path(attempt["workspace"])
            environment.release(PreparedEnv(workspace, "native", {}))
            self.store.execute(
                "UPDATE attempts SET cleanup_status='cleaned' WHERE id=?",
                (attempt["id"],),
            )
            self.store.audit(actor, "cleanup.complete", {"attempt_id": attempt["id"]})
            count += 1
        return count

    def external_action(self, wid, operation, payload, actor):
        """Record intent before a write; reconcile the same identity before retry."""
        if operation != "create_pr":
            raise ControllerError("bootstrap only supports draft PR creation")
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if not root or root["status"] != "awaiting_review":
            raise ControllerError("a checked and reviewed candidate is required")
        candidate = json.loads(root["candidate_commits"])[root["repo"]]
        if (
            payload.get("repo") != root["repo"]
            or payload.get("head_sha") != candidate["commit"]
        ):
            raise ControllerError("PR payload must name the exact reviewed candidate")
        if root["approved_digest"] != _digest(self._approval_payload(wid)):
            raise ControllerError("approved inputs changed")
        identity = _digest({"root": wid, "operation": operation, "payload": payload})
        action = self.store.one(
            "SELECT * FROM pending_actions WHERE idempotency_key=?", (identity,)
        )
        cmd = self.cfg.commands["code_host"]
        if action and action["status"] == "succeeded":
            return json.loads(action["payload_json"])["result"]
        if action:
            observed = self.runner.run_json(
                cmd.command,
                "reconcile_create_pr",
                payload,
                identity,
                cmd.timeout_seconds,
            )
            if not observed["ok"]:
                raise ControllerError("external action reconciliation failed")
            if observed["evidence"].get("observed"):
                result = observed
            elif observed["evidence"].get("safe_to_retry"):
                result = None
            else:
                raise ControllerError("external action remains ambiguous; no retry")
        else:
            self.store.execute(
                "INSERT INTO pending_actions(id,kind,idempotency_key,payload_json,status,created_at) VALUES (?,?,?,?,?,?)",
                (
                    identity,
                    operation,
                    identity,
                    json.dumps({"root": wid, "input": payload}),
                    "pending",
                    now_iso(),
                ),
            )
            self.store.audit(
                actor,
                "external.intent",
                {"id": identity, "operation": operation, "root": wid},
            )
            result = None
        if result is None:
            try:
                result = self.runner.run_json(
                    cmd.command, operation, payload, identity, cmd.timeout_seconds
                )
                if not result["ok"] or not result["evidence"].get("observed"):
                    raise ControllerError(
                        "external write did not return confirmed evidence"
                    )
            except Exception:
                self.store.execute(
                    "UPDATE pending_actions SET status='ambiguous' WHERE id=?",
                    (identity,),
                )
                raise
        self.store.execute(
            "UPDATE pending_actions SET status='succeeded',payload_json=? WHERE id=?",
            (json.dumps({"root": wid, "input": payload, "result": result}), identity),
        )
        self.store.audit(
            actor,
            "external.confirmed",
            {"id": identity, "evidence": result["evidence"]},
        )
        return result

    def proposal(self, wid, version=None):
        if version is None:
            root = self.store.one(
                "SELECT plan_version FROM work_items WHERE id=?", (wid,)
            )
            if not root:
                raise ControllerError("work item not found")
            version = root["plan_version"]
        row = self.store.one(
            "SELECT payload_json FROM proposals WHERE root_id=? AND version=?",
            (wid, version),
        )
        if not row:
            raise ControllerError("proposal version not found")
        return json.loads(row["payload_json"])

    def status(self):
        allowed, reason = self.dispatch_allowed()
        return {
            "project": self.cfg.project_name,
            "dispatch": {
                "allowed": allowed,
                "reason": reason,
                "pause": self.store.get_op("pause"),
                "drain": self.store.get_op("drain"),
            },
            "work_items": [
                dict(r)
                for r in self.store.query(
                    "SELECT id,title,status,presentation_stage,spent_cents,reserved_cents FROM work_items"
                )
            ],
            "jobs": [
                dict(r)
                for r in self.store.query(
                    "SELECT id,work_item_id,kind,status FROM jobs"
                )
            ],
        }
