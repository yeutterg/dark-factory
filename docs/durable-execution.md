# Durable execution with interchangeable CLI agents

The controller owns approved intent and acceptance. A task route selects its CLI harness independently.

## Configure continuation

Requires Python 3.11+. Add to a private profile with a new state directory:

```toml
[continuation]
max_repairs = 1

[operators]
humans = ["reviewer-name"]
```

Repairs default to zero; setting `max_repairs = 1` adds an explicit conditional repair/check/review branch to the proposal before approval. Changing the human identity list or repair policy invalidates prior approval. Run `dark-factory --config /path/to/private/factory.toml preflight` before starting.

`run-worker`, `demo` and `serve --worker` use the one native dispatch path. After a restart the worker re-acquires its own unstarted claims (leased, no workspace, no process) once the controller revalidates owner, policy, approval, lease and deadline.

## Continue to a concrete human gate

The worker continues eligible work through implementation, canonical checks and independent review. A check failure or a valid blocking review can activate the one preapproved correctness repair; it reconstructs the exact candidate in a fresh workspace, provides failure evidence to the coder and reruns checks and fresh independent review. Budget, routes, permissions and the original root deadline remain shared. Cancellation, policy violations, missing archives, malformed results, provider failures and exhausted limits cannot activate this branch. Failed attempts and costs remain in the packet.

Status is read-only. Continuation records identify queued/running/verified work, dependencies and blockers. Green jobs with missing automated evidence do not produce `ready_for_human_review`. The final packet contains an `exec_summary` with declared planning/implementation assumptions, exact candidate, repairs, remaining manual steps and the next action. A legacy coder that does not return structured assumptions is explicitly marked `implementation_assumptions_recorded = false`; absence of a report is not a claim that no assumptions were made.

Agents should make reasonable assumptions and choose reversible defaults within approved scope. New delivery requirements, permissions, processing routes or spending need a revised proposal. Manual attestations record a named configured human, timestamp, candidate hash and evidence. Worker identities cannot attest or accept. There is no waiver API: neither prompts nor human attestations turn missing criteria into satisfied evidence. Request changes with `reject`, then replan and approve revised acceptance criteria.

The local operator credential authenticates the decision channel; the identity allowlist authorizes its named attestations. This is a single-operator local service, not an identity-provider-backed multi-user system. Simulation profiles may use fixture human names when no allowlist is configured. Live decisions require configured names.

## CLI adapters

Pi retains its native provider configuration and scoped tools. Claude Code retains its subscription adapter for read-only planner/critic/reviewer roles. Codex CLI supports those roles plus coding, with a pinned native invocation, subscription authentication and OS-scoped writes. Each harness owns its route validation, authentication and event parsing; the controller does not interpret provider-specific events.

Live Codex routes are admitted only through an isolated execution environment. The qualified Linux container recipe treats the exact root-owned immutable Codex executable as a pinned sandbox broker because nesting Codex's Bubblewrap launcher inside the factory Bubblewrap boundary prevents the inner user namespace from starting. Only `codex exec` and the version probe receive that exception. Codex's own tool boundary denies the exact authentication file, worker-private paths, protected writes and `/proc`; approved source writes remain available only to the coder. Other commands continue through the outer driver.

Codex 0.164.0 completed a real ChatGPT-subscription probe with GPT-6.1 Sol under that boundary. The image's root-owned system requirements enforce US data residency, and strict invocation ignores user config and rules and disables hooks, apps, browser/computer use, multi-agent execution and MCP servers. Native transport retries remain bounded by the supervised attempt deadline; no factory/provider/account fallback is enabled. This qualifies that exact container route and isolation behavior, not model quality, billing, a complete factory outcome, another host, or the older macOS nested-sandbox path.

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

`FACTORY_CODEX_AUTH_JSON` is an explicitly supplied native ChatGPT authentication document from the external credential store. The adapter never scans the operator's home or borrows another harness's login. It rejects API-key authentication, starts with an isolated `CODEX_HOME`, disables host config/rules/hooks/connectors, denies model-tool access to its exact authentication file and excludes credentials from command environments. A separate credential guardian starts before the file is created, creates it with mode 0600, and erases it on worker EOF (including SIGKILL), normal completion or the bounded credential deadline. Credentials travel to the guardian over a private pipe, never argv/environment/logs. A lock prevents recovery from scrubbing a live guardian's file. Startup and lease recovery erase adapter-declared orphan credentials in retired attempt runtimes, preserving transcripts and workspaces; whole-host failure may require lease fencing before remnants are scrubbed. Authentication files remain excluded from backups. This erases local copies, not provider-side token revocation. Token counters are recorded, but the reserved allowance is charged conservatively because native CLI output is not proof of a subscription bill.

## Recovery and remaining capabilities

Completed native work is not run twice. An unstarted leased dispatch may continue only after controller revalidation, within its original lease and authority. An interrupted active CLI is not replayed: lease recovery fences its process, preserves work and records conservative cost. The step may then continue in a new bounded attempt from the archived work, and Pi forks the archived session; see [interrupted steps](usage.md#interrupted-steps). Bootstrap can still stop on failures outside its preapproved repair branch.

Dispatch discovery reads the controller's own lease table. The same worker identity, original unexpired lease, exact policy and absence of workspace/PID remain mandatory for resumption. A completed candidate's human review is not blocked solely by execution budget/deadline limits or a dispatch pause; missing evidence and changed approval still block readiness. Rejecting a result refreshes its stored packet immediately, before replanning.

Only a classified ordinary canonical-check failure with the complete approved command set, bindings and exact candidate can activate check repair. Source-integrity violations, interrupted/incomplete verification, invalid archives and changed approval stop the root. A nonzero exit by itself is not repair authority.

Drain active jobs before backup. Restore uses a new directory and the pinned controller. There is no controller upgrade or live-state cutover in this change. Required child graphs, cross-repository integration, provider fallback, publishing/hosted-CI continuation and UI remain separate increments. Configured build commands run as canonical checks; the runtime does not invent missing project checks or delivery gates.
