# Docker Compose installation

Docker is an optional installation path for the supervisor and operator client. The default Compose stack also starts a separate execution-worker fixture so the network handoff can be exercised end to end without model calls or private credentials.

## Start the local stack

Requires Docker Engine or Docker Desktop with Compose v2:

```bash
./scripts/compose-init
docker compose up --build -d server worker
./scripts/compose-smoke
docker compose run --rm client tui --actor operator
```

The TUI is a disposable client. Exit it with `q`, `Ctrl+Q` or `Ctrl+C`; the server and worker continue running. Follow or stop the services with:

```bash
docker compose logs -f server worker
docker compose down
```

The named `supervisor-state` and `worker-state` volumes survive `docker compose down`. `docker compose down -v` deletes them and must not be used until required evidence has been backed up and verified.

## Live-only container worker

The repository also includes a live-only local overlay. It installs pinned Pi 1.4.0 and Claude Code 2.1.310 binaries, but the generated profile initially routes all four roles through Pi and Together's `deepseek-ai/DeepSeek-V4.1-Flash`. Claude Code is not an approved route until its account, processing region and subscription policy are filled in and reviewed.

```bash
./scripts/compose-init
./scripts/compose-live-init
./scripts/compose-live-auth
docker compose down
docker compose up --build -d server worker
docker compose run --rm --no-deps server --config /config/factory.toml preflight
docker compose run --rm client tui --actor greg
```

`compose-live-init` creates gitignored `factory.local.toml` and `execution-worker.local.toml`, pins the current source revision and sandbox-driver digest, and selects `compose.live.yaml` through the private `.env`. The generated profiles contain credential references but no credential values, so they are mode `0644` for the image's non-root services; the credential-bearing `.env` remains mode `0600`. The generated supervisor profile has four explicit live routes and no fake route. `compose-live-auth` reads the Together key without echoing it, reuses the current `gh` login when available, and stores both only in `.env`; it never bakes credentials into an image or profile.

The live worker's outer broker runs as root with only `SYS_ADMIN` and `NET_ADMIN`, needed to create mount, PID and optional network namespaces. Before a harness begins, Bubblewrap drops every capability, replaces `/proc`, exposes only `/usr`, `/etc`, the owned runtime and the approved workspace, remounts protected paths read-only, and hides worker/controller state. The worker has no Docker socket. This is a narrower boundary than a privileged or Docker-outside-of-Docker worker, but it is still native Linux namespace isolation rather than a VM.

The live profile asserts that this operator-controlled execution host is in the US and uses Together's published North America route evidence. Confirm both assertions and the pinned prices against the actual account before approving paid work. A passing preflight verifies bindings and credential presence, not token validity, provider billing, model quality or a completed live qualification run.

## Boundaries

- The image is built from digest-pinned Python and Node base images. It builds the wheel from the checked-out source, installs the exact Pi Durable lock, checksum-pins GitHub CLI, and installs exact Pi and Claude Code versions during the image build.
- `.env` is generated with mode `0600`, a unique Compose project/image name and independent random tokens, and is gitignored. This prevents volumes and containers from colliding when several project forks run on one machine. The public `.env.example` contains no usable credentials.
- The server binds only inside its container. The client and fixture worker share the server container's loopback network, so no unauthenticated or plaintext controller port is published on the host.
- Server and worker use separate named state volumes. The worker never opens the supervisor database. Compose injects only the operator token into the client and only the worker token into the worker; the client receives neither worker credentials nor state volumes.
- The default worker route is deterministic simulation. The image contains pinned Pi and Claude Code CLIs for the optional live overlay, but never contains an inference credential. Codex is not installed because live Codex routes remain blocked pending qualification.

For live execution, keep the same supervisor image and replace the fixture worker with a separately qualified worker host/image containing only the exact project-approved agent CLIs, sandbox driver and credentials. A Linux Compose worker is not equivalent to the qualified native macOS sandbox. Remote workers use verified TLS or an explicitly configured authenticated loopback tunnel as described in [distributed execution](distributed-execution.md).

To use private project configuration, copy the three files under `examples/compose/` outside the repository, edit their project bindings, and override the read-only Compose mounts. Never add private profiles, source paths, account references, certificates, credentials or state to this public repository.
