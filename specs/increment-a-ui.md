# Increment A and UI execution plan

Status: selected by the operator on 2026-09-12; live dispatch not started. This document records scope and readiness, not completed capabilities or an approved live route.

## Why and what

Finish the dependable factory before using it to build the project UI. Pi remains the coding harness. Run complete root outcomes through controller-owned planning, critique, approval, isolated implementation, checks and independent review; do not substitute directly supervised agents and call that factory execution.

Issues own the completion checklists: [Pi qualification #6](https://github.com/yeutterg/dark-factory/issues/6), [Increment A #2](https://github.com/yeutterg/dark-factory/issues/2), [fallback and tier routes #5](https://github.com/yeutterg/dark-factory/issues/5), and [UI #3](https://github.com/yeutterg/dark-factory/issues/3). Complete A before B. [Preview/merge execution #4](https://github.com/yeutterg/dark-factory/issues/4) remains separate.

## Current evidence

- Baseline source: `6584f45712b9286c7f3ef671f5062050127900e3`.
- Isolated candidate branch: `feat/increment-a-ui`. Existing controller installation and state are unchanged.
- Fresh Python 3.11 environment: **114 tests passed in 69.62 seconds** on 2026-09-12. This includes the installed Pi 0.85.1 deterministic loopback test. It is not a live provider or model-quality test.
- Pi currently accepts explicit API-key routes in the factory adapter. Each harness must receive only its own explicitly configured credential; native support for another authentication method is not authorization to use it.
- No live Pi coder route/account/limits are configured. Claude Code is read-only in the current worker; direct Codex assignments are outside the controller.
- No web client, sprint snapshot store or takeover CLI exists. The authenticated local API exposes basic status and operator decisions.
- Developer review checkouts and controller-owned execution workspaces have different lifecycles. Only verified, archived controller-owned resources qualify for automatic teardown.

## Ordered work

1. **Qualify Pi.** Select an approved provider, exact model/version, processing-region basis, harness-specific credential reference and limits through private configuration. Keep a provider-only credential store outside candidate source, exclude credentials from logs and archives, prohibit paid fallback, and test filesystem denial/cancellation/refresh behavior. Use ordinary bootstrap tools for a missing bootstrap capability; identify this explicitly. Qualify the resulting controller revision in new state before selecting it for later runs.
2. **Multi-repo records and graph.** Keep roots as the unit of approval and budget; normalize children/dependencies and pin each source. Extend the validated graph and attempt inputs rather than maintaining a parallel multi-repo workflow. Reconstruct combined candidates for integration checks; include all required child and parent acceptance gates.
3. **Dispatch and recovery.** Add per-repository writer exclusion, worker capacity and route/machine availability to transactional claims. Implement explicit failure classes, one shared repair allowance and one approved fresh availability fallback. Fence old attempts; preserve unknown costs and unique work; reconcile pending external actions before replacement.
4. **Tracking and qualification.** Read cross-repo relationships and reconcile configured status writes through command adapters. Prove a real bounded two-repo outcome plus isolated cancellation, stale result, restart, provider denial, lease expiry and archival-failure cases. Close A only when its entire issue checklist has evidence.
5. **UI.** Add normalized read views, exact-input decision preconditions and sprint snapshots to the existing controller API; build the React/Vite root-first client. Keep operational state in SQLite and secrets out of client assets. Qualify one Pi takeover/return path through CLI commands. Test actual API/browser workflows, stale/offline state, keyboard/mobile layouts and installed static assets. Serve a usable local UI and publish the review packet.

## Checks and protected changes

Existing controller, credential, permission, archive and acceptance tests remain required. New cases must demonstrate actual behavior and failure boundaries, including cross-repo integration and stale-decision rejection. Frontend tests and its production build supplement these checks; a browser fixture does not establish a live controller run.

Planned changes include configuration schema, controller/store/worker, Pi authentication, API/CLI, new web package/lockfile, tests, documentation and package asset inclusion. These are sensitive execution and dependency changes: pin existing trusted checks before dispatch and review new checks/bindings before activation. No candidate may upgrade its running controller or weaken a required gate.

## Decisions and limits

The selected scope is A followed by the UI, retaining Pi. Deployment-specific account choices, harness restrictions and limits remain in private operator records. A supported authentication mechanism must not be inferred as permission to transfer credentials between harnesses. Public examples use placeholders; installed credentials and elapsed time do not authorize a route or budget.

No numerical budget, new route, fallback route, processing-region attestation, or installation upgrade is granted by this document. A live proposal must record these along with acceptance/graph/command digests before approval and implementation dispatch. Human review, merges and installation decisions remain explicit. No project-specific product types belong in the factory core.

## Adapter and configuration boundaries

Keep exactly two lifecycle interfaces, Harness and Environment. Integrations outside those lifecycles use named commands. Route/model/tier selection, credential references and limits live in explicit project configuration, not the controller, frontend or prompts. Snapshot resolved bindings before dispatch; defaults and fallback cannot expand an existing approval. Keep live profiles, account identifiers, local paths and raw evidence outside public source.
