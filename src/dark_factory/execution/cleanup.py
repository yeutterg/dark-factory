"""Retry release of an archived attempt without replaying agent execution."""

from __future__ import annotations

import json
import shutil

from dark_factory.adapters.env.base import PreparedEnv
from dark_factory.adapters.env.native import NativeEnvironment
from dark_factory.adapters.env.registry import environment_for
from dark_factory.adapters.harness.registry import adapter_for


def release_attempt_environment(store, attempt) -> None:
    """Release the exact frozen backend for an already archived attempt.

    Non-native backends need the same owned-resource identity or policy descriptor
    they received during execution. It is reconstructed only from the attempt's
    immutable inputs and root snapshot. No harness or model process is started.
    """
    workspace = store.resolve_path(attempt["workspace"])
    inputs = json.loads(attempt["inputs_json"] or "{}")
    recipe = inputs.get("environment") or {"kind": "native"}
    if recipe.get("kind") in {"native", "macos-sandbox"}:
        NativeEnvironment(store.state_dir / "workspaces").release(
            PreparedEnv(workspace, recipe.get("kind", "native"), {})
        )
        return

    job = store.one("SELECT * FROM jobs WHERE id=?", (attempt["job_id"],))
    root = store.one("SELECT * FROM work_items WHERE id=?", (job["work_item_id"],))
    snapshot = json.loads(root["snapshot_json"])
    spec = json.loads(job["spec_json"])
    route = inputs.get("route")
    role = spec.get("role")
    runtime = store.state_dir / "runtime" / attempt["id"]
    environment = environment_for(store.state_dir / "workspaces", recipe, route)
    prepared = PreparedEnv(workspace, recipe["kind"], {})
    immutable = adapter_for(route["harness"]).immutable_runtime_files if role else ()
    environment.reconstruct(
        prepared,
        runtime,
        network=bool(role),
        writable=snapshot["source"]["allowed_paths"]
        if role == "coder"
        else (["."] if not role else []),
        protected=snapshot["source"].get("protected_paths", []),
        immutable_runtime_files=immutable,
    )
    if role:
        environment.reconstruct(
            prepared,
            runtime / "version-probe",
            network=False,
            writable=[],
            protected=[],
        )
    environment.release(prepared)


def retry_replica_cleanup(root, attempt_id) -> None:
    """Retry a private worker replica's failed release idempotently."""
    from dark_factory.store import Store

    store = Store(root)
    try:
        attempt = store.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        if not attempt:
            raise ValueError("cleanup attempt is absent from the private replica")
        if attempt["cleanup_status"] == "cleaned":
            return
        if attempt["cleanup_status"] != "cleanup_failed":
            raise ValueError("attempt does not have a retryable cleanup failure")
        release_attempt_environment(store, attempt)
        store.execute(
            "UPDATE attempts SET cleanup_status='cleaned' WHERE id=?", (attempt_id,)
        )
        shutil.rmtree(store.state_dir / "runtime" / attempt_id, ignore_errors=True)
    finally:
        store.close()
