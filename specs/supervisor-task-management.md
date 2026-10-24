# Supervisor task management plan

Status: implemented candidate; live model and harness qualification pending.

Create a durable task list for each complete root outcome. The supervisor maintains the list as agents report progress and people add work, answers, or verification. Show what is happening now, what happens next, and who is needed in the TUI, a browser view, and a readable `tasks.md` file. Use a small model only where interpreting language is necessary; structured events and controller facts handle routine updates without inference.

The complete outcome includes persistence, agent reporting and instruction delivery, human interaction, Markdown reconciliation, both user interfaces, recovery, and verification. Implementation milestones below are parts of that outcome, not substitute roots. The candidate does not authorize live inference, new routes or spending, deployment, merge, or acceptance.

## Existing foundations and gaps

| Existing component | Reuse and required extension |
| --- | --- |
| `continuation.py` and `control/review.py` | Already derive job progress and a root next action. Extend with task-level focus, dependencies, waiting reasons, and human actions. |
| `store.py` | Already owns SQLite transactions, jobs, attempts, evidence, approvals, and audit history. Add task and delivery records under the same controller ownership. |
| Planner output in `agents/prompts/planner.md` and `control/roles.py` | Already contains task descriptions and check bindings. Give tasks stable identities and persist them as individually addressable records. |
| `adapters/decision/jev.py` and `control/interruptions.py` | Optional model advice already exists for interruption decisions. The current choice-only contract cannot generate and reconcile a task list. Reuse the bounded command pattern, not its choice-only schema. |
| `adapters/harness/base.py` and `worker.py` | Harness events exist, but general progress is accumulated for the attempt archive. The interface has no instruction-delivery method for an active agent. Add durable progress and capability-declared delivery. |
| `execution/service.py` and `execution/client.py` | Extend the existing authenticated, project-scoped worker boundary for reports and instructions. Workers must not open controller state. |
| `operator_commands.py`, `api.py`, and `ui/` | Share task commands and one projection across CLI, TUI, Markdown imports, and browser controls. |
| Browser UI | A focused authenticated task dashboard now shares the controller projection and commands with the TUI. It does not attempt to implement the entire future web product. |

The current controller uses a bounded execution graph. Adding a task does not currently create another approved implementation attempt. Required-child and cross-repository execution remain capability gaps; a nested checklist must not conceal them.

## User experience

Each root shows its intended outcome, current state, next action, waiting items, and task list. Each active agent also has its own current task and next task. If several assignments are active, show all of them; the root summary must not imply that only one is running.

Example presentation:

```text
Improve reconnect behavior                    Build · 3 of 7 tasks complete
Now   Agent: adding regression coverage
Next  Controller: run canonical checks after implementation finishes
You   Verify reconnect on the exact candidate when checks pass
Update received 12 seconds ago · worker connected

[x] Reproduce the reconnect failure             Agent · reported complete
[x] Implement the retry correction              Agent · reported complete
[x] Review configuration edge cases             Agent · reported complete
[>] Add regression coverage                    Agent
[ ] Run canonical checks                       Controller · waiting on build
[ ] Verify reconnect on hardware               You · waiting on candidate
[ ] Review and accept the complete outcome      You · waiting on evidence

Inbox: 1 new request awaiting reconciliation
```

Checkbox completion and verification have separate labels. An agent reporting completion does not establish that checks passed or that the outcome was accepted. Counts describe the current list, not percentage estimates of remaining effort; adding tasks changes the denominator.

The overview exposes “Needs you,” “Running,” and “Waiting,” along with completed roots. “Needs you” is a badge as well as a filter, because a root can require a human answer while unrelated eligible work continues. Do not conceal active work by assigning a root to an exclusive human-wait lane.

## Task records and ownership

Use a `Task` distinct from an execution `Job`. A task describes useful work or a decision. A job is an approved, budgeted execution unit. Several tasks can belong to one implementation job; creating a task does not automatically create a job.

| Field group | Required content |
| --- | --- |
| Identity | Stable task ID, root ID, proposal version, optional parent task ID |
| Meaning | Title, description, completion condition, linked acceptance criterion IDs |
| Responsibility | Owner kind: agent, controller, human, or external; optional specific actor or assignment |
| Planning | Dependencies, user priority, required or optional classification, existing job/gate binding where applicable |
| Progress | State, waiting reason, reported result, verification state, current attempt reference |
| Evidence | Candidate identity and evidence references when the completion condition requires them |
| History | Origin, creator, timestamps, revision, superseded-by link, event references |

