# Plan: bootstrap structure

## Package

Conventional layout under `src/dark_factory/`.

| Module | Role |
| --- | --- |
| `config.py` | Load, validate, snapshot `factory.toml` |
| `models.py` | Work item, job, attempt, evidence, cost, audit |
| `store.py` | SQLite schema, claims, leases, audit |
| `contracts.py` | Versioned JSON for integration commands |
| `runner.py` | Argument-array command execution, limits, validation |
| `controller.py` | Graph validation, approval, dispatch policy, packets |
| `worker.py` | prepare → execute → capture → archive → clean |
| `api.py` | Authenticated claim/heartbeat/result HTTP surface |
| `cli.py` | Operator commands |

Adapters: `adapters/harness/` (protocol + fake + Pi), `adapters/env/` (native), `adapters/projects/` and `adapters/git/` (GitHub JSON commands). Prompts live in `agents/prompts/`; skills in `agents/skills/`.

## Data

Four main records: work item, job, attempt, evidence. Supporting tables: workers, costs, audit, pending external actions, pauses/drains.

Approval digest covers plan text, graph, acceptance, limits, pinned command bindings, and permitted routes. Implementation jobs are blocked until that digest is recorded.

## Execution

Workers authenticate and claim the next ready job if window, capabilities, concurrency, dependencies, and reserved budget allow. Bootstrap allows one heavy job per worker and stops on failure without fallback.

Trusted tracking/merge commands run on the controller, not in the coding sandbox.

## Tests

Behavior tests: config errors, graph validation, approval gate, runner invalid output/timeout, archive-before-cleanup, cancel, restart, geography policy, prompt override, adapter swap.
