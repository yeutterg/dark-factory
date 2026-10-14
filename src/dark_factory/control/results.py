"""Attempt settlement: evidence checks, costs and bounded repair."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from dark_factory import artifacts
from dark_factory.control.base import ControllerError, atomic
from dark_factory.control.roles import parse_role
from dark_factory.store import new_id, now_iso


class ResultsMixin:
    """Settle attempt results and activate the preapproved repair branch."""

    def _fail_root(self, wid, error):
        self.store.execute(
            "UPDATE work_items SET status='failed',presentation_stage='Review' WHERE id=?",
            (wid,),
        )
        self._packet(wid, error)

    @atomic
    def finish_attempt(
        self,
        attempt_id,
        ok,
        output,
        cost_cents,
        archive_path,
        error=None,
        timed_out=False,
        cancelled=False,
    ):

        attempt = self.store.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        if not attempt:
            raise ControllerError("attempt not found")
        if attempt["status"] not in {"leased", "running", "cancelling"}:
            raise ControllerError(
                "attempt is no longer active; late or duplicate result rejected"
            )
        if type(cost_cents) is not int or cost_cents < 0:
            raise ControllerError("cost must be a non-negative integer")
        job = self.store.one("SELECT * FROM jobs WHERE id=?", (attempt["job_id"],))
        root = self.store.one(
            "SELECT * FROM work_items WHERE id=?", (job["work_item_id"],)
        )
        snapshot = json.loads(root["snapshot_json"])
        result = {}
        archive_digest = None
        if archive_path:
            expected = self.cfg.state_dir / "archives" / attempt_id
            try:
                if Path(archive_path).resolve() != expected.resolve():
                    raise ValueError("unexpected archive path")
                manifest = artifacts.verify(expected)
                if (
                    manifest["attempt_id"] != attempt_id
                    or manifest["job_id"] != job["id"]
                ):
                    raise ValueError("archive identity mismatch")
                result = json.loads((expected / "result.json").read_text())
                if result["inputs"] != json.loads(attempt["inputs_json"]):
                    raise ValueError("evidence inputs mismatch")
                if result.get("output") != output:
                    raise ValueError("output differs from archived evidence")
                archive_digest = artifacts.digest(expected / "manifest.json")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                ok = False
                error = f"invalid durable archive: {exc}"
        elif ok:
            ok = False
            error = "missing durable archive"
        timed_out = (
            timed_out
            or datetime.fromisoformat(attempt["deadline"]) <= datetime.now(timezone.utc)
            or (
                job["lease_until"]
                and datetime.fromisoformat(job["lease_until"])
                <= datetime.now(timezone.utc)
            )
        )
        cancelled = (
            cancelled
            or attempt["status"] == "cancelling"
            or root["status"] == "cancelled"
        )
        ok = bool(ok and not timed_out and not cancelled and result.get("ok", False))
        if job["kind"] not in {"plan", "critique"} and not self._approval_current(root):
            ok = False
            error = "approved inputs changed during attempt"
        parsed = None
        if ok and job["kind"] in {"plan", "critique", "review"}:
            try:
                parsed = self._parse_role(
                    job["kind"],
                    output,
                    snapshot,
                    json.loads(root["plan_json"]).get("proposal", {}),
                )
                if job["kind"] != "plan" and not parsed["passed"]:
                    ok = False
                    error = "blocking independent findings: " + json.dumps(
                        parsed["findings"]
                    )
            except (ValueError, KeyError, TypeError) as exc:
                ok = False
                error = f"invalid role result: {exc}"
        if cost_cents > json.loads(job["spec_json"])["budget_cents"]:
            ok = False
            error = "attempt spending exceeded reserved limit"
        if ok and job["kind"] == "implement":
            candidate = result.get("candidate", {})
            if not candidate.get("commit") or candidate.get("scope_violations"):
                ok = False
                error = "candidate missing or changed protected/out-of-scope paths"
            else:
                self.store.execute(
                    "UPDATE work_items SET candidate_commits=? WHERE id=?",
                    (
                        json.dumps(
                            {
                                root["repo"]: {
                                    "commit": candidate["commit"],
                                    "base": candidate["base"],
                                    "archive": archive_path,
                                    "diff_sha256": candidate["diff_sha256"],
                                }
                            }
                        ),
                        root["id"],
                    ),
                )
        status = (
            "cancelled"
            if cancelled
            else "timed_out"
            if timed_out
            else "succeeded"
            if ok
            else "failed"
        )
        self.store.execute(
            "UPDATE attempts SET status=?,ended_at=?,cost_cents=?,error=?,archive_path=?,result_json=? WHERE id=?",
            (
                status,
                now_iso(),
                cost_cents,
                error,
                archive_path,
                json.dumps(result),
                attempt_id,
            ),
        )
        self.store.execute(
            "UPDATE jobs SET status=?,worker_id=NULL,lease_until=NULL WHERE id=?",
            (status, job["id"]),
        )
        reservation = json.loads(job["spec_json"])["budget_cents"]
        self.store.execute(
            "UPDATE work_items SET reserved_cents=MAX(reserved_cents-?,0),spent_cents=spent_cents+? WHERE id=?",
            (reservation, cost_cents, root["id"]),
        )
        route = json.loads(attempt["inputs_json"]).get("route")
        self.store.execute(
            "INSERT INTO costs(id,root_id,attempt_id,source,amount_cents,currency,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id("cost"),
                root["id"],
                attempt_id,
                route["billing_account"] if route else "local-check",
                cost_cents,
                "USD",
                "estimated"
                if snapshot["simulation"]
                else result.get("cost_status", "reported" if not route else "unknown"),
                now_iso(),
            ),
        )
        self.store.execute(
            "INSERT INTO evidence(id,attempt_id,kind,path,digest,payload_json,inputs_json) VALUES (?,?,?,?,?,?,?)",
            (
                new_id("ev"),
                attempt_id,
                job["kind"],
                archive_path,
                archive_digest,
                json.dumps({"ok": ok, "output": output, "error": error}),
                attempt["inputs_json"],
            ),
        )
        self.store.execute(
            "UPDATE workers SET busy=0 WHERE id=?", (attempt["worker_id"],)
        )
        if ok and job["kind"] == "plan":
            packet = json.loads(root["plan_json"])
            packet["proposal"] = parsed
            self.store.execute(
                "UPDATE work_items SET plan_json=?,packet_json=? WHERE id=?",
                (json.dumps(packet), json.dumps(packet), root["id"]),
            )
        elif ok and job["kind"] == "critique":
            packet = json.loads(root["plan_json"])
            packet["critique"] = parsed
            self.store.execute(
                "UPDATE work_items SET critique_json=?,packet_json=?,status='awaiting_approval',presentation_stage='Plan' WHERE id=?",
                (json.dumps(parsed), json.dumps(packet), root["id"]),
            )
        elif ok and job["kind"] == "review":
            for optional in self.jobs(root["id"]):
                if optional["status"] == "standby":
                    self.store.execute(
                        "UPDATE jobs SET status='not_needed' WHERE id=?",
                        (optional["id"],),
                    )
            self.store.execute(
                "UPDATE work_items SET status='awaiting_review',presentation_stage='Review' WHERE id=?",
                (root["id"],),
            )
            self._packet(root["id"])
        elif not ok:
            correctness_failure = (
                archive_digest
                and not timed_out
                and not cancelled
                and cost_cents <= json.loads(job["spec_json"])["budget_cents"]
                and self._approval_current(root)
                and (
                    job["kind"] == "check"
                    and self._checks_authorize_repair(root, snapshot, result)
                    or job["kind"] == "review"
                    and parsed is not None
                    and not parsed["passed"]
                    and parsed["findings"]
                )
            )
            if not correctness_failure or not self._activate_repair(
                root["id"], job, attempt_id
            ):
                self._fail_root(root["id"], error or output or status)
            if cancelled:
                self.store.execute(
                    "UPDATE work_items SET status='cancelled' WHERE id=?", (root["id"],)
                )
        self.store.audit(
            attempt["worker_id"],
            "attempt.finish",
            {"attempt_id": attempt_id, "status": status, "archive": archive_path},
        )
        self.reconcile_continuations()

    def _checks_authorize_repair(self, root, snapshot, result):
        if result.get("failure_kind") != "canonical-check-failure":
            return False
        checks = result.get("checks")
        expected = snapshot["source"]["checks"]
        candidate = (
            json.loads(root["candidate_commits"]).get(root["repo"], {}).get("commit")
        )
        if not isinstance(checks, list) or len(checks) != len(expected):
            return False
        for cid, check in zip(expected, checks):
            if not isinstance(check, dict):
                return False
            # Repetition is valid only when explicitly present in the approved
            # sequence. No missing, extra or reordered execution can grant repair.
            if check.get("command") != cid:
                return False
            binding = snapshot["bindings"][cid]
            if (
                check.get("binding") != binding
                or check.get("argv") != binding["argv"]
                or check.get("candidate") != candidate
                or type(check.get("exit_code")) is not int
                or check["exit_code"] < 0
            ):
                return False
        return bool(candidate) and any(check["exit_code"] != 0 for check in checks)

    def _activate_repair(self, wid, failed_job, attempt_id):
        if json.loads(failed_job["spec_json"]).get("repair"):
            return False
        repairs = [
            j for j in self.jobs(wid) if json.loads(j["spec_json"]).get("repair")
        ]
        if not repairs or any(j["status"] != "standby" for j in repairs):
            return False
        # Every repair and follow-up was in the original approved graph. Failed evidence
        # stays archived; only a fresh candidate plus checks/review can satisfy acceptance.
        for job in self.jobs(wid):
            if job["kind"] in {"check", "review"} and not json.loads(
                job["spec_json"]
            ).get("repair"):
                self.store.execute(
                    "UPDATE jobs SET status='superseded_by_repair' WHERE id=?",
                    (job["id"],),
                )
        for job in repairs:
            self.store.execute(
                "UPDATE jobs SET status='ready' WHERE id=?", (job["id"],)
            )
        self.store.execute(
            "UPDATE work_items SET status='running',presentation_stage='Build' WHERE id=?",
            (wid,),
        )
        self.store.audit(
            "controller",
            "repair.activate",
            {
                "root": wid,
                "failed_attempt": attempt_id,
                "repair_jobs": [j["id"] for j in repairs],
            },
        )
        self._packet(wid)
        return True

    def required_jobs(self, wid):
        jobs = self.jobs(wid)
        repair_active = any(
            json.loads(j["spec_json"]).get("repair")
            and j["status"] not in {"standby", "not_needed"}
            for j in jobs
        )
        return [
            j
            for j in jobs
            if not (
                json.loads(j["spec_json"]).get("repair")
                and j["status"] in {"standby", "not_needed"}
                or repair_active
                and j["kind"] in {"check", "review"}
                and j["status"] == "superseded_by_repair"
            )
        ]

    _parse_role = staticmethod(parse_role)
