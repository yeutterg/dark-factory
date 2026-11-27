"""GitHub PR/check JSON commands. Credentials remain on the controller."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

from dark_factory.contracts import ContractError, validate_request
from dark_factory.runner import CommandRunner


def api(path, method="GET", payload=None):
    argv = ["gh", "api", "--method", method, path]
    if payload is not None:
        argv += ["--input", "-"]
    runner = CommandRunner(Path.cwd())
    import os

    code, out, err = runner.supervise(
        argv,
        json.dumps(payload).encode() if payload is not None else b"",
        60,
        Path.cwd(),
        dict(os.environ),
    )
    if code:
        raise ContractError(
            f"GitHub request failed ({code}): {err.decode(errors='replace')[-1000:]}"
        )
    return json.loads(out) if out.strip() else {}


def download_artifact(repo, payload):
    import os

    artifact_id = payload.get("artifact_id")
    expected = str(payload.get("expected_digest", "")).removeprefix("sha256:")
    destination = Path(str(payload.get("destination_path", "")))
    if type(artifact_id) is not int or artifact_id <= 0:
        raise ContractError("positive artifact id required")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ContractError("artifact SHA-256 digest required")
    if not destination.is_absolute() or destination.exists():
        raise ContractError("destination must be a new absolute path")
    runner = CommandRunner(Path.cwd())
    code, archive, err = runner.supervise(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/artifacts/{artifact_id}/zip",
            "-H",
            "Accept: application/vnd.github+json",
        ],
        b"",
        120,
        Path.cwd(),
        dict(os.environ),
    )
    if code:
        raise ContractError(
            "GitHub artifact download failed: " + err.decode(errors="replace")[-500:]
        )
    if hashlib.sha256(archive).hexdigest() != expected:
        raise ContractError("downloaded artifact digest did not match code host")
    destination.mkdir(parents=True, mode=0o700)
    files = []
    import io

    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for member in bundle.infolist():
            relative = Path(member.filename)
            mode = member.external_attr >> 16
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or member.is_dir()
                or mode & 0o170000 == 0o120000
            ):
                if member.is_dir():
                    continue
                raise ContractError("artifact contains an unsafe path or symlink")
            data = bundle.read(member)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files.append(
                {
                    "name": f"{destination.name}/{relative.as_posix()}",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    if not files:
        raise ContractError("artifact archive was empty")
    return {"files": files, "archive_sha256": expected}


BRANCH = re.compile(r"dark-factory/[a-z0-9][a-z0-9._/-]{2,100}")


def _push_exact_commit(repo, source, branch, expected):
    """Push one commit from a fresh repository, never from the agent's checkout.

    The candidate checkout is agent-written: its .git may hold hooks or config that
    would run with the controller's credentials. Only the commit's objects leave it
    (through a bundle, which runs no hooks); the push happens from a new bare
    repository with no hooks, no template and no inherited repository config.
    """
    import os
    import tempfile

    from dark_factory.adapters.git.local import git

    if git(source, "rev-parse", "HEAD") != expected:
        raise ContractError("the candidate checkout is not at the exact commit")
    with tempfile.TemporaryDirectory(prefix="push-") as scratch:
        bundle = Path(scratch) / "candidate.bundle"
        fresh = Path(scratch) / "fresh.git"
        git(source, "bundle", "create", str(bundle), "HEAD")
        fresh.mkdir()
        git(fresh, "init", "--bare", "--template=")
        git(fresh, "fetch", "--no-tags", str(bundle), "HEAD")
        if git(fresh, "rev-parse", "FETCH_HEAD") != expected:
            raise ContractError("the bundle did not contain the exact commit")
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        code, _, err = CommandRunner(fresh).supervise(
            [
                "git",
                "--git-dir",
                str(fresh),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                "push",
                f"https://github.com/{repo}.git",
                f"{expected}:refs/heads/{branch}",
            ],
            b"",
            120,
            fresh,
            env,
        )
    if code:
        raise ContractError(f"push failed: {err.decode(errors='replace')[-500:]}")


def push_branch(op, repo, payload, result):
    """Publish one exact commit as a new branch. Never overwrites, never forces.

    Only branches under ``dark-factory/`` are accepted, so the default branch and
    anyone's work are out of reach. An existing branch at the same commit counts
    as already done; at another commit it is refused.
    """
    branch, expected = payload.get("branch"), payload.get("head_sha")
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or ".." in branch:
        raise ContractError("branch must be a new dark-factory/ branch")
    if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{40}", expected):
        raise ContractError("exact head SHA required")
    ref = f"repos/{repo}/git/ref/heads/{quote(branch, safe='/')}"
    try:
        existing = api(ref).get("object", {}).get("sha")
    except ContractError as exc:
        # Only a missing branch means "absent"; rate limits, auth and network
        # failures must not fall through to a write.
        if "404" not in str(exc) and "not found" not in str(exc).lower():
            raise
        existing = None
    if existing:
        if existing != expected:
            raise ContractError(
                "branch exists at a different commit; refusing to overwrite"
            )
        result["evidence"] = {"observed": True, "branch": branch, "head_sha": expected}
        return
    if op == "reconcile_push_branch":
        result["evidence"] = {
            "observed": False,
            "safe_to_retry": True,
            "head_sha": expected,
        }
        return
    source = payload.get("source_path")
    if not isinstance(source, str) or not Path(source).is_dir():
        raise ContractError("source_path must be the candidate checkout")
    _push_exact_commit(repo, Path(source), branch, expected)
    observed = api(ref).get("object", {}).get("sha")
    if observed != expected:
        raise ContractError("branch read-back did not match the pushed commit")
    result["evidence"] = {"observed": True, "branch": branch, "head_sha": expected}


def repository(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value
    ):
        raise ContractError("repo must be owner/name")
    return value


def handle(request):
    validate_request(request)
    op = request["operation"]
    payload = request["input"]
    repo = repository(payload.get("repo"))
    result = {
        "contract_version": 1,
        "request_id": request["request_id"],
        "operation": op,
        "ok": True,
    }
    if op == "download_run_artifact":
        result["evidence"] = download_artifact(repo, payload)
    elif op == "inspect_pr":
        number = payload.get("number")
        if type(number) is not int or number <= 0:
            raise ContractError("positive PR number required")
        pr = api(f"repos/{repo}/pulls/{number}")
        sha = pr["head"]["sha"]
        checks = api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
        statuses = api(f"repos/{repo}/commits/{sha}/status?per_page=100")
        reviews = api(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
        comments = api(f"repos/{repo}/pulls/{number}/comments?per_page=100")
        if (
            checks.get("total_count", 0) > 100
            or statuses.get("total_count", 0) > 100
            or len(reviews) >= 100
            or len(comments) >= 100
        ):
            raise ContractError(
                "check or review pagination exceeds bootstrap limit; required evidence incomplete"
            )
        result["evidence"] = {
            "external_id": f"github:{repo}:pr:{number}",
            "url": pr["html_url"],
            "head_sha": sha,
            "base_sha": pr["base"]["sha"],
            "base_ref": pr["base"]["ref"],
            "state": pr["state"],
            "merged": pr["merged"],
            "merge_commit_sha": pr.get("merge_commit_sha"),
            "merged_at": pr.get("merged_at"),
            "mergeable": pr["mergeable"],
            "checks": checks["check_runs"],
            "statuses": statuses["statuses"],
            "reviews": [
                {
                    "author": (review.get("user") or {}).get("login", ""),
                    "state": review.get("state", ""),
                    "commit_id": review.get("commit_id", ""),
                    "submitted_at": review.get("submitted_at", ""),
                    "body": review.get("body") or "",
                    "url": review.get("html_url", ""),
                }
                for review in reviews
            ],
            "review_comments": [
                {
                    "author": (comment.get("user") or {}).get("login", ""),
                    "path": comment.get("path", ""),
                    "line": comment.get("line") or comment.get("original_line"),
                    "commit_id": comment.get("commit_id", ""),
                    "created_at": comment.get("created_at", ""),
                    "body": comment.get("body") or "",
                    "url": comment.get("html_url", ""),
                }
                for comment in comments
            ],
            "required_check_policy": "unknown; GitHub branch/ruleset requirements must be checked by the human merger",
        }
    elif op == "list_open_prs":
        prs = api(f"repos/{repo}/pulls?state=open&per_page=100")
        if len(prs) >= 100:
            raise ContractError(
                "more open pull requests than one page; gate incomplete"
            )
        result["evidence"] = {
            "prs": [
                {
                    "number": pr["number"],
                    "head_sha": pr["head"]["sha"],
                    "head_ref": pr["head"]["ref"],
                    "title": pr.get("title", ""),
                    "url": pr["html_url"],
                    "body": pr.get("body") or "",
                }
                for pr in prs
            ]
        }
    elif op == "dispatch_workflow":
        workflow, ref = payload.get("workflow"), payload.get("ref")
        inputs = payload.get("inputs")
        if not isinstance(workflow, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+\.ya?ml", workflow
        ):
            raise ContractError("workflow must be a workflow file name")
        if not isinstance(ref, str) or not ref or ref.startswith("-"):
            raise ContractError("ref required")
        if not isinstance(inputs, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in inputs.items()
        ):
            raise ContractError("workflow inputs must be strings")
        api(
            f"repos/{repo}/actions/workflows/{workflow}/dispatches",
            "POST",
            {"ref": ref, "inputs": inputs},
        )
        result["evidence"] = {"observed": True, "request_id": inputs.get("request_id")}
    elif op == "find_run":
        marker, commit = payload.get("request_id"), payload.get("commit")
        if not isinstance(marker, str) or not marker:
            raise ContractError("request_id required")
        if not isinstance(commit, str) or not re.fullmatch("[0-9a-f]{40}", commit):
            raise ContractError("exact commit required")
        runs = api(f"repos/{repo}/actions/runs?event=workflow_dispatch&per_page=50")
        # The run name carries both the request id and the commit it was asked to
        # build; a run for another commit is never accepted as this stage's.
        matches = [
            r
            for r in runs.get("workflow_runs", [])
            if marker in (r.get("name") or r.get("display_title") or "")
            and commit in (r.get("name") or r.get("display_title") or "")
        ]
        result["evidence"] = {"run_id": matches[0]["id"] if len(matches) == 1 else None}
    elif op == "get_run":
        run = api(f"repos/{repo}/actions/runs/{int(payload['run_id'])}")
        result["evidence"] = {
            "status": run["status"],
            "conclusion": run.get("conclusion") or "",
            "url": run["html_url"],
            "head_sha": run["head_sha"],
        }
    elif op == "list_run_artifacts":
        listing = api(
            f"repos/{repo}/actions/runs/{int(payload['run_id'])}/artifacts?per_page=100"
        )
        if listing.get("total_count", 0) > 100:
            raise ContractError(
                "more artifacts than one page; the digest would be partial"
            )
        # Only artifacts GitHub attributes to this exact run, with the digest GitHub
        # computed, are evidence; a forged upload elsewhere is never listed here.
        result["evidence"] = {
            "artifacts": [
                {
                    "name": a["name"],
                    "size": a["size_in_bytes"],
                    "digest": a.get("digest") or "",
                    "id": a["id"],
                }
                for a in listing.get("artifacts", [])
                if not a.get("expired")
            ]
        }
    elif op == "set_status":
        sha, state = payload.get("sha"), payload.get("state")
        context = payload.get("context")
        if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{40}", sha):
            raise ContractError("exact commit SHA required")
        if state not in {"success", "pending", "failure", "error"}:
            raise ContractError("invalid status state")
        if not isinstance(context, str) or not context.startswith("dark-factory/"):
            raise ContractError("status context must start with dark-factory/")
        api(
            f"repos/{repo}/statuses/{sha}",
            "POST",
            {
                "state": state,
                "context": context,
                "description": str(payload.get("description", ""))[:140],
                **(
                    {"target_url": payload["target_url"]}
                    if isinstance(payload.get("target_url"), str)
                    else {}
                ),
            },
        )
        result["evidence"] = {"observed": True, "sha": sha, "state": state}
    elif op in {"push_branch", "reconcile_push_branch"}:
        push_branch(op, repo, payload, result)
    elif op in {"create_pr", "reconcile_create_pr"}:
        head = payload.get("head")
        base = payload.get("base")
        expected = payload.get("head_sha")
        if not all(
            isinstance(v, str) and v and not v.startswith("-")
            for v in (head, base, expected)
        ):
            raise ContractError("head, base and exact head_sha required")
        if not re.fullmatch("[0-9a-f]{40}", expected):
            raise ContractError("exact head SHA required")
        observed = api(f"repos/{repo}/commits/{quote(head, safe='')}")["sha"]
        if observed != expected:
            raise ContractError("head branch changed; refusing external write")
        marker = f"<!-- dark-factory:{request['request_id']} -->"
        owner = repo.split("/")[0]
        prs = api(
            f"repos/{repo}/pulls?state=all&head={quote(owner + ':' + head, safe='')}&base={quote(base, safe='')}&per_page=100"
        )
        matches = [pr for pr in prs if marker in (pr.get("body") or "")]
        if len(matches) > 1:
            raise ContractError("ambiguous duplicate action marker")
        if matches:
            pr = matches[0]
            if pr["head"]["sha"] != expected:
                raise ContractError("existing PR revision changed")
            result["evidence"] = {
                "observed": True,
                "url": pr["html_url"],
                "number": pr["number"],
                "head_sha": expected,
                "reconciled": True,
            }
        elif op == "reconcile_create_pr":
            result["evidence"] = {
                "observed": False,
                "safe_to_retry": len(prs) < 100,
                "head_sha": expected,
            }
        else:
            if (
                not isinstance(payload.get("title"), str)
                or not payload["title"].strip()
            ):
                raise ContractError("title required")
            if len(prs) >= 100:
                raise ContractError("PR reconciliation pagination incomplete")
            pr = api(
                f"repos/{repo}/pulls",
                "POST",
                {
                    "head": head,
                    "base": base,
                    "title": payload["title"],
                    "body": payload.get("body", "") + "\n\n" + marker,
                    "draft": True,
                },
            )
            confirmed = api(f"repos/{repo}/pulls/{pr['number']}")
            if confirmed["head"]["sha"] != expected or marker not in (
                confirmed.get("body") or ""
            ):
                raise ContractError("PR write read-back did not match intention")
            result["evidence"] = {
                "observed": True,
                "url": confirmed["html_url"],
                "number": confirmed["number"],
                "head_sha": expected,
                "draft": True,
            }
    else:
        raise ContractError(f"unsupported code-host operation {op!r}")
    return result


def main():
    try:
        print(json.dumps(handle(json.load(sys.stdin))))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
