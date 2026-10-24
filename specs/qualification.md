# Bootstrap qualification — 2026-09-11

## Dependency refresh — 2026-10-16

The workflow stack is pinned to Pi Durable, Pi AI and Chord 1.4.0 with TypeScript 7.0.2 and Node 22 typings 22.20.5. The strict engine check and actual in-memory preflight pass under Node 24.22.0; CI exercises both supported Node majors. The local container image pins Pi 1.4.0, Claude Code 2.1.310 and GitHub CLI 2.103.0. Its Bubblewrap probe ran with the same read-only root, `no-new-privileges`, `SYS_ADMIN`/`NET_ADMIN` broker caps and unconfined outer seccomp recipe used by the live worker: the inner process had `CapEff=0`, a new PID namespace, no view of worker-private paths, read-only protected paths, scoped workspace writes and working outbound TLS. The complete Python suite passed **330 tests with 13 policy/platform skips** on both the host and Python 3.11 container, and the strict TypeScript, Ruff, formatting and mypy checks passed without paid inference. These checks establish local integration and namespace behavior only; they do not qualify live accounts, model quality, subscription billing, processing geography or Codex's still-blocked isolation boundary. Earlier version-specific evidence below remains historical rather than being rewritten.

## Operational preflight candidate — 2026-10-13

Review-fix source `f0e6a685c7bd60190a6eeee465e7ef28df2209ec`: **234 local tests passed in 451.25 seconds**; hosted CI at that exact head passed **223 tests with 11 platform-specific skips** and strict engine type-checking. Clean wheel SHA-256 `cf4d13b4f5618ba0587a56bb1cc64ab7eb65749b2a06009d2801ce00206bd98c` was installed separately, matched the current Python source, and invoked from an unrelated directory. Its exact engine preflight and five durable simulation jobs reached human review at candidate `fb41eaf9536a6cffb0738f763ba871dac32b3b9d`; all five archives/checkpoints verified, no execution workspaces or outstanding reservations remained, declared assumptions and the manual diff gate persisted. This is not a live model run, acceptance, merge or controller upgrade. Original private deployment settings/state remain untouched.

Pricing follow-up: installed Pi's custom-model contract defaults missing rates to zero. Live custom routes now require a full pinned pricing table and evidence reference before configuration/admission, inject the exact prices into Pi and independently estimate cost from validated counters. Missing/zero/invalid live usage and interrupted partial usage fail closed with conservative reservation charging. Published prices and controller ceilings do not establish a provider invoice ceiling; account-side controls remain a live qualification gate. Earlier wheel/full-suite evidence below applies to the preflight commit, not automatically to this later pricing change.

Harness-declared immutable runtime inputs protect Pi's model/settings files from agent writes without blocking session output. Native sandbox tests deny changes to those inputs and the sandbox policy, preserve other runtime writes and reject escaping metadata paths. The installed Pi loopback outcome includes pinned nonzero fixture prices and fresh planner/critic/coder/reviewer contexts under these guards; no live account inference occurs.

The installed Pi adversarial variant also actually requests a model-file rewrite through its native `write` tool: the tool reports permission denial, the approved route remains intact, valid implementation continues, canonical checks pass and independent fresh review completes. This exercises the worker-to-environment-to-harness link rather than just separate component assertions.

Devin preflight follow-up: the exact engine entry point now probes every import and real registry/model/SQLite startup in memory before claims. Unstarted post-lease failures are fenced with zero spending and released reservations, retaining the assignment and original root limits; callback-started, workspace/PID and other-owner cases cannot use this recovery. Late retired results fail. Workflow-unavailable reasons persist for unfinished roots and clear on successful revalidation; idle/disabled dispatch does not launch Node. Dedicated regressions include a package whose root imports successfully while its engine-only SQLite subpath is absent.

The candidate adds state-free configuration/installation diagnostics and checks Pi Durable dependencies, Node compatibility and engine loading before taking an attempt lease. Missing installation dependencies cannot consume an agent reservation. A passing preflight is not real-work readiness. Installed Codex 0.162.1 proves workspace reads and dummy credential denial for its command sandbox, but the factory's nested macOS sandbox fails at startup. Live Codex remains code-blocked; no real authentication document was read or used. Earlier exploratory stream probes used only dummy tokens and did not complete model execution; they are not qualification evidence. See [readiness gates](operational-readiness.md).

