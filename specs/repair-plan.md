# Repair and hardening plan — 2026-11-19

Status: **plan only; nothing here is implemented.** Baseline at `8437f2d`:
349 tests pass, 15 skipped; Ruff, format and mypy clean.

This plan follows a full review of the controller, execution path, adapters,
Warp definition reader, improvement loop and dependencies. It replaces nothing
in `spec.md`, `plan.md` or `tasks.md` until Phase 0 amends them.

## Goals

1. Fix the defects that break or wedge real outcomes.
2. Make the live execution boundary defensible.
3. Shrink and regularise the code: one way to do each thing.
4. Read real Warp Factories `v1alpha1` trees correctly, and say plainly what
   is not executed.
5. Self-improvement on by default, through the ordinary factory workflow, to a
   draft pull request, inside code-enforced scope limits. Never merged, never
   installed.
6. Preserve end-to-end behaviour and durability throughout.

## Decisions (recorded from the operator, 2026-11-19)

| # | Decision |
|---|---|
| D1 | Self-improvement runs the ordinary pipeline (plan, critique, implement, canonical checks, independent review) and opens a draft PR. It does not merge or upgrade a controller. |
| D2 | Plan approval for improvement roots comes from a standing operator policy in `factory.toml`, bounded in code. The constitution is amended to say so. |
| D3 | Improvement scope uses the same `allowed_paths` / `protected_paths` mechanism as any repository. Default allowed paths are the factory definition (prompts and skills). Factory source is included only by explicit operator setting. |
| D4 | Remove the Pi Durable workflow runtime. Keep the Pi coding-agent harness. |
| D5 | Drop the legacy config dialect and the shared-token worker channel. |
| D6 | `v1alpha2` gets a clear "unsupported version" report. No blueprint support until Warp makes it current. |

## Design rules for every phase

These are the consistency rules. A change that needs to break one says why.

- **One state vocabulary.** `JobStatus`, `AttemptStatus`, `WorkStatus` are
  `StrEnum`s and are the only source of status values. Named sets
  (`ACTIVE_ATTEMPTS`, terminal sets) are defined once beside them.
- **One rule, one function.** `job_ready()`, `budget_headroom()`, `expired()`
  each exist once and are called everywhere the rule applies.
- **One transaction form.** `@atomic` for controller mutations. No slow work
  (git, hashing, subprocess, network) inside a transaction.
- **One config dialect, typed.** Each `factory.toml` table has a dataclass and
  a validator function. No raw-dict property views. `config.py` does not import
  adapters; adapters register validation hooks that a composition step calls.
- **One execution path.** A single-host install is a supervisor plus one
  execution worker over loopback, using the same client and protocol as a
  remote worker.
- **One prompt layering.** Code-owned contract (output schema, invariants)
  plus authored role guidance (packaged, overridden, or Warp body). Authored
  text can never remove the contract.
- **One list of protected paths, one "resolve inside root" helper, one harness
  CLI skeleton, one retrying HTTP helper.**
- **Durable writes are atomic.** Temp file, fsync, rename. Recorded digests
  are checked when the artifact is used, not only when it is written.
- **Every failure leaves a reason an operator can read.** No silent `continue`,
  no discarded stderr, no state that only manual file deletion can clear.

## Phase 0 — Governance

Docs only. Required first because the constitution currently forbids D1/D2
("The factory does not choose the roadmap"; spec acceptance 13).

- Amend the constitution (minor version bump): improvement roots are the one
  class of work the factory may originate. Their approval is the operator's
  standing policy, bound to the profile digest and the code-enforced limits in
  Phase 6. Merge, acceptance and installation remain human.
- Amend `spec.md`: replace scope bullet and acceptance 13 (proposal-only) with
  the Phase 6 contract; replace the Pi Durable scope bullet; restate Warp
  acceptance 12 per Phase 5.
- Mark `durable-runtime.md` superseded where it describes the engine; keep the
  continuation and repair sections, which stay.
- Move historical reviews (`implementation-review.md`,
  `post-claude-merge-review.md`) under `specs/history/`.
- Add this plan's phases to `tasks.md`.

