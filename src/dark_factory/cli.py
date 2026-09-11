"""Operator CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dark_factory.config import ConfigError, load_config
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.worker import Worker


def packaged_prompts() -> Path:
    return Path(__file__).parent / "agents" / "prompts"


def build_controller(config_path: str) -> tuple[Controller, Store]:
    cfg = load_config(config_path)
    store = Store(cfg.state_dir)
    return Controller(cfg, store, packaged_prompts()), store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dark-factory", description="Dark Factory bootstrap controller"
    )
    parser.add_argument("--api-url", help="Use an already running local controller")
    parser.add_argument("--config", required=True, help="Path to factory.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    p_intake = sub.add_parser("intake")
    p_intake.add_argument(
        "--issues-json", required=True, help="JSON file of selected issues"
    )
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("work_item_id")
    p_approve = sub.add_parser("approve")
    p_approve.add_argument("work_item_id")
    p_approve.add_argument("--actor", default="operator")
    p_reject = sub.add_parser("reject")
    p_reject.add_argument("work_item_id")
    p_reject.add_argument("--reason", default="rejected")
    p_reject.add_argument("--actor", default="operator")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("drain")
    sub.add_parser("undrain")
    sub.add_parser("recover")
    sub.add_parser("cleanup")
    sub.add_parser("reload")
    p_backup = sub.add_parser("backup")
    p_backup.add_argument("destination")
    p_restore = sub.add_parser("restore")
    p_restore.add_argument("source")
    p_restore.add_argument("destination")
    p_gate = sub.add_parser("manual-gate")
    p_gate.add_argument("work_item_id")
    p_gate.add_argument("--name", required=True)
    p_gate.add_argument("--candidate", required=True)
    p_gate.add_argument("--actor", required=True)
    p_gate.add_argument("--evidence", required=True)
    p_pr = sub.add_parser("create-pr")
    p_pr.add_argument("work_item_id")
    p_pr.add_argument("--payload-json", required=True)
    p_pr.add_argument("--actor", required=True)
    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("job_id")
    p_run = sub.add_parser("run-worker")
    p_run.add_argument("--max-jobs", type=int, default=0)
    p_run.add_argument("--worker-id", default="local")
    p_accept = sub.add_parser("accept")
    p_accept.add_argument("work_item_id")
    p_accept.add_argument("--actor", default="operator")
    p_proposal = sub.add_parser("proposal")
    p_proposal.add_argument("work_item_id")
    p_proposal.add_argument("--version", type=int)
    p_review = sub.add_parser("review")
    p_review.add_argument("work_item_id")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument(
        "--worker",
        action="store_true",
        help="Run the single local worker in this controller process",
    )
    p_adapter = sub.add_parser("adapter")
    p_adapter.add_argument("name", choices=["github-tracking", "github-codehost"])
    demo = sub.add_parser("demo")
    demo.add_argument(
        "--issues-json", help="optional issues file; default uses a fixture"
    )

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (ConfigError, ControllerError, RuntimeError, ValueError, OSError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "adapter":
        if args.name == "github-tracking":
            from dark_factory.adapters.projects.github import main as tracking_main

            tracking_main()
            return 0
        from dark_factory.adapters.git.github import main as git_main

        git_main()
        return 0

    if args.cmd == "restore":
        from dark_factory.artifacts import restore

        restore(Path(args.source), Path(args.destination))
        return 0
    if args.api_url:
        from urllib.request import Request, urlopen

        cfg = load_config(args.config)
        data = vars(args).copy()
        if args.cmd == "intake":
            data["issues"] = json.loads(Path(args.issues_json).read_text())
        req = Request(
            args.api_url.rstrip("/") + "/operator",
            data=json.dumps(data).encode(),
            headers={
                "Authorization": f"Bearer {cfg.operator_token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=30) as response:
            print(response.read().decode())
        return 0
    ctrl, store = build_controller(args.config)
    try:
        if args.cmd == "backup":
            from dark_factory.artifacts import backup

            backup(store, Path(args.destination))
            return 0
        if args.cmd == "manual-gate":
            ctrl.record_manual_gate(
                args.work_item_id, args.name, args.candidate, args.actor, args.evidence
            )
            return 0
        if args.cmd == "create-pr":
            result = ctrl.external_action(
                args.work_item_id,
                "create_pr",
                json.loads(Path(args.payload_json).read_text()),
                args.actor,
            )
            print(json.dumps(result, indent=2))
            return 0
        if args.cmd == "cleanup":
            print(json.dumps({"cleaned": ctrl.cleanup()}))
            return 0
        if args.cmd == "recover":
            print(json.dumps({"recovered": ctrl.recover()}))
            return 0
        if args.cmd == "reload":
            return 0 if ctrl.reload() else 2
        if args.cmd == "undrain":
            ctrl.undrain("operator")
            return 0
        if args.cmd == "status":
            print(json.dumps(ctrl.status(), indent=2))
            return 0
        if args.cmd == "intake":
            issues = json.loads(Path(args.issues_json).read_text(encoding="utf-8"))
            ids = ctrl.intake_issues(issues)
            print(json.dumps({"ids": ids}))
            return 0
        if args.cmd == "plan":
            graph = ctrl.start_planning(args.work_item_id)
            print(json.dumps({"jobs": [j.job_id for j in graph.jobs]}))
            return 0
        if args.cmd == "approve":
            digest = ctrl.approve(args.work_item_id, args.actor)
            print(json.dumps({"digest": digest}))
            return 0
        if args.cmd == "reject":
            ctrl.reject(args.work_item_id, args.actor, args.reason)
            return 0
        if args.cmd == "pause":
            ctrl.pause("operator")
            return 0
        if args.cmd == "resume":
            ctrl.resume("operator")
            return 0
        if args.cmd == "drain":
            ctrl.drain("operator")
            return 0
        if args.cmd == "cancel":
            ctrl.cancel_job(args.job_id, "operator")
            return 0
        if args.cmd == "run-worker":
            worker = Worker(ctrl.cfg, store, ctrl, args.worker_id)
            n = worker.run_loop(args.max_jobs)
            print(json.dumps({"jobs_run": n}))
            return 0
        if args.cmd == "accept":
            ctrl.accept(args.work_item_id, args.actor)
            return 0
        if args.cmd == "proposal":
            print(json.dumps(ctrl.proposal(args.work_item_id, args.version), indent=2))
            return 0
        if args.cmd == "review":
            row = store.one(
                "SELECT packet_json, status FROM work_items WHERE id=?",
                (args.work_item_id,),
            )
            if not row:
                raise ControllerError("work item not found")
            print(row["packet_json"] or "{}")
            return 0
        if args.cmd == "serve":
            from dark_factory.api import serve

            host = args.host or ctrl.cfg.api_host
            port = args.port or ctrl.cfg.api_port
            server = serve(ctrl, host, port, ctrl.cfg.worker_token)
            print(json.dumps({"listening": f"{host}:{port}"}))
            import threading

            stop = threading.Event()

            def local_worker():
                while not stop.is_set():
                    try:
                        ctrl.recover("local-recovery")
                        Worker(ctrl.cfg, store, ctrl, "local").run_loop()
                    except Exception as exc:
                        store.audit("local", "worker.error", {"error": str(exc)})
                    stop.wait(1)

            thread = (
                threading.Thread(target=local_worker, daemon=True)
                if args.worker
                else None
            )
            if thread:
                thread.start()
            try:
                server.serve_forever()
            finally:
                stop.set()
                ctrl.drain("shutdown")
                for job in store.query(
                    "SELECT id FROM jobs WHERE status IN ('leased','running')"
                ):
                    ctrl.cancel_job(job["id"], "shutdown")
                if thread:
                    thread.join()
                server.server_close()
            return 0
        if args.cmd == "demo":
            return _demo(ctrl, store, args.issues_json)
        return 1
    finally:
        store.close()


def _demo(ctrl: Controller, store: Store, issues_json: str | None) -> int:
    if any(route.harness != "fake" for route in ctrl.cfg.routes.values()):
        raise ControllerError("demo requires explicit fake routes")
    if issues_json:
        issues = json.loads(Path(issues_json).read_text(encoding="utf-8"))
    else:
        issues = [
            {
                "repo": ctrl.cfg.repositories[0],
                "number": 1,
                "title": "Bootstrap demo outcome",
                "body": "Make message() return ready while preserving the canonical verification contract.",
                "is_root": True,
            }
        ]
    ids = ctrl.intake_issues(issues)
    wid = ids[0]
    ctrl.start_planning(wid)
    worker = Worker(ctrl.cfg, store, ctrl, "demo")
    worker.run_loop(max_jobs=2)  # plan + critique
    digest = ctrl.approve(wid, "simulation-demo")
    worker.run_loop(max_jobs=10)
    status = ctrl.status()
    print(
        json.dumps({"work_item_id": wid, "digest": digest, "status": status}, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
