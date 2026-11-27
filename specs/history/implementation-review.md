# Implementation review — 2026-09-11

**Historical review of the initial scaffold.** Subsequent implementation and qualification are tracked in [qualification.md](qualification.md) and [tasks.md](tasks.md); the remaining-gates list below describes the state before that work.

The repository implements a simulation scaffold. It does not meet the bootstrap exit conditions, and increments A–C are absent. Review was against the supplied proposal, constitution and local specifications. No live provider, GitHub, deployment or controller state was exercised.

## Defects corrected

- **False execution success:** missing Pi or unknown harnesses silently became FakeHarness. Dispatch now rejects every unqualified live route; fake execution must be explicitly configured. The demo uses a simulation actor and packets identify missing real evidence.
- **Fabricated external effects:** GitHub adapters reported PR creation, observed checks and status writes without making requests. Unsupported operations now fail explicitly. Intake identifies supplied issue fixtures as its source.
- **Loss of unique work:** preparation erased existing workspaces and archive failures still triggered teardown. Workspaces now use attempt IDs, refuse replacement, preserve files after archive failure, and verify copied content before cleanup. Cleanup failures are audited separately. This is file read-back verification, not yet power-loss or backup/restore qualification.
- **Approval bypass:** approval was allowed before critique, did not cover the planner output, and its digest was never checked at dispatch. Approval now requires both planning gates, includes stable graph/config/route/command/prompt inputs, and is rechecked before post-approval dispatch and acceptance. Replanning cannot erase existing history. Configuration changes conservatively require a new decision; a versioned replan workflow is still missing.
- **Racing claims and costs:** claim and completion decisions now run in SQLite transactions with per-connection thread serialization. Bootstrap permits one active assignment. Costs are linked to attempts and simulation costs are labeled estimated; duplicate results cannot charge twice.
- **Late results:** completed/cancelled attempts reject further results. Expired results cannot report success; timeout/cancellation states are preserved. Rejected, cancelled and failed roots cannot dispatch queued successors. Cancellation retains its budget reservation pending reconciliation.
- **Unbounded commands:** stdout and stderr are drained together with a combined enforced limit and a monotonic deadline. Process groups are killed on completion/failure so child processes cannot keep running after timeout.
- **Configuration and presentation:** unknown fields, invalid numeric limits, malformed windows and timezones fail early. Overnight windows use the local start day and date exceptions. Relative executable paths resolve against the profile. CLI config selection is explicit. Python 3.11+ and tomllib match the proposal. New config history stores only a digest/project summary rather than the worker token; health responses no longer expose project state. Existing database history is not rewritten.

## Remaining bootstrap gates

1. **Real repository execution and evidence:** clone a pinned source into an owned environment, pass selected requirements to the planner, parse and validate its proposal, pass the plan to a fresh critic, reconstruct candidate commits for canonical checks, and give an independent reviewer the exact diff and evidence. The current empty workspace/changelog check and canned role outputs cannot qualify a feature.
2. **Harness and isolation qualification:** bind the exact approved model/provider/account/region/version, enforce permissions and protected paths, isolate controller credentials and state, supervise silent/hung processes, load selected skills, snapshot resources, and record real usage/session identities. Pi binary detection proves none of these.
3. **Controller ownership and recovery:** enforce one controller owner per state directory; renew/fence leases; reconcile interrupted/cancelled attempts, budgets and pending actions; verify database/artifact backup and restoration. Current dispatch stops behind an interrupted lease instead of retrying it. Never point candidate tests at live state.
4. **Approval and acceptance workflow:** persist a reviewable proposal snapshot and versioned replan decision, pin executable/check contents as well as argv, support manual evidence tied to candidates, enforce required gates and human-reviewed revisions, and provide complete failure/review packets with archive links. Current acceptance is simulation-only.
5. **Integration contracts:** implement actual GitHub reads/writes with operation-specific versioned schemas, stable action identities, credential scopes, read-back verification and ambiguous-write recovery. The alternate tracker test proves command replacement, not a live integration.
6. **Configuration lifecycle and accounting:** atomic reload with last-valid retention, selected skill loading, external secret references, account/route and separate planning budgets, complete time/usage records and reconciliation. Strict validation is improved but these lifecycle capabilities are not implemented.
7. **Worker API:** per-worker identity, validated bounded requests/results, archive transport and verification, and a worker client. The existing shared-token API is a local prototype and is not qualified for remote workers.

These gaps remain required work under the original specification; they have not been reclassified as later increments. Live execution remains disabled until the relevant gates are implemented and verified. Use ordinary coding tools to finish bootstrap; do not use this scaffold as a live self-development controller.

## Validation

The original 16 tests passed before review. Regression coverage was added for early approval, input changes, concurrent claims through separate database connections, duplicate/cancelled/expired results, preservation on failed archive, owned-workspace boundaries, route fallback, fabricated external operations, overnight windows, invalid configuration, bounded stderr and config token retention. Tests use temporary state and fake integrations only.

Final validation: **43 tests passed** under Python 3.11 in an isolated virtual environment; `git diff --check` passed. No live integration qualification, merge, deployment or installation upgrade was performed.
