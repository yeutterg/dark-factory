"""Approval authority: the exact policy inputs a decision is bound to."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dark_factory.artifacts import digest_cached
from dark_factory.config import ConfigError, load_config, load_prompt
from dark_factory.control.base import ControllerCore, ControllerError, _digest
from dark_factory.runner import CommandRunner
from dark_factory.workflow import runtime_policy

# The approved runner code is the whole dark_factory package.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class PolicyMixin(ControllerCore):
    """Evaluate, memoize and compare the policy that approvals cover."""

    def _save_config(self):

        raw = copy.deepcopy(self.cfg.raw)
        # A literal dev token is intentionally not a recoverable credential snapshot.
        for key in ("worker_token", "operator_token"):
            if key in raw.get("runtime", {}):
                raw["runtime"][key] = "<redacted>"
        self.store.save_config_revision(self.cfg.digest, json.dumps(raw), True)

    def reload(self, actor="operator") -> bool:

        try:
            candidate = load_config(self.cfg.source_path)
            if candidate.state_dir != self.cfg.state_dir:
                raise ConfigError("reload cannot move controller state")
            if (
                candidate.project_id != self.cfg.project_id
                or candidate.project_name != self.cfg.project_name
                or candidate.repositories != self.cfg.repositories
            ):
                raise ConfigError("reload cannot change project ownership")
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

    @contextmanager
    def decision(self):
        """Share one policy evaluation across a complete controller decision.

        Policy inputs are re-read for every outermost decision, so on-disk or
        in-memory changes between decisions are still detected. The scope is per
        thread: API and worker threads never see each other's snapshot.
        """
        scope = self._decision_scope
        scope.depth = getattr(scope, "depth", 0) + 1
        try:
            yield
        finally:
            scope.depth -= 1
            if not scope.depth:
                scope.policy = None

    def _policy(self) -> dict:
        scope = self._decision_scope
        if not getattr(scope, "depth", 0):
            return self._evaluate_policy()
        if getattr(scope, "policy", None) is None:
            scope.policy = self._evaluate_policy()
        return scope.policy

    def _approval_current(self, row) -> bool:
        """Whether the recorded approval still covers the current inputs."""
        return bool(row["approved_digest"]) and row["approved_digest"] == _digest(
            self._approval_payload(row["id"])
        )

    def _evaluate_policy(self) -> dict:
        # Availability can narrow at any time; the approved execution authority stays fixed.

        package = {
            str(p.relative_to(PACKAGE_ROOT)): digest_cached(p)
            for p in sorted(PACKAGE_ROOT.rglob("*.py"))
            if "node_modules" not in p.parts
        }
        # Copied so a held snapshot never aliases mutable configuration objects.
        return copy.deepcopy(
            {
                "runner_code": package,
                "routes": {k: vars(v) for k, v in self.cfg.routes.items()},
                "commands": self._bindings(),
                "assets": self._assets(),
                "sources": self.cfg.sources,
                "execution": self.cfg.execution,
                "workflow": runtime_policy(self.cfg),
                "operators": self.cfg.raw.get("operators", {}),
                "continuation": {
                    "max_repairs": self.cfg.max_repairs,
                    "max_resumes": self.cfg.max_resumes,
                },
                "decider": self.cfg.decider,
                "budgets": self.cfg.raw.get("budgets", {}),
                "geography": self.cfg.geography_policy,
                "deployment": {
                    "project_id": self.cfg.project_id,
                    "role_routes": self.cfg.role_routes,
                    "environments": self.cfg.environments,
                    "workers": self.cfg.workers,
                },
            }
        )

    def _assets(self) -> dict:
        assets: dict[str, Any] = {}
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
                    files[str(file.relative_to(path))] = digest_cached(file)
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

        bindings = {}
        for cid, command in self.cfg.commands.items():
            argv = list(command.command)
            executable = shutil.which(argv[0])
            if not executable:
                raise ControllerError(f"command executable not found: {argv[0]}")
            argv[0] = str(Path(executable).resolve())
            files = {argv[0]: digest_cached(Path(argv[0]))}
            for i, arg in enumerate(argv[1:], 1):
                path = (
                    Path(arg)
                    if Path(arg).is_absolute()
                    else self.cfg.source_path.parent / arg
                )
                if "{" not in arg and path.is_file():
                    argv[i] = str(path.resolve())
                    files[argv[i]] = digest_cached(path)
            if "-m" in argv:
                index = argv.index("-m") + 1
                module = (
                    importlib.util.find_spec(argv[index]) if index < len(argv) else None
                )
                if module and module.origin and Path(module.origin).is_file():
                    files[str(Path(module.origin).resolve())] = digest_cached(
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

    def _approval_payload(self, wid):
        row = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        return {
            "plan": json.loads(row["plan_json"]),
            "critique": row["critique_json"],
            "snapshot": json.loads(row["snapshot_json"]),
            "jobs": [
                {k: j[k] for k in ("id", "kind", "spec_json", "depends_on")}
                for j in self.jobs(wid)
            ],
            "ceiling": row["spending_ceiling_cents"],
            "policy": self._policy(),
        }
