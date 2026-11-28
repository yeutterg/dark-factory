"""Work started by an outside coding agent, carried through the same pipeline.

The agent is a client of the factory, not a way around it. It works from an issue,
an approved plan and a worktree the factory owns; the factory then builds, tests and
reviews the exact commit and opens the pull request. A contributor can start work and
submit plans and candidates; it cannot approve, accept or change configuration.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from dark_factory.adapters.git.local import clone, git
from dark_factory.adapters.harness.external import tree_fingerprint
from dark_factory.control.base import ControllerCore, ControllerError, atomic
from dark_factory.models import TERMINAL_ROOT
from dark_factory.store import now_iso

EXTERNAL_DIR = "external"
SUBMISSION_DIR = "external-submissions"
RECOVERY_DIR = "external-recovery"


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
            submission = self._next_submission(root, ext)
            if submission is None:
                return "wait"
            return {
                "actor": ext["actor"],
                "worktree": submission["path"],
                "submission": {
                    "seq": submission["seq"],
                    "digest": submission["digest"],
                },
            }
        return None

    def _next_submission(self, root, ext):
        """The oldest captured submission still bound to the approved plan, verified."""
        snapshot = json.loads(root["snapshot_json"])
        for row in self.store.query(
            "SELECT * FROM external_submissions WHERE root_id=? AND consumed=0 ORDER BY seq",
            (root["id"],),
        ):
            reason = None
            if (
                row["plan_digest"] != root["approved_digest"]
                or row["base"] != snapshot["base"]
            ):
                reason = "stale: the plan or base changed after it was submitted"
            elif (
                not Path(row["path"]).is_dir()
                or tree_fingerprint(Path(row["path"])) != row["digest"]
            ):
                reason = "tampered: the captured tree no longer matches its digest"
            if reason is None:
                return row
            self.store.execute(
                "UPDATE external_submissions SET consumed=2 WHERE root_id=? AND seq=?",
                (root["id"], row["seq"]),
            )
            self.store.audit(
                ext["actor"],
                "work.submission_refused",
                {"root": root["id"], "seq": row["seq"], "reason": reason},
            )
        return None

    def _external_consume(self, wid, spec):
        if spec.get("kind") == "plan":
            self.store.execute(
                "UPDATE external_work SET proposal_json=NULL WHERE root_id=?", (wid,)
            )
        else:
            seq = self.store.one(
                "SELECT MIN(seq) s FROM external_submissions WHERE root_id=? AND consumed=0",
                (wid,),
            )["s"]
            self.store.execute(
                "UPDATE external_submissions SET consumed=1 WHERE root_id=? AND seq=?",
                (wid, seq),
            )

    # ---- the contributor's commands ----------------------------------------------

    def work_start(self, repo, number, actor, title="", body=""):
        known = {r["id"] for r in self.store.query("SELECT id FROM work_items")}
        # Tracker-backed intake names only the repository and the issue number: a
        # title in the request makes the tracker treat it as fixture data, which live
        # routes refuse. Fixtures are built explicitly, and only in simulation.
        issue: dict = {"repo": repo, "number": number}
        simulation_only = all(
            route.evidence_kind == "simulation" for route in self.cfg.routes.values()
        )
        if simulation_only and (title or body):
            issue.update(title=title, body=body, is_root=True)
        wid = self.intake_issues([issue], actor)[0]
        if wid in known and not self._external_row(wid):
            # A contributor may not take over work an operator started or a person
            # already decided on; only a root it created is its own.
            raise ControllerError(
                "that issue already has a root the factory did not start for you; "
                "ask an operator"
            )
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
            "base_commit": snapshot["base"],
            "repo_url": snapshot["source"].get("remote", ""),
            "branch_suggestion": f"dark-factory/contrib/{wid}/1",
            "allowed_paths": snapshot["source"]["allowed_paths"],
            "protected_paths": snapshot["source"].get("protected_paths", []),
            "contributor": ext["actor"],
        }

    @atomic
    def work_submit(self, wid, actor, bundle=None):
        """Capture the worktree as an immutable submission. Edits made afterwards, by
        the agent or anyone, cannot change what the factory builds and reviews."""
        self._owned(wid, actor)
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if root["status"] not in {"approved", "running"}:
            raise ControllerError(
                f"nothing to submit to (status: {root['status']}); get the plan approved"
            )
        if self.store.one(
            "SELECT 1 FROM external_submissions WHERE root_id=? AND consumed=0", (wid,)
        ):
            raise ControllerError("a candidate is already waiting to be checked")
        ext = self._external_row(wid)
        bundle_tree = None
        temporary = None
        if bundle is not None:
            import base64
            import binascii
            import tempfile

            from dark_factory.execution.packages import MAX_BYTES

            try:
                raw = base64.b64decode(bundle, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ControllerError("submission bundle is not valid base64") from exc
            if not raw or len(raw) > MAX_BYTES:
                raise ControllerError("submission bundle exceeds the transport limit")
            temporary = Path(
                tempfile.mkdtemp(prefix="submission-", dir=self.cfg.state_dir)
            )
            bundle_path = temporary / "candidate.bundle"
            bundle_path.write_bytes(raw)
            snapshot = json.loads(root["snapshot_json"])
            try:
                bundle_tree = temporary / "tree"
                clone(Path(snapshot["input_repo"]), bundle_tree, snapshot["base"])
                git(bundle_tree, "bundle", "verify", str(bundle_path))
                git(bundle_tree, "fetch", str(bundle_path), "HEAD")
                git(bundle_tree, "checkout", "--detach", "FETCH_HEAD")
                if not self._contained_submission_base(
                    bundle_tree, snapshot["base"], "FETCH_HEAD"
                ):
                    raise ControllerError(
                        "submission bundle does not descend from the approved base"
                    )
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        elif not ext["worktree"] or not Path(ext["worktree"]).is_dir():
            raise ControllerError("run work-checkout first or submit a git bundle")
        seq = (
            self.store.one(
                "SELECT COALESCE(MAX(seq),0) n FROM external_submissions WHERE root_id=?",
                (wid,),
            )["n"]
            + 1
        )
        captured = self.cfg.state_dir / SUBMISSION_DIR / wid / str(seq)
        captured.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(
                bundle_tree or Path(ext["worktree"]),
                captured,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git"),
            )
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
        snapshot = json.loads(root["snapshot_json"])
        self.store.execute(
            "INSERT INTO external_submissions(root_id,seq,path,digest,plan_digest,base,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                wid,
                seq,
                str(captured),
                tree_fingerprint(captured),
                root["approved_digest"],
                snapshot["base"],
                now_iso(),
            ),
        )
        self.store.audit(actor, "work.submit", {"root": wid, "seq": seq})
        if bundle_tree:
            shutil.rmtree(bundle_tree.parent, ignore_errors=True)
        return self.work_status(wid, actor)

    @staticmethod
    def _contained_submission_base(repository, base, head):
        try:
            git(repository, "merge-base", "--is-ancestor", base, head)
            return True
        except Exception:
            return False

    def work_status(self, wid, actor=None):
        root = self.store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        if not root:
            raise ControllerError("work item not found")
        ext = self._external_row(wid)
        if actor is not None and ext and ext["actor"] != actor:
            raise ControllerError("this work belongs to another contributor")
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
            "awaiting_submission": bool(
                ext
                and not self.store.one(
                    "SELECT 1 FROM external_submissions WHERE root_id=? AND consumed=0",
                    (wid,),
                )
            ),
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
                actor,
                "publish.skipped",
                {"root": root["id"], "origin": root["origin"], "reason": "simulation"},
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
                # Pinned when the plan was approved: a later profile edit, or another
                # repository's setting, cannot redirect this pull request or its retries.
                "base": snapshot.get("target_branch") or "main",
                "head_sha": candidate["commit"],
                "title": title,
                "body": body.rstrip() + f"\n\nDark-Factory-Root: {root['id']}\n",
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

    def _preserve_unique_work(self, wid, tree):
        """Keep edits that were never submitted before their checkout is removed."""
        last = self.store.one(
            "SELECT digest FROM external_submissions WHERE root_id=? ORDER BY seq DESC LIMIT 1",
            (wid,),
        )
        if last and last["digest"] == tree_fingerprint(tree):
            return None  # Everything in it was submitted, and is evidence.
        try:
            git(tree, "add", "-A")
            patch = git(tree, "diff", "--cached", "--binary", "HEAD")
        except Exception as exc:
            self.store.audit(
                "controller",
                "external.preserve_failed",
                {"root": wid, "error": str(exc)[:300]},
            )
            raise
        if not patch.strip():
            return None
        target = self.cfg.state_dir / RECOVERY_DIR
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{wid}-{int(time.time())}.patch"
        path.write_text(patch + "\n")
        self.store.audit(
            "controller",
            "external.preserved",
            {"root": wid, "path": str(path), "bytes": path.stat().st_size},
        )
        return path

    def sweep_external_worktrees(self):
        """Worktrees and captured submissions of finished work are removed; unique
        unsubmitted edits are saved as a patch first. Returns bytes freed."""
        from dark_factory.control.resources import tree_size

        freed = 0
        base = self.cfg.state_dir / EXTERNAL_DIR
        if base.is_dir():
            for entry in sorted(base.iterdir()):
                root = self.store.one(
                    "SELECT status FROM work_items WHERE id=?", (entry.name,)
                )
                if root is not None and root["status"] not in TERMINAL_ROOT:
                    continue
                try:
                    self._preserve_unique_work(entry.name, entry)
                except Exception:
                    continue  # Could not save it: the checkout stays until it can be.
                freed += tree_size(entry)
                shutil.rmtree(entry, ignore_errors=True)
        submissions = self.cfg.state_dir / SUBMISSION_DIR
        if submissions.is_dir():
            for entry in sorted(submissions.iterdir()):
                root = self.store.one(
                    "SELECT status FROM work_items WHERE id=?", (entry.name,)
                )
                if root is not None and root["status"] not in TERMINAL_ROOT:
                    continue
                if self.store.one(
                    "SELECT 1 FROM external_submissions WHERE root_id=? AND consumed=0",
                    (entry.name,),
                ):
                    self._save_unconsumed(entry)
                freed += tree_size(entry)
                shutil.rmtree(entry, ignore_errors=True)
        days = self.cfg.disk["keep_finished_days"]
        recovery = self.cfg.state_dir / RECOVERY_DIR
        if recovery.is_dir():
            for patch in recovery.glob("*.patch"):
                if time.time() - patch.stat().st_mtime > days * 86400:
                    freed += patch.stat().st_size
                    patch.unlink()
        return freed

    def _save_unconsumed(self, directory):
        """A submission nobody built is unique work too: keep it as a tarball."""
        import tarfile

        target = self.cfg.state_dir / RECOVERY_DIR
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{directory.name}-submitted-{int(time.time())}.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            archive.add(directory, arcname=directory.name)
        self.store.audit(
            "controller",
            "external.preserved",
            {"root": directory.name, "path": str(path), "bytes": path.stat().st_size},
        )
