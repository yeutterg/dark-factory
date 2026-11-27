"""Work started by an outside coding agent, carried through the same pipeline.

The agent is a client of the factory, not a way around it. It works from an issue,
an approved plan and a worktree the factory owns; the factory then builds, tests and
reviews the exact commit and opens the pull request. A contributor can start work and
submit plans and candidates; it cannot approve, accept or change configuration.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from dark_factory.adapters.git.local import clone
from dark_factory.control.base import ControllerCore, ControllerError, atomic
from dark_factory.models import TERMINAL_ROOT
from dark_factory.store import now_iso

EXTERNAL_DIR = "external"


class ExternalWorkMixin(ControllerCore):
    def _external_row(self, wid):
        return self.store.one("SELECT * FROM external_work WHERE root_id=?", (wid,))

    def _owned(self, wid, actor):
        row = self._external_row(wid)
        if not row:
            raise ControllerError("this work did not start through work-start")
        if row["actor"] != actor:
            raise ControllerError("this work belongs to another contributor")
        return row

    # ---- dispatch hooks ---------------------------------------------------------

    def _external_dispatch(self, root, job, spec):
        """None for ordinary work, "wait" while the agent has not delivered, else its inputs."""
        ext = self._external_row(root["id"])
        if not ext:
            return None
        if job["kind"] == "plan" and spec.get("role") == "planner":
            if ext["proposal_json"]:
                return {"actor": ext["actor"], "proposal_json": ext["proposal_json"]}
            return None  # The factory's planner drafts the plan.
        if job["kind"] == "implement":
            if ext["submitted"] <= ext["consumed"]:
                return "wait"
            return {"actor": ext["actor"], "worktree": ext["worktree"]}
        return None

    def _external_consume(self, wid, spec):
        if spec.get("kind") == "plan":
            self.store.execute(
                "UPDATE external_work SET proposal_json=NULL WHERE root_id=?", (wid,)
            )
        else:
            self.store.execute(
                "UPDATE external_work SET consumed=consumed+1 WHERE root_id=?", (wid,)
            )

    # ---- the contributor's commands ----------------------------------------------

    def work_start(self, repo, number, actor, title="", body=""):
        wid = self.intake_issues(
            [
                {
                    "repo": repo,
                    "number": number,
                    "title": title,
                    "body": body,
                    "is_root": True,
                }
            ],
            actor,
        )[0]
        self.store.execute(
            "INSERT OR IGNORE INTO external_work(root_id,actor,created_at) VALUES (?,?,?)",
            (wid, actor, now_iso()),
        )
        self._owned(wid, actor)
        return self.work_status(wid, actor)

    def work_plan(self, wid, actor, proposal=None):
        """Submit a plan draft, or ask the factory's planner for one. Critique and a
        person's approval follow either way; no code is written before that."""
        self._owned(wid, actor)
        if proposal is not None:
            if not isinstance(proposal, dict):
                raise ControllerError("a plan draft must be a JSON object")
            self.store.execute(
                "UPDATE external_work SET proposal_json=? WHERE root_id=?",
                (json.dumps(proposal), wid),
            )
        self.start_planning(wid, actor)
        return self.work_status(wid, actor)

    @atomic
    def work_checkout(self, wid, actor):
        """A worktree the factory owns, at the approved base. Edit only there."""
        ext = self._owned(wid, actor)
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if root["status"] not in {"approved", "running"}:
            raise ControllerError(
                "no worktree until a person approves the plan "
                f"(status: {root['status']})"
            )
        snapshot = json.loads(root["snapshot_json"])
        path = self.cfg.state_dir / EXTERNAL_DIR / wid
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            clone(Path(snapshot["input_repo"]), path, snapshot["base"])
        self.store.execute(
            "UPDATE external_work SET worktree=? WHERE root_id=?", (str(path), wid)
        )
        return {
            "work_item_id": wid,
            "worktree": str(path),
            "base": snapshot["base"],
            "allowed_paths": snapshot["source"]["allowed_paths"],
            "protected_paths": snapshot["source"].get("protected_paths", []),
            "contributor": ext["actor"],
        }

    @atomic
    def work_submit(self, wid, actor):
        """Hand the worktree to the factory as a candidate. It is judged like any other."""
        ext = self._owned(wid, actor)
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if root["status"] not in {"approved", "running"}:
            raise ControllerError(
                f"nothing to submit to (status: {root['status']}); get the plan approved"
            )
        if not ext["worktree"] or not Path(ext["worktree"]).is_dir():
            raise ControllerError("run work-checkout first")
        if ext["submitted"] > ext["consumed"]:
            raise ControllerError("a candidate is already waiting to be checked")
        self.store.execute(
            "UPDATE external_work SET submitted=submitted+1 WHERE root_id=?", (wid,)
        )
        self.store.audit(actor, "work.submit", {"root": wid})
        return self.work_status(wid, actor)

    def work_status(self, wid, actor=None):
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if not root:
            raise ControllerError("work item not found")
        ext = self._external_row(wid)
        view = self.continuation(wid)
        candidate = json.loads(root["candidate_commits"] or "{}").get(root["repo"], {})
        return {
            "work_item_id": wid,
            "status": root["status"],
            "stage": root["presentation_stage"],
            "next": view.get("next_action"),
            "candidate": candidate.get("commit"),
            "pull_request": self._created_pr(wid, root["repo"]),
            "contributor": ext["actor"] if ext else None,
            "awaiting_submission": bool(ext and ext["submitted"] <= ext["consumed"]),
        }

    # ---- after review: the draft pull request --------------------------------------

    def _publish_candidate(self, root, branch, title, body, actor, marker):
        """Push the exact reviewed commit to a new branch and open a draft PR."""
        if self.store.get_op(marker, ""):
            return None
        snapshot = json.loads(root["snapshot_json"])
        if snapshot["simulation"]:
            self.store.set_op(marker, "skipped: simulation", "controller")
            self.store.audit(
                actor, "publish.skipped", {"root": root["id"], "why": "simulation"}
            )
            return {"root": root["id"], "published": "skipped: simulation"}
        candidate = json.loads(root["candidate_commits"])[root["repo"]]
        archive = self.store.resolve_path(candidate["archive"]) / "workspace"
        self.external_action(
            root["id"],
            "push_branch",
            {
                "repo": root["repo"],
                "branch": branch,
                "head_sha": candidate["commit"],
                "source_path": str(archive),
            },
            actor,
        )
        result = self.external_action(
            root["id"],
            "create_pr",
            {
                "repo": root["repo"],
                "head": branch,
                "base": self.cfg.improvement["base_branch"],
                "head_sha": candidate["commit"],
                "title": title,
                "body": body,
            },
            actor,
        )
        self.store.set_op(marker, json.dumps(result["evidence"]), "controller")
        return {"root": root["id"], "published": result["evidence"].get("url", "")}

    def external_tick(self):
        """Open the draft PR for reviewed work an outside agent started."""
        published = []
        for root in self.store.query(
            "SELECT w.* FROM work_items w JOIN external_work e ON e.root_id=w.id "
            "WHERE w.status='awaiting_review'"
        ):
            candidate = json.loads(root["candidate_commits"])[root["repo"]]
            ext = self._external_row(root["id"])
            result = self._publish_candidate(
                root,
                f"dark-factory/work/{root['id'].removeprefix('wi_')}-{candidate['commit'][:8]}",
                root["title"],
                "Opened by Dark Factory after its build, test and independent review "
                f"of this exact commit. Contributor: {ext['actor']}. A person reviews "
                "and merges; nothing is installed automatically.",
                "controller",
                "work:published:" + root["id"],
            )
            if result:
                published.append(result)
        return published

    def sweep_external_worktrees(self):
        """Worktrees of finished or replaced work are removed; returns bytes freed."""
        from dark_factory.control.resources import tree_size

        base = self.cfg.state_dir / EXTERNAL_DIR
        freed = 0
        if not base.is_dir():
            return 0
        for entry in sorted(base.iterdir()):
            root = self.store.one(
                "SELECT status FROM work_items WHERE id=?", (entry.name,)
            )
            if root is None or root["status"] in TERMINAL_ROOT:
                freed += tree_size(entry)
                shutil.rmtree(entry, ignore_errors=True)
        return freed