Proposed task states are `inbox`, `queued`, `active`, `waiting`, `reported_done`, `done`, and `cancelled`. Superseded tasks remain in history and leave the active list. Verification is separate: `not_required`, `pending`, `passed`, `failed`, or `stale`.

`reported_done` means the assigned agent says it finished. `done` means the task's declared completion condition has been satisfied: for a simple human action, an attributed completion; for an automated check, successful bound evidence; for a manual acceptance test, an attributed candidate-specific attestation. Parent task completion derives from required children. The root still requires the existing acceptance decision.

Required tasks inherit the approved outcome's obligations. Neither the model nor a checkbox can delete an acceptance criterion, remove a required check, downgrade required work to optional, or waive a manual gate. Such changes need the existing revised-proposal process. Optional ideas can be deferred with a recorded reason.

Keep task nesting shallow in the interface. A child task is checklist decomposition, not a claim that the factory can execute a required tracker child or another repository.

## Markdown storage and direct editing

Maintain `<state_dir>/work-items/<root_id>/tasks.md` for every root. This file is a first-class readable and editable representation of the list. Keep runtime files outside the source checkout and untracked, as required for other controller state.

SQLite remains authoritative for execution, approvals, evidence, task revisions, and delivery history. Generate Markdown from those records so the existing transactional controller remains the single writer of accepted state. Human file edits are proposed changes that are validated and committed through the same task commands as UI edits. There must not be two independent scheduling authorities.

Illustrative file format:

```markdown
---
schema_version: 1
root_id: root_123
plan_version: 2
task_revision: 17
---

# Improve reconnect behavior

## Current state
Agent is adding regression coverage. Last reported: 2026-10-23T18:20:00Z.

## Next action
Controller will run canonical checks after implementation finishes.

## Tasks
- [x] Reproduce the failure <!-- task:t1 owner:agent -->
- [ ] Add regression coverage <!-- task:t2 owner:agent -->
- [ ] Verify reconnect on hardware <!-- task:t3 owner:human -->

## Inbox
- [ ] Also check reconnect after a brief network interruption
```

The full rendered file includes progress and verification labels, dependencies, and links to supporting records. Stable IDs live in machine-readable comments; new unchecked entries without IDs are captured into the inbox. The parser supports a deliberately small documented format instead of interpreting arbitrary Markdown as commands.

Editable content includes new inbox items, task descriptions, requested ordering, notes, and completion requests. Current state, next action, execution facts, and approval metadata are generated fields. An edit to generated content cannot change controller facts. Removing a required row is rejected as a removal request, not treated as a waiver.

A checked human-action box can record attributed completion through an authenticated import. A checked manual-test box requests the same candidate and evidence form used by the UI. Merely editing a local file cannot prove who verified a candidate. An agent-owned checkbox is a report or operator annotation and cannot forge a successful check.

Provide an explicit CLI import and an “Import file changes” UI action. A file watcher may detect changes and show a pending-edit badge, but it must not infer human identity or silently approve attestations. Agent sandboxes receive a task snapshot and use scoped reporting; they never receive write access to this canonical file.

Use revision checks and atomic replacement. Before exporting over an existing file, compare its digest with the last generated version. Preserve changed bytes as a pending import rather than overwriting them. If the file's base revision is stale, show the conflicting tasks and require reconciliation. Duplicate imports use the same content digest and do not create duplicate tasks. A database commit followed by an export failure is recoverable: regenerate the file from accepted records after preserving any external edits.

Backups include accepted task records, event history, the Markdown file, and pending imports. An exported Markdown list is useful to a person, but restoring it alone cannot reconstruct approvals or verification evidence.

## Cheap model and structured outputs

The configurable `task_supervisor` inference role is implemented with an OpenAI-compatible strict-JSON-Schema adapter. A lightweight Luna/Flash-class model remains the intended live candidate. No provider account, model version, route, region, or spending has been selected or qualified by this candidate; pin those values and current price evidence before a live run.

