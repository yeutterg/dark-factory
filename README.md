# Dark Factory

Run coding agents through a complete, reviewable workflow:

**Plan → approve → implement → check → review → accept.**

You select the work and approve the plan. Dark Factory runs the jobs in isolated workspaces, checks the changes, and saves the code and evidence for review. Missing acceptance evidence keeps the work incomplete.

The current bootstrap is a Python CLI for one repository and one worker, with Pi for coding and a [Claude Code subscription adapter](docs/claude-subscription.md) for read-only planning and review. Live-model qualification is still pending. Multi-repository work, a web UI and previews come later.

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
```

The demo uses simulated agents with real Git workspaces and checks. It makes no model calls and stops for review.

Run tests with `python -m pytest`.

[Usage guide](docs/usage.md) · [Live configuration](examples/factory-live.toml) · [Qualification status](specs/qualification.md) · [Roadmap](specs/tasks.md)

MIT licensed.
