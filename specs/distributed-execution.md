# Project-scoped distributed execution

## Selected outcome and authority

The operator requested project-local factories with strictly separated inference/account tracking and optional local, container, VM and remote execution. This is a new execution/deployment outcome, not completion of Increment A or its required children. Ordinary implementation tools are used in a separate candidate checkout because the installed bootstrap cannot execute this outcome. No live controller state, paid inference, infrastructure provisioning, acceptance, merge or installation is authorized by this implementation.

## Design

One project owns one controller state directory. The controller retains approvals, reservations, deadlines, workflow checkpoints and acceptance. A separately authenticated execution worker receives a bounded, portable assignment, not the controller database or operator token. Its disposable sandbox receives only the selected route credential and task inputs. Workflow, harness, provider/account, sandbox and worker placement remain separate choices.

Use disler/durable-pi's explicit device registry, versioned contracts, durable request identity and reconnect reconciliation as references. Do not copy its shared device state, shell-capable device tokens, host-login discovery, cleartext LAN tokens or mutable mid-run model policy. Its start directories are explicitly not sandboxes. Pin all approved execution inputs and fail closed on incompatible workers.

## Implementation plan

1. Add stable project identity and state ownership; explicit role routes, account kind/scope and sandbox recipes. Reject cross-project reload/state access and implicit host credential discovery in project-scoped deployments.
2. Add a sandbox registry: existing macOS native isolation, hardened OCI command prefixes, and a bounded named-command contract for VM/other external sandbox providers. Pin recipes and limits; unsupported or unqualified live combinations fail closed. Never give an agent a Docker socket.
3. Add a project/worker-scoped execution API alongside the existing local operator API. Credentials resolve externally, workers cannot impersonate each other, and assignments/heartbeats/results are fenced by attempt identity. Preserve controller ownership; remote workers do not open its SQLite database.
4. Transfer only selected attempt inputs and verified archives through a bounded portable package. Reject traversal, escaping links, tampering and controller-supplied arbitrary host paths. Reuse existing harness execution/check/archive behavior in a private worker-side attempt replica with no scheduling or approval authority.
5. Add an outbound worker client with durable dispatch/result journal. Reconnection reconciles an existing attempt; interrupted execution never blindly replays. Keep worker artifacts until controller verification/readback succeeds. Use HTTPS or an explicitly local authenticated tunnel; do not expose plaintext remote tokens.
6. Integrate CLI, preflight, examples, deployment documentation and tests. Prove a full separated-process controller/worker simulation through plan, approval, implementation, canonical checks and independent review; verify account/project isolation, stale results, cancellation, reconnect and archive-before-cleanup.

## Acceptance and qualification

- Local behavior remains compatible; project-scoped deployments opt into explicit immutable bindings.
- Project/worker/account mismatches and unapproved routing fail before dispatch or settlement.
- No operator/controller credentials or unrelated project records occur in assignment packages or sandbox environments.
- Factory and executor use separate paths, stores and processes. Input/result identities survive transport; one verified archive is authoritative.
- Resource limits and permitted paths remain recipe-controlled, not model-controlled. Container/VM backends are not interchangeable security guarantees.
- Fake/local provider fixtures are simulation. A backend is not live-qualified merely because its command can be constructed or its version probed.
- No remote machine, VM, image pull, account purchase or running controller upgrade occurs as part of tests without separate authority.
- All missing live/backend qualification remains explicit in preflight and the handoff; no claim that Increment A, the UI or full operational readiness is complete.

## Verification record

- Revision `99fa872` passed hosted CI: 242 tests passed, 12 environment-dependent tests skipped, and strict TypeScript checking passed. Local distributed protocol/recovery tests passed all 14 cases, including separate-process completion, TLS, cancellation, start fencing and ambiguous-acknowledgement retry without replay.
- The installed cached OCI image passed an isolation test: permitted writes succeeded, protected writes and another project's private file were denied, and no host Docker socket was visible. Subsequent local qualification also encountered a 30-second container startup timeout; it is not evidence of stable live-route qualification.
- A supervisor wheel/image was built from the initial implementation and its installed CLI ran read-only, without networking, as UID 10001. This smoke test is not a hosted deployment, full container workflow qualification or qualification of later source revisions.
- Native live qualification is not green: installed Claude version drifted from the pinned fixture; Pi/Codex startup checks timed out or lacked the expected diagnostic. The unchanged readiness checkout reproduced the Codex diagnostic failure. Do not silently update approved pins or infer provider entitlement from these checks.
- VM lifecycle coverage uses an explicitly non-isolating driver fixture. An actual VM adapter, inference gateway, dedicated billing/subscription accounts and remote host still require separate setup and qualification. No inference spending, provisioning, merge or controller upgrade was performed.
