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

## Boundaries

- The image is built from digest-pinned Python and Node base images. It builds the wheel from the checked-out source and installs the exact Pi Durable lock during the image build.
- `.env` is generated with mode `0600`, a unique Compose project/image name and independent random tokens, and is gitignored. This prevents volumes and containers from colliding when several project forks run on one machine. The public `.env.example` contains no usable credentials.
- The server binds only inside its container. The client and fixture worker share the server container's loopback network, so no unauthenticated or plaintext controller port is published on the host.
- Server and worker use separate named state volumes. The worker never opens the supervisor database. Compose injects only the operator token into the client and only the worker token into the worker; the client receives neither worker credentials nor state volumes.
- The default worker route is deterministic simulation. The image intentionally contains no Pi, Claude Code or Codex CLI and no inference credential.

For live execution, keep the same supervisor image and replace the fixture worker with a separately qualified worker host/image containing only the exact project-approved agent CLIs, sandbox driver and credentials. A Linux Compose worker is not equivalent to the qualified native macOS sandbox. Remote workers use verified TLS or an explicitly configured authenticated loopback tunnel as described in [distributed execution](distributed-execution.md).

To use private project configuration, copy the three files under `examples/compose/` outside the repository, edit their project bindings, and override the read-only Compose mounts. Never add private profiles, source paths, account references, certificates, credentials or state to this public repository.