The model interprets free text, proposes decomposition, matches updates to tasks, identifies duplicates, drafts concise summaries, and recommends a next task. Code owns persistence, dependencies, eligibility, approvals, limits, evidence checks, and instruction delivery. The model receives no shell, tracker-write, merge, signing, or operator tools.

Use schema-constrained output when the selected provider supports it, followed by independent application validation. Valid JSON alone is insufficient: references, allowed transitions, scope, revision, ownership, and evidence bindings must also validate.

An illustrative response envelope:

```json
{
  "schema_version": 1,
  "root_id": "root_123",
  "base_revision": 17,
  "processed_event_ids": ["event_41"],
  "operations": [
    {
      "op": "admit_task",
      "task_id": "task_inbox_1",
      "criterion_ids": ["AC-2"],
      "source_event_ids": ["event_41"]
    }
  ],
  "current_summary": "Agent is adding regression coverage.",
  "suggested_next_task_id": "t2",
  "needs_decision": []
}
```

Use a discriminated union of narrowly defined operations, not unrestricted JSON Patch or SQL. Supported proposals include adding a task, clarifying a task, reporting progress, identifying a duplicate, recording a blocker, and proposing dependencies. The controller assigns durable IDs and returns explicit applied/rejected results. The model cannot author human attestations, successful test results, approval records, execution claims, or acceptance.

Every material claim cites an input event or controller fact. Unknown IDs, cycles, edits to another root, stale revisions, invented evidence, and changes to protected obligations are rejected. Scope interpretation is advisory: a model saying “in scope” does not itself authorize execution. Routine decomposition stays under an approved parent criterion; new or ambiguous requirements enter proposal review.

Treat user descriptions, agent output, and Markdown as source data. Instructions inside those fields cannot alter the supervisor's policy or output contract. Accept no hidden reasoning trace; retain the short operational explanation and source references needed for audit.

Cost defaults to evaluate, not approved live limits:

- Invoke on a new free-text request, substantive agent update, blocker, or reconciliation after an attempt. Do not invoke on UI refresh, heartbeat, each token, or every tool call.
- Apply typed status and check-result events without inference. Reuse the original planner's task breakdown.
- Coalesce ordinary narrative updates within a 30-second window per root. A new user request can trigger reconciliation immediately, subject to the same budget.
- Send the relevant task slice, approved outcome summary, and new events; target at most 4,000 input tokens and 800 output tokens per call. Keep full dependency and evidence validation in code. Oversized input is split or left pending, never silently stripped of required context.
- Start evaluation with a maximum of 20 supervisor requests per root, including bounded retries. No calls while idle and no automatic upgrade to a larger model.
- Reserve supervisor cost before dispatch, record usage and conservative unresolved cost, and count it toward both a configured supervisor allowance and the existing root ceiling. The current optional decider pattern must not be assumed to provide this new accounting automatically.
- Reuse a result for the same input digest. Allow at most one repair request for malformed output within the same limits; repeated failure leaves the event pending with a visible reason.

At those example caps, 20 fully sized calls use up to 80,000 input and 16,000 output tokens before any separately metered provider categories. Estimate dollars from the selected route's verified rates, including billable reasoning or other applicable charges. Show supervisor usage separately from coding-agent usage. Provider prices and schema compatibility remain qualification inputs, not assumptions in this plan.

If the model is unavailable or its allowance is exhausted, controller-derived status, human commands, structured agent updates, and already eligible execution continue. Uninterpreted requests remain visibly pending. A required unresolved request prevents the root from being presented as complete. Model availability must not become a prerequisite for rendering the UI.

## Supervisor reconciliation loop

1. Persist the incoming event before acknowledging it. Record source, root, attempt where applicable, sequence number, and idempotency key.
2. Apply controller facts and valid structured progress directly. Queue free-text events for bounded model reconciliation.
3. Take a consistent root/task revision and select the necessary context. Run inference outside the database transaction.
4. Validate the returned operations against the current revision and approved scope. If another edit arrived, discard stale proposals and reconcile fresh input within limits; never overwrite the newer state.
5. Commit accepted operations, history, and outgoing instruction intents together. Preserve user wording and attribution when the model proposes a clearer title.
6. Recompute current focus, eligible next actions, waiting reasons, human needs, and root continuation using canonical facts.
7. Publish the updated shared projection and regenerate Markdown safely. Deliver admitted instructions through the worker channel and retain their acknowledgement state.

