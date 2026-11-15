# =============================================================================
# MMRelay DEV image for the interop lab.
#
# Mirrors the upstream Dockerfile dependency install (pip install ".[e2e]"
# into a /install prefix, python:3.15-slim-bookworm runtime with git/procps)
# so E2EE and relay dependencies are baked. The mmrelay source is NOT baked
# into the running path: the host checkout is mounted read-only at /src and
# PYTHONPATH=/src/src shadows the baked package, so branch iteration needs
# no rebuild. The baked install provides package metadata + entry points.
#
# Build requires the extra build context "mmrelay-src" pointing at the host
# checkout (see compose.yaml x-mmrelay-build).
# =============================================================================

# Build stage: identical dependency install to the upstream Dockerfile.
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential=12.9 \
    git=1:2.39.5-0+deb12u2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==80.9.0 wheel==0.45.1

# Copy source from the read-only mmrelay-src context (host checkout).
COPY --from=mmrelay-src pyproject.toml MANIFEST.in setup.py README.md LICENSE ./
COPY --from=mmrelay-src src/ ./src/

# hadolint ignore=DL3013
RUN python -m pip install --no-cache-dir --timeout=300 --retries=3 \
    --prefix=/install ".[e2e]"

# -----------------------------------------------------------------------------
# medre core OVERRIDE for the mmrelay images (r26 round: medre-core-src,
# R2 round: mmrelay-core-src; mirrors the matrix override below).
#
# mmrelay's pyproject pins medre core to a landed main revision; that pin is
# NOT touched (the mmrelay checkout stays byte-identical). The candidate
# matrix adapter requires core APIs newer than that pin, and from candidate
# core #504 the LEGACY helper names mmrelay's source imports
# (extract_relation_text) are removed — so the mmrelay images bake the core
# checkout given by MMRELAY_BAKE_CORE_SRC (default: MEDRE_CORE_SRC):
#   r26 round  : medre-core-src  = MEDRE_CORE_SRC  (#504 head, the only
#                candidate core then; still exported all legacy names)
#   R2 round   : mmrelay-core-src = interop-medre-core-505 (PR #505 head,
#                the additive prerequisite preserving the legacy helpers AND
#                carrying the R2 APIs the candidate matrix adapter imports)
# medre-normal itself mounts and runs the #504 head; only the mmrelay bake
# differs. MMRelay's DECLARED core/Matrix pair must still qualify separately
# — a rig-only bake override proves rig compatibility, not the declared pin.
#
# NOTE: this is a LOCAL DIRECTORY install (same recipe as the medre-dev
# image), NOT git+file like the matrix override: the context is a detached
# git WORKTREE whose .git gitfile points outside the build context, so a
# git+file clone cannot resolve inside the build (the same reason the matrix
# override context is a standalone clone). Provenance is instead stamped into
# medre-bake-pin.json from the MMRELAY_BAKE_CORE_REF build arg (lab/up.sh
# exports the checkout's exact HEAD SHA), verifiable INSIDE the container.
# --no-deps: only the medre dist itself is replaced; the primary install
# above already resolved its dependencies. Installed BEFORE the matrix
# override so the baked core+adapter pair is consistent.
# -----------------------------------------------------------------------------
COPY --from=mmrelay-core-src pyproject.toml README.md /app/adapter-core/
COPY --from=mmrelay-core-src src/ /app/adapter-core/src/
ARG MMRELAY_BAKE_CORE_REF=main
RUN python -m pip install --no-cache-dir --timeout=300 --retries=3 \
    --force-reinstall --no-deps --prefix=/install /app/adapter-core \
    && python -c "import json,glob; \
p=glob.glob('/install/lib/python*/site-packages')[0]; \
u={'source': 'mmrelay-core-src build context', 'ref': '${MMRELAY_BAKE_CORE_REF}', \
'via': 'local directory install (worktree gitfile cannot resolve inside the build)'}; \
json.dump(u, open(p + '/medre-bake-pin.json', 'w')); \
print('baked medre core for mmrelay from', u.get('source'), 'ref', u.get('ref'))"

# -----------------------------------------------------------------------------
# medre-adapter-matrix OVERRIDE (rig-level, MMRELAY_EXPECTED_BRANCH-style).
#
# mmrelay's pyproject pins medre-adapter-matrix to a landed main revision;
# that pin is NOT touched here (the mmrelay checkout stays byte-identical).
# Instead the rig's MEDRE_MATRIX_SRC checkout is provided as the extra build
# context "medre-matrix-src" (see compose.yaml x-mmrelay-build) and the
# installed distribution is force-replaced from it at the ref given by the
# MEDRE_MATRIX_REF build arg (lab/up.sh exports the checkout's exact HEAD
# SHA). Installing via git+file makes pip record vcs_info.commit_id in
# direct_url.json, so the baked revision is verifiable INSIDE the container.
# --no-deps: the primary install above already resolved the adapter's
# dependencies (medre core, mindroom-nio, ...); only the adapter dist itself
# is replaced. The mmrelay source is PYTHONPATH-shadowed at runtime, but the
# ADAPTER is not — this baked install is the code the relays execute.
# -----------------------------------------------------------------------------
COPY --from=medre-matrix-src . /app/adapter-matrix/
ARG MEDRE_MATRIX_REF=main
RUN python -m pip install --no-cache-dir --timeout=300 --retries=3 \
    --force-reinstall --no-deps --prefix=/install \
    "medre-adapter-matrix @ git+file:///app/adapter-matrix@${MEDRE_MATRIX_REF}" \
    && python -c "import json,glob; \
u=json.load(open(glob.glob('/install/lib/python*/site-packages/medre_adapter_matrix-*.dist-info/direct_url.json')[0])); \
print('baked medre-adapter-matrix from', u.get('url'), 'commit', (u.get('vcs_info') or {}).get('commit_id'))"

# Runtime stage
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9

RUN groupadd --gid 1000 mmrelay && \
    useradd --uid 1000 --gid mmrelay --shell /bin/bash --create-home mmrelay

RUN apt-get update && apt-get install -y --no-install-recommends \
    git=1:2.39.5-0+deb12u2 \
    procps=2:4.0.2-3 \
    && (apt-get install -y --no-install-recommends bluez=5.66-1+deb12u2 || echo "Warning: bluez package not found for this architecture. BLE support will be unavailable.") \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

RUN mkdir -p /app /data /config && chown -R mmrelay:mmrelay /app /data

ARG BUILD_DATE
ARG VCS_REF
LABEL org.opencontainers.image.title="MMRelay dev (interop-lab)" \
    org.opencontainers.image.description="MMRelay dev container: deps baked, source mounted at /src" \
    org.opencontainers.image.revision="${VCS_REF:-unknown}" \
    org.opencontainers.image.created="${BUILD_DATE:-unknown}"

ENV PYTHONUNBUFFERED=1
ENV MPLCONFIGDIR=/tmp/matplotlib
ENV PATH=/usr/local/bin:/usr/bin:/bin
ENV MMRELAY_HOME=/data
ENV MMRELAY_READY_FILE=/tmp/mmrelay-ready
# Dev shadowing: mounted branch source takes precedence over the baked install.
ENV PYTHONPATH=/src/src

USER 1000:1000

WORKDIR /app

CMD ["python", "-m", "mmrelay.cli"]
