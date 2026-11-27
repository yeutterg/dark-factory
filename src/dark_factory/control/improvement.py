"""The improvement loop: the one class of work the factory may originate.

It runs the ordinary pipeline. What differs is who approves (a standing operator
policy, bound to the profile digest and limited in code), where it may change
files (a narrow scope with a small diff cap) and where it ends (a draft pull
request and the usual human review). Merge, acceptance and installation are
never covered by the policy.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dark_factory.control.base import ControllerCore, ControllerError
from dark_factory.improvement import collect_signals
from dark_factory.models import TERMINAL_ROOT, sql_set
from dark_factory.store import new_id, now_iso

PACKAGED = ["src/dark_factory/agents/prompts", "src/dark_factory/agents/skills"]
# The output contracts are code-owned: no standing policy may edit them.
CONTRACTS = [
    f"src/dark_factory/agents/prompts/{role}.contract.md"
    for role in ("planner", "critic", "coder", "reviewer")
]
ORIGIN = "improvement:"
POLICY_ACTOR = "policy:improvement"


def _covers(outer: str, inner: str) -> bool:
    return inner == outer or inner.startswith(outer.rstrip("/") + "/")


def _intersect(first: list[str], second: list[str]) -> list[str]:
    """Paths allowed by both lists; where one is inside the other, the narrower wins."""
    out = []
    for a in first:
        for b in second:
            if _covers(b, a):
                out.append(a)
            elif _covers(a, b):
                out.append(b)
    return list(dict.fromkeys(out))


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40] or "signal"


class ImprovementMixin(ControllerCore):
    """Detect recurring failures and carry one scoped change to a draft PR."""

    # ---- settings ------------------------------------------------------------

    def _improvement_target(self):
        """(repository, why not) for the repository the loop may change."""
        settings = self.cfg.improvement
        if not settings["enabled"]:
            return None, "disabled"
        repo = settings["repository"]
        if not repo:
            return None, "no target repository: set improvement.repository"
        simulation = any(
            r.evidence_kind == "simulation" for r in self.cfg.routes.values()
        )
        if repo not in self.cfg.sources and not simulation:
            return None, "the target repository has no configured source"
        if repo in self.cfg.sources:
            try:
                self.improvement_scope(self.cfg.sources[repo])
            except ControllerError as exc:
                return None, str(exc)
        return repo, ""

    def improvement_scope(self, source, fixture=False):
        """The source as the standing policy may use it.

        Eligible paths are the factory definition's prompts and skills (or the packaged
        ones), intersected with the repository's own allowlist and with any narrower
        operator restriction. The policy can only narrow what the repository allows,
        never widen it; protected paths stay in force, and the code-owned output
        contracts are always protected.
        """
        settings = self.cfg.improvement
        eligible = self._eligible_paths(source, fixture)
        allowed = _intersect(eligible, source["allowed_paths"])
        if settings["allowed_paths"]:
            allowed = _intersect(allowed, settings["allowed_paths"])
        if not allowed:
            raise ControllerError(
                "no eligible prompt or skill path is inside the repository's allowlist "
                "and the improvement restriction"
            )
        return {
            **source,
            "allowed_paths": allowed,
            "protected_paths": list(
                dict.fromkeys(
                    [
                        *source.get("protected_paths", []),
                        *settings["protected_paths"],
                        *CONTRACTS,
                    ]
                )
            ),
            "diff_limits": {
                "max_files": settings["max_files"],
                "max_changed_lines": settings["max_changed_lines"],
            },
        }

    def _eligible_paths(self, source, fixture):
        """Prompt and skill paths in the target checkout; nothing else is eligible."""
        if fixture:
            # A simulation fixture has no real definition; it exercises the machinery.
            return list(source["allowed_paths"])
        checkout = Path(source["path"]).resolve()
        root = self.cfg.definition.get("root")
        if not root or not Path(root).resolve().is_relative_to(checkout):
            return list(PACKAGED)
        base = Path(root).resolve().relative_to(checkout)
        definition = self.cfg.definition
        paths = [
            (base / agent["prompt"]).as_posix()
            for agent in definition.get("agents", [])
            if agent.get("selected") and agent.get("role") and agent.get("prompt")
        ]
        skills = [
            *definition.get("shared_skills", []),
            *(p for group in definition.get("role_skills", {}).values() for p in group),
        ]
        for skill in skills:
            resolved = Path(skill).resolve()
            if resolved.is_relative_to(checkout):
                paths.append(resolved.relative_to(checkout).as_posix())
        return list(dict.fromkeys(paths))

    # ---- signals -------------------------------------------------------------

    def refresh_improvement_signals(self):
        """Persist new evidence. Skipped unless a count of the records it reads moved."""
        fingerprint = json.dumps(
            [
                self.store.one(
                    "SELECT COUNT(*) FROM attempts WHERE status IN ('failed','timed_out')"
                )[0],
                self.store.one("SELECT COUNT(*) FROM interruptions")[0],
                self.store.one("SELECT COUNT(*) FROM audit WHERE action='reject'")[0],
            ]
        )
        if self.store.get_op("improvement:seen", "") == fingerprint:
            return 0
        added = 0
        with self.store.transaction():
            for row in collect_signals(self.store):
                cursor = self.store.execute(
                    "INSERT OR IGNORE INTO improvement_signals"
                    "(signal,root_id,attempt_id,label,roles,detail,at) VALUES (?,?,?,?,?,?,?)",
                    (
                        row["signal"],
                        row["root_id"],
                        row["attempt_id"],
                        row["label"],
                        json.dumps(list(row["roles"])),
                        row["detail"],
                        row["at"] or now_iso(),
                    ),
                )
                added += cursor.rowcount
            self.store.set_op("improvement:seen", fingerprint, "controller")
        return added

    # ---- the tick ------------------------------------------------------------

    def improvement_tick(self, actor="improvement"):
        """One cheap pass: refresh evidence, start at most one root, advance open ones."""
        repo, reason = self._improvement_target()
        if repo is None:
            return {"state": reason}
        self.refresh_improvement_signals()
        created = self._maybe_create_root(repo, actor)
        advanced = [self._advance_improvement(root) for root in self._open_roots()]
        return {"created": created, "advanced": [a for a in advanced if a]}

    def _open_roots(self):
        return self.store.query(
            "SELECT * FROM work_items WHERE origin LIKE 'improvement:%' "
            f"AND status NOT IN {sql_set(TERMINAL_ROOT)} ORDER BY created_at"
        )

    def _maybe_create_root(self, repo, actor):
        settings = self.cfg.improvement
        if len(self._open_roots()) >= settings["max_open"]:
            return None
        cooldown = (
            datetime.now(UTC) - timedelta(days=settings["cooldown_days"])
        ).isoformat()
        signals = self.store.query(
            "SELECT signal,COUNT(*) n,MIN(label) label FROM improvement_signals "
            "GROUP BY signal HAVING n>=? ORDER BY n DESC,signal",
            (settings["threshold"],),
        )
        for item in signals:
            last = self.store.one(
                "SELECT created_at FROM work_items WHERE origin=? ORDER BY created_at DESC LIMIT 1",
                (ORIGIN + item["signal"],),
            )
            if last and (last["created_at"] or "") > cooldown:
                fresh = self.store.one(
                    "SELECT COUNT(*) n FROM improvement_signals WHERE signal=? AND at>?",
                    (item["signal"], last["created_at"]),
                )["n"]
                if fresh < settings["threshold"]:
                    continue  # Suppressed until there is new evidence.
            return self._create_root(repo, item["signal"], item["label"], actor)
        return None

    def _create_root(self, repo, signal, label, actor):
        evidence = [
            dict(r)
            for r in self.store.query(
                "SELECT root_id,attempt_id,detail,at FROM improvement_signals "
                "WHERE signal=? ORDER BY at",
                (signal,),
            )
        ]
        title = f"Improve factory: {label}"
        lines = [
            f"Dark Factory observed `{signal}` in {len(evidence)} distinct root outcomes.",
            "",
            "Make one narrowly scoped change to the factory definition (prompts or "
            "skills) that addresses this recurring failure. Do not change application "
            "behaviour, permissions, budgets, routes or the running controller.",
            "",
            "Evidence:",
            *(f"- root {e['root_id']}: {e['detail']}" for e in evidence[:10]),
        ]
        wid = new_id("wi")
        count = self.store.one(
            "SELECT COUNT(*) n FROM work_items WHERE origin=?", (ORIGIN + signal,)
        )["n"]
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO work_items
                (id,external_id,url,title,body,repo,status,is_root,created_at,origin)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    wid,
                    f"improvement:{slug(signal)}:{count + 1}",
                    "",
                    title,
                    "\n".join(lines),
                    repo,
                    "selected",
                    1,
                    now_iso(),
                    ORIGIN + signal,
                ),
            )
            self.store.audit(
                actor,
                "improvement.created",
                {"root": wid, "signal": signal, "evidence_roots": len(evidence)},
            )
        try:
            self.start_planning(wid, actor)
        except Exception as exc:
            # A root that cannot even be planned must not hold the only slot forever.
            self.store.execute(
                "UPDATE work_items SET status='failed' WHERE id=?", (wid,)
            )
            self.store.audit(
                actor, "improvement.error", {"root": wid, "error": str(exc)[:300]}
            )
            return None
        return wid

    def _advance_improvement(self, root):
        status = root["status"]
        if status == "awaiting_approval" and not root["approved_digest"]:
            return self._approve_by_policy(root)
        if status == "awaiting_review":
            return self._publish(root)
        return None

    # ---- approval by standing policy ------------------------------------------

    def _policy_blockers(self, root):
        """Why the standing policy cannot approve this plan; a person decides instead."""
        blockers = []
        try:
            if not json.loads(root["critique_json"] or "{}").get("passed"):
                blockers.append("the critique did not pass")
            proposal = json.loads(root["plan_json"] or "{}").get("proposal", {})
        except ValueError:
            return ["the plan could not be read"]
        if any(q.get("blocking") for q in proposal.get("needs_input", [])):
            blockers.append("the planner asked a blocking question")
        snapshot = json.loads(root["snapshot_json"])
        if not snapshot["source"].get("allowed_paths"):
            blockers.append("the improvement scope has no allowed paths")
        if snapshot["source"].get("diff_limits") is None:
            blockers.append("the diff limits are missing")
        if snapshot["policy"] != self._policy():
            blockers.append("the profile changed since planning")
        return blockers

    def _approve_by_policy(self, root):
        wid = root["id"]
        held = "improvement:held:" + wid
        blockers = self._policy_blockers(root)
        if blockers:
            if self.store.get_op(held, "") != json.dumps(blockers):
                self.store.set_op(held, json.dumps(blockers), "controller")
                self.store.audit(
                    POLICY_ACTOR,
                    "improvement.needs_human",
                    {"root": wid, "why": blockers},
                )
            return None
        self.approve_standing(wid)
        return {"root": wid, "approved": POLICY_ACTOR}

    # ---- the draft pull request -----------------------------------------------

    def _publish(self, root):
        signal = root["origin"].removeprefix(ORIGIN)
        candidate = json.loads(root["candidate_commits"])[root["repo"]]
        evidence = [
            dict(r)
            for r in self.store.query(
                "SELECT root_id,detail FROM improvement_signals WHERE signal=? ORDER BY at",
                (signal,),
            )
        ]
        body = "\n".join(
            [
                "Opened by Dark Factory's improvement loop as a **draft**. A person "
                "reviews, merges or closes it; nothing here is installed automatically.",
                "",
                f"Recurring failure: `{signal}` in {len(evidence)} root outcomes.",
                *(f"- {e['root_id']}: {e['detail']}" for e in evidence[:10]),
            ]
        )
        return self._publish_candidate(
            root,
            f"dark-factory/improvement/{slug(signal)}-{candidate['commit'][:8]}",
            root["title"],
            body,
            POLICY_ACTOR,
            "improvement:published:" + root["id"],
        )

    def improvement_status(self):
        repo, reason = self._improvement_target()
        return {
            "enabled": repo is not None,
            "state": reason or "on",
            "settings": self.cfg.improvement,
            "open": [r["id"] for r in self._open_roots()],
        }
