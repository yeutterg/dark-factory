# Specification: Dark Factory bootstrap

## Purpose

Spend less time coordinating coding agents. A human selects work, approves a plan, and later reviews a result packet. The factory executes bounded jobs and records evidence.

This specification covers **bootstrap**: one project, one machine, one worker, one harness, single-repo outcomes. Multi-repo, web UI, preview delivery, and automatic fallback are later increments.

## User loop

Plan → approve → execute → review → merge and accept.

### Packet 1 — approve the plan
Short Why / What / Assumption / Needs your input, then a complete proposal. Record approver, time, and digest of plan, graph, acceptance criteria, and limits.

### Packet 2 — review the result
What changed / What to review / What to test. Link diffs, evidence, and remaining manual steps. Failed work uses the same packet and is not presented as ready to merge.

## Scope

- Python controller and worker, SQLite, artifact store, TOML profile.
- Bounded acyclic job graph and approval record.
- JSON command runner with GitHub tracking and PR/check adapters.
- Replaceable role prompts and skills.
- CLI: intake, plan, critique, approve, run, review, pause/drain, cancel.
- Fake harness for tests; Pi adapter qualified when the binary is present.
- Native macOS execution profile for bootstrap.
- Cancellation, timeouts, restart from durable state, archive-before-cleanup.

Out of scope for bootstrap: web UI, TUI, settings editor, multi-repo graphs, automatic model fallback, preview/merge automation, DORA, arena, extra harnesses.

## Acceptance

1. Load and strictly validate `factory.toml`; retain last valid revision on failed reload.
2. Intake selected issues into work items without choosing the backlog.
3. Produce a plan job and a fresh-context critique job; require approval before implementation.
4. Run one bounded single-repo outcome through implementation, checks, and independent review using a disposable workspace.
5. Persist costs, attempts, and durable artifacts after workspace cleanup.
6. Cancel and timeout stop work without fallback. Restart resumes from durable records, not late unpublished results.
7. An unrelated tracking command can replace the bundled GitHub adapter without controller code changes.

## Presentation stages

Plan, Build, Check, Review, Done are display groups. They summarize workflow; they are not extra execution states.