The candidate wheel (`ccfac8ca1b194505c072b4fbfb5ad932e960c36e638afed7883dcaadf7804ea4`) was installed without source editing into a clean Python 3.11 environment. Packaged engine source and lock loaded after pinned dependencies were installed. Running from an unrelated directory completed all five simulation jobs through actual Pi Durable checkpoints to `awaiting_review`: exact candidate `12fdf2b260e55434ca1fdbde5ad65cfb60ecb153`, declared assumptions, zero outstanding reservations and a preserved manual diff gate. The local service returned healthy, served the exact result packet, refused a worker credential on the operator endpoint (401), and allowed state-free preflight while owning state. The test service was drained/stopped. Backup manifest and all five restored archives verified; restored SQLite integrity/foreign keys, five attempts/checkpoints and empty execution workspaces were independently read back. Restored controller state is mutable and includes relocation metadata; the backup's original root manifest is not a seal for the modified restored database. This remains simulation, not a live provider result, human acceptance or controller cutover.

## Durable review follow-up — 2026-10-13

Regression tests cover controller claims without engine checkpoints, unchanged owner/lease/policy/workspace/PID guards, valid human review after execution limits, refreshed rejection packets, and repair refusal for integrity/execution/archive/approval failures and incomplete or incorrectly bound checks. Existing native and actual Pi Durable bounded repair loops still reach the human gate.

The Codex adapter uses a separate pipe-lifetime credential guardian, plus harness-declared stale-file scrubbing on controller startup and lease recovery. Dummy-token tests kill a worker process with SIGKILL, exercise the actual adapter lifecycle, bound credential lifetime while the parent remains alive, preserve live guardian ownership and retained transcripts, and reject symlinks/overwrites. Local file erasure is not provider token revocation or installed/live Codex isolation qualification. No external inference or live controller state is used.

Follow-up review coverage preserves explicitly repeated checks by matching the exact approved sequence. Live Codex configuration and direct invocation are code-blocked until installed credential/tool isolation is proven; the outer runtime read grant is needed by native authentication and is not proof that agent tools cannot read it. Only explicitly classified simulation fixtures are admitted. Pi and Claude admission is unchanged.

## Durable candidate — 2026-10-13

The optional Pi Durable 1.2.0 engine is pinned and type-checked. Tests run its actual SQLite task engine with fixture CLI work, including crash-before-dispatch recovery, completion reconciliation without duplicate execution, and refusal to replay interrupted active CLI work. Native and durable workflows exercise failed checks and blocking reviews through one preapproved repair, fresh verification and the human gate. Assumptions and repair history appear in the executive summary. No external inference spending or running-controller upgrade is part of these checks.

Full local suite: **148 passed** under Python 3.11 in 512.74 seconds, including installed Pi/Claude loopback fixtures on macOS. A final strict workflow-engine type guard has additional targeted configuration regressions; hosted CI checks the final PR revision.

The candidate wheel was built with installed Python 3.11 build tools after package-index timeouts. Its packaged engine source/lock was inspected, and a clean virtual environment ran the installed wheel from an unrelated directory through five jobs to `awaiting_review`, with zero reserved allowance and declared assumptions in the review packet. The optional repair branch was correctly marked unused. This is simulation evidence, not live model acceptance.

Codex CLI invocation, subscription authentication, stream parsing, conservative accounting, timeout and credential cleanup have fixture coverage. Installed Codex permission/isolation and live subscription qualification remain pending. The historical qualification below applies to earlier revisions, not a claim that this candidate has been human-accepted. Required-child graphs, cross-repository integration and hosted-CI continuation inside the controller remain unsupported.

Implementation and automated qualification are complete for the single-repository bootstrap. **Live frontier-model qualification and human acceptance of this revision remain pending.** The operator was asked for an exact provider/model/account/region and spending ceiling; these have not been inferred from installed credentials.

## Verified

