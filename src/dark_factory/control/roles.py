"""Validate structured planner, critic and reviewer output against approved scope."""

from __future__ import annotations

import json


def parse_role(kind, output, snapshot, proposal=None):
    text = output.strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    if kind == "plan":
        assumptions = parsed.get("assumptions", [])
        if not isinstance(assumptions, list) or not all(
            isinstance(a, str) and a.strip() for a in assumptions
        ):
            raise ValueError("planning assumptions must be a list of strings")
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
            raise ValueError("independent review must cover every approved criterion")
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
