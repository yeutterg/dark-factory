"""One report over every stage of both loops, from the records the factory already keeps.

Because every change and every release takes the same path, one table answers the
optimisation questions: which stage costs the most time, on which runner, and how
often it fails. Nothing here is estimated; each number is a stored duration.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from dark_factory.store import Store


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _row(key: tuple[str, ...], samples: list[tuple[str, float]]) -> dict[str, Any]:
    durations = sorted(s for _, s in samples)
    p95 = durations[min(len(durations) - 1, math.ceil(0.95 * len(durations)) - 1)]
    return {
        "repo": key[0],
        "loop": key[1],
        "stage": key[2],
        "runner": key[3],
        "runs": len(samples),
        "passed": sum(1 for outcome, _ in samples if outcome == "passed"),
        "failed": sum(1 for outcome, _ in samples if outcome == "failed"),
        "mean_seconds": round(sum(durations) / len(durations), 3),
        "p95_seconds": round(p95, 3),
        "total_seconds": round(sum(durations), 3),
    }


def stage_metrics(store: Store) -> dict[str, Any]:
    groups: dict[tuple[str, ...], list[tuple[str, float]]] = {}

    def add(key, outcome, seconds):
        if seconds is not None:
            groups.setdefault(key, []).append((outcome, seconds))

    # The change loop: the factory's own jobs, and the pipeline stages inside the check.
    for row in store.query(
        "SELECT w.repo repo,j.kind kind,a.status status,a.started_at s,a.ended_at e,"
        "a.result_json r FROM attempts a JOIN jobs j ON a.job_id=j.id "
        "JOIN work_items w ON w.id=j.work_item_id WHERE a.ended_at IS NOT NULL"
    ):
        outcome = "passed" if row["status"] == "succeeded" else "failed"
        add(
            (row["repo"], "change", row["kind"], "factory"),
            outcome,
            _seconds(row["s"], row["e"]),
        )
        for stage in json.loads(row["r"] or "{}").get("stages", []):
            if stage.get("status") in {"passed", "failed"}:
                add(
                    (
                        row["repo"],
                        "change",
                        stage["stage"],
                        stage.get("runner", "local"),
                    ),
                    stage["status"],
                    stage.get("seconds"),
                )
    # The release loop.
    for row in store.query(
        "SELECT r.repo repo,s.stage stage,s.status status,s.runner runner,s.seconds seconds "
        "FROM release_stages s JOIN releases r ON r.id=s.release_id "
        "WHERE s.status IN ('passed','failed') AND s.seconds IS NOT NULL"
    ):
        add(
            (row["repo"], "release", row["stage"], row["runner"] or "factory"),
            row["status"],
            row["seconds"],
        )
    rows = [_row(key, samples) for key, samples in sorted(groups.items())]
    slowest = sorted(rows, key=lambda r: r["total_seconds"], reverse=True)[:5]
    return {
        "stages": rows,
        "slowest": [
            {k: r[k] for k in ("repo", "loop", "stage", "runner", "total_seconds")}
            for r in slowest
        ],
    }
