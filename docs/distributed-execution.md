# Project-scoped supervisor and execution workers

The supervisor owns one project's approvals, limits, SQLite state and canonical evidence. Execution workers are separate processes, optionally on separate machines. Their disposable task sandboxes never receive the operator credential or open the supervisor database. A supervisor container needs persistent state, not a Docker socket. Workers need only their own project/worker token and explicitly bound inference accounts.

This implements the network boundary inspired by [disler/durable-pi](https://github.com/disler/durable-pi), not its device-wide shell authority. Pi Durable remains an optional supervisor-side dispatch runtime. Pi, Claude Code and Codex remain independent task harnesses; existing role/admission restrictions still apply. This is not a claim that any particular hosted account, VM service or live container route has been qualified.

## What runs where

- **Operator machine:** the TUI or CLI is only a client. It can exit without stopping the supervisor or workers.
- **Supervisor host or container:** runs `serve`, owns the project configuration, approval state, SQLite database and canonical evidence, and runs the optional Pi Durable dispatch engine. Its image needs Python and, for Pi Durable, the pinned Node packages. With external workers it needs no agent CLI, inference credential or Docker socket.
- **Execution worker machine:** runs `execution-worker` at the same Dark Factory revision. It installs only the exact Pi, Claude Code or Codex versions selected by its project routes and holds only that worker's project-scoped inference credentials.
- **Attempt sandbox:** the worker launches each assignment in a disposable native, OCI or VM environment. The workspace and build products are archived and verified before teardown; the supervisor database and operator credential never enter it.

`serve --worker` is the single-machine convenience mode: supervisor and worker responsibilities are co-located on one host, but attempts still use fresh isolated workspaces. It does not imply Docker. In a separated deployment, the supervisor may use the repository Dockerfile while workers remain native Macs, VMs, or container-capable hosts. Installing an agent CLI on both tiers is unnecessary unless that machine also acts as an execution worker.

For a local containerized installation, the [Docker Compose stack](docker-compose.md) supplies a server, a disposable operator client and a separate simulation worker without publishing the controller port. It is an installation and handoff fixture, not live Linux-agent qualification.

## Independent selections

Pin `project.id`, all four `role_routes`, each route's `project_id`, `account_kind`, `billing_account`, `auth_env`, model, provider, endpoint and CLI version. Routes separately name an `environment`. Registered workers have explicit environment membership and distinct external token references. A worker's CLI profile contains its own account bindings, not a copy of `factory.toml`.

Account bindings are operator policy, not provider account introspection. Dedicated provider accounts/projects must actually be configured and verified externally for separate invoices/quotas. Different labels or keys on one shared subscription do not establish that separation. The factory prevents implicit account selection and host-login discovery in project-scoped routes; it does not invent provider entitlements. Inference credential names must be project-prefixed, and cannot name a worker/operator credential variable.

The route and environment recipes are part of approved policy. Reload cannot switch project ownership; changed relevant policy invalidates old approval. A state directory belongs to one project/repository set. Existing populated legacy state is not automatically adopted into a new project identity.

## Placement and backends

- Local macOS worker: existing native macOS sandbox, fresh workspace/runtime/home per attempt.
- Remote macOS worker: the same worker/client through HTTPS or an authenticated loopback tunnel. Closing the review client does not stop a hosted supervisor/worker.
- OCI sandbox: digest-pinned installed image, read-only root/source, explicit writable/protected mounts, no host socket in the sandbox, dropped capabilities, non-root worker/user and CPU/memory/PID limits. Image pulls are never implicit. Pin the trusted worker's local Unix socket with `daemon_host`; the isolated agent never receives it. Canonical checks use disabled networking and may select an image-pinned executable via `command_binaries`; actual execution argv is retained separately from the original binding. Inference networking requires a separately prepared project-labelled internal bridge with isolated IPv4/IPv6 gateway modes. An explicitly approved inference gateway must live on that network; broad/default/host bridge access is rejected. The gateway's upstream/account/geography policy and native client behavior need real qualification before live admission.
- VM/other sandbox: the named-command environment contract allows a separately qualified infrastructure adapter. Its argv and driver files must be pinned. Alternatively, install an execution worker inside a separately provisioned VM and use its qualified local backend. No VM vendor, VM creation or cloud budget is assumed.

Worker placement and sandbox kind are different axes: a remote worker can launch containers, a worker inside a VM can launch containers, and a native Mac worker can use macOS isolation. A container backend is not a VM-level security guarantee. Linux workers cannot execute a macOS sandbox recipe. Worker/controller execution code must match, and host command binaries must match approved bindings unless a pinned OCI command override explicitly replaces the host binary.

## Running a separated fixture

Start from [the supervisor profile](../examples/factory-distributed.toml) and [the worker profile](../examples/execution-worker.toml). Replace project/worker identities and credential references. Keep real values in external secret storage. The supervisor profile uses fake inference only and is not a live account approval.

```bash
dark-factory --config project-factory.toml preflight
dark-factory --config project-factory.toml serve
# Separate process/machine, installed at the same engine revision:
dark-factory execution-worker --profile project-worker.toml
```

Use the existing operator intake/plan/proposal/approve/review/manual-gate commands through `--api-url`; retain the exact human approval gate. Configuring external workers disables `serve --worker` and `run-worker` for that profile. The execution worker does not need `--config` and must not be pointed at supervisor state.

For a remotely reachable supervisor, use TLS:

```bash
dark-factory --config project-factory.toml serve --host 0.0.0.0 \
  --tls-cert /credentials/server.pem --tls-key /credentials/server-key.pem
```

Supply a trusted `ca_file` in the worker profile for a private CA; hostname and certificate verification remain enabled. Plain HTTP is accepted only at a loopback URL, for local operation or an explicitly configured authenticated tunnel. Redirects are rejected so worker credentials cannot move to another endpoint. The original legacy worker endpoints are disabled for project-scoped deployments; worker credentials cannot approve or accept work.

## Handoff, failure and cleanup

The supervisor publishes a versioned portable assignment with durable request identity, exact claim digest and a bounded package. Only the selected root's inputs, prior evidence and canonical check resources transfer. Worker preflight checks project/code/environment/account compatibility before requesting a lease. The worker keeps a private attempt replica for execution, never a replica with scheduling/approval authority.

The worker journals an execution UUID and obtains an idempotent supervisor-side start authorization before spawning an executor. A restarted broker reconciles that exact process identity; an interrupted start is not authorization to replay. A local heartbeat file stops the executor if its broker dies or loses contact. Configure each worker's `lease_seconds` (30–300, default 30) before approval for expected transport/preparation latency; renewal never extends the original task/root deadline. Supervisor cancellation/lease fencing prevents stale settlement. A delivery that never obtained start authority is retired at zero cost and requeued under the original limits; expired started work is conservatively fenced, not replayed.

The worker verifies its archive before removing its execution worktree. The supervisor independently verifies the transferred archive and assignment inputs before settling. A result-digest acknowledgement plus confirmed environment release permits deletion of the worker's entire task replica, input checkout, copied historical archives and build files. Only a small receipt remains at the worker; canonical code/evidence remains at the supervisor. Ambiguous result delivery retries the same journalled result, never the agent. Failed upload or missing archive does not authorize deleting unique work.

OCI containers have deterministic owned names/labels and explicit teardown with an absence readback. A failed inspect or unavailable daemon is not proof of deletion; resources are retained when cleanup cannot be confirmed. Removing a Docker client alone is not sufficient cleanup. Supervisor backups include dispatch records and verified evidence. Artifact retention at the supervisor is a separate operator policy, not automatic deletion on agent exit.

Declare `sources.<repo>.ephemeral_paths` for disposable build/cache directories. They cannot overlap approved source or protected inputs, and tracked files are never discarded. Verified archives record the excluded paths; required deliverables/logs must live outside those directories. After committed settlement, the supervisor drops bulky reproducible dispatch packages while retaining their digest and canonical evidence. Worker cleanup also requires its matching project/worker/attempt ownership marker.

Named sandbox drivers receive `--factory-policy <JSON> -- <argv...>` for execution, and `--factory-policy <JSON> --operation stop|release` for lifecycle. Stop must terminate/synchronize task output; release destroys owned infrastructure only after verified local archival. Release must be idempotent: if it fails, the worker retains its runtime and durable journal, reconstructs the exact frozen policy descriptor, and retries release without invoking the harness or agent again. The supervisor records cleanup only after the owning worker confirms that retry against the settled result digest. Driver files are digest-checked before execution and again before a reconstructed release. The included driver fixture deliberately provides no security isolation and cannot qualify a live VM backend.

Version 1 packages have a 64 MiB compressed/expanded limit. Oversized roots fail explicitly rather than streaming unbounded data or silently omitting files. This is a capability limit to check before real intake. Provider invoices, live model quality, native subscription entitlement, remote host setup and VM/OCI live qualification remain separate acceptance evidence.
