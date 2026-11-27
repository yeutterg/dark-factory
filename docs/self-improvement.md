# Self-improvement

Dark Factory watches its own outcomes. When the same failure recurs across enough
distinct root outcomes, it opens one improvement root, runs it through the ordinary
pipeline (plan, critique, implement, canonical checks, independent review), and
opens a **draft pull request**. A person reviews, merges or closes it. Nothing is
merged, accepted or installed automatically, and the loop never edits the running
installation.

It is on by default. Set `enabled = false` in `[improvement]` to turn it off.

## What triggers it

Signals come from records, not from a model: failed canonical checks, blocking
review findings, failed or timed-out stages, interruptions by category, and human
rejections. Roots the loop created never count as evidence. A signal needs
`threshold` distinct roots (the definition's `selfImprovement.failedRunThreshold`,
else three). Evidence is stored as it happens, so status reads do not recompute it.

## What it may do

Approval of an improvement plan comes from your standing policy in `factory.toml`,
not from a model. It is bound to the profile digest, so changing the profile (or
any limit below) ends the approval. The policy approves only if the critique
passed, the planner asked no blocking question, scope and limits are present and
the profile has not changed since planning; otherwise the root waits for a person.
It covers improvement roots only, and never covers merge, acceptance or installation.

```toml
[improvement]
enabled = true
repository = "owner/name"          # the checkout it may change (default: the only repository)
base_branch = "main"
allowed_paths = []                  # optional restriction; it can only narrow the eligible set
protected_paths = []                # added to the repository's own protected paths
max_files = 3                       # a larger diff is a scope violation; no repair is offered
max_changed_lines = 80
max_open = 1                        # one improvement root at a time
cooldown_days = 7                   # a signal waits this long after its root, unless new evidence arrives
budget_cents = 500                  # a dedicated allowance, inside the account allowance
threshold = 3
```

The target must be a separate checkout: the profile is refused if it names the
running installation or a directory that holds the controller's state. What the standing policy may change is **only prompts and skills**: the selected Warp agents' prompt
files and skill directories (or the packaged prompts and skills when the target has no definition),
intersected with the repository's own allowlist and with `allowed_paths` here. It never covers runner,
automation or router definitions, and the code-owned output contracts are protected. If the intersection
is empty the loop idles and `status` says why. Protected paths (checks, CI, routes, budgets, permissions,
profiles) are enforced in every sandbox and when the candidate is captured.

## Where it ends

On a passing review the controller pushes the exact candidate commit to a new
`dark-factory/improvement/<signal>-<sha>` branch (never an existing branch, never
forced) and opens a draft pull request listing the regressions it addresses. The
root then waits at the normal human review gate. Routes marked as simulation run
the same path and never publish.

`dark-factory status` shows the loop's state and the open improvement root.
