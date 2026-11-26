# checkov:skip=CKV_DOCKER_2:Compose owns service health; the healthcheck is process liveness only.
# BuildKit named contexts are validated by the source manifest guard.
# hadolint global ignore=DL3022
# =============================================================================
# MEDRE image for the openHop profile (the `openhop` compose profile).
#
# This is a SECOND MEDRE build anchor, separate from lab/medre/Dockerfile.
# The default rig's image installs medre 0.1.0 (pinned fd2927d) with the
# matrix/meshtastic/lxmf adapters; the openHop profile needs medre 0.2.3
# (pinned 05684df6) because medre-adapter-meshcore 0.2.0 requires
# medre>=0.2.0,<0.3.0. Changing the default image would silently move the
# default rig, so the openHop profile gets its own anchor and its own
# checkouts (sources/medre-openhop, sources/medre-adapter-meshcore,
# sources/medre-adapter-meshtastic-openhop).
#
# The adapter set is deliberately minimal: medre core + meshcore adapter +
# meshtastic adapter. NO matrix and NO lxmf adapter -- the openHop profile
# qualifies the MEDRE<->openHop companion link and the existing MeshC
# meshtastic sim endpoint, not Matrix rooms.
#
# The medre-adapter-meshcore distribution declares medre>=0.2.0,<0.3.0 and
# meshcore==2.3.15 (the upstream, unmodified MeshCore SDK). Both are
# resolved in the SAME pip transaction as the local core build, so the
# local medre 0.2.3 satisfies the adapter's medre requirement.
#
# Build requires the extra build contexts (see compose.yaml
# x-medre-openhop-build):
#   medre-openhop-src              medre core checkout at the pinned SHA
#   medre-meshcore-src             medre-adapter-meshcore checkout
#   medre-meshtastic-openhop-src   medre-adapter-meshtastic checkout
#
# The package CODE is not the running surface: the checkouts are mounted
# read-only at /src/{core,meshcore,meshtastic} and PYTHONPATH shadows the
# baked install (pkgutil namespace merging; core src first so its
# medre/__init__.py wins the namespace import). The baked install supplies
# dependency pins, package metadata, and the `medre` console script.
#
# python:3.12-slim-bookworm: the meshcore adapter requires >=3.11 and the
# medre core supports 3.11+.
# =============================================================================

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential=12.9 \
    git=1:2.39.5-0+deb12u2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.2.1 wheel==0.45.1

COPY --from=medre-openhop-src pyproject.toml README.md ./core/
COPY --from=medre-openhop-src src/ ./core/src/
COPY --from=medre-meshcore-src pyproject.toml README.md ./meshcore/
COPY --from=medre-meshcore-src src/ ./meshcore/src/
COPY --from=medre-meshtastic-openhop-src pyproject.toml README.md ./meshtastic/
COPY --from=medre-meshtastic-openhop-src src/ ./meshtastic/src/

# ONE pip transaction: the meshcore adapter's `medre>=0.2.0,<0.3.0`
# requirement is satisfied by the local medre 0.2.3 build, so no medre
# distribution is pulled from an index.
RUN python -m pip install --no-cache-dir --timeout=300 --retries=3 \
    --prefix=/install ./core ./meshcore ./meshtastic

# Runtime stage
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

RUN groupadd --gid 1000 medre && \
    useradd --uid 1000 --gid medre --shell /bin/bash --create-home medre

RUN apt-get update && apt-get install -y --no-install-recommends \
    git=1:2.39.5-0+deb12u2 \
    procps=2:4.0.2-3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

RUN mkdir -p /app /data /config && chown -R medre:medre /app /data

ARG BUILD_DATE
ARG VCS_REF
LABEL org.opencontainers.image.title="MEDRE dev (hardware-lab, openHop profile)" \
    org.opencontainers.image.description="MEDRE 0.2.3 core + meshcore/meshtastic adapters: deps baked from selected checkouts, source worktrees mounted at /src" \
    org.opencontainers.image.revision="${VCS_REF:-unknown}" \
    org.opencontainers.image.created="${BUILD_DATE:-unknown}"

ENV PYTHONUNBUFFERED=1
ENV PATH=/usr/local/bin:/usr/bin:/bin
ENV MEDRE_HOME=/data
# Dev shadowing: the mounted checkouts take precedence over the baked
# install. Core src FIRST so its pkgutil.extend_path medre/__init__.py wins
# the namespace import and merges the adapter portions.
ENV PYTHONPATH=/src/core/src:/src/meshcore/src:/src/meshtastic/src

USER 1000:1000

WORKDIR /app

CMD ["medre", "run"]
