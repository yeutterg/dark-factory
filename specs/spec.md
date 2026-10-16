# Specification: Dark Factory bootstrap

## Purpose

Spend less time coordinating coding agents. A human selects work, approves a plan, and later reviews a result packet. The factory executes bounded jobs and records evidence.

This specification covers **bootstrap**: one project, one machine, one worker and interchangeable per-task CLI harnesses for single-repo outcomes. The [durable extension](durable-runtime.md) adds optional dispatch checkpoints and one preapproved correctness repair. Multi-repo, web UI, preview delivery, and automatic provider fallback are later increments.

## User loop

Plan → approve → execute → review → merge and accept.

### Packet 1 — approve the plan
Short Why / What / Assumption / Needs your input, then a complete proposal. Record approver, time, and digest of plan, graph, acceptance criteria, and limits.

### Packet 2 — review the result
What changed / What to review / What to test. Link diffs, evidence, and remaining manual steps. Failed work uses the same packet and is not presented as ready to merge.

Declare reasonable planning and implementation assumptions in the executive summary, alongside repair history and the exact candidate. Continue eligible automated work until a genuine human gate. Manual attestations require a configured human identity for live work, time, candidate and evidence; agents cannot satisfy human-only criteria or waive missing evidence.

## Scope

- Python controller and worker, SQLite, artifact store, TOML profile.
- Bounded acyclic job graph and approval record.
- JSON command runner with GitHub tracking and PR/check adapters.
- Replaceable role prompts and skills.
- CLI: intake, plan, critique, approve, run, review, pause/drain, cancel.
- Fake harness for tests; qualify Pi against pinned invocation, route binding, geography, credentials, cancellation, usage, and independent review. Binary presence alone is not qualification.
- Native macOS execution profile for bootstrap.
- Claude Code subscription OAuth adapter for read-only planning/critique/review: explicit model/effort, isolated credentials, no API fallback, native stream evidence and bounded cancellation. Live route qualification remains separate.
- Cancellation, timeouts, restart from durable state, archive-before-cleanup.
- Native or Pi Durable dispatch runtime, independent of the selected CLI harness; Codex subscription adapter with live isolation qualification pending.
- Optional original-approval-bound repair/check/review branch; no reset of root budgets or deadlines.

Originally out of scope for bootstrap, and still separate from its execution authority: web UI, multi-repo graphs, automatic model fallback, preview/merge automation, DORA, arena and Claude coding/session takeover. The later TUI increment is a thin operator client over the same command boundary; it does not own scheduling or execution.

## Acceptance

1. Load and strictly validate `factory.toml`; retain last valid revision on failed reload.
2. Intake selected root outcomes without choosing the backlog. Verify GitHub parent/child relationships rather than trusting a caller's root flag. Reject child-only selection and roots requiring unsupported child graphs; never shrink a root to fit bootstrap. Unknown relationship observations fail closed. Planning, approval, dispatch and acceptance reject known child records or roots with stored children.
3. Produce a plan job and a fresh-context critique job; require approval before implementation.
4. Run one bounded single-repo outcome through implementation, checks, and independent review using a disposable workspace.
5. Persist costs, attempts, and durable artifacts after workspace cleanup.
6. Cancel and timeout stop work without fallback. Restart resumes from durable records, not late unpublished results.
7. An unrelated tracking command can replace the bundled GitHub adapter without controller code changes.

8. Every approved acceptance criterion binds checks or manual gates and receives an explicit independent assessment. Missing or stale evidence blocks acceptance even when all jobs report success. The critic compares the criteria with the original request; scope changes require new approval.
9. Simulation evidence cannot satisfy an operator-declared live-evidence requirement.

## Presentation stages

Plan, Build, Check, Review, Done are display groups. They summarize workflow; they are not extra execution states.
