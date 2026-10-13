# Modular durable execution

## Approved direction

Preserve task-selected CLI harnesses and the project-neutral factory. Add Pi Durable as an optional execution runtime, behind an application-owned interface. Do not replace the controller's policy or acceptance authority, use Pi's model loop for CLI assignments, or upgrade a running controller.

## Boundaries

| Component | Owns | Does not own |
| --- | --- | --- |
| Controller | Root intent, proposals, dependency eligibility, routes, reservations, leases, exact evidence, acceptance | CLI protocols or Pi Durable records |
| Workflow runtime | Durable dispatch intent and execution reconciliation | Roadmap, approvals, retries, credentials, acceptance |
| Harness adapter | CLI invocation, authentication, events, session identity, cancellation, usage | Root scheduling or permission grants |
| Environment adapter | Owned workspace and OS access policy | Model or workflow selection |
| Named commands | Tracker, code host and canonical checks | Core project-specific types |

The workflow protocol exchanges only an attempt ID, an immutable input digest and observed execution status. The controller records the claim before submission; Pi Durable records task intent before requesting execution. These are separate transactions, reconciled by attempt identity. A repeated request observes terminal controller state rather than rerunning a CLI. An interrupted active CLI is fenced by existing lease recovery; transcript/session records are evidence, not permission to replay side effects.

Pi Durable runs no inference and receives no model, tracker or operator credentials. Its private database sits under the candidate controller's own state directory. SQLite uses FULL synchronization and an exclusive owner lock. Runtime source and dependency lock are included in the approval policy. The native runtime remains available; an unavailable selected runtime fails closed, without implicit fallback.

## Implementation sequence and acceptance

1. Extract a workflow lifecycle interface and a harness registry. Existing Pi and Claude Code assignments retain their contracts. Add an explicit Codex subscription CLI adapter with isolated credentials and conservative usage accounting.
2. Add a pinned Pi Durable task adapter and bounded, versioned local bridge. Prove a complete simulation outcome and interrupted dispatch reconciliation without duplicate attempts or inference spending.
3. Persist root continuation views after execution and recovery. Every unfinished job has an eligible next action, dependency wait or explicit blocker. A profile can preapprove one conditional correctness-repair branch followed by canonical checks and independent review, sharing the original scope, budget and deadline. Read-only status never pauses execution. Unsupported child graphs remain explicit capability gaps.
4. Incorporate issue #9 and its review comment: manual attestations include named human, timestamp, candidate and evidence; known worker identities cannot attest. Live decision identities are configured outside core. No in-place waiver exists: scope changes require a revised proposal and fresh approval. Rejections and blocking review findings remain inputs to replanning.
5. Run Python safety/acceptance/adapter tests, real Pi Durable integration and crash tests, packaging checks and the full suite. Publish a draft PR with exact evidence and remaining live/manual gates.

## Issue mapping and limits

- #9: human attribution and non-waivable exact-candidate acceptance are part of this change. A waived model assessment is invalid, never satisfied evidence.
- #8: continuation visibility, dropped-handoff reconciliation, restart, bounded correctness repair and early-exit regressions are included for the supported single-repository graph. Required child graphs, cross-repository integration, provider fallback and hosted-CI continuation are still Increment A work. This PR must not close #8 or #2.
- #6: no live qualification is claimed by fixture runs. Authentication and subscriptions do not authorize inference spending, new processing routes or controller installation.
- #1: develop in a separate checkout; use only dedicated test state. Merge and installation remain human actions.

## Recovery contract

CLI internals are opaque to the workflow runtime. A process death may lose uncommitted streamed output. No adapter promises exactly-once CLI side effects or automatic session takeover. Safe recovery consists of observing completed attempts, fencing abandoned processes, preserving work and publishing a precise next decision. Any future session-resume policy must be explicitly approved and qualified per harness.

## Post-merge review hardening

The five reviewed failure paths have explicit regression contracts: discover controller leases before any engine database exists without bypassing resume guards; preserve valid human review after execution allowance/deadline exhaustion; refresh rejected result packets without replacing proposal packets; independently erase transient credentials after worker death and scrub retired legacy remnants; permit repair only for complete, correctly bound canonical-check failures, never source-integrity or execution failures. Credential paths and cleanup remain harness metadata, not provider-specific controller logic. Local erasure is not token revocation; live isolation/account qualification remains separate.
