# Durable execution with interchangeable CLI agents

The controller owns approved intent and acceptance. The workflow runtime checkpoints dispatch. A task route selects its CLI harness independently. Pi Durable does not run its own model loop for Codex, Claude Code or Pi CLI assignments.

## Enable the optional runtime

Requires Python 3.11+ and Node 22.19+. Install the pinned dependencies in the package containing your candidate controller:

```bash
cd src/dark_factory/adapters/workflow/pi_engine
npm ci --ignore-scripts
npm run check
```

For a wheel installation, find the adapter directory with `python -c 'from dark_factory.adapters.workflow.pi_durable import ENGINE_DIR; print(ENGINE_DIR)'`, then run the same commands there. The wheel includes the engine source and lock file, not downloaded dependencies. Run this only for the separately selected candidate installation.

Add to a private profile with a new state directory:

```toml
[workflow]
engine = "pi-durable"
node_binary = "node"

[continuation]
max_repairs = 1

[operators]
humans = ["reviewer-name"]
```

Native execution is the default when `workflow` is absent. Repairs default to zero; setting `max_repairs = 1` adds an explicit conditional repair/check/review branch to the proposal before approval. Runtime selection does not select a model, discover an account or authorize billing. Changing the engine, code, dependency lock, human identity list or repair policy invalidates prior approval.

Run `dark-factory --config /path/to/private/factory.toml preflight` after installation. The worker checks the pinned packages, Node minimum and actual engine import before claiming any attempt. An installation failure leaves eligible work unleased with no attempt cost or reservation; install the selected dependencies and retry, without switching engines or opening a different state directory. The scheduler's probes inherit no credentials or Node hooks.

The probe runs the exact dispatch entry point in `--preflight` mode, including all imported subpaths, registry/models construction and SQLite startup against an in-memory database. No controller checkpoint or agent is started. Validation is reused only within that one runtime/claim cycle; idle/paused/drained workers with no pending reconciliation do not start Node. If startup fails after a lease but before the local execution callback, the controller may fence it as `not_started`, release its reservation unbilled and leave the same authorized assignment queued under its original root limits. It requires matching ownership, leased status and no workspace/PID; started/uncertain work cannot use this path, and late results are rejected. A persisted workflow-unavailable reason keeps unfinished roots explicitly blocked until validation succeeds; completed candidates retain their human gate. This is prerequisite recovery, not a new paid attempt, fallback route or permission expansion.

`run-worker`, `demo` and `serve --worker` use the selected runtime. No model calls occur inside the durable runtime itself. It receives only dispatch identity and observed status over bounded local pipes, with no model or operator credentials. SQLite uses FULL synchronization and a single owner lock; each attempt has a separate database so opening one cannot resume unrelated work.

## Continue to a concrete human gate

The worker continues eligible work through implementation, canonical checks and independent review. A check failure or a valid blocking review can activate the one preapproved correctness repair; it reconstructs the exact candidate in a fresh workspace, provides failure evidence to the coder and reruns checks and fresh independent review. Budget, routes, permissions and the original root deadline remain shared. Cancellation, policy violations, missing archives, malformed results, provider failures and exhausted limits cannot activate this branch. Failed attempts and costs remain in the packet.

Status is read-only. Continuation records identify queued/running/verified work, dependencies and blockers. Green jobs with missing automated evidence do not produce `ready_for_human_review`. The final packet contains an `exec_summary` with declared planning/implementation assumptions, exact candidate, repairs, remaining manual steps and the next action. A legacy coder that does not return structured assumptions is explicitly marked `implementation_assumptions_recorded = false`; absence of a report is not a claim that no assumptions were made.

Agents should make reasonable assumptions and choose reversible defaults within approved scope. New delivery requirements, permissions, processing routes or spending need a revised proposal. Manual attestations record a named configured human, timestamp, candidate hash and evidence. Worker identities cannot attest or accept. There is no waiver API: neither prompts nor human attestations turn missing criteria into satisfied evidence. Request changes with `reject`, then replan and approve revised acceptance criteria.

The local operator credential authenticates the decision channel; the identity allowlist authorizes its named attestations. This is a single-operator local service, not an identity-provider-backed multi-user system. Simulation profiles may use fixture human names when no allowlist is configured. Live decisions require configured names.

## CLI adapters

Pi retains its native provider configuration and scoped tools. Claude Code retains its subscription adapter for read-only planner/critic/reviewer roles. Codex CLI supports those roles plus coding, with a pinned native invocation, subscription authentication and OS-scoped writes. Each harness owns its route validation, authentication and event parsing; the controller does not interpret provider-specific events.

