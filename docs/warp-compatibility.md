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

# Only needed when several agents share a type; one candidate maps automatically.
[definition.roles]
coder = "implement-fast"
```

The repositories in `factory.yaml` must exactly match `repositories` in the
operator profile. A mapped agent's model must match the route already approved
locally, unless it names no model of its own: `auto`, `auto-*`, `custom:*` and a
router name all mean "use the approved Dark Factory role route". They do not
enable routing. A `harness` request is compared with the route's harness and any
difference is reported, never acted on.

Validate a tree without opening controller state:

```bash
dark-factory definition-validate ./factory
```

The reader is checked against Warp's published JSON Schemas and the sixteen trees
in `warpdotdev/warp-factory-examples` (vendored under `tests/fixtures/warp/`, with
provenance). Every tree the schemas accept loads here in strict mode.

## What is read, and what runs

The JSON report lists every resource with one of three statuses: *mapped or
executed* (used by the fixed workflow), *preserved* (read, pinned, not scheduled)
or *recognized-not-executed*.

- `factory.yaml`: identity, repositories (required), agent defaults and requested
  models. `schemaVersion` must be `v1alpha1`. `v1alpha2` and a `workers/`
  directory produce one clear "not supported" error.
- `agents/*/agent.md`: `agentType` must be uppercase; null or omitted means
  `CUSTOM`. `SPEC`, `IMPLEMENT` and `REVIEW` map to the planner, coder and
  reviewer roles. `VERIFY`, `TRIAGE`, `CUSTOM` and the foreman are read and
  reported but do not change the fixed `plan → critique → approve → implement →
  check → review` graph. Exactly one agent must be `FOREMAN` or `MAIN`.
- **Prompts are layered.** A mapped agent's body (frontmatter removed) replaces the
  role's guidance. The factory's output contract is always appended and cannot be
  removed by any file. An empty body keeps the packaged guidance. The body is
  read once when the definition loads, so what is digested is what is delivered.
- `skills/*/SKILL.md` and `agents/<name>/skills/*/SKILL.md`: shared and
  role-scoped skills, snapshotted at load.
- `self-improvement/agent.md` and `selfImprovement.failedRunThreshold`: guidance
  and threshold for Dark Factory's own improvement loop.
- `scorers/*/scorer.md` (requires `labels` and `passingScore`), `runners/*.yaml`
  (references must resolve), `automations`, `routers`, `benchmarks` and
  `webhooks`: validated and pinned, not executed. The operator-approved
  execution profile remains authoritative. Secrets, MCP servers, hooks and
  network policies in YAML never grant access.

Strict mode rejects unknown fields, agent types and directories. A tree may hold
at most 256 files, 256 KiB per file and 2 MiB in total.

The definition digest covers every file in the tree, including agents nothing
maps. The digest, the mapped prompts and the skill contents are part of the policy
snapshot, so changing one invalidates affected pending approval or dispatch
evidence under the same rules as other configuration inputs.

## Self-improvement

Dark Factory's own improvement loop is on by default. See
[Self-improvement](self-improvement.md). The read-only report is unchanged:

```bash
dark-factory --config factory.toml improve
```

It groups failed checks, blocking reviews, failed stages, interruptions, timeouts
and human rejections across distinct root outcomes and prints the smallest
prompt, skill, runner or route target, plus a tracker-ready description. It
performs no inference and creates nothing. The threshold is
`selfImprovement.failedRunThreshold` from the definition when set, otherwise
`[improvement] threshold`, otherwise three distinct roots; `--minimum-occurrences`
overrides it for one report (minimum two).