Exit: documents reviewed and merged by a human before Phase 1 starts.

## Phase 1 — Remove, then patch the urgent dependencies

Deleting first means later fixes touch less code.

**Remove the Pi Durable runtime (D4)**
- Move re-acquisition of unstarted claims (`leased`, no workspace, no pid) into
  the worker's ordinary startup path, with the existing owner, policy, lease
  and deadline guards from `resume_claim`. This is the only durability the
  engine provided.
- Delete `adapters/workflow/` (including `pi_engine/`), `workflow.py`, the
  `[workflow]` config table, the Node preflight, `workflow_unavailable` state,
  the `workflow-settled:` ops keys and the engine entries in the policy digest.
- Remove the Node job from CI. Node stays in worker images for agent CLIs.
- Keep the crash/restart tests, re-pointed at the native path.

**Remove the legacy dialect and channel (D5)**
- Delete `[execution] profile`, `pi_binary`/`claude_binary`/`codex_binary`,
  literal `worker_token`/`operator_token`, and the shared-token worker
  endpoints in `api.py`.
- `serve --worker` stays as a launcher that starts one loopback execution
  worker. `run-worker` and `demo` use the same path. *Open design point:
  confirm while implementing that the in-process `Worker` and the remote
  executor can share one entry without a second protocol; if not, report
  before proceeding.*
- Rewrite `examples/*.toml` in the one dialect; delete duplicates.

**Remove dead code**
- `NativeEnvironment.run`, `CommandRunner.run_plain`,
  `harness/registry.capabilities`, `attach_command`, `LOCAL_ONLY`, unused
  model fields and constants, job status `running`, `jobs.reserved_cents`.

**Low-risk dependency batch**
- Node image to 24.22.3 by digest; Python image digest refresh (3.11.18).
- Ruff 0.17.5, mypy 2.4.1, pytest floor `>=9.1`, setuptools floor raised,
  drop `wheel` from build requirements. GitHub CLI 2.105.0 with new checksums.
- Fix Claude Code version drift between `docs/claude-subscription.md` and the
  Dockerfile.

Exit: full suite green; demo and Compose smoke run end to end; a killed worker
between claim and start re-acquires the same attempt on restart.

## Phase 2 — Correctness

Each item lands with a regression test. The three reproduction scripts from
the review become the first tests.

| # | Defect | Fix |
|---|---|---|
| C1 | Replanned root can never be accepted (`control/tasks.py:89`) | Supersede prior-plan tasks at replan; filter by plan version |
| C2 | Step time limit settles as operator cancel and cancels the root | Distinguish deadline from cancel in heartbeat reply; settle as `time_limit` interruption; start the harness timeout from the attempt deadline |
| C3 | Lease renewed only inside the harness loop; good results become `timed_out` | Dedicated heartbeat thread from claim to release; `stopped()` becomes read-only |
| C4 | Failed `start_planning` blocks the root forever | Do clone and binding work before the transaction; make the inputs directory step idempotent |
| C5 | Ambiguous or pending external action blocks all dispatch with no exit | Scope the block to its own root; add an operator command to reconcile or abandon it, independent of root status |
| C6 | Task-supervisor failures bypass request and budget caps | One call row per request; add, never overwrite, cost; release reservations on crash recovery |
| C7 | Policy evaluation error makes an active attempt unsettleable | Settle with an explicit `policy_unavailable` failure; never raise out of `finish_attempt` |
| C8 | Decider can auto-resume non-transient causes | Decider may only confirm or hold a transient cause; classification uses structured failure kinds, not text matching on model output |
| C9 | One bad streamed task event kills the attempt | Reject the event, record it, continue; namespace model-supplied idempotency keys per attempt |
| C10 | Resume loses saved work in three paths; prompt claims otherwise | Clear `resumed_by_attempt` on release; never overwrite a recorded archive with NULL; state in the prompt whether work was restored |
| C11 | Late supervisor admission blocks acceptance permanently | Refuse admission against a finished job, or bind to the next eligible job; require `base_revision` |
| C12 | Archive integrity is only self-consistent | Compare the recorded manifest and diff digests at acceptance, review input and resume |
| C13 | Dead-process pid kept for multi-command checks | Update pid and identity together |
| C14 | Cancel and replan races (`cancelling` omitted) | Use `ACTIVE_ATTEMPTS` everywhere; cancelling a root signals all its attempts |
| C15 | Jobs skipped silently on caps | Record the blocking cap in the continuation view; state budget windows explicitly |
| C16 | `import_tasks` applies half a file | Validate the whole file, then apply in one transaction |
| C17 | External requirement names are untyped | Match on kind and name |
| C18 | Check job has no overall bound | Share one deadline across its commands |