Reconciliation runs on events and after recovery. Reading status must not invoke a model, create tasks, or pause execution.

Current state combines observed assignment state and attributed agent progress. Display “agent reports” where appropriate. A connected worker is not proof of recent progress; show last report and heartbeat separately. If no report exists, display the known job and “task-level progress not reported.”

The next action is the first eligible required action after dependencies, human gates, approved routes, resource availability, pause/drain state, deadline, and budget are considered. User priority breaks ties among eligible tasks. When nothing is eligible, show the exact dependency or constraint and the actor who can resolve it. The model may recommend an ordering but cannot override those rules.

## Agent reporting and instruction delivery

Extend the worker protocol with scoped progress submission, pending-instruction retrieval, and acknowledgement. Bind every operation to project, worker, root, job, attempt, execution identity, and task revision as applicable. An expired or replaced attempt cannot mutate the current list or consume instructions intended for its replacement.

Normalize adapter events such as `task_started`, `task_progress`, `task_reported_done`, `task_blocked`, and `task_proposed`. Only documented structured adapter events enter this path; text that resembles an event is untrusted narrative. The worker durably spools reports and retries transport with stable IDs. Local and distributed workers obey the same contract.

Adapters declare reporting and instruction-delivery capabilities independently. Progress support does not imply safe active-session steering. Add a narrow delivery method only where the harness supports and can qualify it; do not assume Pi, Claude Code, and Codex have equivalent semantics.

| Assignment state | Delivery behavior |
| --- | --- |
| Not started | Include admitted tasks and their revision in the next immutable assignment brief. |
| Active, qualified turn-boundary steering available | Queue the instruction and deliver at the supported boundary. Preserve payload digest and acknowledgement. |
| Active, steering unsupported | Show “queued for the next eligible assignment”; do not pretend that the agent received it. |
| Existing implementation finished | Use an explicitly approved bounded follow-up branch if available; otherwise prepare a revised proposal. The existing repair branch is not a general new-feature allowance. |
| Root accepted, cancelled, or otherwise closed | Capture as a proposed follow-up linked to the root. Do not silently reopen accepted work. |

Instruction states are `queued`, `delivered`, `acknowledged`, `resolved`, and `superseded`. Transport receipt is not agent acknowledgement, and acknowledgement is not task completion. Use these exact distinctions in the UI.

Every delivered instruction is an immutable append-only input record. Preserve the original attempt input digest and attach the ordered instruction history to its result and archive. Evidence and review must identify the effective instructions and exact candidate. Do not mutate a frozen prompt or conceal later steering from the reviewer.

Before implementation settlement advances to checks/review, reconcile required admitted instructions and their disposition atomically. A request that arrives concurrently with settlement must either bind to that attempt before closure or remain queued for a subsequent authorized assignment. It must never disappear between them.

Cancellation and urgent replacement use explicit existing stop/recovery semantics. Ordinary task additions do not kill processes or replay an interrupted agent. Recovery redelivers pending messages by identity and reconciles acknowledgement; it does not promise exactly-once arbitrary agent side effects.

## Human actions and approval boundaries

| User action | Exact effect |
| --- | --- |
| Add an agent task | Persist immediately in the inbox, reconcile against the current outcome, then show queued delivery or the specific proposal/limit/capability blocker. |
| Add a task for me | Create a human-owned task with a completion condition; it becomes a dependency only when explicitly linked. |
| Answer a question | Record the answer, resolve the waiting condition if valid, and queue the answer for the assigned agent. |
| Check off an ordinary human action | Record actor and time, mark the action complete, and release eligible dependents. |
| Verify a candidate | Open the manual verification form with the exact candidate and evidence requirement. Record through the existing human-gate boundary. |
| Mark an agent task complete | Record an operator completion report; do not manufacture check evidence or acceptance. |
| Reorder tasks | Change priority among eligible tasks; preserve dependencies and do not preempt active work implicitly. |
| Edit or delete a task | Apply ordinary clarification with history; request revised approval for changed required scope or completion obligations. |
| Reopen a completed task | Record why; invalidate relevant completion/verification as appropriate and queue permitted follow-up work. An accepted root needs a follow-up decision. |

