# Universal pipeline plan — 2026-11-26

Status: **plan only; nothing here is implemented.** It extends
[repair-plan.md](repair-plan.md) and changes its order (see "Order").

## Goal

Every change and every release in every adopted repository follows one path
through the factory, whether the work was done by a built-in harness or by any
off-the-shelf coding agent. A repository adopts the factory as its CI/CD.

One path means one place to measure and optimise.

## Decisions (recorded from the operator, 2026-11-26)

| # | Decision |
|---|---|
| P1 | The factory runs the stages. It does not only observe another CI. |
| P2 | Each stage can run on a local runner, GitHub Actions or Blacksmith. Local runners on any machine take work when they have spare capacity. |
| P3 | Work starts as an issue. A plan is approved before a pull request is opened. A pull request with no approved plan is blocked, not adopted retroactively. |
| P4 | Workspaces are always removed when a build ends. Build outputs are discarded when their candidate is superseded, merged or closed. |
| P5 | One factory host per computer. Each repository carries its own configuration for how its kinds of release are built and shipped. |
| P6 | GitHub is the only code host for now. Nothing in core may assume it, so GitLab can be added as an adapter. |
| P7 | A repository's pipeline and release configuration lives in that repository, as YAML, and is read from its main branch. It extends the Warp-style `factory/` tree through a namespaced file (`x-dark-factory`) rather than forking the format. The factory is the execution pipeline around it. Authority never moves into the repository (see Configuration). |