Worker robustness (same phase, same tests):
- Heartbeat failure tolerates transient errors up to the lease; only an
  explicit refusal cancels.
- Executor result written atomically; a corrupt result is quarantined and
  reported, not retried forever. Executor stderr kept as a bounded diagnostic.
- Worker loop survives HTTP and transport errors with capped backoff. Idle
  polling stops re-hashing source and probing harness versions each second.
- Harness CLIs stop when their consumer stops (all adapters, not only Codex).
- Package transfer handles in-tree relative symlinks; anything it refuses is
  refused at pack time with a recorded reason.

Exit: all regression tests green; a soak test (repeated kill and restart of
supervisor and worker mid-attempt under fake routes) ends with no stuck root,
no leaked reservation and no lost archive.

## Phase 3 — Isolation and credentials

- **Base image to Debian trixie** for bubblewrap 0.12 (bookworm's 0.8.0 has
  unfixed CVE-2026-87766). Re-run the isolation tests and Compose smoke.
- **Credentials leave the agent's reach.** Inference keys are passed by
  guarded file or descriptor (as Codex already is), not process environment;
  `/proc` exposure of the parent environment is closed. One redaction pass
  over every harness's stderr and result text.
- **Resource limits are enforced**: cgroup or `prlimit` on the Bubblewrap
  path, sized tmpfs, and `mem_limit` / `pids_limit` / `cpus` in Compose.
- **Live container privilege.** Replace `seccomp=unconfined` with a written
  profile; drop capabilities that are not needed; move secrets from service
  environment to mounted secret files. *Spike first:* whether the worker can
  run non-root with nested user namespaces so Codex no longer needs the
  self-sandbox exemption. Report the result before choosing the design.
- **macOS profile** rewritten deny-by-default (explicit read, exec, mach and
  network allowlists). Until re-qualified, live routes on it need an explicit
  operator opt-in.
- **Protected paths** from one constant, applied unconditionally in every
  environment and at capture.
- **API**: threaded accept with handshake and read timeouts; login throttle;
  session expiry cleanup; minimum token length enforced at config load;
  unknown exceptions return a 500 body instead of dropping the connection.
- **GitHub token**: document and preflight-check a repository-scoped token;
  stop mounting the whole checkout into the supervisor.
- **Stop hook**: add the project's own token shapes to the secret scan, refuse
  untracked `*.toml` outside known example names, scan the full push range,
  and never push to the default branch.
- **Operator identity**: record that actor names are asserted, not
  authenticated; per-operator tokens are a later increment and are listed in
  Out of scope.

Exit: isolation test suite passes in the new image; a planted dummy credential
is unreadable from every harness's tools; a fork bomb in a check is contained.

## Phase 4 — Consistency refactor

No behaviour change; tests are the guard. One PR per bullet.

- Apply the status enums and the shared rule helpers across `control/`.
- `config.py` split into per-table dataclasses and validators; delete the raw
  views and lazy imports; fix the stale "requires macos-sandbox" error.
- `worker._execute` split into prepare, run-role, run-checks,
  archive-and-release.
- `control/tasks.py` split into records and view, operator mutations,
  supervisor, Markdown projection, and worker progress.
- `ui/screens.py` split one screen per module.
- Harness adapters share one streaming-CLI base (environment allowlist, usage
  validation, failure result, redaction). Environment adapters share the path
  helper and limit defaults; OCI and command environments stop inheriting the
  macOS prefix.
- `ops` table accessed through named helpers per use; cache refreshes stop
  writing audit rows. Fold `evidence` into `attempts` or use its digest (C12
  decides which).
