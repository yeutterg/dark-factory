# Digest-pinned, multi-architecture build inputs. Override only with reviewed digests.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
ARG NODE_IMAGE=node:24.22.0-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5

FROM ${NODE_IMAGE} AS pi-engine
WORKDIR /engine
COPY src/dark_factory/adapters/workflow/pi_engine/package.json ./
COPY src/dark_factory/adapters/workflow/pi_engine/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

FROM ${PYTHON_IMAGE} AS wheel-builder
WORKDIR /source
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --wheel-dir /wheels .

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends git procps \
    && rm -rf /var/lib/apt/lists/*
COPY --from=wheel-builder /wheels /tmp/wheels
RUN python -m pip install --no-index --find-links=/tmp/wheels dark-factory \
    && rm -rf /tmp/wheels
COPY --from=pi-engine /usr/local/bin/node /usr/local/bin/node
COPY --from=pi-engine /engine/node_modules /tmp/pi-node_modules
RUN engine_dir="$(python -c 'from dark_factory.adapters.workflow.pi_durable import ENGINE_DIR; print(ENGINE_DIR)')" \
    && cp -a /tmp/pi-node_modules "$engine_dir/node_modules" \
    && rm -rf /tmp/pi-node_modules \
    && node --version \
    && test "$(NODE_NO_WARNINGS=1 node --experimental-strip-types "$engine_dir/main.ts" --preflight)" = '{"version":1,"type":"ready"}' \
    && dark-factory --help >/dev/null
RUN useradd --uid 10001 --create-home factory \
    && mkdir /state \
    && chown factory:factory /state
USER 10001:10001
WORKDIR /state
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["dark-factory"]
CMD ["--config", "/config/factory.toml", "serve"]

# This image contains the supervisor/operator client and Pi Durable dispatch engine.
# It intentionally does not bake in inference credentials or agent CLIs. A live
# execution-worker image must add only its project-approved, exactly pinned harnesses.
