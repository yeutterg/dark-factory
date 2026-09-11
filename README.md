# Dark Factory

Plan selected work, approve it, run bounded coding jobs, and review a result packet.

This repository is the **bootstrap** (v0.1): CLI, one controller, one worker, SQLite, a fake harness (Pi when installed), GitHub JSON adapters, and a single-repo loop. The web UI, multi-repo fallback, and preview/merge automation are later increments.

## Loop

Plan → approve → execute → review → merge and accept.

You approve the plan. Later you review the code and remaining tests, then authorize merge. The factory does not choose the backlog.

Authoritative behavior is in:

- `.specify/memory/constitution.md`
- `specs/spec.md`
- `specs/plan.md`
- `specs/tasks.md`

## Setup

Python 3.10 or newer.

```bash
python3 -m pip install -e ".[dev]"
cp examples/factory.toml factory.toml
# edit state_dir, repositories, routes, and worker_token
dark-factory --config factory.toml demo
```

`demo` runs intake → plan → critique → approve → implement → check → independent review using the fake harness. Human merge remains an operator action.

Useful commands:

```bash
dark-factory --config factory.toml status
dark-factory --config factory.toml review <work-item-id>
dark-factory --config factory.toml accept <work-item-id>
dark-factory --config factory.toml pause
dark-factory --config factory.toml drain
```

Point `commands.tracking` and `commands.code_host` at any argv that speaks the JSON contract on stdin/stdout. Replacing those commands does not require controller changes.

## Policy

No live model route is enabled by the example profile. Unknown processing geography is rejected. Blocked regions include China. Secrets stay in external credentials referenced by TOML, not in git.

Tests:

```bash
python3 -m pytest
```
