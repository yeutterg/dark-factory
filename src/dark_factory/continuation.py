"""Project-neutral root continuation view derived from canonical controller facts."""

from __future__ import annotations

import json


def describe(root, jobs, coverage, dispatch_reason=""):
    succeeded = {j["logical_id"] for j in jobs if j["status"] == "succeeded"}
    work = []
    for job in jobs:
        missing = [dep for dep in json.loads(job["depends_on"]) if dep not in succeeded]
        state = (
            "verified"
            if job["status"] == "succeeded"
            else "running"
            if job["status"] in {"leased", "running", "cancelling"}
            else "blocked"
            if missing or job["status"] != "ready"
            else "queued"
        )
        work.append(
            {
                "job_id": job["id"],
                "kind": job["kind"],
                "state": state,
                "status": job["status"],
                "waiting_on": missing,
            }
        )
    status = root["status"]
    automatic_missing = [
        ref
        for c in coverage
        for ref in c["missing_evidence"]
        if not ref.startswith("manual:")
    ]
    if status == "accepted":
        action = {"kind": "complete"}
    elif status in {"failed", "rejected", "cancelled"}:
        action = {
            "kind": "blocked",
            "reason": status,
            "next_step": "Inspect retained evidence and create a revised proposal.",
        }
    elif status == "selected":
        action = {
            "kind": "human_plan",
            "next_step": "Select planning for the complete root.",
        }
    elif status == "interrupted":
        action = {
            "kind": "human_resume",
            "next_step": "Resume the interrupted step from its saved work, or stop.",
        }
    elif status == "awaiting_approval":
        action = {
            "kind": "human_approval",
            "next_step": "Review and approve the exact proposal.",
        }
    elif status == "awaiting_review":
        if (
            automatic_missing
            or not jobs
            or any(j["status"] != "succeeded" for j in jobs)
        ):
            action = {
                "kind": "blocked",
                "reason": "required automated evidence is missing",
                "missing_evidence": automatic_missing,
                "next_step": "Reconcile missing verification; do not accept this root.",
            }
        else:
            action = {
                "kind": "human_review",
                "next_step": "Inspect the exact candidate and record its required manual tests.",
            }
    elif any(w["state"] == "running" for w in work):
        action = {
            "kind": "running",
            "jobs": [w["job_id"] for w in work if w["state"] == "running"],
        }
    elif dispatch_reason:
        action = {
            "kind": "blocked",
            "reason": dispatch_reason,
            "next_step": "Resolve the stated dispatch constraint without expanding approved authority.",
        }
    else:
        queued = [w["job_id"] for w in work if w["state"] == "queued"]
        action = (
            {"kind": "dispatch", "jobs": queued}
            if queued
            else {
                "kind": "blocked",
                "reason": "unfinished required work has no eligible assignment",
                "next_step": "Reconcile dependencies or revise the proposal; the root remains incomplete.",
            }
        )
    return {
        "root_id": root["id"],
        "plan_version": root["plan_version"],
        "work": work,
        "next_action": action,
    }
