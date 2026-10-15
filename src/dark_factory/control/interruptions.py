"""Interrupted steps: continue automatically when trivial, otherwise ask a person."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from dark_factory.control.base import ControllerCore, ControllerError, atomic
from dark_factory.interruptions import CATEGORY_TEXT, Verdict, categorize, decide
from dark_factory.store import new_id, now_iso

QUESTION = {
    "type": "choice",
    "instructions": (
        "A step in an autonomous software factory stopped before it finished. "
        "Decide what should happen next. Resuming starts a new attempt of the same "
        "step from the work it saved, within the limits a person approved."
    ),
    "options": {
        "resume": "The interruption looks temporary or trivial, such as a lost "
        "worker, a provider or network hiccup or a malformed answer, and "
        "continuing from the saved work is likely to succeed",
        "ask": "A person should look first: the cause is unclear, or continuing "
        "may repeat the same failure",
        "stop": "Continuing is unlikely to help: the step looks stuck, is looping "
        "or is heading in the wrong direction",
    },
}


class InterruptionMixin(ControllerCore):
    """Record, classify and resolve interrupted attempts."""

    def _record_interruption(self, root, job, attempt_id, error, timed_out):
        """Called while settling an attempt; classification happens afterwards."""
        category = categorize(error or "", timed_out)
        self.store.execute(
            "UPDATE jobs SET status='interrupted' WHERE id=?", (job["id"],)
        )
        self.store.execute(
            "INSERT INTO interruptions(id,root_id,job_id,attempt_id,at,category,detail,outcome) VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id("int"),
                root["id"],
                job["id"],
                attempt_id,
                now_iso(),
                category,
                (error or "")[:2000],
                "pending",
            ),
        )
        self.store.audit(
            "controller",
            "interruption",
            {
                "root": root["id"],
                "job": job["id"],
                "attempt": attempt_id,
                "category": category,
            },
        )
        self._packet(root["id"], f"interrupted: {CATEGORY_TEXT[category]}")

    def _resume_blockers(self, root, job):
        """Limits nobody may resume past; each is a code-enforced approval boundary."""
        blockers = []
        if not self._approval_current(root) and job["kind"] not in {"plan", "critique"}:
            blockers.append("approved inputs changed; a new plan is needed")
        snapshot = json.loads(root["snapshot_json"])
        if datetime.fromisoformat(snapshot["deadline"]) <= datetime.now(UTC):
            blockers.append("the work's approved time limit has passed")
        need = json.loads(job["spec_json"])["budget_cents"]
        if (
            root["spent_cents"] + root["reserved_cents"] + need
            > root["spending_ceiling_cents"]
        ):
            blockers.append("not enough approved budget is left for another attempt")
        return blockers

    def _automatic_resumes(self, job_id):
        return self.store.one(
            "SELECT COUNT(*) n FROM interruptions WHERE job_id=? AND outcome='auto_resumed'",
            (job_id,),
        )["n"]

    def _interruption_state(self, row):
        """Facts a decision agent may see: no credentials, bounded text."""
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (row["job_id"],))
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (row["root_id"],))
        attempt = self.store.one(
            "SELECT * FROM attempts WHERE id=?", (row["attempt_id"],)
        )
        evidence = self.store.one(
            "SELECT payload_json FROM evidence WHERE attempt_id=?", (row["attempt_id"],)
        )
        output = json.loads(evidence[0]).get("output") or "" if evidence else ""
        spec = json.loads(job["spec_json"])
        started = datetime.fromisoformat(attempt["started_at"])
        ended = datetime.fromisoformat(attempt["ended_at"] or now_iso())
        return {
            "step": job["kind"],
            "role": spec.get("role"),
            "cause": CATEGORY_TEXT[row["category"]],
            "error": row["detail"],
            "attempts_of_this_step": self.store.one(
                "SELECT COUNT(*) n FROM attempts WHERE job_id=?", (job["id"],)
            )["n"],
            "automatic_resumes_used": self._automatic_resumes(job["id"]),
            "automatic_resumes_allowed": self.cfg.max_resumes,
            "minutes_run": round((ended - started).total_seconds() / 60, 1),
            "time_limit_minutes": round(spec["timeout_seconds"] / 60, 1),
            "attempt_cost_cents": attempt["cost_cents"],
            "step_budget_cents": spec["budget_cents"],
            "work_spent_cents": root["spent_cents"],
            "work_ceiling_cents": root["spending_ceiling_cents"],
            "output_tail": output[-2000:],
        }

    def _ask_decider(self, row):
        """Advice from the configured decision agent, or None. Never raises."""
        decider = self.cfg.decider
        if not decider:
            return None
        cmd = self.cfg.commands[decider["command"]]
        try:
            result = self.runner.run_json(
                cmd.command,
                "decide",
                {
                    "state": self._interruption_state(row),
                    "questions": {"next": QUESTION},
                },
                new_id("req"),
                cmd.timeout_seconds,
            )
            answer = result["evidence"]["answers"]["next"]
            return {
                "agent": str(result["evidence"].get("agent", "decider")),
                "model": str(result["evidence"].get("model", "")),
                "decision": answer["choice"],
                "confidence": float(answer["confidence"]),
            }
        except Exception as exc:  # A decider never stops the factory.
            self.store.audit(
                "controller",
                "decider.unavailable",
                {"interruption": row["id"], "error": str(exc)[:500]},
            )
            return None

    def settle_interruptions(self, actor="controller"):
        """Resume trivial interruptions and hand the rest to a person."""
        settled = 0
        for row in self.store.query(
            "SELECT * FROM interruptions WHERE outcome='pending' ORDER BY at,id"
        ):
            # The decision agent runs outside any transaction.
            advice = self._ask_decider(row)
            with self.store.transaction(), self.decision():
                settled += self._apply_interruption(row["id"], advice, actor)
        return settled

    def _apply_interruption(self, interruption_id, advice, actor):
        row = self.store.one(
            "SELECT * FROM interruptions WHERE id=?", (interruption_id,)
        )
        if not row or row["outcome"] != "pending":
            return 0
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (row["job_id"],))
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (row["root_id"],))
        if job["status"] != "interrupted" or root["status"] in {
            "cancelled",
            "failed",
            "rejected",
            "accepted",
        }:
            self._close_interruption(row["id"], "superseded", actor, None)
            return 1
        verdict = decide(
            row["category"],
            self._automatic_resumes(job["id"]),
            self.cfg.max_resumes,
            self._resume_blockers(root, job),
            advice,
            self.cfg.decider.get("min_confidence", 0.8),
        )
        if verdict.action == "resume":
            self._close_interruption(row["id"], "auto_resumed", actor, verdict)
            self.store.execute(
                "UPDATE jobs SET status='ready' WHERE id=?", (job["id"],)
            )
            self.store.execute(
                "UPDATE work_items SET status='running' WHERE id=?", (root["id"],)
            )
        else:
            self._close_interruption(row["id"], "asked", actor, verdict)
            self.store.execute(
                "UPDATE work_items SET status='interrupted' WHERE id=?", (root["id"],)
            )
        self._packet(root["id"])
        self.reconcile_continuations()
        return 1

    def _close_interruption(self, interruption_id, outcome, actor, verdict):
        self.store.execute(
            "UPDATE interruptions SET outcome=?,actor=?,decided_at=?,verdict_json=COALESCE(?,verdict_json) WHERE id=?",
            (
                outcome,
                actor,
                now_iso(),
                json.dumps(vars(verdict)) if isinstance(verdict, Verdict) else None,
                interruption_id,
            ),
        )
        self.store.audit(
            actor,
            "interruption." + outcome,
            {
                "interruption": interruption_id,
                **({"verdict": vars(verdict)} if isinstance(verdict, Verdict) else {}),
            },
        )

    def _asked(self, wid):
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if not root or root["status"] != "interrupted":
            raise ControllerError("work is not waiting on an interrupted step")
        rows = self.store.query(
            "SELECT * FROM interruptions WHERE root_id=? AND outcome='asked'", (wid,)
        )
        if not rows:
            raise ControllerError("no interrupted step is waiting")
        return root, rows

    @atomic
    def continue_work(self, wid, actor):
        """A person resumes interrupted steps within the approved limits."""
        root, rows = self._asked(wid)
        self._require_human(actor, root)
        for row in rows:
            job = self.store.one("SELECT * FROM jobs WHERE id=?", (row["job_id"],))
            blockers = self._resume_blockers(root, job)
            if blockers:
                raise ControllerError("cannot resume: " + "; ".join(blockers))
            self._close_interruption(row["id"], "resumed", actor, None)
            self.store.execute(
                "UPDATE jobs SET status='ready' WHERE id=?", (job["id"],)
            )
        self.store.execute("UPDATE work_items SET status='running' WHERE id=?", (wid,))
        self._packet(wid)
        self.reconcile_continuations()

    @atomic
    def stop_work(self, wid, actor, reason="stopped after an interruption"):
        """A person stops interrupted work; it needs a new plan to continue."""
        root, rows = self._asked(wid)
        self._require_human(actor, root)
        for row in rows:
            self._close_interruption(row["id"], "stopped", actor, None)
            self.store.execute(
                "UPDATE jobs SET status='failed' WHERE id=?", (row["job_id"],)
            )
        self.store.audit(actor, "stop", {"work_item_id": wid, "reason": reason})
        self._fail_root(wid, reason)
        self.reconcile_continuations()

    def _resume_input(self, job_id, attempt_id):
        """Claim-time link from a resumed step to the attempt it continues."""
        row = self.store.one(
            "SELECT * FROM interruptions WHERE job_id=? AND outcome IN ('auto_resumed','resumed') AND resumed_by_attempt IS NULL ORDER BY at DESC LIMIT 1",
            (job_id,),
        )
        if not row:
            return None
        previous = self.store.one(
            "SELECT archive_path,result_json FROM attempts WHERE id=?",
            (row["attempt_id"],),
        )
        result = json.loads(previous["result_json"] or "{}")
        self.store.execute(
            "UPDATE interruptions SET resumed_by_attempt=? WHERE id=?",
            (attempt_id, row["id"]),
        )
        return {
            "attempt": row["attempt_id"],
            "archive": previous["archive_path"],
            "category": row["category"],
            "detail": row["detail"],
            "session_id": result.get("session_id"),
        }
