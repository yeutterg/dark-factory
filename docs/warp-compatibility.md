# Warp-shaped factory definitions

Dark Factory can read the portable core of a Warp Factories `v1alpha1`
definition without adopting Warp's scheduler or allowing definition files to
grant authority.

## Configure the definition

Keep runtime authority in the private `factory.toml` profile and point it at a
version-controlled definition tree:

```toml
[definition]
format = "warp-v1alpha1"
path = "./factory"
strict = true
```

The repositories in `factory.yaml` must exactly match `repositories` in the
operator profile. A mapped agent's explicit model must match the route already
approved locally. Warp's `model: auto` means "use the approved Dark Factory role
route"; it does not enable unbounded routing.

Validate a tree without opening controller state:

```bash
dark-factory definition-validate ./factory
```

The JSON result distinguishes mapped roles from recognized resources that are
not executed. Unknown fields fail in strict mode. Known but unsupported
resources remain visible in the compatibility report rather than being treated
as active.

## Supported execution surface

- `factory.yaml`: identity, repositories, agent defaults and requested models.
- `agents/*/agent.md`: `SPEC`, `IMPLEMENT`, and `REVIEW` map to the existing
  planner, coder, and reviewer roles.
- `skills/*/SKILL.md`: shared skills.
- `agents/<name>/skills/*/SKILL.md`: skills scoped to the mapped role.
- `scorers/*/scorer.md`: parsed and pinned, but not executed as LLM judges in
  this increment.
- `runners/*.yaml`: validated and reported; the operator-approved local
  execution profile remains authoritative.

Foreman, triage, and custom agents are preserved in the report but do not alter
the fixed `plan → critique → approve → implement → check → review` graph.
Automations, routers, benchmarks and webhooks are pinned and reported but not
executed. Secrets, MCP servers, hooks and network policies in YAML never grant
access.

The definition digest, resource hashes, mapped prompts, and skill contents are
part of the policy snapshot. Changing one invalidates affected pending approval
or dispatch evidence under the same rules as other configuration inputs.

## Proposal-only improvement

Recurring outcome analysis is deterministic and appears in the authenticated
status projection by default. The dedicated command prints the same detailed
proposal view:

```bash
dark-factory --config factory.toml improve
```

The command groups failed checks, blocking reviews, failed stages,
interruptions, timeouts, and human rejections across distinct root outcomes.
Three distinct roots are required by default; use `--minimum-occurrences` to
raise or lower the threshold, with a minimum of two.

The output suggests the smallest prompt, skill, runner, or route target and
contains a tracker-ready work-item description. It performs no inference and
does not create a work item, branch, pull request, application change, external
action, or configuration reload. A human chooses whether to select the
recommendation as ordinary work.