- Compute the review packet's continuation and coverage once per call.
- Rename `interruptions.py` to `resume_policy.py`; merge the two retrying POST
  helpers in `adapters/decision/`.
- The Warp definition becomes a dataclass, not a dict.

Exit: suite green with no test edits beyond imports; source is smaller than at
the end of Phase 2.

## Phase 5 — Warp Factories `v1alpha1` compatibility

Target: any tree Warp's validator accepts loads here, and the report is
truthful about what runs.

**Fix what breaks real runs**
- Prompt layering: the code-owned output contract is always appended to the
  agent body. Split each packaged prompt into guidance and contract.
- Strip frontmatter; snapshot prompt and skill content at load so digest and
  use cannot diverge.

**Accept what upstream accepts**
- Several agents of one mapped type: selected by `[definition.roles]` in the
  profile; a single candidate maps automatically.
- Empty agent body: allowed; the packaged guidance is used.
- `agentType`: uppercase only; null means `CUSTOM`; `VERIFY` recognised and
  reported.
- Models: `auto`, `auto-*`, `custom:*` and router names all mean "use the
  approved local route". A concrete model id must equal the route's model.
  A `harness` block is compared with the route's harness and reported.
- `self-improvement/agent.md` read (feeds Phase 6). `v1alpha2` and `workers/`
  produce a single clear unsupported-version error (D6).

**Enforce what upstream enforces**
- Exactly one FOREMAN/MAIN; `repositories` key required; runner references
  resolve; scorer required fields; runner `platform` rules; automation trigger
  shape. Upstream's size caps (256 files, 256 KiB per file, 2 MiB total).
- Strict mode is strict: unknown agent types and directories are errors.

**Make the evidence honest**
- Digest covers every file in the tree, including unmapped agents and unknown
  directories.
- The report states per resource: executed, mapped, or recognised and not
  executed. Remove the hardcoded `"syntax": "compatible"`.
- Parser hardening: catch `TypeError` and `RecursionError`, file names and
  line numbers in errors, BOM handling, frontmatter fence detection, YAML 1.1
  boolean and date keys.

**Prove it**
- Fix `examples/warp-factory` (foreman, runner file, skill frontmatter) and
  the test fixture.
- CI conformance test: Warp's published example trees and JSON Schemas,
  vendored at a pinned commit as test data. `jsonschema` is a dev dependency
  only; the runtime parser stays hand-written.
- A fake-harness test that asserts the delivered prompt contains the contract
  and no frontmatter.
- Update `docs/warp-compatibility.md`.

Not executed, and reported as such: scorers as LLM judges, automations,
routers, benchmarks, webhooks, foreman and triage scheduling, MCP servers,
hooks, secrets and network policy from YAML.

## Phase 6 — Self-improvement, on by default

**Detection** (existing analysis, made stateful)
- Signals are computed from structured failure kinds, persisted with their
  evidence, and no longer recomputed on every status read.
- Fix role attribution (review failures point at coder and reviewer).
- Threshold: Warp's `selfImprovement.failedRunThreshold` when a definition
  sets it, otherwise three distinct roots.
- Improvement roots never count toward improvement signals.

**Workflow** (D1, D2)
- A triggered signal creates an improvement root with origin
  `improvement:<signal>` and its evidence attached.
- It runs the ordinary graph: plan, critique, implement, canonical checks,
  independent review. No special-case stages.
- Approval is recorded as the standing policy, citing the profile digest. A
  failed critique, any blocking planner question, or any limit below stops the
  root for a human instead.
- On a passing review the controller opens a **draft** PR through the existing
  code-host command, listing the regressions addressed with evidence links.
  The root then waits at the normal human review gate.
- Simulation routes run the same path and never publish.

**Scope limits, enforced in code** (D3)

| Limit | Default | Where enforced |
|---|---|---|
| Target repository | The source that holds the definition, in a separate checkout | Config load; refuses the running installation and its state directory |
| Allowed paths | Definition prompts and skills | Existing scope check at capture |
| Protected paths | Checks, CI, routes, budgets, permissions, profiles | Existing protected-path check |
| Diff size | Small file and changed-line caps | Capture; exceeding is a scope violation, no repair |
| Concurrency | One open improvement root | Root creation |
| Cooldown | Per signal; a closed or rejected PR suppresses it until new evidence | Root creation |
| Budget | Dedicated cap, inside the account cap | Dispatch |