Manual verification must support both passing and failing results. Extend the operator command to expose that distinction; the underlying controller already accepts a `passed` argument. A changed candidate makes previous candidate-bound attestations stale. An unchecked box cannot erase the historical attestation; it creates a new attributed action.

Use simple task entry: title, assigned to agent/me, optional details. Infer suggested dependencies and criterion links, then show the resulting classification. Routine work inside existing approved obligations should continue without repeated approval prompts. New delivery scope, permissions, routes, spending, required criteria, or execution branches require a revised decision under the constitution.

Root-level inbox items without a justified binding remain captured but not executable. Project-level inbox ideas remain unselected until linked to a valid complete root through tracker intake. The supervisor does not choose the roadmap from loose notes.

## TUI and browser delivery

Extend the root overview with compact Now, Next, Needs you, task counts, and update freshness. Keep the existing lifecycle rail and worker visibility. Avoid false precision about progress or remaining time.

Add a Tasks tab to root detail with All, Agent, Mine, Waiting, and Inbox filters. Show owner, dependencies, reported completion, verification, delivery status, and history. Include add, edit, reorder, answer, complete, verify, and import-file actions. Use contextual keyboard shortcuts with visible help; preserve existing navigation and approval bindings.

Build a focused browser dashboard with the same overview, root detail, task controls, and verification forms. Consume the same projection and operator commands. Keep credentials server-side behind an authenticated same-origin operator session; apply origin/CSRF checks to browser mutations. Never embed a shared operator bearer token in public JavaScript or an unauthenticated page.

Start with revision-aware polling, consistent with the current TUI polling design. Both clients show disconnected/stale state, preserve in-progress form edits during refresh, and resolve concurrent changes explicitly. UI refreshes generate no inference requests. Push streaming can be added later if measurements justify it; it is not necessary for this outcome.

The browser dashboard is delivered as part of this root, not deferred merely because the current repository has no web client. It includes task management and its necessary authentication and serving path, without expanding into unrelated settings, analytics, or preview products.

## Persistence and commands

Add focused records for tasks, task events, supervisor requests/results, instruction delivery, and Markdown export/import revisions. Prefer ordinary relational records plus immutable audit events over introducing a second workflow engine. Use foreign keys and uniqueness constraints for root/task identity, source-event deduplication, request identity, and delivery acknowledgement.

Task commands include add, update, reorder, answer, complete, reopen, and import. Commands take an expected revision and idempotency key. Return the accepted revision and resulting disposition. Verification, approval, cancellation, and acceptance continue through their existing controller methods. Conflicting edits return a conflict rather than last-write-wins.

Extend the shared projection with `tasks`, `current_actions`, `next_actions`, `waiting_on`, `needs_human`, `inbox_count`, `task_revision`, `last_progress_at`, and `supervisor_usage`. Overview payloads contain compact summaries; detail requests fetch task history and evidence links without embedding full transcripts.

Existing roots can be backfilled from proposal tasks, jobs, and manual gates without inference. Mark reconstructed progress as reconstructed; do not infer historical completion of individual tasks from a successful implementation job alone. Existing approvals must not silently acquire a new inference route, allowance, follow-up branch, or steering policy. Adoption of those capabilities requires a new approved policy snapshot; read-only projection and export can remain available.

## Implementation sequence

| Milestone | Work and exit condition |
| --- | --- |
| 1. Contracts and task state | Update the governing specification/plan/tasks, define schemas and transitions, add migrations and revisioned controller commands. Demonstrate task creation, dependency handling, immutable obligations, and recovery using deterministic fixtures. |
| 2. Markdown and projection | Add safe export/import, conflict preservation, provenance, current/next derivation, and shared summaries. Demonstrate human editing without lost updates or forged verification. |
| 3. Worker integration | Add durable progress, instruction queue/acknowledgement, capability negotiation, effective-input history, and supported turn-boundary delivery. Prove local and distributed behavior, including the unsupported-delivery path and settlement race. |
| 4. Small-model reconciliation | Add the supervisor role, structured schema, bounded event batching, usage reservations, retries, and validation. Evaluate candidate small models on a fixed representative corpus before selecting a live route. |
| 5. TUI and browser | Deliver interactive task management and candidate-bound human actions on both surfaces. Validate navigation, accessibility, concurrency, and stale/disconnected behavior. |
| 6. Complete-root qualification | Exercise user addition through delivery and completion, manual verification, candidate invalidation, restart, Markdown import, and model-budget exhaustion. Record exact candidate evidence and the remaining human acceptance decision. |

