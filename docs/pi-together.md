# Pi + Together AI

The factory Pi adapter does not read `~/.pi` from the sandbox. It starts a fresh `PI_CODING_AGENT_DIR` per attempt and injects only the approved route.

On this machine Pi 1.2.0 is already logged into Together (`pi auth check --provider together` → `ready`). The worker uses that login without copying the key into git:

1. `TOGETHER_API_KEY` if set, otherwise
2. `pi auth print-api-key --provider together`

The isolated `models.json` points at `https://api.together.ai/v1` with `api = "openai-completions"`.

## Pin prices before live custom-model execution

Pi defaults custom model prices to zero. Live routes with an explicit `api` therefore require a complete approved `pricing` table and `pricing_evidence`. The adapter passes those rates into `models.json` and independently estimates cost from validated usage counters. A zero default, missing/invalid usage or interrupted partial output cannot be recorded as a free paid run. Unknown/interrupted usage consumes at least the full attempt reservation; reported overruns are not discounted. Price changes invalidate approval with the rest of the route policy.

Harness metadata declares `agent/models.json` and `agent/settings.json` immutable inside the native sandbox. The environment denies writes to those approved inputs and its own sandbox policy while retaining writable session/output paths. Parent-side setup occurs before the sandboxed CLI starts; model tools cannot rewrite route/pricing/retry inputs under the broader runtime write grant. Installed Pi still completes its four fresh contexts against the deterministic local provider with these input guards and nonzero fixture estimates.

The [Together model page](https://www.together.ai/models/deepseek-v4-1-flash) currently lists per-million-token input/output/cache-read rates of $0.30/$1.20/$0.006. The annotated live template supplies those illustrative values and a zero cache-write rate for this OpenAI-compatible route; verify the actual account/model contract before approving it. These are client estimates, not provider invoices. Native CLI usage arrives after a request, so cancellation at the estimate ceiling can lag an in-flight request. A strict invoice ceiling needs separately verified provider/account spending controls; controller reservations alone cannot guarantee it. No new private profile, account budget or inference run is approved by these examples.

## Live profile

`factory.local.toml` is gitignored. It pins:

- harness `pi` 1.2.0
- provider `together`
- model `deepseek-ai/DeepSeek-V4.1-Flash` (registered explicitly; Together's Pi catalog still listed V4-Flash-0731 after `pi update --models`)
- region `US` with Together's published North America hosting evidence for DeepSeek
- `macos-sandbox`

Together serverless does not let the customer pick a region. Dedicated endpoints would if a later run requires a pin.

## Run

```bash
# from the dark-factory checkout, venv active
dark-factory --config factory.local.toml status
```

Coding jobs use the Together coder route. Planning with Claude Fable remains a separate assignment (Claude Code subscription or the in-session planner). Do not send implementation work to a China-hosted DeepSeek endpoint.
