"""Plan graphs: the bootstrap job graph and its structural validation."""

from __future__ import annotations

from dark_factory.config import FactoryConfig
from dark_factory.control.base import ControllerError
from dark_factory.models import JOB_KINDS, JobSpec, PlanGraph


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
    root_id: str, repo: str, ceiling: int, route_ids: list[str], max_repairs: int = 0
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
    if max_repairs:
        jobs += [
            JobSpec(
                "repair",
                "implement",
                role="coder",
                repo=repo,
                depends_on=["critique"],
                budget_cents=200,
                timeout_seconds=1800,
                repair=True,
            ),
            JobSpec(
                "repair-check",
                "check",
                repo=repo,
                depends_on=["repair"],
                timeout_seconds=300,
                repair=True,
            ),
            JobSpec(
                "repair-review",
                "review",
                role="reviewer",
                repo=repo,
                depends_on=["repair-check"],
                budget_cents=50,
                timeout_seconds=600,
                repair=True,
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
