# Bootstrap qualification — 2026-09-11

Implementation and automated qualification are complete for the single-repository bootstrap. **Live frontier-model qualification and human acceptance of this revision remain pending.** The operator was asked for an exact provider/model/account/region and spending ceiling; these have not been inferred from installed credentials.

## Verified

- **76 tests passed** under Python 3.11 on this Mac. The suite exercises real Git source/candidate/check flow, structured roles, approval invalidation, command replacement, prompt overrides, manual gates, atomic claims, duplicate/late results, process deadlines, preserved work after archival failure, and separate API decision credentials.
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
