# Digest-pinned, multi-architecture build inputs. Override only with reviewed digests.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
ARG NODE_IMAGE=node:24.22.0-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5
ARG CLAUDE_CODE_VERSION=2.1.310
ARG CODEX_VERSION=0.164.0
ARG PI_CODING_AGENT_VERSION=1.4.0
ARG GH_VERSION=2.103.0

FROM ${NODE_IMAGE} AS pi-engine
WORKDIR /engine
COPY src/dark_factory/adapters/workflow/pi_engine/package.json ./
COPY src/dark_factory/adapters/workflow/pi_engine/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

FROM ${NODE_IMAGE} AS harnesses
ARG CLAUDE_CODE_VERSION
ARG CODEX_VERSION
ARG PI_CODING_AGENT_VERSION
RUN npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm install --global --ignore-scripts "@openai/codex@${CODEX_VERSION}" \
    && npm install --global --ignore-scripts \
      "@earendil-works/pi-coding-agent@${PI_CODING_AGENT_VERSION}" \
    && test "$(claude --version)" = "${CLAUDE_CODE_VERSION} (Claude Code)" \
    && test "$(codex --version)" = "codex-cli ${CODEX_VERSION}" \
    && test "$(pi --version)" = "${PI_CODING_AGENT_VERSION}"

FROM ${PYTHON_IMAGE} AS gh-cli
ARG TARGETARCH
ARG GH_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && case "$TARGETARCH" in \
      amd64) sha=fdd768eb70f6409f80fcfa9f3a1c0adf0fc98d827ab3ce0bed2ac22762c5d786 ;; \
      arm64) sha=7f322df34e624d14f5ec0c4acb121fd6dfa14392fdbbd1066aa8e0a05ac3721e ;; \
      *) echo "unsupported GitHub CLI architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --proto '=https' --tlsv1.2 \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${TARGETARCH}.tar.gz" \
      --output /tmp/gh.tar.gz \
    && echo "$sha  /tmp/gh.tar.gz" | sha256sum --check --strict \
    && mkdir /tmp/gh \
    && tar --extract --gzip --file /tmp/gh.tar.gz --strip-components=1 --directory /tmp/gh

FROM ${PYTHON_IMAGE} AS wheel-builder
WORKDIR /source
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --wheel-dir /wheels ".[dev]"

FROM ${PYTHON_IMAGE} AS runtime
ARG GH_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap git procps \
    && rm -rf /var/lib/apt/lists/*
COPY --from=wheel-builder /wheels /tmp/wheels
RUN python -m pip install --no-index --find-links=/tmp/wheels "dark-factory[dev]" \
    && rm -rf /tmp/wheels
COPY --from=gh-cli /tmp/gh/bin/gh /usr/local/bin/gh
COPY --from=pi-engine /usr/local/bin/node /usr/local/bin/node
COPY --from=harnesses /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=harnesses /usr/local/bin/claude /usr/local/bin/claude
RUN ln -s \
      /usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js \
      /usr/local/bin/pi \
    && ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
COPY --from=pi-engine /engine/node_modules /tmp/pi-node_modules
COPY examples/compose/codex-requirements.toml /etc/codex/requirements.toml
RUN engine_dir="$(python -c 'from dark_factory.adapters.workflow.pi_durable import ENGINE_DIR; print(ENGINE_DIR)')" \
    && chmod 0444 /etc/codex/requirements.toml \
    && cp -a /tmp/pi-node_modules "$engine_dir/node_modules" \
    && rm -rf /tmp/pi-node_modules \
    && node --version \
    && claude --version \
    && codex --version \
    && pi --version \
    && bwrap --version \
    && test "$(gh --version | head -n 1 | cut -d ' ' -f 1-3)" = "gh version ${GH_VERSION}" \
    && test "$(NODE_NO_WARNINGS=1 node --experimental-strip-types "$engine_dir/main.ts" --preflight)" = '{"version":1,"type":"ready"}' \
    && dark-factory --help >/dev/null
RUN useradd --uid 10001 --create-home factory \
    && mkdir /state \
    && chown factory:factory /state \
    && chmod 1777 /state
USER 10001:10001
WORKDIR /state
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["dark-factory"]
CMD ["--config", "/config/factory.toml", "serve"]

# This image contains the supervisor/operator client, Pi Durable dispatch engine,
# and exactly pinned Pi, Claude Code and Codex harnesses. It never bakes in inference
# credentials. Live workers receive project-scoped credentials at runtime and run
# harnesses only through a separately qualified isolated environment recipe.
