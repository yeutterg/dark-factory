# Tasks: bootstrap (v0.1)

Implementation authority remains the [specification](spec.md) and [constitution](../.specify/memory/constitution.md). [Qualification](qualification.md) separates automated proof from the pending live-model run.

- [x] Python package, CLI, constitution, specification, examples and packaged assets.
- [x] Strict TOML schema, relative/packaged asset resolution, atomic reload and last-valid retention.
- [x] SQLite records, exclusive process ownership, transactions, approvals, costs, operational state and pending external actions.
- [x] Versioned JSON command contracts; bounded output, timeout/process cleanup and ambiguous-write recovery tests.
- [x] GitHub selected issue reads, draft PR creation/reconciliation/read-back and PR/check inspection commands. No automatic merge.
- [x] Real Git source snapshots and isolated attempt workspaces; native macOS policy and supervised Pi JSON harness.
- [x] Structured planning/critique, exact proposal approval, implementation, canonical checks, independent review and candidate-bound manual acceptance.
- [x] Per-criterion completion contract, independent coverage review, candidate-bound evidence ledger and simulation/live qualification boundary.
- [x] Archive verification and synchronization before cleanup, failed-archive preservation, cleanup retry and cancellation/lease fencing.
- [x] Local authenticated API, separate operator credential, CLI and one local service worker.
- [x] Real-repository fixture loop, alternate tracker command, prompt override, installed-Pi loopback contract test and restorable-state tests.
- [x] Retained increment issues: [A #2](https://github.com/yeutterg/dark-factory/issues/2), [B #3](https://github.com/yeutterg/dark-factory/issues/3), [C #4](https://github.com/yeutterg/dark-factory/issues/4); [self-development process #1](https://github.com/yeutterg/dark-factory/issues/1).
- [ ] Run the bounded real-model outcome using the operator-approved route/geography/account and spending limit; review the actual plan and result packets. No route is approved by the example profile or fixture tests.
- [ ] Human review of bootstrap revision and explicit merge/installation decision. Never upgrade a running controller automatically.

- [x] Root-scope guards: observed GitHub hierarchy, strict normalized scope metadata, atomic rejection of unsupported child graphs, and checks at planning/approval/dispatch/acceptance; regression-tested without expanding bootstrap capability.

- [x] Claude Code read-only subscription adapter, native OAuth credential reference, worker/config integration, stream/accounting contracts and installed-CLI local-provider isolation test. Live subscription route/allowance/geography qualification remains pending.

- [ ] Bounded automatic fallback and frontier/workhorse/cheap defaults: [#5](https://github.com/yeutterg/dark-factory/issues/5), part of increment A; subscription exhaustion must never enable paid credits.

## Modular durable extension

- [x] Runtime protocol with native default and opt-in pinned Pi Durable dispatch adapter; no model/provider authority in the runtime.
- [x] Harness registry and isolated Codex subscription event contract; preserve individual Pi and Claude Code routes.
- [x] Persisted root continuation, dropped-handoff reconciliation, missing-evidence readiness guards and one original-approval-bound repair/check/review branch.
- [x] Executive-summary assumptions and repairs, named human candidate-bound attestations, no waiver mechanism and rejection feedback into replanning.
- [x] Real durable engine crash/restart fixtures and bounded-repair regressions without external inference spending.
- [ ] Installed Codex isolation and live account qualification; required-child/cross-repository delivery and hosted-CI orchestration remain separate work.

## Controller TUI increment

- [x] Paper-derived controller overview with separate execution and worker scrolling, terminal-state progress suppression, and compact done-today summary.
- [x] Issue activity/evidence tabs, proposal and candidate decisions, manual-gate evidence, explicit tracker-root intake, settings, and worker host/assignment/preflight views.
- [x] Authenticated projection and operator-command transport with a service-owned default TUI; explicit embedded maintenance mode never runs a worker or opens state beside the service.
- [x] Target-size and compact-terminal smoke coverage, authenticated projection tests, and command-boundary regressions.
- [x] Bounded read-only tracker discovery with hierarchy metadata, complete-root selection guards, and on-demand native archived diff rendering.

## Supervisor task management increment

- [x] Durable revisioned tasks distinct from jobs, seeded from planner output and bound to approved criteria, jobs and manual gates.
- [x] Current/next/human-needed projection, attributed agent progress, explicit instruction delivery states and stale-attempt fencing.
- [x] Conflict-preserving `tasks.md` export and authenticated import of the documented checkbox subset.
- [x] Bounded structured-output supervisor command with schema validation, geography/config binding, usage accounting and no expensive fallback.
- [x] Shared task commands and controls in the TUI and authenticated browser dashboard with HttpOnly sessions, CSRF and revision checks.
- [ ] Qualify the selected small model/provider/pricing and each desired harness's live instruction boundary. Fixture and mocked-schema tests are not live qualification.
- [ ] Human review of the exact candidate in both UIs and explicit merge/installation decision.

## Portable definition and improvement slice

- [x] Strict Warp `v1alpha1` definition reader with complete resource hashing,
  fixed-role prompt mapping, shared and role-scoped skills, repository/model
  binding checks, compatibility reporting, and approval-policy inclusion.
- [x] Deterministic proposal-only recurring-failure analysis with distinct-root
  thresholds, cited evidence, suggested definition targets, and no inference or
  external action.
- [ ] Behavioral compatibility for automations, routers, benchmarks, webhooks,
  arbitrary agent graphs, or external skill references remains outside this
  slice and must not be implied by syntax compatibility.

## Repair and hardening ([plan](repair-plan.md))

- [x] Phase 0 governance: constitution 1.2.0, spec amendments, history move.
- [x] Phase 1 removals: Pi Durable runtime, shared-token channel, legacy config dialect (`[project] id` is now mandatory; examples converted; `factory-live.toml` and `factory-durable.toml` removed), dead code (`attach_command`, `run_plain`, `NativeEnvironment.run`) and dev-tool/image bumps. Deferred: unused job status `running` and `jobs.reserved_cents` (Phase 4 and Phase 7 migrations). Open design point resolved: the in-process worker and the remote executor share `Worker._execute`; they differ only in transport, so a simulation-only profile keeps the in-process worker and any real route needs registered workers.
- [x] Phase 2 correctness: C1–C18 and worker robustness done with regression tests (`tests/test_correctness.py`, `tests/test_worker_robustness.py`); kill-and-restart soak in `tests/test_soak.py`.
- [~] Phase 3 isolation and credentials: done — trixie base (Bubblewrap 0.12, image builds), process limit and sized scratch in the bwrap driver, Compose resource limits, `NAME_FILE` credential files, one redaction pass, one protected-path list, API hardening (timeouts, off-thread TLS handshake, login throttle, session cleanup, 500 bodies, 32-character token floor), supervisor mounts only `.git`, GitHub token-scope preflight, stop hook hardening, operator-identity note. Spike result (non-root worker with a written seccomp profile) is in `compose.live-hardened.yaml`, opt-in until a live Codex run qualifies it. Not done — credentials by file or descriptor for Claude Code and Pi and closing `/proc` exposure of the harness environment (needs live probes; Phase 8), macOS deny-by-default profile (cannot be tested on Linux), per-process memory and CPU caps (container-level only).
- [~] Phase 4 consistency refactor: done — one active-attempt constant used everywhere, shared protected-path list, shared secret and redaction helpers, one retrying POST for the decision adapters, `interruptions.py` renamed `resume_policy.py`, one prompt layering. Not done — the larger splits (`config.py` into per-table validators, `Worker._execute`, `control/tasks.py`, `ui/screens.py`), the shared harness CLI base, status enums applied to every SQL literal, the Warp definition as a dataclass.
- [x] Phase 5 Warp `v1alpha1` compatibility: reader checked against Warp's published schemas and 16 example trees (`tests/test_warp_conformance.py`); layered prompts keep the output contract; roles selection, aliases, self-improvement agent, version and strictness rules, whole-tree digest, per-resource report. Not done: runner platform field rules beyond `platform.os`, and automation trigger contents beyond presence.
- [x] Phase 6 self-improvement on by default: `[improvement]` table, persisted signals (loop roots excluded, role attribution fixed), standing-policy approval bound to the profile digest, scoped paths and diff cap, dedicated budget, concurrency and cooldown, target refusals, draft PR publication through a new `push_branch` code-host operation (`tests/test_improvement_loop.py`, `docs/self-improvement.md`). The push and PR path is unit-tested against the contract but has not run against a live GitHub repository (Phase 8).
- [~] Phase 7 operations: done — WAL, `synchronous=FULL`, busy timeout and an integrity check on open; numbered migrations and refusal of newer state; sub-second timestamps with rowid tie-breaks; structured JSON logs from the audit trail; incremental credential scrub; audit retention for noisy records only; decider cost records; SIGTERM drains, stops process groups and records why (roots are not cancelled); backup/restore keeps a verifying manifest and cleans up a failed restore; hashed lockfiles (`requirements.lock`, `requirements-dev.lock`) installed with `--require-hashes`; CI pins actions by SHA, adds Python 3.13/3.14, `pip-audit`, live-overlay validation and Dependabot; the runtime image has no dev tools unless asked. Not done — locked manifests for the harness CLIs (their installers need live qualification), `/state` permissions (needs the non-root worker adopted), an image vulnerability scan.
- [ ] Phase 8 requalification (needs live probes and human approval).

## Universal pipeline ([plan](pipeline-plan.md), [guide](../docs/pipeline.md))

- [x] P0 governance: constitution 1.3.0 and the spec amendment.
- [x] P1 resource ownership and cleanup: owners and removal rules, sweep at startup and every ten minutes, disk floor and budget, `disk` and `gc`.
- [x] P2 repository pipeline: `factory/x-dark-factory.yaml` read from main by commit and pinned per root, fixed stages, build and test inside the check, stage evidence bound to the commit and pipeline digest. Not done: a stage-level container environment (stages run in the check job's environment).
- [x] P3 external implementers and the gate: `work-*` commands, a contributor credential, factory-owned worktrees, `ExternalHarness`, draft PR after review, `dark-factory/gate`. A resubmission after a new push goes through `work-plan` again.
- [x] P4 runners: local capacity limits and labels, GitHub Actions and Blacksmith through one adapter, secret grants, signing secrets refused on local runners. Hosted *change* stages wait for publishing the candidate.
- [x] P5 release roots: two human decisions bound to the package digest, manual verification, retention, rollback.
- [x] P6 code-host contract: written contract, reference fake, conformance suite over both adapters, no GitHub calls outside the two adapters.
- [x] P7 metrics: one report over both loops.
- [ ] P8 adoption, one repository at a time, each with human approval. Dark Factory's own pipeline file is written; cloud, app and firmware are not adopted.
