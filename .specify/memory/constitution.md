# Dark Factory Constitution

## Core Principles

### Human approval owns intent
The factory does not choose the product roadmap. Selected work arrives from the project's tracker. Planning may assume and recommend, but implementation, privileged merges, and acceptance require recorded human decisions.

One exception: improvement roots. The factory may originate work that changes its own definition (prompts and skills) in response to recurring, evidenced failures. Plan approval for such a root is the operator's standing policy in `factory.toml`, bound to the profile digest and to code-enforced limits (target, paths, diff size, concurrency, cooldown, budget). The result is a draft pull request. Merge, acceptance and installation remain human decisions; a standing policy never covers them.

### Stages are fixed; authority is not in the repository
Every change and every release follows one fixed stage order. A repository supplies the steps for each stage in its own configuration, read from its main branch, but only the operator's host profile grants runners, secrets, budgets and credentials. A release may be built, packaged and deployed by the factory only after a recorded human decision bound to the exact package digest; production promotion needs its own decision. Signing keys never reach a runner the profile has not explicitly granted them.

### Evidence has exact inputs
Checks, reviews, previews, and costs are tied to the commit map, recipe revision, environment, and command bindings that produced them. Changed relevant inputs invalidate affected evidence or approval.

### Models judge; code enforces
Scheduling, arithmetic, leases, budgets, windows, and permission checks are code. Models draft, critique, implement, and interpret evidence. They cannot waive required checks, expand authorization, or declare themselves accepted.

### Bound execution and archive before delete
Attempts are leased, budgeted, and time-bounded. Default policy is one initial attempt, one targeted correctness repair, and at most one provider/environment fallback. Unique work is archived and verified before owned resources are removed. An interrupted attempt (lost worker, engine or provider fault, unusable output, time limit) is not a result. It may continue from its archived work in a new bounded attempt: automatically for transient causes within the approved resume count, otherwise by a recorded human decision. Resumes never exceed the approved ceiling or deadline, and an interrupted process is never replayed. A decision agent may advise; code applies the limits.

### Project behavior stays outside the core
The controller depends on normalized records, harness and environment protocols, and named JSON commands. Vendor APIs, Restful product details, and preview recipes live in adapters or project scripts.

## Invariants

1. One controller process owns one state directory (SQLite plus artifacts). Every directory the factory creates has an owner and a removal rule, and new work is refused before the disk fills.
2. `factory.toml` is the settings source. Pauses, drains, and run decisions are runtime state.
3. Reload cannot expand approved routes, permissions, or budget for in-flight work.
4. No inference in a blocked processing geography. Unknown geography blocks a route.
5. Coding agents do not receive controller merge, tracker, signing, or host Docker-socket credentials.
6. Successful process exit is not feature acceptance.
7. Bootstrap may stop on failure without automatic fallback.

## Governance

Derive implementation from `specs/spec.md`, `specs/plan.md`, and `specs/tasks.md`. This constitution overrides convenience. Prompt and skill changes are reviewed like code and cannot grant permissions.

**Version**: 1.3.0 | **Ratified**: 2026-09-11 | **Last Amended**: 2026-11-26
