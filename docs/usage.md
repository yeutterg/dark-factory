# Usage guide

Plan selected work, approve it, execute one isolated repository change, and review the exact candidate and evidence. Python 3.11+, one controller, one local worker. Multi-repo execution, web UI, previews, automatic fallback and merging belong to increments A–C.

The bootstrap implementation includes real Git workspaces and checks, structured plan/critique/review assignments, a Pi adapter, native macOS isolation, SQLite approvals and leases, verified archives, backup/restore, and JSON GitHub commands. The installed Pi adapter is exercised with a deterministic local HTTP provider in tests; this is not evidence of frontier-model quality or permission to use a live inference account. See [qualification](../specs/qualification.md).

The optional [durable execution layer](durable-execution.md) checkpoints dispatch without replacing individual CLI agents. It also describes preapproved bounded correctness repairs, subscription adapters and named human decision policy.

## Install and try the isolated fixture

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp examples/factory.toml factory.toml
# Choose a new state_dir for this experiment.
dark-factory --config factory.toml demo
# Keep this process running:
dark-factory --config factory.toml serve --worker
# In another terminal:
dark-factory --config factory.toml tui --actor greg
```

`demo` uses explicit fake model routes but real disposable Git checkouts, a real candidate commit, and a canonical Python check. It auto-approves only this simulation, records `simulation-demo` as the actor, and stops at result review. It never calls a model provider, creates a PR or merges. Packaged skills use `@name`; project prompt/skill paths resolve relative to the profile.

## Check a deployment before dispatch

```bash
dark-factory --config /path/to/private/factory.toml preflight
```

Preflight loads the strict profile, verifies source revisions, command executable presence, pinned CLI versions and available role coverage, and checks that the selected workflow runtime can load. Live profiles also need named human identities, distinct private random worker/operator credentials of at least 32 characters, native isolation and live-evidence policy. It returns JSON and exits 2 on a failed check. It never opens/locks/migrates controller state, invokes project verification commands, extracts native logins or calls a model provider. It can therefore run while a controller owns its state directory. Credential diagnostics report environment presence only, not token validity; no credential values are printed.

A passing preflight is not live qualification, plan approval, acceptance or an installation decision. It does not validate the behavior of canonical commands, model quality, account processing policy or subscription overage controls. Read [operational readiness](../specs/operational-readiness.md) before treating a deployment as ready for real work.

## Run selected work with Pi

If Pi is already logged into Together on this Mac (`pi auth check --provider together`), the worker reuses that login. See [Pi + Together](pi-together.md). Use [the annotated live profile](../examples/factory-live.toml) as a template. Supply an explicit source repository/revision, allowed paths, protected verification files, canonical commands, external credential references, exact Pi/model/provider/account/endpoint/region, and budgets. The template intentionally fails validation until those decisions are provided. Region labels are operator assertions backed by the required evidence reference; they are not inferred from model origin. Verify the provider's processing policy before approving a route.

Live Pi requires an explicitly qualified isolated environment. The native `macos-sandbox` profile denies reads of user/private data, writes outside the owned workspace/runtime, protected-file changes, and model shell/extension execution. The local Compose live overlay uses the separately pinned `container-bwrap` command environment described in [Docker Compose](docker-compose.md#live-only-container-worker). Model roles get read tools; only the coder gets edit/write tools. Canonical check processes have no network access or controller/model credentials. These are native OS isolation boundaries, not VMs; a different host or image needs its own qualification.

Select the complete root outcome. The GitHub adapter reads parent relationships and child counts; caller flags cannot turn a live child into a root. Bootstrap rejects child-only selection and roots with required children because child-graph execution is not implemented yet. Preserve the root and use an appropriate orchestration path; do not substitute one child to fit bootstrap. Standalone single-repository outcomes remain supported. Custom tracker discovery commands supply an opaque provider-qualified `external_id`, configured `repo`, positive display `number`, string `title`/`url`, and truthful `parent_id`, `is_root` and `required_child_count`; unresolved or inconsistent relationships fail rather than imply a root.

`discover` lists open issues carrying the `selected` label. To also take work from GitHub Projects, add `--project owner/number` (repeatable) to the tracking command's argv, plus `--theme-field <name>` if the board's theme field isn't called `Theme`. Discovery then lists open issues on those boards from configured repositories, with each item's project, status and theme; the TUI groups new work by project and theme. Project boards need a token with the `read:project` scope, and an unreadable board fails discovery instead of being skipped. Projects only change what is listed: intake, root scope and planning are the same.

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

The result packet's `exec_summary` lists declared assumptions, the exact candidate, repair history and remaining manual steps. Agents choose reasonable reversible defaults inside approved scope; implementation assumptions cannot grant new authority. Configure `[operators] humans` for live decisions. Attestations require a named human, timestamp, candidate SHA and evidence. Rejecting a result records feedback for the next proposal; missing criteria cannot be waived in place.

Use `require_live_evidence = true` on a source to prevent simulation from qualifying its outcomes. Routes explicitly classify `evidence_kind` as `live` or `simulation`; fake routes are always simulation. A real Pi process connected to a fixture provider is still simulation. This classification is operator policy, not something a model can change.

Review the numbered test instructions and archived diff/check/reviewer outputs. Record every declared manual gate against the exact candidate SHA, then accept:

```bash
dark-factory --config factory.toml manual-gate <work-item-id> \
  --name "Inspect candidate diff" --candidate <sha> --actor greg \
  --evidence "Reviewed the exact diff and confirmed the expected behavior"