Assumptions. The operator confirmed A1 to A3 and the release retention rule
on 2026-11-26. A4 is the starting rule only; lifting it is tracked in
[issue 18](https://github.com/yeutterg/dark-factory/issues/18).

- A1. Build and test run before independent review. Package and sign run
  before any deploy or OTA.
- A2. Merge, release approval and promotion to production stay human
  decisions. The factory executes the deploy only after the recorded decision.
- A3. One change belongs to one repository. A feature that spans
  repositories is several linked changes. Cross-repository roots stay out of
  scope. A repository may run a cross-repository compatibility check as an
  ordinary test step.
- A4. Release signing keys start on hosted runners only. Signing on local
  runners is deferred to issue 18.
- A5. The pipeline file is YAML and sits in the repository's `factory/` tree
  as a namespaced extension that Warp's validator ignores (P7). Phase 5
  checks whether any Warp `v1alpha1` resource can carry the same information
  directly, so the extension can shrink.

## The one path

Core owns the stage names and their order. A repository cannot add, remove or
reorder stages. It binds each stage to steps, or declares it `not_applicable`
with a reason. A stage that is neither bound nor declared fails validation.

**Change loop** — one issue, one repository, one pull request:

| # | Stage | Who | Output |
|---|---|---|---|
| 1 | intake | tracker | root |
| 2 | plan | factory planner or external agent | plan draft |
| 3 | critique | factory critic | critique |
| 4 | approve | human | approval bound to plan digest |
| 5 | implement | factory harness or external agent | candidate commit |
| 6 | build | factory runner | build evidence |
| 7 | test | factory runner | test evidence |
| 8 | review | factory reviewer, independent of the implementer | review |
| 9 | accept | human | acceptance; merge is a human action |

Stages 6 and 7 replace today's single `check` stage. Existing
`sources.<repo>.checks` become `test` steps.

**Release loop** — one release root, one repository, one release kind:

| # | Stage | Who | Output |
|---|---|---|---|
| 1 | assemble | factory | release commit, list of included roots |
| 2 | build | factory runner | release build |
| 3 | test | factory runner | release test evidence |
| 4 | package | factory runner | signed package, digest |
| 5 | approve | human | approval bound to package digest |
| 6 | stage | factory runner | staged deploy or beta OTA |
| 7 | verify | factory runner, manual steps allowed | soak evidence |
| 8 | promote | human, then factory runner | production deploy or OTA |
| 9 | close | factory | release notes, post-release check |

`rollback` is a named operator command that redeploys the previous kept
package through stages 6 to 9.

A release root is created by an operator command. The factory never
originates one.

## Configuration: two layers

**Host profile** (`factory.toml`, private, one per computer) holds authority:
repositories, credentials, routes, budgets, runners, which runner may hold
which secret, disk budget.

**Repository pipeline** (`factory/x-dark-factory.yaml`, in the repository,
reviewed like code) holds behaviour: steps for each stage, release kinds,
artifacts, caches, manual steps. It is a protected path, so an agent cannot
change the pipeline that judges its own change, and it takes effect only when
it is on the repository's main branch.

How it is read:

- The factory resolves the main branch to a commit and reads the file at that
  commit with `git show`; no checkout is needed.
- A root pins the config commit and digest when it is planned. The approval
  is bound to that digest. A later change on main applies to new roots only;
  in-flight work keeps the configuration it was approved under.
- A pull request that edits `factory/` is a protected-path change. It runs
  the ordinary pipeline and needs a human merge. An improvement root may not
  edit it.

The pipeline file can request a runner or a secret by name. Only the host
profile can grant one. The host profile also decides which repositories the
factory listens to and records a trusted first commit for each when it is
adopted, so adoption itself is a host decision. This is the same split the
Warp definition uses today.

Illustrative content (names are examples, not the Restful configuration; shown
as TOML for brevity, the file is YAML):

```toml
schema_version = 1

[change.build]
runner = "local"
labels = ["esp-idf"]
steps = [["./scripts/factory/build", "--unsigned"]]
artifacts = ["build/*.bin"]
caches = ["ccache", "managed_components"]

[change.test]
steps = [["./scripts/factory/test-host"]]

[release.beta-ota]
branch = "main"
keep = 2

[release.beta-ota.package]
runner = "gha"
steps = [["./scripts/factory/package", "--sign"]]
secrets = ["firmware-signing"]

[release.beta-ota.stage]
steps = [["./scripts/factory/ota", "--campaign", "beta"]]

[release.beta-ota.verify]
steps = [["./scripts/factory/soak-report"]]
manual = ["Confirm on a physical lamp"]

[release.beta-ota.promote]
steps = [["./scripts/factory/ota", "--campaign", "production"]]
```

An app repository would define kinds such as `internal`, `testflight` and
`store`. A cloud repository would define `staging` and `production`. The
stage order is identical in all three.

**Step contract.** A step is an argv run in a clean checkout of the exact
commit. It receives `FACTORY_STAGE`, `FACTORY_KIND`, `FACTORY_COMMIT` and
`FACTORY_OUT`. Anything it wants kept goes in `FACTORY_OUT`. Exit status is
the result. The same argv runs on every runner, so local and hosted evidence
have the same shape.

## Entry by an external agent

The agent is a client of the factory, not a bypass.

- `dark-factory work start <issue>` creates or finds the root.
- `dark-factory work plan` submits a plan draft, or asks the factory planner
  for one. Critique and human approval follow as for any root.
- `dark-factory work checkout` returns a factory-owned worktree for the
  approved root. The factory owns its lifetime, which is what stops stale
  worktrees.
- `dark-factory work submit` records the candidate commit and opens the pull
  request through the code-host adapter.
- `dark-factory work status` reports the stage, blocking reason and evidence.

All output is JSON, so any agent can drive it. `dark-factory init-repo`
writes the pipeline skeleton and an `AGENTS.md` section that tells an agent
to use these commands.

Rules:

- The agent uses a **contributor credential**. It can start work, submit
  plans and candidates, and read status. It cannot approve, accept, release
  or change configuration.
- The implement step is recorded as an attempt of kind `external`, with a
  deadline and no model budget. Path scope and diff checks apply to the
  candidate exactly as for a factory harness.
- Review is always done by the factory's reviewer route. An external agent
  never reviews its own candidate.
- **Enforcement** is a required status, `dark-factory/gate`, on every pull
  request, with branch protection. The factory polls open pull requests in
  adopted repositories. A pull request with no approved root gets a failing
  status that says how to start one.
- A new push is a new candidate. Evidence for the old one is invalid and its
  outputs are discarded.

## Runners

A stage job is claimed like any job today: leased, heartbeated, time-bounded.

- **local**: a factory worker on any registered machine. Workers advertise
  labels (`esp-idf`, `macos`, `docker`). A worker claims a job only under its
  configured parallel limit, load ceiling and free-disk floor. This is how
  spare compute is used.
- **gha**: the factory dispatches one reusable workflow, which runs the same
  step argv and uploads `FACTORY_OUT`. The factory waits, downloads, and
  records digests.
- **blacksmith**: the `gha` adapter with a different runner label.

Stage jobs are not agent jobs. They may receive secrets named in the host
profile, delivered by file. Agent jobs never do. A host profile that grants a
signing secret to a local runner fails validation unless explicitly
overridden (A4).

Runner fallback follows the constitution: at most one, and only when the
pipeline names it.

## Cleanup

Every directory, worktree, container and volume the factory creates is
recorded in one `resources` table with its owner and size.

| Resource | Removed when |
|---|---|
| Stage workspace | The attempt ends, in any state |
| External-agent worktree | Candidate is merged, closed or superseded and the work is pushed |
| Stage outputs (`FACTORY_OUT`) | Candidate superseded, or root merged or closed |
| Release packages | Older than the kind's `keep` count (default 2, for rollback) |
| Named caches | Over the host's cache cap, least recently used first |
| Containers and volumes | With their attempt; all are labelled |

- Evidence records (digests, results, trimmed logs) are kept. They are small.
- Unique work is archived before removal, as the constitution requires.
- A sweep on startup removes anything whose owner is gone, including after a
  crash.
- The host profile sets a disk budget. Below the floor the factory refuses
  new builds with a reason, and does not fill the disk.
- `dark-factory disk` shows usage by repository and kind. `dark-factory gc`
  runs the sweep on demand.

This pulls the "Disk" item forward from Phase 7 of the repair plan.

## Code-host portability

The tracker and code-host adapters are already JSON commands. This plan
completes that boundary:

- One written contract for the operations core needs: list issues, inspect
  and list pull requests, create pull request, set status, dispatch and read
  a hosted run, fetch run outputs.
- No `gh` or GitHub API call outside the two GitHub adapters.
- A conformance test suite that any adapter must pass, run against the fake.

A GitLab adapter is then a second implementation of the contract. It is not
built in this plan.

## Phases

Each phase lands with its tests. Exit criteria use fake routes and a fixture
repository unless stated.

**P0 — Governance**
- Constitution 1.3.0: the factory may execute release and deploy stages after
  a recorded human approval bound to the package digest; every factory-owned
  resource has a recorded owner and a removal rule.
- Amend `spec.md`: many repositories per host, the two loops, external
  implementers, release roots. Record P1–P6 and A1–A5.

**P1 — Resource ownership and cleanup**
- `resources` table, removal at attempt end, startup sweep, disk budget,
  bounded caches, `disk` and `gc` commands.
- Exit: a worker killed mid-build leaves nothing after restart; a superseded
  candidate's outputs are gone; a full disk refuses work with a reason.

**P2 — Repository pipeline and stages**
- Host profile holds many repositories. This is done together with the
  pending legacy-dialect removal, so the config format changes once.
- Pipeline YAML parser and validator, read from main by commit; `pipeline-validate` command; config pinned per root.
- Fixed stage list in core; `check` becomes `build` and `test`; step
  contract; evidence bound to commit, pipeline digest and runner.
- Exit: two fixture repositories with different steps run the same stage
  order; an unbound stage fails validation; a changed pipeline file
  invalidates old evidence.

**P3 — External implementers and the gate**
- Contributor credential, `work` commands, factory-owned worktrees, pull
  request polling, the required status, `init-repo`.
- Exit: a scripted "agent" using only the CLI takes an issue to an accepted
  pull request; an unplanned pull request is blocked; a second push
  supersedes the first and its outputs are removed.

**P4 — Runners**
- Worker labels and capacity checks; the hosted adapter and reusable
  workflow; secret grants and their validation.
- Exit: the same stage produces identical evidence shape on local and on a
  faked hosted runner; a busy worker does not claim; a signing secret is
  never present in an agent job.

**P5 — Release roots**
- Release kinds, assemble, package, approval bound to digest, stage, verify
  with manual steps, promote, close, rollback, retention.
- Exit: a fixture release reaches production only after two recorded human
  decisions; a changed package invalidates the approval; rollback redeploys
  the kept package.

**P6 — Code-host contract**
- The written contract, removal of stray GitHub calls, conformance suite.
- Exit: core passes with the fake adapter alone; the GitHub adapter passes
  the suite.

**P7 — Metrics**
- Per stage, repository and runner: queue time, duration, cache use, outcome.
  One report command.

**P8 — Adoption** (each step needs its own human approval)
1. Dark Factory itself.
2. The cloud repository.
3. The app repository.
4. The firmware repository, including hardware manual steps.
- Each adoption writes the repository's pipeline file, wraps its existing
  build and release scripts as steps, and turns on the required status.
- The orchestrator's issue-lifecycle and review-feedback hooks are retired
  for adopted repositories, so one system owns the lifecycle.
- A one-time, operator-confirmed cleanup of the existing orchestrator
  worktrees, using their current fail-closed rule (clean and merged only).

## Order

```
repair 1 ─ 2 ─ 3 ─ 4 ─┬─ P0 ─ P1 ─ P2 ─┬─ P3 ─┐
                      │                ├─ P4 ─┼─ P5 ─ P7 ─ P8
                      │                └─ P6 ─┘
                      └─ repair 5 ─ 6 ─ 7 ─ 8 (alongside)
```

- Repair phases 1 to 4 come first. The pipeline reuses the job, lease,
  worker and config code those phases fix and reshape. Phase 3 must land
  before any stage job holds a secret.
- P1 comes first inside this plan, because every later phase creates
  resources.
- Repair phases 5 to 8 no longer block this plan and can run alongside.

## Out of scope

Cross-repository roots, a GitLab adapter, GitLab CI as a runner, automatic
merge, automatic release creation, automatic promotion, and replacing the
repositories' own build scripts.

## Open points that report back before the design is fixed

- Whether an `external` implement attempt needs a lease at all, or only a
  deadline.
- How the hosted adapter authenticates run outputs, so a forged upload cannot
  become evidence.
- Whether hardware bench steps should be manual steps or a labelled local
  runner on the bench machine.