Use the factory for implementation only against a separate candidate checkout and dedicated state. Never start a candidate scheduler against the live controller state. If the factory cannot execute the complete selected root, report that capability gap and obtain an authorized execution approach; do not select a child milestone as a replacement root. Installation and live controller cutover remain separate decisions.

## Acceptance and evaluation

| ID | Required proof |
| --- | --- |
| STM-01 | A planned root becomes a durable task list with stable IDs, owners, dependencies, and criterion bindings. Reconciliation and restart do not duplicate tasks. |
| STM-02 | Meaningful agent progress updates current state and next action on both UIs and Markdown. Missing or stale reports remain explicit; heartbeats do not fabricate progress. |
| STM-03 | User-added work is persisted immediately and reaches the assigned agent at a qualified delivery boundary, with separate queued, delivered, acknowledged, and resolved states. Unsupported steering has an honest actionable disposition. |
| STM-04 | Human completion releases valid dependencies. Manual verification requires a named human, exact candidate, and evidence; failure and later candidate changes remain visible. |
| STM-05 | Required scope, budgets, routes, dependencies, and acceptance cannot be bypassed by task text, model output, Markdown edits, or a checkbox. New scope follows revised approval. |
| STM-06 | Duplicate, delayed, out-of-order, cross-root, and stale-attempt reports cannot corrupt current state. Concurrent human edits and stale model responses preserve the newer revision. |
| STM-07 | A required request arriving during settlement cannot be lost or bypassed on the path to acceptance. Delivered instruction history is bound to the effective candidate evidence. |
| STM-08 | Markdown edits round-trip through import with stable IDs. Export never overwrites pending human edits; stale bases and failed writes recover without data loss. A file alone cannot impersonate a human attestation. |
| STM-09 | Supervisor calls are event-triggered, capped, metered, and included in root limits. Idle periods and UI refreshes cost zero inference. Budget exhaustion or provider failure preserves usable deterministic operation. |
| STM-10 | TUI and browser expose equivalent task actions and authoritative state. Browser credentials remain protected, and mutations enforce authenticated identity and revision checks. |
| STM-11 | Backup/restore retains tasks, pending requests, instruction acknowledgements, evidence, and Markdown edits without rerunning agent side effects. |
| STM-12 | End-to-end evidence covers the complete root, both UIs, real terminal/browser interaction, and explicitly qualified adapter/model behavior. Fixture success is not a claim of live qualification or installation. |

The model evaluation corpus should include ordinary progress, duplicate requests, ambiguous references, contradictory updates, blockers, new scope disguised as a small task, fabricated evidence, malicious embedded instructions, and long lists requiring context selection. Score schema validity, correct task matching, missed required work, invented work, unsupported completion claims, next-action usefulness, latency, and total cost per root. Compare against deterministic handling and a human-labeled expected result.

Select the cheapest candidate that meets the agreed quality bar on this corpus. All authority violations must be rejected by code even when the model fails. Record semantic error rates separately; schema validity alone is not sufficient evidence that a model manages the list correctly. Do not enable automatic expensive fallback to hide poor performance.

Relevant automated suites include continuation, root scope, operator commands/API, TUI, interruption/restart, worker execution and distributed recovery, plus new focused task, supervisor-schema, Markdown, browser-auth, and instruction-delivery tests. Run repository-required lint/type/package checks for the implemented change. Manual acceptance uses the exact candidate at normal and compact terminal sizes and desktop/mobile browser widths.

## Decisions fixed by this plan and remaining qualification

Use a task list per complete root, a small structured-output supervisor, deterministic execution authority, explicit delivery acknowledgements, human-owned completion, candidate-bound verification, and a Markdown representation backed by the existing SQLite controller. Deliver both TUI and browser interactions in the same complete outcome.

No additional product clarification is required to start implementation planning. Remaining technical qualification is concrete: select and measure the actual small model, prove each adapter's delivery boundary, set approved supervisor limits, and verify browser authentication. Failure to qualify one capability must remain visible and must not be described as complete implementation of the full outcome.
