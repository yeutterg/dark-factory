# Dark Factory

Plan selected work, approve it, execute one isolated repository change, and review the exact candidate and evidence. Python 3.11+, one controller, one local worker. Multi-repo execution, web UI, previews, automatic fallback and merging belong to increments A–C.

The bootstrap implementation includes real Git workspaces and checks, structured plan/critique/review assignments, a Pi adapter, native macOS isolation, SQLite approvals and leases, verified archives, backup/restore, and JSON GitHub commands. The installed Pi adapter is exercised with a deterministic local HTTP provider in tests; this is not evidence of frontier-model quality or permission to use a live inference account. See [qualification](specs/qualification.md).

## Install and try the isolated fixture

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp examples/factory.toml factory.toml
# Choose a new state_dir for this experiment.
dark-factory --config factory.toml demo
```

`demo` uses explicit fake model routes but real disposable Git checkouts, a real candidate commit, and a canonical Python check. It auto-approves only this simulation, records `simulation-demo` as the actor, and stops at result review. It never calls a model provider, creates a PR or merges. Packaged skills use `@name`; project prompt/skill paths resolve relative to the profile.

## Run selected work with Pi

Use [the annotated live profile](examples/factory-live.toml) as a template. Supply an explicit source repository/revision, allowed paths, protected verification files, canonical commands, external credential references, exact Pi/model/provider/account/endpoint/region, and budgets. The template intentionally fails validation until those decisions are provided. Region labels are operator assertions backed by the required evidence reference; they are not inferred from model origin. Verify the provider's processing policy before approving a route.

Only `macos-sandbox` admits live Pi. The qualified native profile denies reads of user/private data, writes outside the owned workspace/runtime, protected-file changes, and model shell/extension execution. Model roles get read tools; only the coder gets edit/write tools. Canonical check processes have no network access or controller/model credentials. This is macOS native isolation, not a VM. Toolchains under system/Homebrew paths are supported; a toolchain in a private home directory needs a separately qualified execution profile.

Supply selected issues as JSON. With `repo` and `number`, the GitHub adapter reads the real issue. A supplied `title` explicitly selects caller-provided fixture data and is labeled in intake evidence.

```bash
dark-factory --config factory.toml intake --issues-json selected.json
dark-factory --config factory.toml plan <work-item-id>
dark-factory --config factory.toml run-worker --max-jobs 2
dark-factory --config factory.toml review <work-item-id>
dark-factory --config factory.toml approve <work-item-id> --actor greg
dark-factory --config factory.toml run-worker
dark-factory --config factory.toml review <work-item-id>
```

Every approved acceptance criterion has a stable ID and binds canonical checks or declared manual gates. The critic checks that the criteria cover the original request; the independent reviewer must assess every ID with evidence references. The result packet includes `acceptance_coverage` and missing evidence. Successful jobs or a reviewer saying “passed” cannot make an incomplete root accepted. Changing approved scope requires another proposal and approval.

Use `require_live_evidence = true` on a source to prevent simulation from qualifying its outcomes. Routes explicitly classify `evidence_kind` as `live` or `simulation`; fake routes are always simulation. A real Pi process connected to a fixture provider is still simulation. This classification is operator policy, not something a model can change.

Review the numbered test instructions and archived diff/check/reviewer outputs. Record every declared manual gate against the exact candidate SHA, then accept:

```bash
dark-factory --config factory.toml manual-gate <work-item-id> \
  --name "Inspect candidate diff" --candidate <sha> --actor greg \
  --evidence "Reviewed the exact diff and confirmed the expected behavior"
dark-factory --config factory.toml accept <work-item-id> --actor greg
```

Acceptance does not perform a merge or release. Projects that require merge before acceptance must declare and record that manual gate. Bootstrap leaves Git publishing, merge authorization and installation upgrades with the operator. To create a draft PR for an already published candidate branch, `create-pr <work-item-id> --payload-json pr.json --actor greg` uses an exact `repo`, `head`, `base`, `head_sha`, `title` and `body`. The action is recorded before writing, reconciled after ambiguous failures, and verified by read-back. Changed PR heads require a new decision. `inspect_pr` returns observed checks/statuses and explicitly leaves branch/ruleset requirements to the human merger; it cannot waive them.

## Operate one controller

For interactive cancellation/approval while the worker runs, start the local service:

```bash
dark-factory --config factory.toml serve --worker
# In another terminal, use the separate operator credential from the profile:
dark-factory --config factory.toml --api-url http://127.0.0.1:8745 status
dark-factory --config factory.toml --api-url http://127.0.0.1:8745 pause
```

The API binds to loopback only. Worker credentials cannot invoke operator decisions. A process lock prevents a second controller from opening the same state directory. Pauses and drains stop new dispatch; cancellation is separate. `resume` clears pause; `undrain` clears drain. Active attempts retain their input snapshots. A valid reload changes future defaults; incompatible approved work stops for a new proposal. Invalid reload retains the last valid in-memory configuration and records an audit event.

`cancel <job-id>` requests process termination and capture. `recover` fences expired leases, preserves unique work and records conservative estimated cost. It never retries automatically. Unknown interrupted usage consumes the reserved allowance. To repair a stopped outcome, inspect the failure packet and use `plan` to create a new version with preserved history and a fresh approval. Bootstrap stops on failure without automatic repair/fallback.

## Preserve and restore work

Every attempt archives the checkout (including Git history and untracked/ignored work), outputs, inputs, usage and session records. Hash verification and filesystem synchronization precede cleanup. Archive failure retains the original workspace. `cleanup` retries teardown only for verified archives and factory-owned terminal workspaces; it never prunes developer worktrees. Archives remain available for review/recovery; operators manage retention after merge and backup.

Drain active work before backup. Restore only into a new, absent directory:

```bash
dark-factory --config factory.toml backup /path/to/new-backup
dark-factory --config factory.toml restore /path/to/new-backup /path/to/new-state
```

Restore verifies artifacts and SQLite integrity and relocates state-owned path references without rewriting historical approvals/evidence. Point a separate profile at the restored state, inspect it, and explicitly resume. Existing live state is never overwritten. Legacy simulation databases without pinned inputs are rejected rather than silently promoted into approved work.

## Verify and extend

```bash
python -m pytest
```

Tests use temporary repositories/state and fake external services. On macOS with Pi installed, the suite also runs the actual Pi binary through a deterministic loopback provider, exercising tool writes, fresh sessions and OS isolation with no inference spending. [Qualification details](specs/qualification.md) distinguish this from live model qualification.

Implementation authority: [constitution](.specify/memory/constitution.md), [behavior](specs/spec.md), [structure](specs/plan.md), [tasks](specs/tasks.md). Use a pinned accepted controller and a separate candidate checkout/state directory for future factory-built increments. Never run a candidate scheduler against live controller state.
