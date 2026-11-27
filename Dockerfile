# Digest-pinned, multi-architecture build inputs. Override only with reviewed digests.
ARG PYTHON_IMAGE=python:3.11.18-slim-trixie@sha256:61e01f72680f86a5279ad510818ab7b2ee9f049822589d4e34e33e044360356c
ARG NODE_IMAGE=node:24.22.3-trixie-slim@sha256:4d00acb4bcc42c454c4eb801526f7ad7f772ea3bd4fd168c5cc2e384abcbb9d4
ARG CLAUDE_CODE_VERSION=2.1.310
ARG CODEX_VERSION=0.164.0
ARG PI_CODING_AGENT_VERSION=1.4.0
ARG GH_VERSION=2.105.0

FROM ${NODE_IMAGE} AS node-runtime

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
      amd64) sha=79e7ab5db738c48777b696e374f8b428f0647debfb424f44d2f426370407fe2f ;; \
      arm64) sha=b1278c75107423d818f1937fd92cafc9e5e9b606631568f68d7fb816f5885a02 ;; \
      *) echo "unsupported GitHub CLI architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --proto '=https' --tlsv1.2 \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${TARGETARCH}.tar.gz" \
      --output /tmp/gh.tar.gz \
    && echo "$sha  /tmp/gh.tar.gz" | sha256sum --check --strict \
    && mkdir /tmp/gh \
    && tar --extract --gzip --file /tmp/gh.tar.gz --strip-components=1 --directory /tmp/gh

FROM ${PYTHON_IMAGE} AS wheel-builder
# The runtime image carries no test or lint tools unless a build asks for them
# (LOCKFILE=requirements-dev.lock), as the live overlay does to run this repository's own checks.
ARG LOCKFILE=requirements.lock
WORKDIR /source
COPY pyproject.toml README.md requirements.lock requirements-dev.lock ./
COPY src ./src
# Every dependency is installed from a lockfile whose hashes must match; the
# project's own wheel is built last, without resolving anything.
RUN python -m pip wheel --require-hashes --wheel-dir /wheels -r "${LOCKFILE}" \
    && python -m pip wheel --no-deps --wheel-dir /wheels .

FROM ${PYTHON_IMAGE} AS runtime
ARG GH_VERSION
ARG LOCKFILE=requirements.lock
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap git procps \
    && rm -rf /var/lib/apt/lists/*
COPY --from=wheel-builder /wheels /tmp/wheels
COPY --from=wheel-builder /source/${LOCKFILE} /tmp/dependencies.lock
RUN python -m pip install --no-index --find-links=/tmp/wheels --require-hashes -r /tmp/dependencies.lock \
    && python -m pip install --no-index --no-deps --find-links=/tmp/wheels dark-factory \
    && rm -rf /tmp/wheels /tmp/dependencies.lock
COPY --from=gh-cli /tmp/gh/bin/gh /usr/local/bin/gh
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=harnesses /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=harnesses /usr/local/bin/claude /usr/local/bin/claude
RUN ln -s \
      /usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js \
      /usr/local/bin/pi \
    && ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
COPY examples/compose/codex-requirements.toml /etc/codex/requirements.toml
RUN chmod 0444 /etc/codex/requirements.toml \
    && node --version \
    && claude --version \
    && codex --version \
    && pi --version \
    && bwrap --version \
    && test "$(gh --version | head -n 1 | cut -d ' ' -f 1-3)" = "gh version ${GH_VERSION}" \
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

# This image contains the supervisor/operator client and exactly pinned Pi, Claude
# Code and Codex harnesses. It never bakes in inference
# credentials. Live workers receive project-scoped credentials at runtime and run
# harnesses only through a separately qualified isolated environment recipe.
