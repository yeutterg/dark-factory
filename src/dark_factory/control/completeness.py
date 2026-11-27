"""Shadow completeness judgments between canonical checks and code review."""

from __future__ import annotations

import json
from datetime import datetime

from dark_factory.control.base import ControllerCore
from dark_factory.execution.packages import identity
from dark_factory.store import new_id, now_iso

OPTIONS = {
    "supported": "The cited evidence directly exercises the behavior this criterion promises.",
    "gap": "The evidence passes but leaves a concrete part of the promised behavior untested.",
    "insufficient_evidence": "The supplied state is insufficient to decide whether the behavior is exercised.",
}


class CompletenessMixin(ControllerCore):
    """Records advisory decisions. It never blocks review or authorizes repair."""

    def recover_completeness(self):
        """Conservatively settle calls that were in flight when the controller stopped."""
        with self.store.transaction():
            for row in self.store.query(
                "SELECT * FROM completeness_assessments WHERE status='pending'"
            ):
                self.store.execute(
                    "UPDATE work_items SET reserved_cents=MAX(reserved_cents-?,0),"
                    "spent_cents=spent_cents+? WHERE id=?",
                    (row["reserved_cents"], row["reserved_cents"], row["root_id"]),
                )
                self.store.execute(
                    "UPDATE completeness_assessments SET status='interrupted',"
                    "cost_cents=reserved_cents,reserved_cents=0,settled_at=? WHERE id=?",
                    (now_iso(), row["id"]),
                )
                self.store.execute(
                    "INSERT INTO costs(id,root_id,attempt_id,source,amount_cents,status,created_at) "
                    "VALUES (?,?,?,?,?,'charged',?)",
                    (
                        new_id("cost"),
                        row["root_id"],
                        None,
                        "completeness-shadow-interrupted",
                        row["reserved_cents"],
                        now_iso(),
                    ),
                )

    def _completeness_candidate(self):
        settings = self.cfg.decider
        if not settings.get("completeness_shadow"):
            return None
        for root in self.store.query(
            "SELECT * FROM work_items WHERE status='running' AND candidate_commits<>'{}' "
            "ORDER BY created_at,rowid"
        ):
            jobs = self.jobs(root["id"])
            if not any(j["kind"] == "review" and j["status"] == "ready" for j in jobs):
                continue
            check = next(
                (
                    j
                    for j in reversed(jobs)
                    if j["kind"] == "check" and j["status"] == "succeeded"
                ),
                None,
            )
            if not check:
                continue
            attempt = self.store.one(
                "SELECT * FROM attempts WHERE job_id=? AND status='succeeded' "
                "ORDER BY ended_at DESC LIMIT 1",
                (check["id"],),
            )
            if attempt:
                return root, attempt
        return None

    def completeness_tick(self):
        selected = self._completeness_candidate()
        if not selected:
            return None
        root, attempt = selected
        candidate = json.loads(root["candidate_commits"])[root["repo"]]
        proposal = json.loads(root["plan_json"])["proposal"]
        check_result = json.loads(attempt["result_json"] or "{}")
        archive = self.store.resolve_path(candidate["archive"])
        diff = (
            (archive / "diff.patch").read_text()
            if (archive / "diff.patch").is_file()
            else ""
        )
        settings = self.cfg.decider
        command = self.cfg.commands[settings["command"]]
        checks = []
        for item in check_result.get("checks", []):
            if not isinstance(item, dict):
                continue
            checks.append(
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"stdout", "stderr"}
                }
                | {
                    "stdout_tail": str(item.get("stdout", ""))[-2_000:],
                    "stderr_tail": str(item.get("stderr", ""))[-2_000:],
                }
            )
        state = {
            "root": root["id"],
            "candidate": candidate["commit"],
            "criteria": proposal["acceptance"],
            "check_results": checks,
            "diff_excerpt": diff[:20_000],
            "decision_binding": json.loads(root["snapshot_json"])["policy"][
                "commands"
            ].get(settings["command"], {}),
            "expected_model": settings["completeness_model"],
        }
        questions = {
            criterion["id"]: {
                "type": "choice",
                "instructions": (
                    "Judge only whether the supplied candidate-bound evidence directly "
                    f"exercises this approved criterion: {criterion['description']}"
                ),
                "options": OPTIONS,
            }
            for criterion in proposal["acceptance"]
        }
        evidence_digest = identity({"state": state, "questions": questions})
        assessment_id = new_id("ca")
        budget = settings["completeness_budget_cents"]
        with self.store.transaction():
            if self.store.one(
                "SELECT id FROM completeness_assessments WHERE root_id=? AND candidate=? "
                "AND evidence_digest=?",
                (root["id"], candidate["commit"], evidence_digest),
            ):
                return None
            current = self.store.one(
                "SELECT spent_cents,reserved_cents,spending_ceiling_cents FROM work_items WHERE id=?",
                (root["id"],),
            )
            if (
                current["spent_cents"] + current["reserved_cents"] + budget
                > current["spending_ceiling_cents"]
            ):
                status = "budget_unavailable"
                budget = 0
            else:
                status = "pending"
                self.store.execute(
                    "UPDATE work_items SET reserved_cents=reserved_cents+? WHERE id=?",
                    (budget, root["id"]),
                )
            self.store.execute(
                """INSERT INTO completeness_assessments
                (id,root_id,candidate,evidence_digest,status,reserved_cents,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    assessment_id,
                    root["id"],
                    candidate["commit"],
                    evidence_digest,
                    status,
                    budget,
                    now_iso(),
                ),
            )
            if status != "pending":
                self.store.execute(
                    "UPDATE completeness_assessments SET settled_at=? WHERE id=?",
                    (now_iso(), assessment_id),
                )
        if status != "pending":
            return {"root": root["id"], "status": status}
        response = None
        cost = 0
        final_status = "unavailable"
        agent = model = ""
        try:
            result = self.runner.run_json(
                command.command,
                "decide",
                {"state": state, "questions": questions},
                "completeness-" + evidence_digest[:24],
                command.timeout_seconds,
            )
            evidence = result["evidence"]
            answers = evidence["answers"]
            if set(answers) != set(questions) or any(
                answer.get("choice") not in OPTIONS for answer in answers.values()
            ):
                raise ValueError("completeness answers do not cover approved criteria")
            response = {
                "answers": answers,
                "evidence_refs": {
                    criterion["id"]: ["check:" + check for check in criterion["checks"]]
                    for criterion in proposal["acceptance"]
                },
            }
            agent = str(evidence.get("agent", "decider"))
            model = str(evidence.get("model", ""))
            if model != settings["completeness_model"]:
                raise ValueError(
                    "completeness response model differs from pinned model"
                )
            reported = (evidence.get("usage") or {}).get("cost_cents", 0)
            if type(reported) is not int or not 0 <= reported <= budget:
                raise ValueError("completeness cost exceeds its reservation")
            cost = reported
            final_status = "recorded"
        except Exception as exc:
            response = {"error": str(exc)[:500]}
        with self.store.transaction():
            self.store.execute(
                "UPDATE work_items SET reserved_cents=MAX(reserved_cents-?,0),"
                "spent_cents=spent_cents+? WHERE id=?",
                (budget, cost, root["id"]),
            )
            self.store.execute(
                """UPDATE completeness_assessments SET status=?,response_json=?,agent=?,
                model=?,reserved_cents=0,cost_cents=?,settled_at=? WHERE id=?""",
                (
                    final_status,
                    json.dumps(response),
                    agent,
                    model,
                    cost,
                    now_iso(),
                    assessment_id,
                ),
            )
            if cost:
                self.store.execute(
                    "INSERT INTO costs(id,root_id,attempt_id,source,amount_cents,status,created_at) "
                    "VALUES (?,?,?,?,?,'charged',?)",
                    (
                        new_id("cost"),
                        root["id"],
                        None,
                        "completeness-shadow",
                        cost,
                        now_iso(),
                    ),
                )
        return {"root": root["id"], "status": final_status, "answers": response}

    def completeness_report(self):
        rows = self.store.query(
            "SELECT c.*,w.status root_status FROM completeness_assessments c "
            "JOIN work_items w ON w.id=c.root_id ORDER BY c.created_at"
        )
        report = []
        for row in rows:
            response = json.loads(row["response_json"] or "{}")
            choices = [a.get("choice") for a in response.get("answers", {}).values()]
            reviewer_blocked = bool(
                self.store.one(
                    """SELECT 1 FROM attempts a JOIN jobs j ON j.id=a.job_id
                    WHERE j.work_item_id=? AND j.kind='review' AND a.status='failed' LIMIT 1""",
                    (row["root_id"],),
                )
            )
            human_rejected = bool(
                self.store.one(
                    "SELECT 1 FROM audit WHERE action='reject' AND "
                    "json_extract(payload_json,'$.work_item_id')=? LIMIT 1",
                    (row["root_id"],),
                )
            )
            elapsed = None
            if row["settled_at"]:
                elapsed = (
                    datetime.fromisoformat(row["settled_at"])
                    - datetime.fromisoformat(row["created_at"])
                ).total_seconds()
            report.append(
                {
                    "root": row["root_id"],
                    "candidate": row["candidate"],
                    "status": row["status"],
                    "choices": choices,
                    "reviewer_blocked": reviewer_blocked,
                    "human_rejected": human_rejected,
                    "possible_false_alarm": "gap" in choices
                    and not reviewer_blocked
                    and not human_rejected,
                    "possible_miss": "gap" not in choices
                    and (reviewer_blocked or human_rejected),
                    "seconds": elapsed,
                    "cost_cents": row["cost_cents"],
                    "model": row["model"],
                }
            )
        return {
            "shadow": True,
            "runs": len(report),
            "cost_cents": sum(item["cost_cents"] for item in report),
            "possible_false_alarms": sum(
                item["possible_false_alarm"] for item in report
            ),
            "possible_misses": sum(item["possible_miss"] for item in report),
            "assessments": report,
        }
