# One pipeline for every change and release

Every repository the factory serves follows the same fixed stages, whether the work was done by the
built-in harnesses or by any outside coding agent. The repository says what each stage runs; the
operator's host profile decides what it may use. Because every build takes the same path, one report
shows where time goes (`dark-factory metrics`).

```
change:   intake → plan → critique → approve → implement → build → test → review → accept
release:  assemble → build → test → package → approve → stage → verify → promote → close
```

## The repository's file

`factory/x-dark-factory.yaml`, read from the repository's **main branch** by commit (no checkout) and
pinned when a root is planned. Edits on a branch do nothing until a person merges them, and the file is
a protected path, so a change cannot rewrite the pipeline that judges it. Every stage is bound to steps
or declared `not_applicable` with a reason; nothing is skipped silently. Scripts it names must be
protected paths. `dark-factory init-repo <path>` writes a skeleton; `dark-factory pipeline-validate <path>`
checks it. See `examples/` and `factory/x-dark-factory.yaml` in this repository.

A step is an argv run in a clean checkout of the exact commit with `FACTORY_STAGE`, `FACTORY_KIND`,
`FACTORY_COMMIT` and `FACTORY_OUT` (where outputs go). Exit status is the result; the same argv runs on
every runner. Build and test run before review. The host's canonical `checks` still run after them.

## What the host grants

```toml
[runners.local]            # always exists; add labels it offers
labels = ["esp-idf"]
max_parallel = 1           # spare-capacity limits: parallel stages, load per CPU, free disk
max_load = 0.8
[runners.signer]
kind = "gha"               # or "blacksmith"; `runs_on` and `workflow` are optional
secrets = ["signing-key"]
[secret_classes]
signing-key = "signing"    # a signing secret cannot be granted to a local runner
[disk]
min_free_gb = 2            # new work is refused below this
keep_finished_days = 7
```

A stage may only use a runner, label or secret the profile grants. A secret reaches only the stage that
asked for it; on a local runner its value comes from `DARK_FACTORY_SECRET_<NAME>` (or `_FILE`). Hosted
runs use the reusable workflow in `examples/github/dark-factory-stage.yml`; artifacts are listed from the
code host's own record of that run. Change stages currently run on local runners.

## Working from any coding agent

An agent is a client of the factory, not a way around it. Give it `runtime.contributor_token_env`; that
credential can run only the `work-*` commands.

1. `dark-factory work-start <repo> <issue>`: create or find the root.
2. `dark-factory work-plan <id> [--proposal '<json>']`: submit a plan draft, or let the factory plan.
   The critic reviews it and a person approves it before any code exists.
3. `dark-factory work-checkout <id>`: a worktree the factory owns.
4. `dark-factory work-submit <id>`: hand it over. The factory captures the diff under the same scope,
   protected-path and size rules, then builds, tests and independently reviews the exact commit.
5. `dark-factory work-status <id>`: stage and what blocks it.

After review the factory pushes the reviewed commit to a new `dark-factory/work/...` branch and opens a
draft pull request. With `[gate] enabled = true` it also sets a required status, `dark-factory/gate`,
on every open pull request: green only for the exact commit it reviewed under an approved plan.

## Releases

A person starts one: `release-start <repo> <kind>`. The factory then runs build, test and package. A
person approves the exact package digest (`release-approve <id> package`); stage and verify follow;
manual verification items need a person (`release-confirm`); a second decision promotes
(`release-approve <id> promote`). A changed package voids its approval. `close` writes the notes and keeps
only the newest `keep` packages (default 2); `release-rollback` redeploys the previous kept one.
Local release stages run in a fresh checkout as the factory's user with only the secrets they were
granted; use a hosted runner or an isolated environment where that is not enough.

## Cleanup

Every directory the factory creates has an owner: workspaces, runtime and dispatch belong to an attempt,
inputs and external worktrees to a root. They are removed when the owner finishes and its unique work is
archived and verified, or after `keep_finished_days`; orphans are removed outright; finished roots'
checkouts are pruned. The sweep runs at startup and every ten minutes. `dark-factory disk` shows use
by area and root, and `dark-factory gc` sweeps now. A code-host contract (`docs/code-host-contract.md`)
keeps GitLab a second adapter away.