Live Codex routes are currently blocked in configuration validation and again before adapter invocation. The outer CLI sandbox permits runtime reads for native authentication; the proposed inner tool-permission denial has not yet been qualified with the installed CLI. Do not interpret fixtures or this example as permission to expose real credentials to agent tools. Only explicitly classified simulation fixtures are admitted until a separate credential/tool-isolation qualification proves that boundary. Pi and Claude route admission is unchanged.

Installed Codex 0.163.0 command-sandbox probes deny a dummy credential file while allowing a workspace read. This proves the command sandbox only. The factory's outer macOS sandbox rejects Codex's nested sandbox at startup; built-in tool isolation and complete native stream/live-account qualification remain unproven. A future environment adapter must qualify the complete boundary rather than remove the outer protection to obtain a successful exit. Private profiles should select an explicitly pinned native executable; a home-installed Node wrapper needs package reads that the current single-file grant does not provide. Current Codex rejects overrides of the built-in `openai` provider; native transport retries are bounded by the supervised attempt deadline, not disabled by invalid configuration. No factory/provider/account fallback is enabled.

An illustrative Codex route (complete the private source, isolation and limits first):

```toml
[routes.codex-coder]
harness = "codex"
harness_version = "REPLACE_WITH_EXACT_CODEX_VERSION_OUTPUT"
model = "REPLACE_WITH_EXPLICIT_GPT_MODEL_ID"
provider = "chatgpt-subscription"
endpoint = "https://chatgpt.com/backend-api"
billing_account = "REPLACE_WITH_PRIVATE_ACCOUNT_REFERENCE"
region = "US"
geography_evidence = "REPLACE_WITH_PROCESSING_POLICY_EVIDENCE"
auth_env = "FACTORY_CODEX_AUTH_JSON"
role = "coder"
effort = "high"
max_cents = 200
```

`FACTORY_CODEX_AUTH_JSON` is an explicitly supplied native ChatGPT authentication document from the external credential store. The adapter never scans the operator's home or borrows another harness's login. It rejects API-key authentication, starts with an isolated `CODEX_HOME`, disables host config/rules/hooks/connectors, denies model-tool access to its credential directory and excludes credentials from command environments. A separate credential guardian starts before the file is created, creates it with mode 0600, and erases it on worker EOF (including SIGKILL), normal completion or the bounded credential deadline. Credentials travel to the guardian over a private pipe, never argv/environment/logs. A lock prevents recovery from scrubbing a live guardian's file. Startup and lease recovery erase adapter-declared orphan credentials in retired attempt runtimes, preserving transcripts and workspaces; whole-host failure may require lease fencing before remnants are scrubbed. Authentication files remain excluded from backups. This erases local copies, not provider-side token revocation. Token/session event contracts and cancellation are fixture-tested; installed Codex permission/isolation and live subscription qualification remain required before operating a live route. Token counters are recorded, but the reserved allowance is charged conservatively because native CLI output is not proof of a subscription bill.

## Recovery and remaining capabilities

Reopening a recorded dispatch observes the same attempt. Completed native work is not run twice. An unstarted leased dispatch may continue only after controller revalidation, within its original lease and authority. An interrupted active CLI is not replayed: lease recovery fences its process, preserves work and records conservative cost. The step may then continue in a new bounded attempt from the archived work, and Pi forks the archived session; see [interrupted steps](usage.md#interrupted-steps). The engine reports a typed reason for a failed dispatch. Bootstrap can still stop on failures outside its preapproved repair branch.

Dispatch discovery includes controller leases created before the engine checkpoint exists. The same worker identity, original unexpired lease, exact policy and absence of workspace/PID remain mandatory for resumption. A completed candidate's human review is not blocked solely by execution budget/deadline limits or a dispatch pause; missing evidence and changed approval still block readiness. Rejecting a result refreshes its stored packet immediately, before replanning.

Only a classified ordinary canonical-check failure with the complete approved command set, bindings and exact candidate can activate check repair. Source-integrity violations, interrupted/incomplete verification, invalid archives and changed approval stop the root. A nonzero exit by itself is not repair authority.

Drain both jobs and the workflow runtime before backup. Backup includes workflow checkpoints; restore uses a new directory and the pinned controller. There is no controller upgrade or live-state cutover in this change. Required child graphs, cross-repository integration, provider fallback, publishing/hosted-CI continuation and UI remain separate increments. Configured build commands run as canonical checks; the runtime does not invent missing project checks or delivery gates.