- **113 tests passed** under Python 3.11 on this Mac after the root-scope and Claude subscription additions. The suite exercises real Git source/candidate/check flow, structured roles, approval invalidation, command replacement, prompt overrides, manual gates, atomic claims, duplicate/late results, process deadlines, preserved work after archival failure, and separate API decision credentials.
- **Installed Pi 0.82.1:** four fresh sessions completed planning, critique, coding and independent review against a deterministic loopback HTTP provider. Pi actually issued its file-write tool; a canonical command checked the resulting commit. This tests harness/protocol/tool/isolation integration, not model judgment or external processing geography.
- **Native macOS isolation:** approved writes succeeded; protected and read-only-role writes failed; host-private data was inaccessible. Check recipes run without network or inference/controller credentials.
- **Completion contract:** every approved criterion binds checks/manual gates and receives an independent assessment. Regression cases reject omitted criteria, unverified criteria, invented check references and missing persisted evidence even when jobs report success. An operator-required live outcome cannot use simulation evidence; the real-Pi fixture is explicitly simulation.
- **Recovery:** expired claims are fenced without automatic replacement; reserved unknown usage is conservatively charged. Failed archival retains the workspace, role output and session runtime. Another process cannot own the same state directory. A backup restored SQLite/artifacts into a separate directory and resumed approved work with the original source snapshot made unavailable.
- **External commands:** the GitHub intake adapter read `github:yeutterg/dark-factory:issue:1` through the real GitHub API. PR/check/read-back and ambiguous-write behavior use deterministic API fixtures. No live PR, merge, deployment or status mutation was performed.
- **Distribution:** an installable wheel was built, installed into a separate clean virtual environment, and ran `demo` from an unrelated directory through all five jobs to `awaiting_review`, with no outstanding reservations. A second installed-wheel run recorded the simulated manual gate, accepted its exact candidate, and completed CLI backup/restore. Packaged prompts and selected skills loaded successfully.
- **Development publishing:** four hook tests verify repository/push-URL scope, local-profile exclusion, real commits/pushes to an isolated bare Git remote and retry after a rejected push.
- **Increment tracking:** existing GitHub issues #2, #3 and #4 cover A–C; no duplicate issues were created.

## Reproduce

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest
```

`tests/test_pi_qualification.py` runs only where macOS and Pi are available. It starts a local deterministic provider fixture; no API key, account access or model spending is used. Live route selection is explicit in `factory.toml` and needs operator-verified processing geography.

## Remaining acceptance

1. Approve the exact live inference route and budget. Use a new source checkout and state directory for one small bounded outcome.
2. Run actual planning and fresh-context critique. Inspect and approve the complete proposal/digest, then execute implementation, canonical checks and independent review.
3. Inspect the result packet and exact candidate, record its manual criteria, and accept the outcome.
4. Review this bootstrap revision before human merge or installation. Keep the running installation pinned; drain and back up before any future upgrade.

The original review identified a simulation-only scaffold. Those implementation findings are addressed by the code and tests above; the historical [implementation review](implementation-review.md) remains as a record of why the changes were needed. No fixture output should be represented as a frontier-model evaluation.

Verified wheel: `dist/dark_factory-0.1.0-py3-none-any.whl`, SHA-256 `9f4d7512741dd5f7370ce3d3db2d5a34e62b10f1fcac272e72c6c03fa5500bc6`. This is a review artifact, not an installed-controller upgrade.

## Claude Code subscription addition

Claude Code 2.1.268 is integrated for read-only planner/critic/reviewer roles. The local native-binary test uses a dummy subscription OAuth token and a loopback provider; it verifies the stream contract, allowed source reading and denial of a symlink to private data. Separate tests cover API-key rejection, identity/model mismatches, failed/missing/duplicate results, budget estimates, cancellation, timeouts and version pinning. Native OAuth sends a bearer token and no API-key header.

The ordinary Max keychain login is visible outside the sandbox but unavailable inside it. The adapter therefore uses the supported inference-only token from `claude setup-token`, supplied through an external environment reference. It does not expose the operator home/keychain or extract native credentials. No real token was generated, stored or used during these tests, and no live inference was performed.

A live subscription pass still needs that credential, the exact model and processing geography, approved limits and account usage-policy verification. No comparison/consolidation workflow or multi-repository controller execution is implied. See [subscription setup](../docs/claude-subscription.md). The wheel hash above belongs to the earlier bootstrap artifact and does not contain this addition; no running installation was upgraded.
