"""Offline golden reviewer evaluation capture, execution, and reporting."""

from __future__ import annotations

import json
import re

import yaml

from dark_factory.store import new_id, now_iso


def capture(controller, work_item_id: str, label: str) -> dict:
    if label not in {"escaped_bug", "human_rejection", "clean_accept"}:
        raise ValueError("invalid eval label")
    root = controller.store.one("SELECT * FROM work_items WHERE id=?", (work_item_id,))
    if not root:
        raise ValueError("work item not found")
    candidate = json.loads(root["candidate_commits"] or "{}").get(root["repo"], {})
    archive = controller.store.resolve_path(candidate.get("archive", ""))
    diff = (
        (archive / "diff.patch").read_text()
        if (archive / "diff.patch").is_file()
        else ""
    )
    reviews = controller.store.query(
        """SELECT a.result_json,a.error FROM attempts a JOIN jobs j ON j.id=a.job_id
        WHERE j.work_item_id=? AND j.kind='review' ORDER BY a.ended_at DESC LIMIT 1""",
        (work_item_id,),
    )
    case = {
        "id": f"{work_item_id}-{label}",
        "origin_root_id": work_item_id,
        "label": label,
        "diff": diff,
        "review": json.loads(reviews[0]["result_json"] or "{}") if reviews else {},
        "ground_truth": {
            "should_block": label != "clean_accept",
            "expected_rules": [],
        },
        "notes": "Captured from verified Dark Factory history; inspect before baseline use.",
    }
    target = (
        controller.cfg.source_path.parent / "evals" / "reviewer" / f"{case['id']}.yaml"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(case, sort_keys=False))
    return {"path": str(target), "case": case["id"]}


def run(controller, suite: str, route_id: str | None = None) -> dict:
    root = controller.cfg.source_path.parent / "evals" / suite
    if suite != "reviewer" or not root.is_dir():
        raise ValueError("reviewer eval suite not found")
    run_id = new_id("eval")
    controller.store.execute(
        """INSERT INTO eval_runs(id,suite,route_id,status,started_at)
        VALUES (?,?,?,?,?)""",
        (run_id, suite, route_id, "running", now_iso()),
    )
    outcomes = []
    for path in sorted(root.glob("*.yaml")):
        case = yaml.safe_load(path.read_text())
        truth = bool(case.get("ground_truth", {}).get("should_block"))
        review = case.get("review", {})
        # Captured cases retain the historical independent verdict. Hand-authored
        # golden cases can provide observed.should_block after an external eval run.
        observed = case.get("observed", {}).get("should_block")
        blocked = bool(observed if observed is not None else not review.get("ok", True))
        rules = sorted(
            str(finding["rule_id"])
            for finding in review.get("findings", [])
            if isinstance(finding, dict) and finding.get("rule_id")
        )
        controller.store.execute(
            """INSERT INTO eval_case_results
            (run_id,case_id,status,should_block,blocked,rules_json,detail_json)
            VALUES (?,?,?,?,?,?,?)""",
            (
                run_id,
                str(case.get("id") or path.stem),
                "passed" if blocked == truth else "failed",
                int(truth),
                int(blocked),
                json.dumps(rules),
                json.dumps({"path": str(path)}),
            ),
        )
        outcomes.append((truth, blocked))
    tp = sum(truth and blocked for truth, blocked in outcomes)
    fp = sum(not truth and blocked for truth, blocked in outcomes)
    fn = sum(truth and not blocked for truth, blocked in outcomes)
    tn = sum(not truth and not blocked for truth, blocked in outcomes)
    report = {
        "run_id": run_id,
        "cases": len(outcomes),
        "recall": tp / (tp + fn) if tp + fn else 1.0,
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "false_positive_rate": fp / (fp + tn) if fp + tn else 0.0,
        "regression": bool(fn or fp),
    }
    controller.store.execute(
        "UPDATE eval_runs SET status='complete',ended_at=?,report_json=? WHERE id=?",
        (now_iso(), json.dumps(report), run_id),
    )
    return report


def report(controller, run_id: str | None = None) -> dict:
    if run_id and not re.fullmatch(r"eval_[a-f0-9]{12}", run_id):
        raise ValueError("invalid eval run id")
    row = controller.store.one(
        "SELECT * FROM eval_runs WHERE id=?"
        if run_id
        else "SELECT * FROM eval_runs ORDER BY started_at DESC LIMIT 1",
        (run_id,) if run_id else (),
    )
    if not row:
        raise ValueError("eval run not found")
    return json.loads(row["report_json"])
