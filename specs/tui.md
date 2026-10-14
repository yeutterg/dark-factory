# Controller TUI increment

This increment implements the Paper **Dark Factory TUI** as an operator adapter.
It does not move scheduling, budget, approval, execution, or acceptance authority
out of the controller. A local TUI owns the controller normally; a remote TUI
reads the authenticated projection and invokes the same operator commands as the
CLI. It never opens a remote controller's SQLite state.

## Screens

- **DF1 overview** groups roots into needs-you, running, and done-today sections,
  followed by a separately scrollable workers pane. Keyboard help remains in the
  footer. Terminal approved, accepted, and done rows have no progress rail. The
  done-today section is a count and `d show`, with no checkboxes or trailing rows.
- **DF2 issue** exposes activity, plan, critique, changes, checks, review, and full
  log tabs. Human approval, rejection, and acceptance use controller commands.
  Budget exhaustion is a paused state with explicit continuation guidance; the UI
  does not enlarge an approved budget.
- **DF3 settings** shows machines, agents/routes, repositories, budgets, schedule,
  and people from the active config. Configuration remains `factory.toml`-owned;
  the screen may request a validated reload but does not synthesize or silently
  expand configuration.
- **DF4 new work** discovers a bounded set of labeled tracker issues through the
  versioned read-only adapter, shows hierarchy scope, and accepts either highlighted
  complete roots or explicit `owner/repo#number` selections. Intake and planning
  remain separately recorded controller commands behind one confirmed operator action.
- **H1/H2/H3 worker views** show host overview, active assignment limits and
  lifecycle, and controller-observed preflight facts. A worker never gains
  operator authority through these views.

## Interaction contract

- `j`/`k` moves within the focused list; Tab changes focus between independently
  scrollable execution and worker panes.
- `Enter` opens the selected root or worker. `i`, `w`, and `s` open new work,
  workers, and settings. `Esc` returns to the overview. `?` shows keys.
- `P`, `D`, and `R` invoke pause/resume, drain/undrain, and recovery through the
  command boundary. Destructive or human-decision actions require a confirmation
  modal and a named actor.
- The projection contains operational facts and credential references only; no
  credential value is serialized.

## Acceptance

Automated tests cover the projection boundary, authenticated remote reads,
terminal-state progress suppression, done-today summary, independent pane focus,
screen navigation, and command dispatch. A real terminal smoke run must render
at 1440 x 900 and at a compact terminal size without crashing or overlapping the
footer.
