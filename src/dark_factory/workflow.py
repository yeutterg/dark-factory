"""Application-owned workflow boundary; no engine types enter controller policy."""

from __future__ import annotations

from typing import Callable, Protocol


class WorkflowRuntime(Protocol):
    name: str

    def run(self, claim: dict, execute: Callable[[], None]) -> None: ...

    def pending_attempts(self) -> list[str]: ...


def runtime_for(cfg, store) -> WorkflowRuntime:
    engine = cfg.raw.get("workflow", {}).get("engine", "native")
    if engine == "native":
        from dark_factory.adapters.workflow.native import NativeRuntime

        return NativeRuntime()
    if engine == "pi-durable":
        from dark_factory.adapters.workflow.pi_durable import PiDurableRuntime

        return PiDurableRuntime(cfg, store)
    raise RuntimeError("unsupported workflow runtime; no fallback permitted")


def runtime_policy(cfg) -> dict:
    from pathlib import Path

    from dark_factory.artifacts import digest

    config = cfg.raw.get("workflow", {"engine": "native"})
    policy = {"config": config, "contract_version": 1}
    if config.get("engine") == "pi-durable":
        directory = Path(__file__).parent / "adapters" / "workflow" / "pi_engine"
        policy["code"] = {
            name: digest(directory / name)
            for name in ("main.ts", "package.json", "package-lock.json")
        }
    return policy
