# checkov:skip=CKV_DOCKER_2:Compose and scenario probes own service health; tools are one-shot commands.
# BuildKit named contexts are validated by the source manifest guard.
# hadolint global ignore=DL3022
# =============================================================================
# MEDRE DEV image for the interop lab (mirrors the mmrelay-dev pattern).
#
# Dependency install is baked from the candidate checkouts' own dependency
# specs (medre core -> matrix adapter -> meshtastic adapter -> lxmf adapter),
# so mindroom-nio[e2e] (vodozemac), the mtjk Meshtastic SDK, and the pinned
# LXMF/RNS stack (lxmf==1.2.1, rns==1.5.7) are present. The package CODE is
# NOT the running surface: the git worktrees are mounted read-only at
# /src/{core,matrix,meshtastic,lxmf} and PYTHONPATH shadows the baked install
# (pkgutil.extend_path namespace merging across the src roots; core's src must
# come FIRST so its medre/__init__.py wins the import). The baked install
# provides dependency pins, package metadata, entry points (including the
# separately distributed medre-adapter-lxmf registration), and the `medre`
# console script.
#
# Build requires the extra build contexts (see compose.yaml x-medre-build):
#   medre-core-src        medre core worktree at 93f43311
#   medre-matrix-src      medre-adapter-matrix worktree at 75d961d8 (PR #42 head)
#   medre-meshtastic-src  medre-adapter-meshtastic worktree at 928b9185 (PR #34 head)
#   medre-lxmf-src        medre-adapter-lxmf worktree (r15q-lxmf) at de04e71d,
#                         mounted/consumed READ-ONLY; provides the optional
#                         lxmf transport used by the `normal` profile only
#
# python:3.12-slim-bookworm: the matrix adapter requires >=3.12 (durable
# mindroom-nio sync backend); core supports 3.11+.
# =============================================================================

# Build stage: deps baked from the candidate checkouts' dependency specs.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential=12.9 \
    git=1:2.39.5-0+deb12u2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==80.9.0 wheel==0.45.1

# All four distributions in ONE pip transaction: the adapters'
# `medre>=0.1.0,<0.2.0` requirement is satisfied by the local core build in
# the same resolve (medre is not published to PyPI); lxmf==1.2.1 and
# rns==1.5.7 come from PyPI via the lxmf adapter's dependency spec.
COPY --from=medre-core-src pyproject.toml README.md ./core/
COPY --from=medre-core-src src/ ./core/src/
COPY --from=medre-matrix-src pyproject.toml README.md ./matrix/
COPY --from=medre-matrix-src src/ ./matrix/src/
COPY --from=medre-meshtastic-src pyproject.toml README.md ./meshtastic/
COPY --from=medre-meshtastic-src src/ ./meshtastic/src/
COPY --from=medre-lxmf-src pyproject.toml README.md ./lxmf/
COPY --from=medre-lxmf-src src/ ./lxmf/src/
RUN python -m pip install --no-cache-dir --timeout=300 --retries=3 \
    --prefix=/install ./core ./matrix ./meshtastic ./lxmf

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
LABEL org.opencontainers.image.title="MEDRE dev (interop-lab)" \
    org.opencontainers.image.description="MEDRE core + matrix/meshtastic adapters: deps baked from candidate checkouts, source worktrees mounted at /src" \
    org.opencontainers.image.revision="${VCS_REF:-unknown}" \
    org.opencontainers.image.created="${BUILD_DATE:-unknown}"

ENV PYTHONUNBUFFERED=1
ENV PATH=/usr/local/bin:/usr/bin:/bin
# MEDRE_HOME single-directory mode (Docker deployment convention):
# state (medre.sqlite mapping db, adapter stores) at /data/state,
# logs dir at /data/logs.
ENV MEDRE_HOME=/data
# Dev shadowing: mounted candidate worktrees take precedence over the baked
# install. Core src FIRST so its pkgutil.extend_path medre/__init__.py wins
# the namespace import and merges the adapter portions. Services override
# this per profile (the stress `medre` relay omits the lxmf src mount).
ENV PYTHONPATH=/src/core/src:/src/matrix/src:/src/meshtastic/src:/src/lxmf/src

USER 1000:1000

WORKDIR /app

CMD ["medre", "run"]
