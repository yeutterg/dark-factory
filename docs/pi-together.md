# Pi + Together AI

The factory Pi adapter does not read `~/.pi` from the sandbox. It starts a fresh `PI_CODING_AGENT_DIR` per attempt and injects only the approved route.

On this machine Pi 0.85.1 is already logged into Together (`pi auth check --provider together` → `ready`). The worker uses that login without copying the key into git:

1. `TOGETHER_API_KEY` if set, otherwise
2. `pi auth print-api-key --provider together`

The isolated `models.json` points at `https://api.together.ai/v1` with `api = "openai-completions"`.

## Live profile

`factory.local.toml` is gitignored. It pins:

- harness `pi` 0.85.1
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
