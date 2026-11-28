"""Operator CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dark_factory import operator_commands
from dark_factory.config import (
    ConfigError,
    load_config,
    load_operator_endpoint,
    load_operator_token,
)
from dark_factory.controller import Controller, ControllerError
from dark_factory.secrets import secret
from dark_factory.store import Store
from dark_factory.worker import Worker


def _housekeeping_loop(ctrl, store, stop, interval=1.0):
    """Fence and reconcile continuously, even with no execution worker polling."""
    last_error = None
    while not stop.is_set():
        try:
            ctrl.recover("housekeeping")
            ctrl.settle_interruptions("housekeeping")
            ctrl.completeness_tick()
            last_error = None
        except Exception as exc:
            if str(exc) != last_error:
                store.audit(
                    "housekeeping", "controller.error", {"error": str(exc)[:500]}
                )
            last_error = str(exc)
        stop.wait(interval)


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
    sub.add_parser("discover", help="List bounded tracker candidates without intake")
    sub.add_parser(
        "preflight",
        help="Read-only installation/configuration checks; no inference or state access",
    )
    p_definition = sub.add_parser(
        "definition-validate",
        help="Validate a Warp v1alpha1 definition and report supported semantics",
    )
    p_definition.add_argument("path", type=Path)
    p_definition.add_argument("--no-strict", action="store_true")
    p_pipeline = sub.add_parser(
        "pipeline-validate",
        help="Validate a repository's pipeline file as read from its main branch",
    )
    p_pipeline.add_argument("repository", type=Path)
    p_pipeline.add_argument("--ref", default="main")
    p_init = sub.add_parser(
        "init-repo",
        help="Write the pipeline skeleton and agent instructions into a repository",
    )
    p_init.add_argument("repository", type=Path)
    p_mcp = sub.add_parser("mcp", help="Run the contributor-scoped stdio MCP server")
    p_mcp.add_argument("--actor", default="mcp")
    p_improve = sub.add_parser(
        "improve",
        help="Report recurring failure patterns as inert improvement proposals",
    )
    p_improve.add_argument("--minimum-occurrences", type=int, default=3)
    p_eval_capture = sub.add_parser("eval-capture")
    p_eval_capture.add_argument("work_item_id")
    p_eval_capture.add_argument(
        "--label",
        required=True,
        choices=["escaped_bug", "human_rejection", "clean_accept"],
    )
    p_eval_run = sub.add_parser("eval-run")
    p_eval_run.add_argument("--suite", default="reviewer")
    p_eval_run.add_argument("--route")
    p_eval_report = sub.add_parser("eval-report")
    p_eval_report.add_argument("--run-id")
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
    p_gate.add_argument("--failed", dest="passed", action="store_false", default=True)
    p_task_add = sub.add_parser("task-add")
    p_task_add.add_argument("work_item_id")
    p_task_add.add_argument("title")
    p_task_add.add_argument(
        "--owner-kind",
        choices=["agent", "controller", "human", "external"],
        default="agent",
    )
    p_task_add.add_argument("--description", default="")
    p_task_add.add_argument("--expected-revision", type=int)
    p_task_add.add_argument("--idempotency-key")
    p_task_add.add_argument("--actor", default="operator")
    p_task_update = sub.add_parser("task-update")
    p_task_update.add_argument("work_item_id")
    p_task_update.add_argument("task_id")
    p_task_update.add_argument("--expected-revision", type=int, required=True)
    p_task_update.add_argument("--title")
    p_task_update.add_argument("--priority", type=int)
    p_task_update.add_argument("--depends-on", type=json.loads)
    p_task_update.add_argument("--actor", default="operator")
    p_task_complete = sub.add_parser("task-complete")
    p_task_complete.add_argument("work_item_id")
    p_task_complete.add_argument("task_id")
    p_task_complete.add_argument("--expected-revision", type=int, required=True)
    p_task_complete.add_argument("--evidence", default="")
    p_task_complete.add_argument("--actor", default="operator")
    p_task_reopen = sub.add_parser("task-reopen")
    p_task_reopen.add_argument("work_item_id")
    p_task_reopen.add_argument("task_id")
    p_task_reopen.add_argument("--expected-revision", type=int, required=True)
    p_task_reopen.add_argument("--reason", required=True)
    p_task_reopen.add_argument("--actor", default="operator")
    p_task_export = sub.add_parser("task-export")
    p_task_export.add_argument("work_item_id")
    p_task_import = sub.add_parser("task-import")
    p_task_import.add_argument("work_item_id")
    p_task_import.add_argument("--expected-revision", type=int)
    p_task_import.add_argument("--actor", default="operator")
    p_task_supervisor = sub.add_parser("task-supervisor-apply")
    p_task_supervisor.add_argument("work_item_id")
    p_task_supervisor.add_argument("--payload-json", required=True)
    p_task_supervisor.add_argument("--actor", default="task-supervisor")
    p_task_reconcile = sub.add_parser("task-reconcile")
    p_task_reconcile.add_argument("work_item_id")
    p_task_reconcile.add_argument("--actor", default="operator")
    p_pr = sub.add_parser("create-pr")
    p_pr.add_argument("work_item_id")
    p_pr.add_argument("--payload-json", required=True)
    p_pr.add_argument("--actor", required=True)
    p_ws = sub.add_parser("work-start", help="Start (or find) a root from an issue")
    p_ws.add_argument("repo")
    p_ws.add_argument("number", type=int)
    p_ws.add_argument("--title", default="")
    p_ws.add_argument("--body", default="")
    p_ws.add_argument("--actor", default="contributor")
    p_wp = sub.add_parser("work-plan", help="Submit a plan draft or ask for one")
    p_wp.add_argument("work_item_id")
    p_wp.add_argument("--proposal", default="", help="JSON plan draft")
    p_wp.add_argument("--actor", default="contributor")
    for name, text in (
        ("work-checkout", "Get a factory-owned worktree for approved work"),
        ("work-submit", "Hand the worktree to the factory as a candidate"),
        ("work-status", "Show the stage and what blocks it"),
    ):
        p_w = sub.add_parser(name, help=text)
        p_w.add_argument("work_item_id")
        p_w.add_argument("--actor", default="contributor")
        if name == "work-submit":
            p_w.add_argument("--bundle", type=Path)
    p_rs = sub.add_parser("release-start", help="Assemble a release from a branch head")
    p_rs.add_argument("repo")
    p_rs.add_argument("kind")
    p_rs.add_argument("--ref", default=None)
    p_rs.add_argument("--version")
    p_rs.add_argument("--actor", default="operator")
    p_ra = sub.add_parser("release-approve", help="A person approves a release gate")
    p_ra.add_argument("release_id")
    p_ra.add_argument("gate", choices=["package", "promote"])
    p_ra.add_argument("--actor", required=True)
    p_rc = sub.add_parser("release-confirm", help="A person confirms a manual item")
    p_rc.add_argument("release_id")
    p_rc.add_argument("--item", required=True)
    p_rc.add_argument("--evidence", default="")
    p_rc.add_argument("--actor", required=True)
    p_rb = sub.add_parser("release-rollback", help="Redeploy the previous kept package")
    p_rb.add_argument("release_id")
    p_rb.add_argument("--actor", required=True)
    for name, text in (
        ("release-retry", "A person retries a failed release from the failed stage"),
        ("release-cancel", "A person cancels a release"),
    ):
        p_h = sub.add_parser(name, help=text)
        p_h.add_argument("release_id")
        p_h.add_argument("--actor", required=True)
    for name, text in (
        ("release-run", "Run the next automatic stage of a release"),
        ("release-status", "Show a release's stages, approvals and next step"),
    ):
        p_r = sub.add_parser(name, help=text)
        p_r.add_argument("release_id")
        p_r.add_argument("--actor", default="operator")
    sub.add_parser("release-list", help="List releases")
    sub.add_parser(
        "metrics", help="Duration and outcome of every stage, by repo and runner"
    )
    sub.add_parser(
        "completeness-report", help="Compare shadow completeness advice with reviews"
    )
    p_quality_run = sub.add_parser(
        "quality-run", help="Run the evidence observer and due pattern analysis"
    )
    p_quality_run.add_argument("--repo")
    p_quality_run.add_argument("--actor", default="operator")
    p_quality_status = sub.add_parser(
        "quality-status", help="Show per-repository quality memory"
    )
    p_quality_status.add_argument("--repo")
    p_quality_approve = sub.add_parser(
        "quality-pattern-approve",
        help="Approve a recommendation; instruction patterns may enter improvement",
    )
    p_quality_approve.add_argument("pattern_id")
    p_quality_approve.add_argument("--actor", required=True)
    sub.add_parser("disk", help="Show disk use by area and by root")
    sub.add_parser("gc", help="Remove resources whose owners are finished")
    p_abandon = sub.add_parser(
        "abandon-action",
        help="Close a pending or ambiguous external write held on one root",
    )
    p_abandon.add_argument("work_item_id")
    p_abandon.add_argument("--reason", required=True)
    p_abandon.add_argument("--actor", default="operator")
    p_evidence = sub.add_parser(
        "pr-evidence",
        help="Read a PR's checks and reviews as outside evidence for the candidate",
    )
    p_evidence.add_argument("work_item_id")
    p_evidence.add_argument(
        "--number", type=int, help="PR number; default is the PR made by create-pr"
    )
    p_evidence.add_argument("--actor", default="operator")
    p_continue = sub.add_parser(
        "continue-work", help="Resume an interrupted step from its saved work"
    )
    p_continue.add_argument("work_item_id")
    p_continue.add_argument("--actor", default="operator")
    p_stop = sub.add_parser("stop-work", help="Stop work after an interruption")
    p_stop.add_argument("work_item_id")
    p_stop.add_argument("--reason", default="stopped after an interruption")
    p_stop.add_argument("--actor", default="operator")
    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("job_id")
    p_run = sub.add_parser("run-worker")
    p_run.add_argument("--max-jobs", type=int, default=0)
    p_run.add_argument("--worker-id", default="local")
    for name in ("execution-worker", "runner"):
        p_remote = sub.add_parser(
            name, help="Run a project-scoped outbound execution runner"
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
    p_tui = sub.add_parser("tui", help="Open the controller operator console")
    p_tui.add_argument("--actor", default="operator")
    p_tui.add_argument(
        "--embedded",
        action="store_true",
        help="Open controller state in this TUI process instead of using the service",
    )
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
    p_adapter.add_argument(
        "name",
        choices=[
            "github-tracking",
            "github-codehost",
            "jev-decision",
            "task-supervisor",
            "quality-observer",
        ],
    )
    p_adapter.add_argument(
        "adapter_args",
        nargs=argparse.REMAINDER,
        help="tracking options such as --project owner/number",
    )
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
    if args.cmd == "definition-validate":
        from dark_factory.adapters.definition.warp import (
            load_definition,
            public_report,
        )

        result = load_definition(args.path, strict=not args.no_strict)
        print(json.dumps(public_report(result), indent=2))
        return 0
    if args.cmd == "pipeline-validate":
        from dark_factory.pipeline import read_pipeline

        pipeline, commit = read_pipeline(args.repository, args.ref)
        if pipeline is None:
            raise ValueError(f"no pipeline file at {args.ref} ({commit[:12]})")
        print(
            json.dumps(
                {
                    "commit": commit,
                    "digest": pipeline.digest,
                    "change": {
                        n: (
                            "not applicable: " + s.not_applicable
                            if not s.applicable
                            else f"{len(s.steps)} step(s) on {s.runner}"
                        )
                        for n, s in pipeline.change.items()
                    },
                    "releases": sorted(pipeline.releases),
                },
                indent=2,
            )
        )
        return 0
    if args.cmd == "init-repo":
        from dark_factory.pipeline import init_repository

        print(json.dumps({"written": init_repository(args.repository)}, indent=2))
        return 0
    if args.cmd == "mcp":
        if not args.config:
            raise ConfigError("--config is required for mcp")
        from dark_factory.mcp import serve as serve_mcp
        from dark_factory.ui.transport import RemoteTransport

        endpoint = args.api_url or os.environ.get("DARK_FACTORY_URL")
        if not endpoint:
            endpoint = load_operator_endpoint(args.config)
        cfg = load_config(args.config)
        if not cfg.contributor_token:
            raise ConfigError("mcp requires a configured contributor token")
        serve_mcp(RemoteTransport(endpoint, cfg.contributor_token), args.actor)
        return 0
    if args.cmd in {"execution-worker", "runner"}:
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
            "max_parallel",
        }:
            raise ConfigError("unknown execution worker profile fields")
        slots = data.get("max_parallel", 1)
        if type(slots) is not int or not 1 <= slots <= 32:
            raise ConfigError("runner max_parallel must be between 1 and 32")
        state = args.profile.parent / data["state_dir"]

        def make_client(slot):
            slot_state = state if slots == 1 else state / f"slot-{slot}"
            return WorkerClient(
                data["url"],
                data["project_id"],
                data["worker_id"],
                secret(data["token_env"]),
                slot_state,
                data.get("accounts", {}),
                args.profile.parent / data["ca_file"] if data.get("ca_file") else None,
            )

        from dark_factory import log

        log.configure()
        clients = [make_client(slot) for slot in range(slots)]
        if args.max_jobs or slots == 1:
            count = clients[0].serve(args.max_jobs)
        else:
            import threading

            threads = [
                threading.Thread(
                    target=item.serve,
                    name=f"dark-factory-runner-{slot}",
                )
                for slot, item in enumerate(clients)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            count = 0
        print(json.dumps({"jobs_run": count}))
        return 0
    if args.cmd == "adapter":
        if args.name == "github-tracking":
            from dark_factory.adapters.projects.github import main as tracking_main

            tracking_main(args.adapter_args)
            return 0
        if args.name == "task-supervisor":
            from dark_factory.adapters.decision.task_supervisor import (
                main as supervisor_main,
            )

            supervisor_main(args.adapter_args)
            return 0
        if args.name == "quality-observer":
            from dark_factory.adapters.decision.quality_observer import (
                main as quality_main,
            )

            quality_main(args.adapter_args)
            return 0
        if args.name == "github-codehost":
            from dark_factory.adapters.git.github import main as git_main

            if args.adapter_args:
                raise ConfigError("github-codehost does not accept adapter options")
            git_main()
            return 0
        from dark_factory.adapters.decision.jev import main as decision_main

        decision_main(args.adapter_args)
        return 0
    if not args.config:
        raise ConfigError("--config is required for controller commands")
    if args.cmd == "preflight":
        from dark_factory.preflight import inspect_configuration

        result = inspect_configuration(load_config(args.config))
        print(json.dumps(result, indent=2))
        return 0 if result["preflight_passed"] else 2
    if args.cmd == "restore":
        from dark_factory.artifacts import restore

        restore(Path(args.source), Path(args.destination))
        return 0
    if args.cmd == "tui":
        from dark_factory.ui.app import run_tui
        from dark_factory.ui.transport import LocalTransport, RemoteTransport

        if args.embedded:
            if args.api_url:
                raise ConfigError("tui --embedded cannot be combined with --api-url")
            ctrl, store = build_controller(args.config)
            try:
                run_tui(LocalTransport(ctrl), args.actor)
                return 0
            finally:
                store.close()
        token = load_operator_token(args.config)
        endpoint = args.api_url or load_operator_endpoint(args.config)
        try:
            run_tui(RemoteTransport(endpoint, token), args.actor)
            return 0
        except OSError as exc:
            raise ControllerError(
                f"controller unavailable at {endpoint}; start it with "
                f"'dark-factory --config {args.config} serve --worker'"
            ) from exc
    command = operator_commands.COMMANDS.get(args.cmd)
    api_url = args.api_url or os.environ.get("DARK_FACTORY_URL")

    def run_remote(endpoint):
        from dark_factory.ui.transport import RemoteTransport

        if command is None or not command.api:
            raise ControllerError(f"{args.cmd} runs locally; omit --api-url")
        transport = RemoteTransport(endpoint, load_operator_token(args.config))
        result = transport.command(args.cmd, _params(args, command), _actor(args))
        text, code = command.render(result)
        # Same output and exit status as the local command.
        if text is not None:
            print(text)
        return code

    if api_url:
        return run_remote(api_url)
    try:
        ctrl, store = build_controller(args.config)
    except RuntimeError as exc:
        if command is None or not command.api or "controller owner" not in str(exc):
            raise
        return run_remote(load_operator_endpoint(args.config))
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
        if args.cmd == "improve":
            from dark_factory.improvement import analyze_improvements

            print(
                json.dumps(
                    analyze_improvements(store, ctrl.cfg, args.minimum_occurrences),
                    indent=2,
                )
            )
            return 0
        if args.cmd in {"eval-capture", "eval-run", "eval-report"}:
            from dark_factory.evals import capture, report, run

            if args.cmd == "eval-capture":
                result = capture(ctrl, args.work_item_id, args.label)
            elif args.cmd == "eval-run":
                result = run(ctrl, args.suite, args.route)
            else:
                result = report(ctrl, args.run_id)
            print(json.dumps(result, indent=2))
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
                (str(args.tls_cert), str(args.tls_key)) if args.tls_cert else None,
            )
            print(json.dumps({"listening": f"{host}:{port}"}))
            import threading

            stop = threading.Event()

            def local_worker():
                last_error = None
                while not stop.is_set():
                    try:
                        Worker(ctrl.cfg, store, ctrl, "local").run_loop()
                        last_error = None
                    except Exception as exc:
                        # The loop retries every second; audit each distinct failure once.
                        if str(exc) != last_error:
                            store.audit("local", "worker.error", {"error": str(exc)})
                        last_error = str(exc)
                    stop.wait(1)

            worker_thread = (
                threading.Thread(target=local_worker, daemon=True)
                if args.worker
                else None
            )
            housekeeping_thread = threading.Thread(
                target=_housekeeping_loop,
                args=(ctrl, store, stop),
                name="dark-factory-housekeeping",
                daemon=True,
            )
            housekeeping_thread.start()
            if worker_thread:
                worker_thread.start()
            import signal

            from dark_factory import log

            log.configure()
            reason = ["interrupt"]

            def terminate(signum, frame):
                reason[0] = signal.Signals(signum).name
                raise KeyboardInterrupt

            signal.signal(signal.SIGTERM, terminate)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                # Drain, stop running work's process groups, record why. Roots are
                # not cancelled: after a restart recovery resumes interrupted work.
                stop.set()
                ctrl.drain("shutdown")
                ctrl.stop_active_processes(reason[0])
                housekeeping_thread.join(timeout=15)
                if worker_thread:
                    worker_thread.join(timeout=15)
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
        elif name == "bundle":
            import base64

            params[name] = (
                base64.b64encode(args.bundle.read_bytes()).decode()
                if args.bundle
                else None
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
