# Build from an explicitly selected digest-pinned Python runtime image.
# Build a reviewed wheel first; this image contains the supervisor, not coding agents.
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git procps && rm -rf /var/lib/apt/lists/*
COPY dist/*.whl /tmp/wheels/
RUN python -m pip install --no-index /tmp/wheels/*.whl
RUN useradd --uid 10001 --create-home factory && mkdir /state && chown factory:factory /state
USER 10001:10001
WORKDIR /state
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["dark-factory"]
CMD ["--config", "/config/factory.toml", "serve"]
# A pi-durable profile also requires Node >=22.19.0 and npm-ci-installed engine
# dependencies in the selected runtime image. State-free preflight fails if absent.
# Pin the resulting image digest: package repositories can change between builds.
