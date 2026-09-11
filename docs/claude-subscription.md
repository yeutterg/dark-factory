# Claude Code subscription planning

The `claude-code` harness runs read-only planner, critic and reviewer assignments through Claude Code. It uses a Claude subscription OAuth token, never a Console API key. Coding, session takeover, automatic plan comparisons and multi-repository controller execution are not enabled by this adapter.

## Authentication

Run the supported `claude setup-token` flow while signed into the intended Claude subscription. Put its token in an external credential store or the worker process environment under `FACTORY_CLAUDE_OAUTH_TOKEN`. Do not put the token in TOML, a repository, a prompt or a result artifact, and do not paste it into an agent conversation.

The worker passes only that credential as `CLAUDE_CODE_OAUTH_TOKEN` to Claude's isolated process. The operator's ordinary keychain login is not mounted into the sandbox. Missing or expired authentication stops the attempt; there is no API-key fallback. Inherited Anthropic keys, provider switches, gateways and proxies are discarded.

This uses the supported subscription automation path described in [Claude authentication](https://code.claude.com/docs/en/authentication). `--bare` is deliberately absent because it ignores subscription authentication. Subscription authorization does not establish which processing regions or usage allowance apply; verify those against the actual account before enabling a live route.

## Profile additions

Add a route like this to a profile with its own approved source, checks, budgets and execution policy. Keep an independently qualified coder route for implementation.

```toml
[execution]
profile = "macos-sandbox"
claude_binary = "/absolute/path/to/claude"
max_seconds = 900

[routes.claude-planner]
harness = "claude-code"
harness_version = "2.1.268 (Claude Code)"
model = "REPLACE_WITH_PINNED_CLAUDE_MODEL_ID"
effort = "xhigh"
provider = "claude-subscription"
endpoint = "https://api.anthropic.com"
billing_account = "REPLACE_WITH_SUBSCRIPTION_ACCOUNT_REFERENCE"
auth_env = "FACTORY_CLAUDE_OAUTH_TOKEN"
region = "UNKNOWN"
geography_evidence = "REPLACE_WITH_VERIFIED_PROCESSING_POLICY"
evidence_kind = "live"
role = "planner"
max_cents = 100
```

This fragment intentionally cannot enable a live route unchanged. Use exact model IDs rather than mutable aliases. The endpoint is Claude Code's native transport; the authentication and billing source is the subscription, not an API key. Define separate role routes if the critic/reviewer needs a different model or effort. Existing approval and root-scope guards still apply.

## Execution and evidence

Each assignment gets a fresh, non-resumable session, safe mode, restricted mode, no implicit settings/skills/hooks/MCP, and only Read/Grep/Glob tools. macOS isolation restricts source reads and denies writes outside the attempt runtime. The only additional read grant is the pinned CLI executable; no home directory or keychain files are exposed. Native repository instructions are supplied by the factory's factual brief.

The harness requires the pinned CLI version, model and session identity, a successful terminal result, and no unapproved tool/model observations. Timeouts, cancellation, invalid JSON, missing/duplicate results, provider errors and estimate-budget overruns fail the attempt. No alternate model is requested. As with any native client, unexpected provider routing is detected from its events and cannot produce accepted evidence; this does not prove the provider never processed a request differently.

Claude's `total_cost_usd` is an API-equivalent client estimate, not proof of a subscription bill. It is stored separately as `api_equivalent_cost_usd`. The factory retains the attempt reservation conservatively with actual cost status `unknown`; it does not record a zero bill or purchase credits. The CLI budget flag also uses the client estimate. Any account-level paid extra usage remains an operator policy prerequisite, not something the adapter changes or can infer from login status.

`tests/test_claude_code.py` exercises authentication selection, output contracts, identity, budgets and process termination. `tests/test_claude_code_native.py` runs the installed binary against a local dummy OAuth provider: a workspace read succeeds, a symlink to a private fixture does not leak its contents, and the native stream completes. No real credential or paid inference is used. This establishes local adapter/isolation behavior, not live model quality, subscription allowance, geography or a completed factory outcome.

For an independent comparison, give Claude the selected root requirements, all required children and the pinned source bundle, without the earlier model's proposal. Consolidation and fresh-context critique remain separate assignments. A root needing unsupported child-graph execution stays blocked rather than being replaced with a child.
