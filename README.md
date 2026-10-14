# Dark Factory

Run coding agents through a complete, reviewable workflow:

**Plan → approve → implement → check → review → accept.**

You select the work and approve the plan. Dark Factory runs the jobs in isolated workspaces, checks the changes, and saves the code and evidence for review. Missing acceptance evidence keeps the work incomplete.

The controller has a terminal operator console and a scriptable Python CLI. Task routes independently select Pi, [Claude Code](docs/claude-subscription.md) or [Codex CLI](docs/durable-execution.md). An optional Pi Durable runtime checkpoints dispatch without replacing those CLI agents or controller policy. Preapproved bounded repair continues through fresh checks and review; review packets disclose assumptions. Live-model qualification is still pending. Multi-repository work, a web UI and previews come later.

[Project-scoped execution](docs/distributed-execution.md) separates the supervisor from execution workers. Pin each project's accounts, CLI/model routes and sandbox recipes; run workers locally or remotely over verified TLS. Portable assignments and verified result acknowledgements let disposable task replicas/builds be removed without deleting canonical review evidence. OCI and named VM/sandbox drivers remain independently qualified backends, not authority to provision infrastructure or change accounts.

## Try it

Requires Python 3.11+.

```bash
git clone https://github.com/yeutterg/dark-factory.git
cd dark-factory
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp examples/factory.toml factory.toml
dark-factory --config factory.toml demo
dark-factory --config factory.toml tui --actor greg
```

The demo uses simulated agents with real Git workspaces and checks. It makes no model calls and stops for review.

Run tests with `python -m pytest -n auto`; lint and type-check with `ruff check src tests` and `mypy`.

[Usage guide](docs/usage.md) · [Pi + Together](docs/pi-together.md) · [Live configuration](examples/factory-live.toml) · [Qualification status](specs/qualification.md) · [Roadmap](specs/tasks.md)

MIT licensed.
