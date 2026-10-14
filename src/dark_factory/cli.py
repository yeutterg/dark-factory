"""Operator CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dark_factory import operator_commands
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dark-factory", description="Dark Factory bootstrap controller"
    )
    parser.add_argument("--api-url", help="Use an already running local controller")
    parser.add_argument(
        "--config", help="Path to factory.toml (not used by execution-worker)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser(
        "preflight",
        help="Read-only installation/configuration checks; no inference or state access",
    )
    p_intake = sub.add_parser("intake")
    p_intake.add_argument(
        "--issues-json", required=True, help="JSON file of selected issues"
    )
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("work_item_id")
    p_plan.add_argument("--actor", default="operator")
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
    p_remote = sub.add_parser(
        "execution-worker", help="Run a project-scoped outbound execution worker"
    )
    p_remote.add_argument("--profile", required=True, type=Path)
    p_remote.add_argument("--max-jobs", type=int, default=0)
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
    p_serve.add_argument("--tls-cert", type=Path)
    p_serve.add_argument("--tls-key", type=Path)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except (ConfigError, ControllerError, RuntimeError, ValueError, OSError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "execution-worker":
        import os
        import time
        import tomllib

        from dark_factory.execution.client import WorkerClient

        data = tomllib.loads(args.profile.read_text())
        if set(data) - {
            "url",
            "project_id",
            "worker_id",
            "token_env",
            "state_dir",
            "accounts",
            "ca_file",
        }:
            raise ConfigError("unknown execution worker profile fields")
        client = WorkerClient(
            data["url"],
            data["project_id"],
            data["worker_id"],
            os.environ.get(data["token_env"], ""),
            args.profile.parent / data["state_dir"],
            data.get("accounts", {}),
            args.profile.parent / data["ca_file"] if data.get("ca_file") else None,
        )
        count = 0
        while not args.max_jobs or count < args.max_jobs:
            if client.run_once():
                count += 1
            elif args.max_jobs:
                break
            else:
                time.sleep(1)
        print(json.dumps({"jobs_run": count}))
        return 0
    if not args.config:
        raise ConfigError("--config is required for controller commands")
    if args.cmd == "preflight":
        from dark_factory.preflight import inspect_configuration

        result = inspect_configuration(load_config(args.config))
        print(json.dumps(result, indent=2))
        return 0 if result["preflight_passed"] else 2
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
    command = operator_commands.COMMANDS.get(args.cmd)
    if args.api_url:
        from urllib.request import Request, urlopen

        if command is None or not command.api:
            raise ControllerError(f"{args.cmd} runs locally; omit --api-url")
        cfg = load_config(args.config)
        data = {
            "cmd": args.cmd,
            "actor": _actor(args),
            **_params(args, command),
        }
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
        if ctrl.cfg.workers and (
            args.cmd == "run-worker" or args.cmd == "serve" and args.worker
        ):
            raise ConfigError(
                "project-scoped workers run separately; use execution-worker"
            )
        if command is not None:
            result = operator_commands.run(
                ctrl, args.cmd, _params(args, command), _actor(args)
            )
            text, code = command.render(result)
            if text is not None:
                print(text)
            return code
        if args.cmd == "backup":
            from dark_factory.artifacts import backup

            backup(store, Path(args.destination))
            return 0
        if args.cmd == "run-worker":
            worker = Worker(ctrl.cfg, store, ctrl, args.worker_id)
            n = worker.run_loop(args.max_jobs)
            print(json.dumps({"jobs_run": n}))
            return 0
        if args.cmd == "serve":
            from dark_factory.api import serve

            host = args.host or ctrl.cfg.api_host
            port = args.port or ctrl.cfg.api_port
            if bool(args.tls_cert) != bool(args.tls_key):
                raise ConfigError("TLS certificate and key must be supplied together")
            server = serve(
                ctrl,
                host,
                port,
                ctrl.cfg.worker_token,
                (str(args.tls_cert), str(args.tls_key)) if args.tls_cert else None,
            )
            print(json.dumps({"listening": f"{host}:{port}"}))
            import threading

            stop = threading.Event()

            def local_worker():
                last_error = None
                while not stop.is_set():
                    try:
                        ctrl.recover("local-recovery")
                        Worker(ctrl.cfg, store, ctrl, "local").run_loop()
                        last_error = None
                    except Exception as exc:
                        # The loop retries every second; audit each distinct failure once.
                        if str(exc) != last_error:
                            store.audit("local", "worker.error", {"error": str(exc)})
                        last_error = str(exc)
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


def _actor(args: argparse.Namespace) -> str:
    return getattr(args, "actor", None) or "operator"


def _params(
    args: argparse.Namespace, command: operator_commands.OperatorCommand
) -> dict:
    params = {}
    for name in command.params:
        if name == "issues":
            params[name] = json.loads(
                Path(args.issues_json).read_text(encoding="utf-8")
            )
        elif name == "payload":
            params[name] = json.loads(
                Path(args.payload_json).read_text(encoding="utf-8")
            )
        else:
            params[name] = getattr(args, name)
    return params


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
