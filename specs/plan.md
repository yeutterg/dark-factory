# Plan: bootstrap structure

The package remains under `src/dark_factory/`. One controller owns one state directory. Local or outbound project-scoped workers execute fixed bounded graphs for single-repository outcomes. Multi-repo execution and fallback remain increment A. The TUI is a later operator adapter, not part of execution authority.

| Module | Responsibility |
| --- | --- |
| `config.py` | Strict TOML, explicit asset/source/command references, route validation |
| `models.py` | Normalized job/graph and record definitions |
| `store.py` | SQLite schema, process lock, transactions, audit and restored path resolution |
| `contracts.py`, `runner.py` | Versioned JSON and bounded process supervision |
| `controller.py` | Composes the controller; the only object that changes execution state |
| `control/policy.py` | Exact approval inputs, evaluated once per decision and compared at approval, dispatch and acceptance |
| `control/planning.py` | Intake, proposal versions, approval and rejection |
| `control/dispatch.py` | Budgets, claims, leases, recovery, cancellation and cleanup |
| `control/results.py`, `control/roles.py` | Attempt settlement, role output validation and the preapproved repair branch |
| `control/review.py`, `control/external.py` | Coverage, packets, continuations, human acceptance and external intentions |
| `attempts.py` | Attempt facts a worker reports through the controller or a remote replica |
| `worker.py` | Prepare, execute, capture, archive, cleanup |
| `artifacts.py` | Hash manifests, filesystem synchronization, backup/restore |
| `operator_commands.py`, `api.py`, `cli.py`, `ui/` | One operator command table for CLI/API/TUI surfaces; separate operator and worker credentials |

Three application-owned lifecycle protocols remain: WorkflowRuntime, Harness and Environment. WorkflowRuntime selects native execution or the pinned Pi Durable dispatch adapter; the controller retains scheduling policy and acceptance authority. The harness registry selects Pi, Claude Code or Codex CLI independently for each task route. The macOS environment enforces file/network/process policy. FakeHarness is limited to deterministic tests and explicitly labeled demonstrations. GitHub tracker/code-host modules are named command adapters; local Git operations are helpers rather than another integration protocol. See [durable execution design](durable-runtime.md).

Roots retain selected intent, versioned proposals, approved scope/budgets/assets/command bindings, the pinned base and candidate commits. Each attempt has fresh Git state and harness context, immutable inputs, an enforced deadline, a renewable lease, usage and durable evidence. Checks reconstruct the candidate from its verified archive. Review receives approved requirements, exact diff and canonical evidence, with implementer narration omitted. Canonical checks cannot be waived by model output.

The local worker can run synchronously through CLI or within the controller service. Outbound execution workers claim project-scoped packages over authenticated TLS and own only their disposable execution directories; no shared filesystem is required. Operator CLI and TUI calls use the service API while the state directory is owned.

Backup includes SQLite, workflow checkpoints, pinned source snapshots, evidence and retained workspaces; authentication files are excluded. Restore uses a new directory, verifies data and resolves historical state-owned paths without changing evidence or approval digests. Profiles may preapprove one targeted correctness repair followed by fresh canonical checks and independent review. Failures outside that branch stop with durable evidence. No automatic provider fallback, merge or installation upgrade.