`[improvement]` in `factory.toml` holds these; `enabled = false` turns the
loop off. Changing a limit changes the policy digest and so the standing
approval. `dark-factory improve` remains as the read-only report.

Exit: with fake routes, three seeded failing roots produce exactly one
improvement root that reaches review with a diff inside the default paths; an
out-of-scope edit and an oversized diff are both refused; a second trigger
during cooldown creates nothing.

## Phase 7 — Operations

- **Logging**: structured logs with attempt and root ids for supervisor,
  worker and API.
- **SQLite**: WAL, `busy_timeout`, `synchronous=FULL`, integrity check on
  open. Numbered, transactional schema migrations; refuse to open newer state.
  Sub-second timestamps plus a monotonic sequence for ordering.
- **Shutdown**: SIGTERM drains, stops child process groups, and records why.
- **Recovery cost**: recovery runs on startup and on lease expiry, not every
  second; credential scrub is incremental.
- **Disk**: reclaim workspaces and runtime directories of fenced and failed
  attempts after archive; audit retention policy.
- **Backup and restore**: sealed manifest stays valid after restore; failed
  restore leaves nothing behind.
- **Costs**: record decider usage; charge actuals when known.
- **Build**: Python lockfile with hashes; runtime image without dev tools;
  harness CLIs installed from locked manifests with `--ignore-scripts`;
  container healthchecks; `/state` no longer world-writable.
- **CI**: actions pinned by SHA; Dependabot; `pip-audit`, `npm audit` and an
  image scan; Python 3.13 and 3.14 in the matrix; validate the live Compose
  overlay; job timeouts and concurrency.
- Tool versions declared once and read by Dockerfile, examples and tests.

Exit: a clean build from lockfiles is reproducible; CI fails on a known-bad
dependency; kill -TERM during an attempt leaves a recoverable state.

## Phase 8 — Requalification

Each is a route or image change and needs its own live probe and human
approval. None is implied by earlier phases.

- Pi coding agent 1.4.0 → 1.5.2.
- Codex CLI 0.164.0 → 0.170.0 (sandbox and trust behaviour changed in 0.169).
- Claude Code 2.1.310 → current stable.
- Model ids only as explicitly approved route changes.
- One complete live outcome, and one live improvement run to a draft PR.

## Order and dependencies

```
0 Governance
└─ 1 Remove + urgent deps
   ├─ 2 Correctness ─┐
   └─ 3 Isolation ───┼─ 4 Consistency ─ 5 Warp ─ 6 Self-improvement ─ 8 Requalification
                     └─ 7 Operations (can run alongside 4–6)
```

Phases 2 and 3 are independent of each other. Phase 6 needs 5 (prompt
layering, definition dataclass) and 2 (external-action handling).

## Working rules

- Build in a separate checkout; never against live controller state.
- One reviewable PR per bullet group; every PR passes Ruff, format, mypy, the
  full suite and Compose smoke.
- A fix lands with the test that would have caught it.
- Anything marked *spike* or *open design point* reports back before the
  design is fixed.

## Out of scope

Multi-repository and child graphs, automatic provider fallback, preview and
merge automation, executing Warp scorers, automations, routers or benchmarks,
`v1alpha2` blueprints, and per-operator authentication.

## Evidence behind this plan

- Reproduced with scripts: C1, C2, C3.
- Verified in source by the reviewer: Warp prompt replacement and frontmatter
  leak, live Compose privileges, Codex self-sandbox exemption, unenforced
  limits, macOS profile shape, heartbeat cancel, stop-hook allowlist and regex.
- From code reading, not independently reproduced: the remaining items.
  Treat each as a hypothesis until its regression test fails on the old code.
- Warp format facts come from Warp's published schemas, documentation,
  example repository and validation endpoint, retrieved 2026-11-19.
- Dependency versions and advisories were checked against the registries on
  2026-11-19.
