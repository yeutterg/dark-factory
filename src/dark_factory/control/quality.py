"""Evidence-only quality memory feeding human-approved self-improvement."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime

from dark_factory.control.base import ControllerCore, ControllerError
from dark_factory.redaction import redact
from dark_factory.store import new_id, now_iso

CATEGORIES = frozenset(
    {
        "architecture",
        "verification",
        "testing",
        "types",
        "agent instruction",
        "tooling",
        "process",
    }
)
CONFIDENCE = frozenset({"low", "medium", "high"})
LAYERS = frozenset(
    {
        "architecture",
        "types or APIs",
        "deterministic enforcement",
        "verification workflow",
        "agent instruction",
    }
)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _bounded(value, limit=3000):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return redact(text)[:limit]


def _compact_record(value, limit=2000):
    """Keep identifiers intact while bounding untrusted evidence text."""
    return {
        key: (_bounded(item, limit) if isinstance(item, (str, dict, list)) else item)
        for key, item in value.items()
    }


class QualityMixin(ControllerCore):
    """Observe, synthesize, then wait for a person before improvement work."""

    def _quality_path(self, repo):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "__", repo)
        return self.cfg.state_dir / "quality" / safe / ".agent-observations"

    def _quality_last(self, repo, kind):
        row = self.store.one(
            """SELECT ended_at FROM quality_runs WHERE repo=? AND kind=?
            AND status='complete' ORDER BY ended_at DESC LIMIT 1""",
            (repo, kind),
        )
        return row["ended_at"] if row else ""

    def recover_quality(self):
        """A provider call cannot survive controller restart; make that explicit."""
        self.store.execute(
            """UPDATE quality_runs SET status='failed',ended_at=?,
            error='controller restarted during quality run' WHERE status='running'""",
            (now_iso(),),
        )

    def _quality_evidence(self, repo, since):
        evidence = []
        available = set()
        roots = {
            row["id"]: row
            for row in self.store.query(
                "SELECT * FROM work_items WHERE repo=? AND origin NOT LIKE 'improvement:%'",
                (repo,),
            )
        }
        for root in roots.values():
            latest_attempt = self.store.one(
                """SELECT MAX(a.ended_at) ended_at FROM attempts a
                JOIN jobs j ON j.id=a.job_id WHERE j.work_item_id=?""",
                (root["id"],),
            )
            if (
                since
                and (root["created_at"] or "") <= since
                and (root["accepted_at"] or "") <= since
                and ((latest_attempt and latest_attempt["ended_at"]) or "") <= since
            ):
                continue
            candidate = json.loads(root["candidate_commits"] or "{}").get(repo, {})
            if not candidate:
                continue
            diff = ""
            archive = candidate.get("archive")
            if archive:
                path = self.store.resolve_path(archive) / "diff.patch"
                if path.is_file() and not path.is_symlink():
                    try:
                        with path.open(errors="replace") as source:
                            diff = redact(source.read(12000))
                        available.add("diffs")
                    except OSError:
                        pass
            evidence.append(
                {
                    "id": "candidate:" + root["id"],
                    "kind": "agent_change",
                    "root_id": root["id"],
                    "commit": candidate.get("commit"),
                    "status": root["status"],
                    "diff": diff,
                }
            )
        for row in self.store.query(
            """SELECT e.root_id,e.pr_number,e.url,e.at,e.payload_json
            FROM external_reviews e JOIN work_items w ON w.id=e.root_id
            WHERE w.repo=? AND (?='' OR e.at>?) ORDER BY e.at LIMIT 100""",
            (repo, since, since),
        ):
            evidence.append(
                {
                    "id": f"pr:{row['root_id']}:{row['pr_number']}:{row['at']}",
                    "kind": "pull_request_review",
                    "root_id": row["root_id"],
                    "url": row["url"],
                    "detail": _bounded(json.loads(row["payload_json"])),
                }
            )
            available.update({"PRs", "review comments"})
        for row in self.store.query(
            """SELECT a.id,a.ended_at,a.status,a.error,a.result_json,j.kind,j.work_item_id
            FROM attempts a JOIN jobs j ON j.id=a.job_id
            JOIN work_items w ON w.id=j.work_item_id WHERE w.repo=?
            AND a.status IN ('failed','timed_out') AND (?='' OR a.ended_at>?)
            ORDER BY a.ended_at LIMIT 100""",
            (repo, since, since),
        ):
            evidence.append(
                {
                    "id": "attempt:" + row["id"],
                    "kind": "ci_or_agent_failure",
                    "root_id": row["work_item_id"],
                    "stage": row["kind"],
                    "status": row["status"],
                    "error": _bounded(row["error"] or ""),
                    "result": _bounded(json.loads(row["result_json"] or "{}")),
                }
            )
            available.add("CI failures")
        for row in self.store.query(
            """SELECT t.id,t.root_id,t.kind,t.payload_json,t.created_at
            FROM task_events t JOIN work_items w ON w.id=t.root_id
            WHERE w.repo=? AND (?='' OR t.created_at>?) ORDER BY t.created_at LIMIT 100""",
            (repo, since, since),
        ):
            evidence.append(
                {
                    "id": "task-event:" + row["id"],
                    "kind": "task_transcript",
                    "root_id": row["root_id"],
                    "event": row["kind"],
                    "detail": _bounded(json.loads(row["payload_json"])),
                }
            )
            available.add("task transcripts")
        for row in self.store.query(
            """SELECT a.id,a.at,a.action,a.payload_json FROM audit a
            WHERE a.action IN ('reject','manual_gate','external.read')
            AND (?='' OR a.at>?) ORDER BY a.at LIMIT 100""",
            (since, since),
        ):
            payload = json.loads(row["payload_json"])
            root_id = payload.get("work_item_id") or payload.get("root")
            if root_id not in roots:
                continue
            evidence.append(
                {
                    "id": "audit:" + row["id"],
                    "kind": "human_correction",
                    "root_id": root_id,
                    "action": row["action"],
                    "detail": _bounded(payload),
                }
            )
            available.add("human corrections")
        inaccessible = [
            name
            for name in (
                "PRs",
                "diffs",
                "review comments",
                "CI failures",
                "task transcripts",
                "human corrections",
            )
            if name not in available
        ]
        return evidence[:300], inaccessible

    def quality_tick(self, actor="quality-observer", force=False, only_repo=None):
        settings = self.cfg.quality_observer
        if not settings.get("enabled"):
            return {"state": "disabled"}
        if only_repo is not None and only_repo not in settings["repositories"]:
            raise ControllerError("repository is not enabled for quality observation")
        results = []
        now = time.time()
        repositories = [only_repo] if only_repo else settings["repositories"]
        for repo in repositories:
            last_observer = self._quality_last(repo, "observer")
            due = (
                not last_observer
                or now - datetime.fromisoformat(last_observer).timestamp()
                >= settings["observer_interval_seconds"]
            )
            pr_count = self.store.one(
                """SELECT COUNT(DISTINCT e.root_id) n FROM external_reviews e
                JOIN work_items w ON w.id=e.root_id
                WHERE w.repo=? AND (?='' OR e.at>?)""",
                (repo, last_observer, last_observer),
            )["n"]
            if force or due or pr_count >= settings["after_agent_prs"]:
                results.append(self._quality_observe(repo, actor))
            last_pattern = self._quality_last(repo, "patterns")
            pattern_due = (
                not last_pattern
                or now - datetime.fromisoformat(last_pattern).timestamp()
                >= settings["pattern_interval_seconds"]
            )
            count = self.store.one(
                "SELECT COUNT(*) n FROM quality_observations WHERE repo=?", (repo,)
            )["n"]
            if count >= 2 and (force or pattern_due):
                results.append(self._quality_patterns(repo, actor))
        return {"runs": results}

    def _quality_input(self, fixed, records_key, records):
        """Fit bounded records to the configured input envelope deterministically."""
        # Reserve a conservative envelope for the code-owned system prompt and
        # structured-output schema added by the adapter.
        limit = self.cfg.quality_observer["max_input_tokens"] * 2 - 4000
        state = dict(fixed)
        for key, value in list(state.items()):
            if not isinstance(value, list):
                continue
            state[key] = [_compact_record(item) for item in value]
            while (
                state[key]
                and len(json.dumps(state, sort_keys=True).encode()) > limit // 3
            ):
                state[key].pop(0)
        state[records_key] = []
        omitted = 0
        for record in records:
            candidate = _compact_record(record)
            state[records_key].append(candidate)
            if len(json.dumps(state, sort_keys=True).encode()) > limit:
                state[records_key].pop()
                omitted += 1
        return state, omitted

    def _quality_call(self, repo, kind, operation, state, actor):
        settings = self.cfg.quality_observer
        run_id = new_id("qrun")
        try:
            self.store.execute(
                """INSERT INTO quality_runs
                (id,repo,kind,status,started_at,evidence_count)
                VALUES (?,?,?,?,?,?)""",
                (
                    run_id,
                    repo,
                    kind,
                    "running",
                    now_iso(),
                    len(state.get("evidence", state.get("observations", []))),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        command = self.cfg.commands[settings["command"]]
        try:
            result = self.runner.run_json(
                command.command,
                operation,
                {
                    **state,
                    "repo": repo,
                    "limits": {
                        "max_input_tokens": settings["max_input_tokens"],
                        "max_output_tokens": settings["max_output_tokens"],
                        "budget_cents": settings["budget_cents"],
                    },
                },
                run_id,
                command.timeout_seconds,
            )
            evidence = result["evidence"]
            cost = int(evidence.get("usage", {}).get("cost_cents", 0))
            if cost > settings["budget_cents"]:
                raise ControllerError("quality observer exceeded its per-run budget")
            model = evidence.get("model", "")
            if model != settings["model"]:
                raise ControllerError(
                    "quality observer response model differs from configured model"
                )
            output = evidence.get("output")
            if not isinstance(output, dict):
                raise ControllerError("quality observer returned no structured output")
            return run_id, output, model, cost
        except Exception as exc:
            self.store.execute(
                "UPDATE quality_runs SET status='failed',ended_at=?,error=? WHERE id=?",
                (now_iso(), str(exc)[:500], run_id),
            )
            self.store.audit(
                actor,
                "quality.error",
                {"repo": repo, "kind": kind, "error": str(exc)[:300]},
            )
            raise

    def _quality_observe(self, repo, actor):
        since = self._quality_last(repo, "observer")
        evidence, inaccessible = self._quality_evidence(repo, since)
        existing = [
            {
                "id": row["id"],
                "title": row["title"],
                "evidence": json.loads(row["evidence_json"]),
            }
            for row in self.store.query(
                "SELECT id,title,evidence_json FROM quality_observations WHERE repo=? ORDER BY created_at",
                (repo,),
            )
        ]
        if not evidence:
            run_id = new_id("qrun")
            self.store.execute(
                """INSERT INTO quality_runs
                (id,repo,kind,status,started_at,ended_at,inaccessible_json)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    run_id,
                    repo,
                    "observer",
                    "complete",
                    now_iso(),
                    now_iso(),
                    json.dumps(inaccessible),
                ),
            )
            self._write_quality_queue(repo)
            self.store.audit(
                actor,
                "quality.observed",
                {
                    "repo": repo,
                    "new": 0,
                    "duplicates": 0,
                    "inaccessible": inaccessible,
                },
            )
            return {
                "repo": repo,
                "kind": "observer",
                "new": 0,
                "duplicates": 0,
                "inaccessible": inaccessible,
            }
        state, omitted = self._quality_input(
            {
                "since": since or None,
                "existing_observations": existing[-100:],
            },
            "evidence",
            evidence,
        )
        if omitted:
            inaccessible.append(f"{omitted} evidence records exceeded the input limit")
        called = self._quality_call(
            repo,
            "observer",
            "observe_quality",
            state,
            actor,
        )
        if called is None:
            return {"repo": repo, "kind": "observer", "state": "already_running"}
        run_id, output, model, cost = called
        known = {item["id"]: item for item in evidence}
        added = duplicates = 0
        for item in output.get("observations", []):
            if not isinstance(item, dict):
                continue
            ids = sorted(set(item.get("evidence_ids", [])))
            if not ids or any(value not in known for value in ids):
                continue
            required = ("title", "what_happened", "why_it_matters")
            if (
                item.get("category") not in CATEGORIES
                or item.get("confidence") not in CONFIDENCE
                or any(
                    not isinstance(item.get(key), str) or not item[key].strip()
                    for key in required
                )
            ):
                continue
            fingerprint = _digest(
                {"evidence": ids, "what": item.get("what_happened", "")}
            )
            roots = sorted(
                {
                    known[value].get("root_id")
                    for value in ids
                    if known[value].get("root_id")
                }
            )
            similar = item.get("similar_observation_ids", [])
            if not isinstance(similar, list):
                similar = []
            existing_ids = {value["id"] for value in existing}
            similar = sorted(
                {
                    value
                    for value in similar
                    if isinstance(value, str) and value in existing_ids
                }
            )
            cursor = self.store.execute(
                """INSERT OR IGNORE INTO quality_observations
                (id,repo,fingerprint,observed_at,title,evidence_json,root_ids_json,
                what_happened,why_it_matters,category,similar_json,confidence,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("obs"),
                    repo,
                    fingerprint,
                    now_iso(),
                    _bounded(item.get("title", ""), 200),
                    json.dumps(ids),
                    json.dumps(roots),
                    _bounded(item.get("what_happened", "")),
                    _bounded(item.get("why_it_matters", "")),
                    item["category"],
                    json.dumps(similar),
                    item["confidence"],
                    now_iso(),
                ),
            )
            added += cursor.rowcount
            duplicates += 1 - cursor.rowcount
        model_inaccessible = output.get("inaccessible_evidence", [])
        if not isinstance(model_inaccessible, list):
            model_inaccessible = []
        inaccessible = sorted(
            set(
                inaccessible
                + [
                    _bounded(item, 200)
                    for item in model_inaccessible
                    if isinstance(item, str) and item.strip()
                ]
            )
        )
        self.store.execute(
            """UPDATE quality_runs SET status='complete',ended_at=?,new_count=?,duplicate_count=?,
            inaccessible_json=?,model=?,cost_cents=? WHERE id=?""",
            (
                now_iso(),
                added,
                duplicates,
                json.dumps(inaccessible),
                model,
                cost,
                run_id,
            ),
        )
        self._write_quality_queue(repo)
        self.store.audit(
            actor,
            "quality.observed",
            {
                "repo": repo,
                "new": added,
                "duplicates": duplicates,
                "inaccessible": inaccessible,
            },
        )
        return {
            "repo": repo,
            "kind": "observer",
            "new": added,
            "duplicates": duplicates,
            "inaccessible": inaccessible,
        }

    def _quality_patterns(self, repo, actor):
        observations = [
            dict(row)
            for row in self.store.query(
                "SELECT * FROM quality_observations WHERE repo=? ORDER BY created_at",
                (repo,),
            )
        ]
        state, omitted = self._quality_input(
            {},
            "observations",
            [
                {
                    **row,
                    "evidence": json.loads(row["evidence_json"]),
                    "root_ids": json.loads(row["root_ids_json"]),
                }
                for row in observations
            ],
        )
        called = self._quality_call(
            repo,
            "patterns",
            "analyze_quality_patterns",
            state,
            actor,
        )
        if called is None:
            return {"repo": repo, "kind": "patterns", "state": "already_running"}
        run_id, output, model, cost = called
        by_id = {row["id"]: row for row in observations}
        added = duplicates = 0
        for item in output.get("patterns", []):
            if not isinstance(item, dict):
                continue
            supporting = sorted(set(item.get("supporting_observation_ids", [])))
            required = (
                "name",
                "shared_root_cause",
                "why_repeating",
                "smallest_systemic_change",
                "verification",
                "uncertainty",
            )
            if (
                len(supporting) < 2
                or any(value not in by_id for value in supporting)
                or any(
                    not isinstance(item.get(key), str) or not item[key].strip()
                    for key in required
                )
            ):
                continue
            evidence_ids = {
                value
                for oid in supporting
                for value in json.loads(by_id[oid]["evidence_json"])
            }
            root_ids = {
                value
                for oid in supporting
                for value in json.loads(by_id[oid]["root_ids_json"])
            }
            if (
                len(evidence_ids) < 2
                or len(root_ids) < 2
                or item.get("prevention_layer") not in LAYERS
                or item.get("confidence") not in CONFIDENCE
            ):
                continue
            fingerprint = _digest(supporting)
            cursor = self.store.execute(
                """INSERT OR IGNORE INTO quality_patterns
                (id,repo,fingerprint,analyzed_at,name,supporting_json,root_cause,
                repetition_reason,prevention_layer,systemic_change,verification,
                confidence,uncertainty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("pat"),
                    repo,
                    fingerprint,
                    now_iso(),
                    _bounded(item.get("name", ""), 200),
                    json.dumps(supporting),
                    _bounded(item.get("shared_root_cause", "")),
                    _bounded(item.get("why_repeating", "")),
                    item["prevention_layer"],
                    _bounded(item.get("smallest_systemic_change", "")),
                    _bounded(item.get("verification", "")),
                    item["confidence"],
                    _bounded(item.get("uncertainty", "")),
                ),
            )
            added += cursor.rowcount
            duplicates += 1 - cursor.rowcount

        def strings(name, maximum=None):
            values = output.get(name, [])
            if not isinstance(values, list):
                return []
            result = [
                _bounded(value, 500) for value in values if isinstance(value, str)
            ]
            return result[:maximum] if maximum else result

        summary = {
            "top_interventions": strings("top_interventions", 3),
            "isolated_observation_ids": [
                value for value in strings("isolated_observation_ids") if value in by_id
            ],
            "additional_evidence_needed": strings("additional_evidence_needed"),
            "omitted_observations": omitted,
        }
        self.store.set_op("quality:pattern-summary:" + repo, json.dumps(summary), actor)
        self.store.execute(
            """UPDATE quality_runs SET status='complete',ended_at=?,new_count=?,duplicate_count=?,
            model=?,cost_cents=? WHERE id=?""",
            (now_iso(), added, duplicates, model, cost, run_id),
        )
        self._write_pattern_report(repo, summary)
        self.store.audit(
            actor,
            "quality.patterns",
            {"repo": repo, "new": added, "duplicates": duplicates},
        )
        return {
            "repo": repo,
            "kind": "patterns",
            "new": added,
            "duplicates": duplicates,
        }

    def _write_quality_queue(self, repo):
        path = self._quality_path(repo)
        path.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Agent observation queue",
            "",
            "Evidence queue only; no finding here authorizes a fix.",
            "",
        ]
        for row in self.store.query(
            "SELECT * FROM quality_observations WHERE repo=? ORDER BY created_at",
            (repo,),
        ):
            similar = json.loads(row["similar_json"])
            lines.extend(
                [
                    f"## Observation: {row['title']}",
                    f"- Date: {row['observed_at']}",
                    f"- Evidence: {', '.join(json.loads(row['evidence_json']))}",
                    f"- What happened: {row['what_happened']}",
                    f"- Why it may matter: {row['why_it_matters']}",
                    f"- Likely category: {row['category']}",
                    f"- Similar observations: {', '.join(similar) if similar else 'none found'}",
                    f"- Confidence: {row['confidence']}",
                    "",
                ]
            )
        self._write_quality_file(path / "queue.md", lines)

    def _write_pattern_report(self, repo, summary):
        path = self._quality_path(repo)
        path.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Agent quality pattern report",
            "",
            "Recommendations require human approval.",
            "",
        ]
        for row in self.store.query(
            "SELECT * FROM quality_patterns WHERE repo=? ORDER BY analyzed_at", (repo,)
        ):
            lines.extend(
                [
                    f"## Pattern: {row['name']} ({row['id']})",
                    f"- Supporting observations: {', '.join(json.loads(row['supporting_json']))}",
                    f"- Shared root cause: {row['root_cause']}",
                    f"- Why agents are likely repeating it: {row['repetition_reason']}",
                    f"- Recommended prevention layer: {row['prevention_layer']}",
                    f"- Smallest effective systemic change: {row['systemic_change']}",
                    f"- How to verify: {row['verification']}",
                    f"- Confidence: {row['confidence']}",
                    f"- Unresolved uncertainty: {row['uncertainty']}",
                    f"- Human approval: {row['approved_by'] or 'pending'}",
                    "",
                ]
            )
        lines.extend(
            [
                "## Three highest-leverage interventions",
                *[f"- {x}" for x in summary["top_interventions"]],
                "",
                "## Isolated observations",
                *(f"- {x}" for x in summary["isolated_observation_ids"]),
                "",
                "## Additional evidence needed",
                *(f"- {x}" for x in summary["additional_evidence_needed"]),
                "",
            ]
        )
        if summary.get("omitted_observations"):
            lines.extend(
                [
                    "",
                    f"- {summary['omitted_observations']} observations exceeded the input limit",
                ]
            )
        self._write_quality_file(path / "pattern-report.md", lines)

    def _write_quality_file(self, destination, lines):
        temporary = destination.with_name(destination.name + "." + new_id("tmp"))
        try:
            temporary.write_text("\n".join(lines), encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def quality_status(self, repo=None):
        enabled = self.cfg.quality_observer.get("repositories", [])
        if repo is not None and repo not in enabled:
            raise ControllerError("repository is not enabled for quality observation")
        repos = [repo] if repo else enabled
        status = {}
        for name in repos:
            runs = [
                dict(row)
                for row in self.store.query(
                    """SELECT kind,status,ended_at,new_count,duplicate_count,
                    inaccessible_json,model,cost_cents,error FROM quality_runs
                    WHERE repo=? ORDER BY started_at DESC LIMIT 2""",
                    (name,),
                )
            ]
            for run in runs:
                run["inaccessible"] = json.loads(run.pop("inaccessible_json"))
            status[name] = {
                "queue": str(self._quality_path(name) / "queue.md"),
                "report": str(self._quality_path(name) / "pattern-report.md"),
                "observations": self.store.one(
                    "SELECT COUNT(*) n FROM quality_observations WHERE repo=?", (name,)
                )["n"],
                "patterns": self.store.one(
                    "SELECT COUNT(*) n FROM quality_patterns WHERE repo=?", (name,)
                )["n"],
                "pending_patterns": [
                    dict(row)
                    for row in self.store.query(
                        """SELECT id,name,prevention_layer,confidence,systemic_change
                        FROM quality_patterns WHERE repo=? AND approved_at IS NULL
                        ORDER BY analyzed_at""",
                        (name,),
                    )
                ],
                "recent_runs": runs,
            }
        return status

    def quality_pattern_approve(self, pattern_id, actor):
        if actor not in self.cfg.humans:
            raise ControllerError(
                "quality pattern approval requires a configured human"
            )
        with self.store.transaction():
            pattern = self.store.one(
                "SELECT * FROM quality_patterns WHERE id=?", (pattern_id,)
            )
            if not pattern:
                raise ControllerError("quality pattern not found")
            if pattern["approved_at"]:
                return {
                    "pattern": pattern_id,
                    "approved_by": pattern["approved_by"],
                    "improvement_root": pattern["improvement_root_id"],
                }
            root = None
            target, _ = self._improvement_target()
            handoff = (
                pattern["prevention_layer"] == "agent instruction"
                and target == pattern["repo"]
            )
            if handoff and len(self._open_roots()) >= self.cfg.improvement["max_open"]:
                raise ControllerError("the improvement loop has no open capacity")
            self.store.execute(
                "UPDATE quality_patterns SET approved_by=?,approved_at=? WHERE id=?",
                (actor, now_iso(), pattern_id),
            )
            # The standing policy can change only prompts and skills. Structural,
            # API, test, and workflow recommendations remain inert when approved.
            if handoff:
                signal = "quality-pattern:" + pattern["fingerprint"]
                for observation_id in json.loads(pattern["supporting_json"]):
                    observation = self.store.one(
                        "SELECT * FROM quality_observations WHERE id=?",
                        (observation_id,),
                    )
                    if not observation:
                        continue
                    roots = json.loads(observation["root_ids_json"]) or [observation_id]
                    for root_id in roots:
                        self.store.execute(
                            """INSERT OR IGNORE INTO improvement_signals
                            (signal,root_id,attempt_id,label,roles,detail,at)
                            VALUES (?,?,?,?,?,?,?)""",
                            (
                                signal,
                                root_id,
                                None,
                                pattern["name"],
                                "[]",
                                observation["what_happened"],
                                observation["observed_at"],
                            ),
                        )
                root = self._create_root(
                    pattern["repo"],
                    signal,
                    pattern["name"],
                    actor,
                )
                self.store.execute(
                    "UPDATE quality_patterns SET improvement_root_id=? WHERE id=?",
                    (root, pattern_id),
                )
            self.store.audit(
                actor,
                "quality.pattern_approved",
                {
                    "pattern": pattern_id,
                    "repo": pattern["repo"],
                    "improvement_root": root,
                },
            )
        summary = json.loads(
            self.store.get_op("quality:pattern-summary:" + pattern["repo"], "{}")
        )
        summary = {
            "top_interventions": summary.get("top_interventions", []),
            "isolated_observation_ids": summary.get("isolated_observation_ids", []),
            "additional_evidence_needed": summary.get("additional_evidence_needed", []),
            "omitted_observations": summary.get("omitted_observations", 0),
        }
        self._write_pattern_report(pattern["repo"], summary)
        return {"pattern": pattern_id, "approved_by": actor, "improvement_root": root}
