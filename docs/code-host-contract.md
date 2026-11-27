# Code-host contract

Core never talks to GitHub (or GitLab) directly. It calls one JSON command, `commands.code_host`,
with one of the operations below, and trusts only the evidence that comes back. A second host is a
second implementation of exactly these operations, nothing in core changes, and
`tests/test_code_host_contract.py` is the suite any implementation must pass. The reference
implementation is `adapters/git/fake_host.py`; the GitHub adapter is `adapters/git/github.py`.

Only the two GitHub adapters (`adapters/git/github.py`, `adapters/projects/github.py`) may call `gh` or
GitHub's API; a test enforces it.

| Operation | Input | Evidence | Rules |
|---|---|---|---|
| `inspect_pr` | `repo`, `number` | `head_sha`, `url`, `checks`, `statuses`, `reviews`, `review_comments` | Read only. |
| `list_open_prs` | `repo` | `prs[]`: `number`, `head_sha`, `head_ref`, `title`, `url`, `body` | Fails rather than return a partial page. |
| `push_branch` | `repo`, `branch`, `head_sha`, `source_path` | `observed`, `branch`, `head_sha` | Branch must be new and under `dark-factory/`. Never overwrites; same commit already there counts as done; another commit is refused. Read back before reporting. |
| `reconcile_push_branch` | as above | `observed`, or `safe_to_retry` | Read only. |
| `create_pr` | `repo`, `head`, `base`, `head_sha`, `title`, `body` | `observed`, `url`, `number`, `head_sha`, `draft` | Always a draft. Refuses if the head moved from `head_sha`. Idempotent by request marker. |
| `reconcile_create_pr` | as above | `observed` (+ details) or `safe_to_retry` | Read only. |
| `set_status` | `repo`, `sha`, `state`, `context`, `description` | `observed`, `sha`, `state` | Context must start with `dark-factory/`. |
| `dispatch_workflow` | `repo`, `workflow`, `ref`, `inputs` | `observed`, `request_id` | Inputs are strings. |
| `find_run` | `repo`, `request_id` | `run_id` or null | Null unless exactly one run matches. |
| `get_run` | `repo`, `run_id` | `status`, `conclusion`, `url`, `head_sha` | Read only. |
| `list_run_artifacts` | `repo`, `run_id` | `artifacts[]`: `name`, `size`, `digest`, `id` | Only the run's own artifacts, with the digest the host computed. |

Every request carries `contract_version`, `request_id`, `operation` and `input`; every result carries
`ok`, `operation`, `evidence` and echoes the request id. Operations the adapter does not know fail with
a `ContractError`, never a partial answer.