dark-factory --config factory.toml accept <work-item-id> --actor greg
```

Acceptance does not perform a merge or release. Projects that require merge before acceptance must declare and record that manual gate. Bootstrap leaves Git publishing, merge authorization and installation upgrades with the operator. To create a draft PR for an already published candidate branch, `create-pr <work-item-id> --payload-json pr.json --actor greg` uses an exact `repo`, `head`, `base`, `head_sha`, `title` and `body`. The action is recorded before writing, reconciled after ambiguous failures, and verified by read-back. Changed PR heads require a new decision. `inspect_pr` returns observed checks, statuses, reviews and review comments, and explicitly leaves branch/ruleset requirements to the human merger; it cannot waive them.

### Outside reviews as evidence

A source can require outside reviewers or CI before acceptance with `external_acceptance`, for example `["Cursor Bugbot", "devin-ai-integration[bot]", "ci/build"]`. The list is frozen into the plan snapshot, so it is part of what you approve. Each name matches a PR check run, a commit status context or a reviewer login. Once the candidate is on a PR, read it:

```bash
dark-factory --config factory.toml pr-evidence <work-item-id> --number 42 --actor greg
```

`--number` defaults to the PR opened by `create-pr`. The PR head must be the exact reviewed candidate, or nothing is recorded. A name passes only when every matching source passes on that commit: the latest check run concluded `success`, the status is `success`, or the reviewer's latest review of that commit is `APPROVED`. Comments alone never pass, so reviewers that only comment should report a check or status. `accept` refuses while any required name is missing or failing, and the result packet lists outside reviews, their comments and `external:` missing evidence. Read the PR again after new pushes or reviews; in the TUI, `o` does this.

## Claude subscription planning

The [Claude Code subscription adapter](claude-subscription.md) supports planner, critic and reviewer assignments using a subscription OAuth token from `claude setup-token`. API-key fallback is prohibited. It does not enable child-graph execution or automatic plan comparisons.

## Operate one controller

Start the controller/supervisor independently of the terminal console:

```bash
dark-factory --config factory.toml serve --worker
# In another terminal; connects to runtime.api_host/runtime.api_port:
dark-factory --config factory.toml tui --actor greg
```

The service owns state and execution. The TUI is only an operator client, so
`q`, `Ctrl+Q`, and `Ctrl+C` close the console without pausing, draining, or
stopping the factory. For offline maintenance only, `tui --embedded` opens the
state in the TUI process; it cannot run alongside the service and it does not
run a worker.

The overview has independently scrollable execution and worker panes. `i`, `w`,
and `s` open new work, worker, and settings views; `?` shows the full key map.
New work uses the configured selected label to discover at most 50 open issues,
shows observed parent/child scope, and still revalidates the selected root during
intake. `dark-factory --config factory.toml discover` exposes the same read-only
view as JSON. Candidate changes render from the bounded verified archive only
when that issue is opened.
The settings view reads the active profile and can request a validated reload,
but `factory.toml` remains the source of truth. Human decisions are confirmed and
recorded with the supplied actor.

To connect from a different host or override the configured loopback endpoint:

```bash
dark-factory --config factory.toml --api-url http://127.0.0.1:8745 status
dark-factory --config factory.toml --api-url http://127.0.0.1:8745 pause
dark-factory --config factory.toml --api-url http://127.0.0.1:8745 tui --actor greg
```

Operator commands accept the same arguments and return the same JSON with or without `--api-url`. A remote CLI or TUI config only needs `[runtime] operator_token_env`; it does not load source checkouts, routes, worker credentials, or execution dependencies. The TUI always reads an authenticated, credential-free projection unless `--embedded` is explicit, and never opens a remote controller's SQLite state. `preflight`, `backup`, `restore`, `run-worker`, `serve`, `demo`, `adapter` and `execution-worker` run only locally. The API binds to loopback unless the project-scoped TLS deployment requirements are met. Worker credentials cannot read the operator projection or invoke operator decisions. A process lock prevents a second controller from opening the same state directory. Pauses and drains stop new dispatch; cancellation is separate. `resume` clears pause; `undrain` clears drain. Active attempts retain their input snapshots. A valid reload changes future defaults; incompatible approved work stops for a new proposal. Invalid reload retains the last valid in-memory configuration and records an audit event.

`cancel <job-id>` requests process termination and capture. `recover` fences expired leases, preserves unique work and records conservative estimated cost. It never replays an interrupted CLI; the step can resume from its saved work instead (see below). Unknown interrupted usage consumes the reserved allowance. Repairs default to zero. With `[continuation] max_repairs = 1`, the original proposal includes one conditional correctness repair followed by fresh checks and review, under the original limits. Other stopped outcomes require inspection and a revised plan/approval. There is no automatic provider fallback.

### Interrupted steps

A step that stops without a result is *interrupted*, not failed: the worker was lost, the durable engine or the provider failed, the agent's answer was malformed, or the step ran out of time. Failed checks, blocking review findings, scope or integrity violations, budget overruns and changed approvals still repair or fail as before.

Transient causes (lost worker, engine, provider, malformed answer) resume automatically, up to `[continuation] max_resumes` times per step (default 1, at most 3). Anything else, or a step that used its automatic resumes, waits for a person under **needs you**:

```bash
dark-factory --config factory.toml continue-work <work-item-id> --actor greg
dark-factory --config factory.toml stop-work <work-item-id> --reason "keeps timing out" --actor greg
```

A resumed attempt starts from the interrupted attempt's archived checkout, uncommitted work included, and its brief says it is continuing. Pi also continues the earlier conversation as a fork of the archived session; the archive is never modified. Each resume is a new attempt with the step's approved per-attempt budget and time limit. Nobody can resume past the root's approved ceiling or deadline, or after its approval changed. Stopping fails the work, which then needs a new plan. In the TUI, `y` resumes and `n` stops and asks why. The resume count is part of the approved plan.

The durable engine reports why a dispatch failed (`storage`, `bridge`, `inputs_changed` or `engine`). Changed inputs are a policy failure and are never resumed.

#### Decision agents

An optional decision agent can confirm or overturn the rules. It answers typed questions with a choice and a confidence, behind the same JSON command contract as the tracker and code host (operation `decide`; see `dark_factory/adapters/decision`). TypeSafe's Jev is included:

```toml
[decider]
region = "US"            # Required: a decider is inference, checked against geography policy
min_confidence = 0.8     # Advice below this is ignored

