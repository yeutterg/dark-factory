"""Release roots: the second loop, from assembled commit to released package.

assemble, build, test and package run automatically. A person approves the exact
package digest. stage and verify follow (verify may need a person to confirm manual
items). A second person decision promotes. close writes the notes and keeps only the
newest packages. The factory never starts a release by itself, and a changed package
voids any approval it had: the decision is bound to the digest, not to the release.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from dark_factory.adapters.git.local import clone, git
from dark_factory.control.base import ControllerCore, ControllerError
from dark_factory.pipeline import Pipeline, PipelineError, read_pipeline
from dark_factory.runner import CommandRunner
from dark_factory.runners import HostedRunner, RunnerError, check_grants
from dark_factory.secrets import secret
from dark_factory.store import new_id, now_iso

AUTOMATIC_FIRST = ("build", "test", "package")
AFTER_PACKAGE_APPROVAL = ("stage", "verify")
TAIL = 4000
SECRET_PREFIX = "DARK_FACTORY_SECRET_"


def package_digest(artifacts: list[dict]) -> str:
    listing = sorted((a["name"], a["sha256"]) for a in artifacts)
    return hashlib.sha256(json.dumps(listing).encode()).hexdigest()


def tree_digest(directory: Path) -> str:
    """The package digest recomputed from the files on disk."""
    found = [
        {
            "name": path.relative_to(directory).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]
    return package_digest(found)


class ReleaseMixin(ControllerCore):
    _release_threads: dict[str, threading.Thread] = {}
    _release_locks: dict[str, threading.RLock] = {}

    def _lock(self, rid):
        return self._release_locks.setdefault(rid, threading.RLock())

    def _verify_package(self, release):
        """What is deployed is what was approved: re-hash the stored package first."""
        directory = self._artifact_dir(release["id"])
        if release["package_digest"] and directory.is_dir():
            if tree_digest(directory) != release["package_digest"]:
                raise ControllerError(
                    "the stored package no longer matches its approved digest"
                )

    # ---- records -----------------------------------------------------------------

    def _release(self, rid):
        row = self.store.one("SELECT * FROM releases WHERE id=?", (rid,))
        if not row:
            raise ControllerError("release not found")
        return row

    def _pipeline_of(self, release):
        return Pipeline.from_dict(json.loads(release["pipeline_json"]))

    def _stage_row(self, rid, stage):
        return self.store.one(
            "SELECT * FROM release_stages WHERE release_id=? AND stage=?", (rid, stage)
        )

    def _set_stage(self, rid, stage, status, **fields):
        self.store.execute(
            "INSERT INTO release_stages(release_id,stage,status) VALUES (?,?,?) "
            "ON CONFLICT(release_id,stage) DO UPDATE SET status=excluded.status",
            (rid, stage, status),
        )
        for key, value in fields.items():
            self.store.execute(
                f"UPDATE release_stages SET {key}=? WHERE release_id=? AND stage=?",
                (value, rid, stage),
            )

    def _artifact_dir(self, rid):
        return self.cfg.state_dir / "release-artifacts" / rid

    # ---- assemble ------------------------------------------------------------------

    def release_start(self, repo, kind, actor, ref=None):
        """Operator-created only: the factory never originates a release."""
        source = self.cfg.sources.get(repo)
        if source is None:
            raise ControllerError("the release repository has no configured source")
        path = Path(source["path"])
        try:
            pipeline, _ = read_pipeline(path, source.get("pipeline_ref", "main"))
        except PipelineError as exc:
            raise ControllerError(
                f"the repository's pipeline is invalid: {exc}"
            ) from exc
        if pipeline is None or kind not in pipeline.releases:
            raise ControllerError(
                f"the repository's pipeline defines no release kind {kind!r}"
            )
        release = pipeline.releases[kind]
        try:
            from dark_factory.runners import check_grants

            check_grants(release.stages, self.cfg.runners, f"release.{kind}")
        except RunnerError as exc:
            raise ControllerError(str(exc)) from exc
        commit = git(
            path, "rev-parse", "--verify", f"{ref or release.branch}^{{commit}}"
        ).strip()
        previous = self.store.one(
            "SELECT commit_sha FROM releases WHERE repo=? AND kind=? AND status='released' "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (repo, kind),
        )
        included = []
        for root in self.store.query(
            "SELECT id,title,candidate_commits FROM work_items WHERE repo=? AND status='accepted' "
            "ORDER BY accepted_at,rowid",
            (repo,),
        ):
            sha = json.loads(root["candidate_commits"]).get(repo, {}).get("commit")
            if not sha:
                continue
            reachable = self._contained(path, sha, commit)
            if reachable and (
                previous is None
                or not self._contained(path, sha, previous["commit_sha"])
            ):
                included.append({"root": root["id"], "title": root["title"]})
        rid = new_id("rel")
        self.store.execute(
            "INSERT INTO releases(id,repo,kind,branch,commit_sha,status,pipeline_json,"
            "roots_json,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                repo,
                kind,
                release.branch,
                commit,
                "assembled",
                json.dumps(pipeline.to_dict()),
                json.dumps(included),
                actor,
                now_iso(),
            ),
        )
        self._set_stage(
            rid,
            "assemble",
            "passed",
            ended_at=now_iso(),
            seconds=0.0,
            evidence_json=json.dumps({"commit": commit, "roots": included}),
        )
        self.store.audit(
            actor,
            "release.start",
            {"release": rid, "repo": repo, "kind": kind, "commit": commit},
        )
        return self.release_status(rid)

    @staticmethod
    def _contained(path, sha, other):
        try:
            git(path, "merge-base", "--is-ancestor", sha, other)
            return True
        except Exception:
            return False

    # ---- running a stage --------------------------------------------------------------

    def _next_automatic(self, release):
        rid, status = release["id"], release["status"]
        if status in {"assembled", "building"}:
            for stage in AUTOMATIC_FIRST:
                row = self._stage_row(rid, stage)
                if row is None or row["status"] not in {"passed", "not_applicable"}:
                    return stage
            return None
        if status in {"package_approved", "staging"}:
            for stage in AFTER_PACKAGE_APPROVAL:
                row = self._stage_row(rid, stage)
                if row is None or row["status"] not in {"passed", "not_applicable"}:
                    return stage
            return None
        if status == "promoted":
            return "close"
        return None

    def release_run_stage(self, rid, actor="controller"):
        """Run the next automatic stage to completion. Returns the stage run, or None."""
        lock = self._lock(rid)
        if not lock.acquire(blocking=False):
            return None  # another thread is already running a stage of this release
        try:
            return self._run_next_stage(rid, actor)
        finally:
            lock.release()

    def _run_next_stage(self, rid, actor):
        release = self._release(rid)
        stage_name = self._next_automatic(release)
        if stage_name is None:
            return None
        if stage_name == "close":
            return self._close(release)
        if stage_name in AFTER_PACKAGE_APPROVAL:
            try:
                self._verify_package(release)
            except ControllerError as exc:
                self.store.execute(
                    "UPDATE releases SET status='failed',reason=? WHERE id=?",
                    (str(exc), rid),
                )
                self.store.audit(actor, "release.package_mismatch", {"release": rid})
                return {"stage": stage_name, "ok": False}
        pipeline = self._pipeline_of(release)
        stage = pipeline.releases[release["kind"]].stages[stage_name]
        if not stage.applicable:
            if stage_name == "package":
                # Nothing is built, so the decision is bound to the exact commit.
                self.store.execute(
                    "UPDATE releases SET package_digest=? WHERE id=?",
                    (
                        hashlib.sha256(
                            f"commit:{release['commit_sha']}".encode()
                        ).hexdigest(),
                        rid,
                    ),
                )
            self._set_stage(
                rid,
                stage_name,
                "not_applicable",
                ended_at=now_iso(),
                seconds=0.0,
                evidence_json=json.dumps({"reason": stage.not_applicable}),
            )
            return self._advance_release(rid)
        self.store.execute(
            "UPDATE releases SET status=? WHERE id=?",
            ("building" if stage_name in AUTOMATIC_FIRST else "staging", rid),
        )
        self._set_stage(
            rid, stage_name, "running", started_at=now_iso(), runner=stage.runner
        )
        began = time.monotonic()
        try:
            evidence = self._execute(release, stage_name, stage)
            ok = evidence["ok"]
        except Exception as exc:
            evidence, ok = {"ok": False, "error": str(exc)[:500]}, False
        seconds = round(time.monotonic() - began, 3)
        self._set_stage(
            rid,
            stage_name,
            "passed" if ok else "failed",
            ended_at=now_iso(),
            seconds=seconds,
            evidence_json=json.dumps(evidence),
        )
        self.store.audit(
            actor, "release.stage", {"release": rid, "stage": stage_name, "ok": ok}
        )
        if not ok:
            self.store.execute(
                "UPDATE releases SET status='failed',reason=? WHERE id=?",
                (f"{stage_name} stage failed", rid),
            )
            return {"stage": stage_name, "ok": False}
        if stage_name == "package":
            self.store.execute(
                "UPDATE releases SET package_digest=? WHERE id=?",
                (evidence["package_digest"], rid),
            )
        return self._advance_release(rid) or {"stage": stage_name, "ok": True}

    def _advance_release(self, rid):
        """Move a release to the state its finished stages imply."""
        release = self._release(rid)

        def done(stage):
            row = self._stage_row(rid, stage)
            return bool(row) and row["status"] in {"passed", "not_applicable"}

        if release["status"] in {"assembled", "building"} and all(
            done(s) for s in AUTOMATIC_FIRST
        ):
            self.store.execute(
                "UPDATE releases SET status='awaiting_package_approval' WHERE id=?",
                (rid,),
            )
        elif release["status"] in {"package_approved", "staging"} and all(
            done(s) for s in AFTER_PACKAGE_APPROVAL
        ):
            self.store.execute(
                "UPDATE releases SET status='verifying' WHERE id=?", (rid,)
            )
            self._maybe_ready_to_promote(rid)
        return {"release": rid, "status": self._release(rid)["status"]}

    def _maybe_ready_to_promote(self, rid):
        release = self._release(rid)
        pipeline = self._pipeline_of(release)
        manual = pipeline.releases[release["kind"]].stages["verify"].manual
        confirmed = {
            r["item"]
            for r in self.store.query(
                "SELECT item FROM release_confirmations WHERE release_id=?", (rid,)
            )
        }
        if release["status"] == "verifying" and set(manual) <= confirmed:
            self.store.execute(
                "UPDATE releases SET status='awaiting_promotion_approval' WHERE id=?",
                (rid,),
            )

    # ---- executing steps -----------------------------------------------------------------

    def _execute(self, release, stage_name, stage):
        runner_cfg = self.cfg.runners[stage.runner]
        if runner_cfg["kind"] != "local":
            return self._execute_hosted(release, stage_name, stage, runner_cfg)
        rid = release["id"]
        try:
            check_grants({stage_name: stage}, self.cfg.runners, "release")
        except RunnerError as exc:
            raise ControllerError(str(exc)) from exc
        values = {}
        for name in stage.secrets:
            key = name.upper().replace("-", "_")
            values[key] = secret(SECRET_PREFIX + key)
            if not values[key]:
                raise ControllerError(f"secret {name} is not provided to the factory")
        base = self.cfg.state_dir / "releases" / rid / stage_name
        shutil.rmtree(base, ignore_errors=True)
        checkout, out = base / "checkout", base / "out"
        out.mkdir(parents=True)
        clone(
            Path(self.cfg.sources[release["repo"]]["path"]),
            checkout,
            release["commit_sha"],
        )
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(base / "home"),
            "FACTORY_STAGE": stage_name,
            "FACTORY_KIND": "release",
            "FACTORY_COMMIT": release["commit_sha"],
            "FACTORY_OUT": str(out),
            # Only the secrets this stage asked for and the profile granted.
            **values,
            **(
                {"FACTORY_PACKAGE": str(self._artifact_dir(rid))}
                if self._artifact_dir(rid).is_dir()
                else {}
            ),
        }
        (base / "home").mkdir()
        steps = []
        ok = True
        try:
            for argv in stage.steps:
                began = time.monotonic()
                code, stdout, stderr = CommandRunner(
                    checkout, self.cfg.max_output_bytes
                ).supervise(argv, b"", stage.timeout_seconds, checkout, env)
                steps.append(
                    {
                        "argv": argv,
                        "exit_code": code,
                        "seconds": round(time.monotonic() - began, 3),
                        "stdout": stdout.decode(errors="replace")[-TAIL:],
                        "stderr": stderr.decode(errors="replace")[-TAIL:],
                    }
                )
                if code:
                    ok = False
                    break
            evidence = {"ok": ok, "runner": "local", "steps": steps}
            if ok and stage_name == "package":
                evidence.update(self._store_artifacts(rid, out, stage.artifacts))
                if not evidence["artifacts"]:
                    evidence["ok"] = False
                    evidence["error"] = (
                        "the package stage produced none of its declared artifacts"
                    )
            return evidence
        finally:
            # Always clean up the build; only declared artifacts were kept.
            shutil.rmtree(base, ignore_errors=True)
            for parent in (base.parent, base.parent.parent):
                try:
                    parent.rmdir()  # only if nothing else is in it
                except OSError:
                    pass

    def _store_artifacts(self, rid, out, patterns):
        destination = self._artifact_dir(rid)
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True)
        found = []
        for path in sorted(
            p for p in out.rglob("*") if p.is_file() and not p.is_symlink()
        ):
            relative = path.relative_to(out).as_posix()
            if any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
                data = path.read_bytes()
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                found.append(
                    {
                        "name": relative,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
        return {"artifacts": found, "package_digest": package_digest(found)}

    def _execute_hosted(self, release, stage_name, stage, runner_cfg):
        command = self.cfg.commands["code_host"]
        result = HostedRunner(
            self.runner.run_json,
            command,
            release["repo"],
            runner_cfg,
            f"{release['id']}-{stage_name}",
        ).run(
            stage,
            "release",
            release["commit_sha"],
            stage.timeout_seconds,
            branch=release["branch"],
        )
        evidence = {
            "ok": result.ok,
            "runner": runner_cfg["kind"],
            "url": result.url,
            "conclusion": result.conclusion,
        }
        if result.ok and stage_name == "package":
            found = [
                {
                    "name": a["name"],
                    "size": a["size"],
                    "sha256": a["digest"].removeprefix("sha256:"),
                }
                for a in result.artifacts
            ]
            if any(not re.fullmatch("[0-9a-f]{64}", a["sha256"]) for a in found):
                evidence["ok"] = False
                evidence["error"] = (
                    "the code host did not report a digest for every artifact"
                )
            elif not found:
                evidence["ok"] = False
                evidence["error"] = "the hosted package stage produced no artifacts"
            else:
                evidence.update(
                    {"artifacts": found, "package_digest": package_digest(found)}
                )
        return evidence

    # ---- the two human decisions ---------------------------------------------------------

    def release_approve(self, rid, gate, actor):
        if gate not in {"package", "promote"}:
            raise ControllerError("gate must be package or promote")
        release = self._release(rid)
        self._require_human(actor, {"snapshot_json": json.dumps({"simulation": False})})
        expected = {
            "package": "awaiting_package_approval",
            "promote": "awaiting_promotion_approval",
        }[gate]
        if release["status"] != expected:
            raise ControllerError(
                f"release is {release['status']}; {gate} approval needs {expected}"
            )
        digest = release["package_digest"]
        if not digest:
            raise ControllerError("there is no package to approve")
        if gate == "promote":
            approved = self.store.one(
                "SELECT digest FROM release_approvals WHERE release_id=? AND gate='package'",
                (rid,),
            )
            if not approved or approved["digest"] != digest:
                raise ControllerError(
                    "the package approval no longer covers this package"
                )
        self.store.execute(
            "INSERT OR REPLACE INTO release_approvals(release_id,gate,actor,at,digest) VALUES (?,?,?,?,?)",
            (rid, gate, actor, now_iso(), digest),
        )
        self.store.execute(
            "UPDATE releases SET status=? WHERE id=?",
            ("package_approved" if gate == "package" else "promoting", rid),
        )
        self.store.audit(
            actor, "release.approve", {"release": rid, "gate": gate, "digest": digest}
        )
        if gate == "promote":
            return self._run_promote(rid)
        return self.release_status(rid)

    def _run_promote(self, rid):
        with self._lock(rid):
            return self._promote_locked(rid)

    def _promote_locked(self, rid):
        release = self._release(rid)
        try:
            self._verify_package(release)
        except ControllerError as exc:
            self.store.execute(
                "UPDATE releases SET status='failed',reason=? WHERE id=?",
                (str(exc), rid),
            )
            return self.release_status(rid)
        stage = self._pipeline_of(release).releases[release["kind"]].stages["promote"]
        if not stage.applicable:
            self._set_stage(
                rid, "promote", "not_applicable", ended_at=now_iso(), seconds=0.0
            )
        else:
            self._set_stage(
                rid, "promote", "running", started_at=now_iso(), runner=stage.runner
            )
            began = time.monotonic()
            try:
                evidence = self._execute(release, "promote", stage)
            except Exception as exc:
                evidence = {"ok": False, "error": str(exc)[:500]}
            self._set_stage(
                rid,
                "promote",
                "passed" if evidence["ok"] else "failed",
                ended_at=now_iso(),
                seconds=round(time.monotonic() - began, 3),
                evidence_json=json.dumps(evidence),
            )
            if not evidence["ok"]:
                self.store.execute(
                    "UPDATE releases SET status='failed',reason='promote stage failed' WHERE id=?",
                    (rid,),
                )
                return self.release_status(rid)
        self.store.execute("UPDATE releases SET status='promoted' WHERE id=?", (rid,))
        return self.release_run_stage(rid) and self.release_status(rid)

    def release_confirm(self, rid, item, actor, evidence=""):
        release = self._release(rid)
        self._require_human(actor, {"snapshot_json": json.dumps({"simulation": False})})
        manual = (
            self._pipeline_of(release).releases[release["kind"]].stages["verify"].manual
        )
        if item not in manual:
            raise ControllerError(
                "that is not a manual verification item of this release"
            )
        if release["status"] not in {"verifying", "package_approved", "staging"}:
            raise ControllerError(f"release is {release['status']}")
        self.store.execute(
            "INSERT OR REPLACE INTO release_confirmations(release_id,item,actor,at,evidence) VALUES (?,?,?,?,?)",
            (rid, item, actor, now_iso(), evidence or "confirmed"),
        )
        self.store.audit(actor, "release.confirm", {"release": rid, "item": item})
        self._maybe_ready_to_promote(rid)
        return self.release_status(rid)

    # ---- close, retention, rollback -------------------------------------------------------

    def _close(self, release):
        rid = release["id"]
        roots = json.loads(release["roots_json"])
        notes = "\n".join(
            [
                f"Release {rid}: {release['repo']} {release['kind']} at {release['commit_sha'][:12]}",
                "",
                *(f"- {r['title']} ({r['root']})" for r in roots),
            ]
        )
        self.store.execute(
            "UPDATE releases SET status='released',closed_at=?,notes=? WHERE id=?",
            (now_iso(), notes, rid),
        )
        self._set_stage(rid, "close", "passed", ended_at=now_iso(), seconds=0.0)
        keep = self._pipeline_of(release).releases[release["kind"]].keep
        pruned = []
        for old in self.store.query(
            "SELECT id FROM releases WHERE repo=? AND kind=? AND status='released' AND pruned=0 "
            "ORDER BY created_at DESC,rowid DESC",
            (release["repo"], release["kind"]),
        )[keep:]:
            shutil.rmtree(self._artifact_dir(old["id"]), ignore_errors=True)
            self.store.execute("UPDATE releases SET pruned=1 WHERE id=?", (old["id"],))
            pruned.append(old["id"])
        self.store.audit(
            "controller", "release.closed", {"release": rid, "pruned": pruned}
        )
        return {"release": rid, "status": "released", "pruned": pruned}

    def release_rollback(self, rid, actor):
        """Redeploy the previous kept package through stage, verify and promote."""
        self._require_human(actor, {"snapshot_json": json.dumps({"simulation": False})})
        current = self._release(rid)
        previous = self.store.one(
            "SELECT * FROM releases WHERE repo=? AND kind=? AND status='released' AND pruned=0 "
            "AND created_at<? AND id<>? ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (current["repo"], current["kind"], current["created_at"], rid),
        )
        if not previous or not self._artifact_dir(previous["id"]).is_dir():
            raise ControllerError("no earlier kept package to roll back to")
        self._verify_package(previous)
        new = new_id("rel")
        self.store.execute(
            "INSERT INTO releases(id,repo,kind,branch,commit_sha,status,pipeline_json,roots_json,"
            "package_digest,rollback_of,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new,
                previous["repo"],
                previous["kind"],
                previous["branch"],
                previous["commit_sha"],
                "awaiting_package_approval",
                previous["pipeline_json"],
                previous["roots_json"],
                previous["package_digest"],
                rid,
                actor,
                now_iso(),
            ),
        )
        for stage in ("assemble", "build", "test", "package"):
            self._set_stage(
                new,
                stage,
                "passed",
                ended_at=now_iso(),
                seconds=0.0,
                evidence_json=json.dumps({"reused_from": previous["id"]}),
            )
        # The package is not rebuilt; its directory is shared by reference.
        shutil.copytree(self._artifact_dir(previous["id"]), self._artifact_dir(new))
        self.store.audit(
            actor,
            "release.rollback",
            {"release": new, "from": rid, "to": previous["id"]},
        )
        return self.release_status(new)

    # ---- views and the tick -------------------------------------------------------------------

    def release_status(self, rid):
        release = self._release(rid)
        return {
            "release": rid,
            "repo": release["repo"],
            "kind": release["kind"],
            "commit": release["commit_sha"],
            "status": release["status"],
            "reason": release["reason"],
            "package_digest": release["package_digest"],
            "rollback_of": release["rollback_of"],
            "roots": json.loads(release["roots_json"]),
            "stages": [
                {
                    "stage": r["stage"],
                    "status": r["status"],
                    "runner": r["runner"],
                    "seconds": r["seconds"],
                }
                for r in self.store.query(
                    "SELECT * FROM release_stages WHERE release_id=? ORDER BY rowid",
                    (rid,),
                )
            ],
            "approvals": [
                {"gate": r["gate"], "actor": r["actor"], "digest": r["digest"]}
                for r in self.store.query(
                    "SELECT * FROM release_approvals WHERE release_id=?", (rid,)
                )
            ],
            "next": self._next_step_text(release),
        }

    def _next_step_text(self, release):
        text = {
            "awaiting_package_approval": "A person approves the exact package digest: release-approve <id> package",
            "awaiting_promotion_approval": "A person approves promotion: release-approve <id> promote",
            "verifying": "A person confirms the manual verification items: release-confirm <id> --item ...",
            "failed": "Inspect the failed stage's evidence, then a person retries (release-retry) or cancels (release-cancel)",
        }
        return text.get(release["status"], "The factory runs the next stage.")

    def release_retry(self, rid, actor):
        """A person retries a failed release from the stage that failed."""
        self._require_human(actor, {"snapshot_json": json.dumps({"simulation": False})})
        release = self._release(rid)
        if release["status"] != "failed":
            raise ControllerError(f"release is {release['status']}, not failed")
        failed = self.store.one(
            "SELECT stage FROM release_stages WHERE release_id=? AND status='failed' "
            "ORDER BY rowid DESC LIMIT 1",
            (rid,),
        )
        if failed is None:
            raise ControllerError("no failed stage to retry")
        stage = failed["stage"]
        order = ("build", "test", "package", "stage", "verify", "promote", "close")
        for later in order[order.index(stage) :]:
            self.store.execute(
                "DELETE FROM release_stages WHERE release_id=? AND stage=?",
                (rid, later),
            )
        if stage in AUTOMATIC_FIRST:
            status = "assembled"
            self.store.execute(
                "UPDATE releases SET package_digest=NULL WHERE id=?", (rid,)
            )
            self.store.execute(
                "DELETE FROM release_approvals WHERE release_id=?", (rid,)
            )
        elif stage in AFTER_PACKAGE_APPROVAL:
            status = "package_approved"
        else:
            status = "awaiting_promotion_approval"  # promotion needs a fresh decision
            self.store.execute(
                "DELETE FROM release_approvals WHERE release_id=? AND gate='promote'",
                (rid,),
            )
        self.store.execute(
            "UPDATE releases SET status=?,reason='' WHERE id=?", (status, rid)
        )
        self.store.audit(actor, "release.retry", {"release": rid, "stage": stage})
        return self.release_status(rid)

    def release_cancel(self, rid, actor):
        self._require_human(actor, {"snapshot_json": json.dumps({"simulation": False})})
        release = self._release(rid)
        if release["status"] in {"released", "cancelled"}:
            raise ControllerError(f"release is already {release['status']}")
        self.store.execute(
            "UPDATE releases SET status='cancelled',reason='cancelled by a person' WHERE id=?",
            (rid,),
        )
        shutil.rmtree(self._artifact_dir(rid), ignore_errors=True)
        self.store.audit(actor, "release.cancel", {"release": rid})
        return self.release_status(rid)

    def release_list(self):
        return [
            {
                k: r[k]
                for k in ("id", "repo", "kind", "status", "commit_sha", "created_at")
            }
            for r in self.store.query(
                "SELECT * FROM releases ORDER BY created_at DESC,rowid DESC"
            )
        ]

    def release_tick(self):
        """Start the next automatic stage of each release in the background."""
        started = []
        for release in self.store.query(
            "SELECT * FROM releases WHERE status IN "
            "('assembled','building','package_approved','staging','promoted')"
        ):
            rid = release["id"]
            thread = self._release_threads.get(rid)
            if thread and thread.is_alive():
                continue
            if self._next_automatic(release) is None:
                continue
            thread = threading.Thread(
                target=self.release_run_stage, args=(rid,), daemon=True
            )
            self._release_threads[rid] = thread
            thread.start()
            started.append(rid)
        return started

    def recover_releases(self):
        """After a restart no stage thread is alive: running stages did not finish."""
        for stuck in self.store.query(
            "SELECT id FROM releases WHERE status='promoting'"
        ):
            promote = self._stage_row(stuck["id"], "promote")
            if (promote is None or promote["status"] != "passed") and not (
                self._release_threads.get(stuck["id"]) or threading.Thread()
            ).is_alive():
                self.store.execute(
                    "UPDATE releases SET status='failed',reason=? WHERE id=?",
                    ("promote was interrupted by a restart", stuck["id"]),
                )
        for row in self.store.query(
            "SELECT release_id,stage FROM release_stages WHERE status='running'"
        ):
            if not (
                self._release_threads.get(row["release_id"]) or threading.Thread()
            ).is_alive():
                self._set_stage(
                    row["release_id"],
                    row["stage"],
                    "failed",
                    ended_at=now_iso(),
                    evidence_json=json.dumps(
                        {"ok": False, "error": "interrupted by a restart"}
                    ),
                )
                self.store.execute(
                    "UPDATE releases SET status='failed',reason=? WHERE id=?",
                    (f"{row['stage']} was interrupted by a restart", row["release_id"]),
                )
