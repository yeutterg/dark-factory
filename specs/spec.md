# Specification: Dark Factory bootstrap

## Purpose

Spend less time coordinating coding agents. A human selects work, approves a plan, and later reviews a result packet. The factory executes bounded jobs and records evidence.

This specification covers **bootstrap**: one project, one machine, one worker and interchangeable per-task CLI harnesses for single-repo outcomes. The [durable extension](durable-runtime.md) adds restart continuation and one preapproved correctness repair. The [task supervisor increment](supervisor-task-management.md) adds a task-level view and focused browser dashboard over the same authority. Multi-repo execution, preview delivery, and automatic provider fallback remain later increments.

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
- One native dispatch path, independent of the selected CLI harness, that re-acquires unstarted claims after restart; Codex subscription adapter with live isolation qualification pending.
- Optional original-approval-bound repair/check/review branch; no reset of root budgets or deadlines.
- Durable task records distinct from execution jobs, with revisioned user actions, attributed progress, instruction delivery states, and a conflict-safe `tasks.md` projection.
- A bounded optional structured-output task supervisor. It may interpret task text and suggest updates; code retains scope, dependency, budget, evidence and acceptance authority.
- Task interaction in the TUI and authenticated focused browser dashboard over the shared operator projection and commands.
- Optional Warp `v1alpha1` definition input for the fixed workflow: mapped role
  prompts, shared and role-scoped skills, requested-model validation, complete
  resource digests, and an explicit compatibility report. YAML cannot grant
  routes, credentials, network access, budget, scheduling, or acceptance.
- Self-improvement, on by default and bounded by `[improvement]` in the
  profile. Recurring failures across distinct roots create at most one
  improvement root at a time. It runs the ordinary graph under the operator's
  standing approval and ends as a draft pull request limited to the allowed
  paths. It never merges, accepts or installs anything. `dark-factory improve`
  is the read-only report.

Still separate from bootstrap execution authority: multi-repo graphs, automatic model fallback, preview/merge automation, DORA, arena and Claude coding/session takeover. The TUI and focused task dashboard are thin operator clients over the same command boundary; neither owns scheduling or execution.

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
10. Task text, structured model output, Markdown edits and checkboxes cannot expand approved scope or manufacture evidence. Task progress binds to the active attempt; human verification binds to identity, candidate and evidence.
11. Task edits use expected revisions and idempotency keys. Markdown export preserves conflicting edits, and instruction transport distinguishes queued, delivered, acknowledged and resolved.
12. A configured Warp `v1alpha1` definition that Warp's own validator accepts
    must load. It must match profile repositories, map `SPEC`, `IMPLEMENT` and
    `REVIEW` agents into the existing graph (several candidates are selected by
    `[definition.roles]`), bind model requests to approved local routes, and
    enter the immutable approval policy with every file digested. The
    code-owned output contract is always delivered with the authored prompt.
    The report states per resource whether it is executed, mapped, or
    recognised and not executed. `v1alpha2` is reported as unsupported.
13. Improvement signals are computed from structured failure kinds, grouped by
    distinct root, and persisted with their evidence. A signal at threshold
    creates one improvement root (origin `improvement:<signal>`) subject to the
    configured concurrency, cooldown, budget, path and diff-size limits. The
    root uses the ordinary graph; approval is the standing policy bound to the
    profile digest. Any limit breach, failed critique or blocking planner
    question stops the root for a human. The only external write is a draft
    pull request. Prompts, skills and models never expand permissions.

## Presentation stages

Plan, Build, Check, Review, Done are display groups. They summarize workflow; they are not extra execution states.


## Universal pipeline (amendment, 2026-11-26)

See [pipeline-plan.md](pipeline-plan.md). One host runs many repositories. Each repository carries its own pipeline configuration (`factory/x-dark-factory.yaml`, read from its main branch by commit and pinned per root). Core owns the stage names and order for the change loop and the release loop. An external coding agent is a client of the factory: it works from an issue, an approved plan and a factory-owned worktree, and a required pull-request status blocks anything without them. Release roots are created only by an operator command. Merge, release approval and production promotion remain human decisions; signing on local runners is deferred (issue 18).