[commands.decider]
argv = ["dark-factory", "adapter", "jev-decision"]
timeout_seconds = 30
```

Jev reads `TYPESAFE_API_KEY` from the controller environment (`--api-key-env` names another variable). It receives the step kind, the cause, the error, timings, costs and the last 2,000 characters of the step's output, never credentials. Confident `resume` resumes a non-trivial interruption; confident `ask` or `stop` holds a trivial one for a person. Neither can lift the limits above, and a missing, failing or unconfident decider falls back to the rules. The decider settings are part of the approved plan. Any command that answers `decide` can replace Jev.

Bundled adapters run through the installed `dark-factory adapter ...`
entrypoint. Profile loading binds that command to the active Python installation,
so an older global script or a source-checkout-relative module path cannot be
selected accidentally. Third-party adapters remain ordinary explicit command
arrays speaking the same JSON contract.

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
python -m pytest -n auto
ruff check src tests && ruff format --check src tests
mypy
```

Tests use temporary repositories/state and fake external services. On macOS with Pi installed, the suite also runs the actual Pi binary through a deterministic loopback provider, exercising tool writes, fresh sessions and OS isolation with no inference spending. [Qualification details](../specs/qualification.md) distinguish this from live model qualification.

Implementation authority: [constitution](../.specify/memory/constitution.md), [behavior](../specs/spec.md), [structure](../specs/plan.md), [tasks](../specs/tasks.md). Use a pinned accepted controller and a separate candidate checkout/state directory for future factory-built increments. Never run a candidate scheduler against live controller state.

## Development stop hook

The repository includes a Codex `Stop` hook in `.codex/hooks.json`. It commits development changes and pushes the current branch to `yeutterg/dark-factory`, then verifies the remote commit. It excludes local profiles, credentials, state and generated artifacts; it refuses unfinished Git operations and never force-pushes. New source, tests and documentation are included. Run relevant tests before ending a turn.

On a new installation, review and trust the hook through Codex `/hooks`. This is the normal [Codex hook trust flow](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks). The hook checkpoints code; it does not merge, accept an outcome or upgrade a controller.
