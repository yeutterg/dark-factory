# Plan: bootstrap structure

The package remains under `src/dark_factory/`. One controller owns one state directory. Local or outbound project-scoped workers execute fixed bounded graphs for single-repository outcomes. Multi-repo execution and fallback remain increment A. The TUI is a later operator adapter, not part of execution authority.

| Module | Responsibility |
| --- | --- |
| `config.py` | Strict TOML, explicit asset/source/command references, route validation |
| `adapters/definition/warp.py` | Strict Warp-shaped parsing, inert compatibility reporting, and normalized fixed-role assets |
| `improvement.py` | Read-only aggregation of recurring evidence into proposal-only recommendations |
| `models.py` | Normalized job/graph and record definitions |
| `store.py` | SQLite schema, process lock, transactions, audit and restored path resolution |
| `contracts.py`, `runner.py` | Versioned JSON and bounded process supervision |
| `controller.py` | Composes the controller; the only object that changes execution state |
| `control/policy.py` | Exact approval inputs, evaluated once per decision and compared at approval, dispatch and acceptance |
| `control/planning.py` | Intake, proposal versions, approval and rejection |
| `control/dispatch.py` | Budgets, claims, leases, recovery, cancellation and cleanup |
| `control/results.py`, `control/roles.py` | Attempt settlement, role output validation and the preapproved repair branch |
| `control/review.py`, `control/external.py` | Coverage, packets, continuations, human acceptance and external intentions |
| `control/tasks.py` | Task revisions, structured reconciliation, progress, instruction delivery and Markdown projection |
| `attempts.py` | Attempt facts a worker reports through the controller or a remote replica |
| `control/completeness.py` | Deduplicated advisory criterion assessments between canonical checks and independent review |
| `worker.py` | Prepare, execute, capture, archive, cleanup |
| `artifacts.py` | Hash manifests, filesystem synchronization, backup/restore |
| `operator_commands.py`, `api.py`, `cli.py`, `ui/` | One operator command table for CLI/API/TUI/browser surfaces; separate operator and worker credentials |

Two application-owned lifecycle protocols remain: Harness and Environment. The controller retains scheduling policy and acceptance authority (the Pi Durable workflow runtime was removed 2026-11-19). The harness registry selects Pi, Claude Code or Codex CLI independently for each task route. The macOS environment enforces file/network/process policy. FakeHarness is limited to deterministic tests and explicitly labeled demonstrations. GitHub tracker/code-host modules are named command adapters; local Git operations are helpers rather than another integration protocol. See [durable execution design](durable-runtime.md).

Roots retain selected intent, versioned proposals, approved scope/budgets/assets/command bindings, the pinned base and candidate commits. Each attempt has fresh Git state and harness context, immutable inputs, an enforced deadline, a renewable lease, usage and durable evidence. Checks reconstruct the candidate from its verified archive. Review receives approved requirements, exact diff and canonical evidence, with implementer narration omitted. Canonical checks cannot be waived by model output.

The local worker can run synchronously through CLI or within the controller service. Outbound execution workers claim project-scoped packages over authenticated TLS and own only their disposable execution directories; no shared filesystem is required. Assignments carry the current candidate, the selected verified resume checkpoint, and job-specific evidence—not every historical workspace. Operator CLI, TUI and browser calls use the service API while the state directory is owned. Task records describe work and decisions; they never create an execution job or grant a route.

Backup includes SQLite, workflow checkpoints, pinned source snapshots, evidence and retained workspaces; authentication files are excluded. Restore uses a new directory, verifies data and resolves historical state-owned paths without changing evidence or approval digests. Profiles may preapprove one targeted correctness repair followed by fresh canonical checks and independent review. Failures outside that branch stop with durable evidence. No automatic provider fallback, merge or installation upgrade.

The Warp definition adapter is an authoring frontend, not another controller.
It contributes immutable prompts and skills to the existing policy snapshot.
The improvement reader consumes existing attempt, interruption, and human
decision records and never mutates controller or external state.
