# Plan: bootstrap structure

The package remains under `src/dark_factory/`. One controller owns one local state directory. One local worker executes a fixed bounded graph for a single repository. Multi-repo execution and fallback are increment A; remote workers, UI and attachment are not bootstrap scaffolding.

| Module | Responsibility |
| --- | --- |
| `config.py` | Strict TOML, explicit asset/source/command references, route validation |
| `models.py` | Normalized job/graph and record definitions |
| `store.py` | SQLite schema, process lock, transactions, audit and restored path resolution |
| `contracts.py`, `runner.py` | Versioned JSON and bounded process supervision |
| `controller.py` | Proposal versions, approval, budgets, leases, packets, external intentions and human acceptance |
| `worker.py` | Prepare, execute, capture, archive, cleanup |
| `artifacts.py` | Hash manifests, filesystem synchronization, backup/restore |
| `api.py`, `cli.py` | Local worker/operator surfaces; separate decision credential |

Exactly two lifecycle protocols remain: Harness and Environment. Pi emits JSON events with native provider configuration; the macOS environment enforces file/network/process policy. FakeHarness is limited to deterministic tests and explicitly labeled demonstrations. GitHub tracker/code-host modules are named command adapters; local Git operations are helpers rather than another integration protocol.

Roots retain selected intent, versioned proposals, approved scope/budgets/assets/command bindings, the pinned base and candidate commits. Each attempt has fresh Git state and harness context, immutable inputs, an enforced deadline, a renewable lease, usage and durable evidence. Checks reconstruct the candidate from its verified archive. Review receives approved requirements, exact diff and canonical evidence, with implementer narration omitted. Canonical checks cannot be waived by model output.

The single worker can run synchronously through CLI or within the local controller service. Operator CLI calls use the service API while the state directory is owned. No remote/shared-filesystem worker protocol is claimed by bootstrap.

Backup includes SQLite, pinned source snapshots, evidence and retained workspaces. Restore uses a new directory, verifies data and resolves historical state-owned paths without changing evidence or approval digests. Failed attempts stop; operators reconcile saved work and create a new proposal version when appropriate. No automatic fallback, merge or installation upgrade.
