"""Controller: the one object that changes execution state.

Responsibilities live in dark_factory.control; this module composes them and
keeps the public import surface stable.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from dark_factory.attempts import AttemptRecords
from dark_factory.config import FactoryConfig
from dark_factory.control.base import ControllerError, _digest, atomic, decided
from dark_factory.control.completeness import CompletenessMixin
from dark_factory.control.dispatch import DispatchMixin
from dark_factory.control.external import ExternalMixin
from dark_factory.control.external_work import ExternalWorkMixin
from dark_factory.control.gate import GateMixin
from dark_factory.control.graph import default_bootstrap_graph, validate_graph
from dark_factory.control.improvement import ImprovementMixin
from dark_factory.control.interruptions import InterruptionMixin
from dark_factory.control.planning import PlanningMixin
from dark_factory.control.policy import PolicyMixin
from dark_factory.control.release import ReleaseMixin
from dark_factory.control.resources import ResourcesMixin
from dark_factory.control.results import ResultsMixin
from dark_factory.control.review import ReviewMixin
from dark_factory.control.tasks import TaskMixin
from dark_factory.control.tasks_markdown import TaskMarkdownMixin
from dark_factory.control.tasks_progress import TaskProgressMixin
from dark_factory.control.tasks_supervisor import TaskSupervisorMixin
from dark_factory.control.windows import window_open
from dark_factory.runner import CommandRunner
from dark_factory.store import Store

__all__ = [
    "Controller",
    "ControllerError",
    "_digest",
    "atomic",
    "decided",
    "default_bootstrap_graph",
    "validate_graph",
    "window_open",
]


class Controller(
    PolicyMixin,
    PlanningMixin,
    CompletenessMixin,
    DispatchMixin,
    ResultsMixin,
    ReviewMixin,
    ExternalMixin,
    ExternalWorkMixin,
    GateMixin,
    InterruptionMixin,
    ImprovementMixin,
    ReleaseMixin,
    ResourcesMixin,
    TaskSupervisorMixin,
    TaskProgressMixin,
    TaskMarkdownMixin,
    TaskMixin,
    AttemptRecords,
):
    """One-process bootstrap controller; only this object mutates execution state."""

    def __init__(
        self, cfg: FactoryConfig, store: Store, packaged_prompts: Path
    ) -> None:
        self.cfg, self.store, self.packaged_prompts = cfg, store, packaged_prompts
        self._decision_scope = threading.local()
        self._release_threads: dict[str, threading.Thread] = {}
        self._release_locks: dict[str, threading.RLock] = {}
        identity = json.dumps(
            {"project_id": cfg.project_id, "project_name": cfg.project_name},
            sort_keys=True,
        )
        with store.transaction():
            owner = store.one("SELECT value FROM meta WHERE key='project_identity'")
            if owner and owner["value"] != identity:
                try:
                    previous = json.loads(owner["value"])
                except ValueError:
                    previous = {}
                if (
                    previous.get("project_id") != cfg.project_id
                    or previous.get("project_name") != cfg.project_name
                ):
                    raise ControllerError("state belongs to a different project")
                store.execute(
                    "UPDATE meta SET value=? WHERE key='project_identity'", (identity,)
                )
            if not owner and store.one("SELECT id FROM work_items LIMIT 1"):
                raise ControllerError(
                    "project-scoped deployment requires fresh state; legacy adoption is not implicit"
                )
            store.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES ('project_identity',?)",
                (identity,),
            )
        self.runner = CommandRunner(cfg.source_path.parent, cfg.max_output_bytes)
        from dark_factory.mirrors import sync_all

        sync_all(cfg)
        self._save_config()
        self.scrub_retired_credentials()
        self.recover_completeness()

    def jobs(self, wid):
        return [
            dict(j)
            for j in self.store.query(
                """SELECT j.* FROM jobs j JOIN work_items w ON w.id=j.work_item_id
                WHERE j.work_item_id=? AND j.plan_version=w.plan_version
                ORDER BY j.position""",
                (wid,),
            )
        ]
