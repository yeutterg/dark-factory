# Dark Factory

Modular framework for building an autonomous software factory with durable coding agents. Provider-agnostic, and adapts to your CI/CD, tracker, and preferred tools. Every acceptance criterion needs evidence and a human decision.

Pick issues from your tracker and approve a plan. Agents then run the work in isolated workspaces: plan, implement, run your checks, and review. The factory keeps the code and the evidence for each acceptance criterion, and you decide what ships.

**Plan → approve → implement → check → review → accept.**

## Built to fit your stack

The controller enforces scheduling, budgets, leases, and approvals. Everything specific to your tools is a replaceable adapter:

- **Coding agents:** Each role (planner, critic, coder, and reviewer) is routed to a harness and model. [Claude Code](docs/claude-subscription.md), [Codex CLI](docs/durable-execution.md), and [Pi](docs/pi-together.md) are supported. With Pi, you can use any provider it supports, such as Together. Routes pin the provider, account, region, and spending limit.
- **Trackers and code hosts:** Work comes in and goes out through small JSON command adapters. GitHub adapters are included; another tracker or code host needs a script that speaks the same commands.
- **Checks and CI/CD:** Acceptance is tied to the canonical commands you already run, such as tests, linters, and builds. Manual gates cover anything else. Results bind to the exact commit, so a passing exit code alone never counts as acceptance.
- **Execution environments:** Workers can run locally or on [remote machines over verified TLS](docs/distributed-execution.md), under native macOS isolation. OCI and VM sandbox drivers plug into the same interface and are qualified separately.
- **Durable runs:** Attempts are leased, budgeted, and checkpointed. An optional [durable workflow runtime](docs/durable-execution.md) checkpoints dispatch. Every workspace is archived and verified before it's removed.
- **Prompts and skills:** Packaged defaults can be overridden per project in `factory.toml`.

You drive it from a terminal console (`dark-factory tui`) or a scriptable CLI. Both work locally or against a remote controller.

![The Dark Factory terminal console: issues waiting on you, running work with stage progress, and workers](docs/images/tui-overview.png)

## Example stack

This is just one example, and it's totally customizable. Each stage is a route or adapter in `factory.toml`, so you can swap any piece for the tools you already use.

| Stage | Example tools | How it connects | Today |
| --- | --- | --- | --- |
| Intake | GitHub Issues and Projects | Tracking adapter reads labeled issues and their sub-issues | Issues built in; Projects needs an adapter update |
| Planning | GPT Astra via Codex CLI | Planner route | Supported harness |
| Critique | Claude Opus via Claude Code | Critic route, on a different model from the planner | Supported harness |
| Approval | You, in the TUI | Recorded human decision with a spending limit | Built in |
| Implementation | DeepSeek Flash via Pi, on the Pi Durable runtime | Coder route through a provider Pi supports, such as Together | Supported harness and runtime |
| Checks | Your test, lint, and build commands; GitHub Actions | Canonical commands run in the sandbox; PR checks are read back | Built in |
| Review | Devin and Bugbot on the draft PR, plus the factory's reviewer | Reviewer route; outside reviewers' PR checks are read back | Reviewer route built in; PR review comments aren't read yet, and outside reviews don't count as evidence yet |
| Accept and merge | You | Accept in the TUI, then merge under your branch rules | Built in; merging stays with you |

Routes pin each model's provider, account, and processing region. A route to a region your policy blocks won't run.

## Principles

- **People own intent.** The factory doesn't choose the roadmap. Implementation and acceptance need recorded human decisions.
- **Models judge; code enforces.** Models can't waive checks, expand their budget or permissions, or accept their own work.
- **Evidence has exact inputs.** Checks and reviews are tied to the commit and environment that produced them.

See the [constitution](.specify/memory/constitution.md) for the full set.

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
dark-factory --config factory.toml tui --actor you
```

The demo uses simulated agents with real Git workspaces and checks. It makes no model calls and stops for your review.

Run tests with `python -m pytest -n auto`; lint and type-check with `ruff check src tests` and `mypy`.

## Status

This is early. The control plane, adapters, and isolation are tested end to end with simulated and loopback providers. Live-model qualification is still pending. Multi-repository work, a web UI, and previews come later.

[Usage guide](docs/usage.md) · [Live configuration](examples/factory-live.toml) · [Qualification status](specs/qualification.md) · [Roadmap](specs/tasks.md)

MIT licensed.
