# Dark Factory Constitution

## Core Principles

### Human approval owns intent
The factory does not choose the roadmap. Selected work arrives from the project's tracker. Planning may assume and recommend, but implementation, privileged merges, and acceptance require recorded human decisions.

### Evidence has exact inputs
Checks, reviews, previews, and costs are tied to the commit map, recipe revision, environment, and command bindings that produced them. Changed relevant inputs invalidate affected evidence or approval.

### Models judge; code enforces
Scheduling, arithmetic, leases, budgets, windows, and permission checks are code. Models draft, critique, implement, and interpret evidence. They cannot waive required checks, expand authorization, or declare themselves accepted.

### Bound execution and archive before delete
Attempts are leased, budgeted, and time-bounded. Default policy is one initial attempt, one targeted correctness repair, and at most one provider/environment fallback. Unique work is archived and verified before owned resources are removed.

### Project behavior stays outside the core
The controller depends on normalized records, harness and environment protocols, and named JSON commands. Vendor APIs, Restful product details, and preview recipes live in adapters or project scripts.

## Invariants

1. One controller process owns one state directory (SQLite plus artifacts).
2. `factory.toml` is the settings source. Pauses, drains, and run decisions are runtime state.
3. Reload cannot expand approved routes, permissions, or budget for in-flight work.
4. No inference in a blocked processing geography. Unknown geography blocks a route.
5. Coding agents do not receive controller merge, tracker, signing, or host Docker-socket credentials.
6. Successful process exit is not feature acceptance.
7. Bootstrap may stop on failure without automatic fallback.

## Governance

Derive implementation from `specs/spec.md`, `specs/plan.md`, and `specs/tasks.md`. This constitution overrides convenience. Prompt and skill changes are reviewed like code and cannot grant permissions.

**Version**: 1.0.0 | **Ratified**: 2026-09-11 | **Last Amended**: 2026-09-11
