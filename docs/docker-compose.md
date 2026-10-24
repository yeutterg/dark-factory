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

The repository also includes a live-only local overlay. It installs pinned Codex CLI 0.164.0, Pi 1.4.0 and Claude Code 2.1.310 binaries. The generated profile routes all four roles through Codex and GPT-6.1 Sol using one explicit ChatGPT subscription account. This is operationally convenient, but it is not independent-model critique or review.

```bash
./scripts/compose-init
./scripts/compose-live-init
./scripts/compose-live-auth
docker compose down
docker compose up --build -d server worker
docker compose run --rm --no-deps server --config /config/factory.toml preflight
docker compose run --rm client tui --actor greg
```

`compose-live-init` creates gitignored `factory.local.toml` and `execution-worker.local.toml`, pins the current source revision and sandbox-driver digest, and selects `compose.live.yaml` through the private `.env`. The generated profiles contain credential references but no credential values, so they are mode `0644` for the image's non-root services; the credential-bearing `.env` remains mode `0600`. The generated supervisor profile has four explicit live routes and no fake route. `compose-live-auth` validates and snapshots the current `${CODEX_HOME:-$HOME/.codex}/auth.json` ChatGPT login, reuses the current `gh` login when available, and stores both only in `.env`; it never bakes credentials into an image or profile. Run `codex login` on the host first if no valid ChatGPT login exists.

The live worker broker drops all capabilities except `SYS_ADMIN`, `NET_ADMIN`, `SETUID`, `SETGID` and `SETFCAP`, which Codex's pinned Linux Bubblewrap launcher needs to create its user and mount namespaces. Ordinary commands still use the factory's outer Bubblewrap driver. Only the exact root-owned immutable `/usr/local/bin/codex exec` binary may use Codex's self-sandbox; arbitrary commands and Codex subcommands cannot bypass the outer driver. Codex's tool sandbox exposes the approved workspace and minimal system files, denies its exact `auth.json`, keeps protected paths read-only, and hides `/proc`, worker/controller state and the Docker socket. This is native Linux namespace isolation rather than a VM.

The live profile asserts that this operator-controlled execution host is in the US. The image installs a root-owned `/etc/codex/requirements.toml` requiring US data residency, which ordinary config and prompts cannot relax. Confirm the host and account policy before approving work. A passing preflight verifies bindings and credential presence, not token validity, subscription entitlement, model quality or a completed factory outcome.

## Boundaries

- The image is built from digest-pinned Python and Node base images. It builds the wheel from the checked-out source, installs the exact Pi Durable lock, checksum-pins GitHub CLI, and installs exact Codex, Pi and Claude Code versions during the image build.
- `.env` is generated with mode `0600`, a unique Compose project/image name and independent random tokens, and is gitignored. This prevents volumes and containers from colliding when several project forks run on one machine. The public `.env.example` contains no usable credentials.
- The server binds only inside its container. The client and fixture worker share the server container's loopback network, so no unauthenticated or plaintext controller port is published on the host.
- Server and worker use separate named state volumes. The worker never opens the supervisor database. Compose injects only the operator token into the client and only the worker token into the worker; the client receives neither worker credentials nor state volumes.
- The default worker route is deterministic simulation. The image contains pinned Codex, Pi and Claude Code CLIs for the optional live overlay, but never contains an inference credential. The live overlay has only explicit live Codex routes.

The exact local image and Codex route passed the bounded qualification recorded in [qualification](../specs/qualification.md). Another image, host, CLI version, account or route needs separate qualification. Remote workers use verified TLS or an explicitly configured authenticated loopback tunnel as described in [distributed execution](distributed-execution.md).

To use private project configuration, copy the three files under `examples/compose/` outside the repository, edit their project bindings, and override the read-only Compose mounts. Never add private profiles, source paths, account references, certificates, credentials or state to this public repository.
