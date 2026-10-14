# Post-Claude merge review plan

## Outcome

Independently review merge `d8f667d` (PR #14) against the constitution and the project-scoped supervisor/worker contract, correct confirmed defects in a separate checkout, and merge the corrective pull request only after exact-head verification succeeds. This review does not authorize live inference, provider-account changes, infrastructure provisioning, controller installation, or mutation of live factory state.

## Review and implementation plan

1. Reconstruct the merged change from its parent and review every behavioral area: controller decomposition, dispatch and leases, route authority, portable execution, archive/cleanup, harness streaming, command/API parity, configuration migration, and CI/tooling.
2. Re-run the merged test/lint/type gates and add focused adversarial tests for any unsafe boundary or regression found. Treat existing green tests as evidence for the exercised cases, not proof that the refactor is correct.
3. Fix only confirmed defects. Preserve project identity, approved routes/budgets/permissions, archive-before-delete, separate worker authentication, and human acceptance. Do not turn the review into a new architectural increment.
4. Record each finding, its mechanism, and its regression test in this file and the pull request. Re-run the complete suite and exact CI on the corrective head.
5. Merge through GitHub after the corrective PR is reviewable, exact-head checks pass, and no unresolved blocking review remains. Verify `origin/main` contains the merge and the source checkout remains clean.

## Acceptance gates

- No stale or cross-project attempt can execute, settle, or delete another worker's resources.
- A controller reload cannot expand an in-flight approval or cause local/API behavior to diverge.
- Read-only archive elision cannot discard unique filesystem or Git state.
- Streaming harnesses preserve bounded output, cancellation, usage, and exact route credentials.
- Refactoring does not bypass the single controller mutation boundary or direct worker facts around `AttemptRecords`.
- Ruff, formatting, mypy, the complete pytest suite, and exact-head hosted CI pass.

## Findings and disposition

1. **Read-only archive elision could discard unique Git-only state.** The merged fingerprint covered refs, object counts, index flags, and stash, but not reflogs, repository-local configuration, hooks, or other owned `.git` state. A branch could be created and deleted, leaving only a unique reflog, while the workspace was classified as unchanged and deleted. The fingerprint now includes every owned Git path using stable identities, with a content digest for Git's noisily rewritten index. Unsafe/external Git directories fail closed to a full archive. Regression coverage exercises unchanged, notes, branch, reflog, config, and untracked states.

2. **A non-fake adapter labelled `simulation` could silently serve another role.** Route selection and project validation treated the evidence label as authority to reuse a route across roles. That could give a planner/reviewer a coder route's CLI, tools, provider, or account. Only the deterministic fake fixture may now span roles; every real Pi, Codex, or Claude route remains bound to its declared role regardless of evidence classification. The installed Pi qualification fixture now declares four explicit role routes.

3. **Failed named-sandbox release was not durably retried.** A command/VM driver release failure could be recorded, but the remote worker then deleted or stranded the only cleanup context, and the supervisor's retry path used a native workspace deletion instead of the pinned external driver. Cleanup is now a separate durable journal phase. It reconstructs the exact approved driver descriptor from frozen attempt inputs, retries only release (never the harness), retains runtime/replica state until confirmation, and binds the supervisor acknowledgement to the settled result digest. The regression test also drops the first cleanup acknowledgement and proves restart-safe retry before deletion.

No live inference, provider-account mutation, infrastructure provisioning, controller installation, or live factory-state mutation was performed.

## Verification evidence

- `ruff format --check src tests`: 93 files formatted.
- `ruff check src tests`: passed.
- `mypy`: passed for 57 source files.
- `python -m pytest -q -n auto`: 264 passed, 14 skipped.
- Exact-head hosted CI and merge verification are required before this plan is complete.
