# checkov:skip=CKV_DOCKER_2:Compose owns service health; the healthcheck is process liveness only.
# BuildKit named contexts are validated by the source manifest guard.
# hadolint global ignore=DL3022
# =============================================================================
# openHop Repeater image for the hardware lab (profile `openhop`).
#
# This is a LAB-OWNED recipe. It is NOT the upstream openhop_repeater
# Dockerfile: upstream builds openhop_core from PyPI and ships a hardware
# radio runtime (spidev, USB, GPIO groups, /dev mounts). The lab must
# qualify the PINNED LOCAL openhop_core and openhop_repeater checkouts, so
# this image installs those two distributions from the selected local
# checkouts instead of from an index, and drops every hardware capability.
#
# Install order matters: openhop_core first (the repeater's dependency),
# then openhop_repeater.
#
# Provenance: both installs are LOCAL DIRECTORY installs of the selected
# checkouts, so pip records a file:// direct_url.json rather than a
# vcs_info.commit_id (the same reason the MMRelay rig bakes its core from a
# local directory). The exact commit is therefore stamped from the build ARG
# (lab/up.sh exports the checkout's HEAD SHA, exactly like MEDRE_CORE_REF)
# into openhop-bake-pin.json next to the installed distributions, and the
# runtime versions are read back from the installed metadata.
#
# Build requires the extra build contexts (see compose.yaml
# x-openhop-repeater-build):
#   openhop-core-src      openhop_core checkout at the pinned SHA
#   openhop-repeater-src  openhop_repeater checkout at the pinned SHA
#
# NO hardware is enabled here: no spidev, no USB, no GPIO groups, no
# /dev mounts, no CAP_SYS_RAWIO. radio_type stays null in the repeater
# config, so the daemon runs without RF I/O.
# =============================================================================

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential=12.9 \
    git=1:2.39.5-0+deb12u2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.2.1 wheel==0.45.1

# --- openhop_core (installed FIRST: the repeater depends on it) ----------------
COPY --from=openhop-core-src pyproject.toml README.md LICENSE /app/core/
COPY --from=openhop-core-src src/ /app/core/src/
RUN python -m pip install --no-cache-dir --timeout=300 --retries=3 \
    --prefix=/install /app/core

# --- openhop_repeater ---------------------------------------------------------
# The upstream distribution resolves its version through setuptools_scm,
# which needs the git metadata that a source copy does not carry. The
# pinned checkout is tag 1.1.4, so that exact version is pinned here and
# the recorded commit below proves WHICH source it came from.
COPY --from=openhop-repeater-src pyproject.toml README.md LICENSE /app/repeater/
COPY --from=openhop-repeater-src repeater/ /app/repeater/repeater/
COPY --from=openhop-repeater-src radio-presets.json radio-settings.json /app/repeater/

ARG OPENHOP_CORE_REF=unknown
ARG OPENHOP_REPEATER_REF=unknown
ARG OPENHOP_CORE_VERSION=1.1.3
ARG OPENHOP_REPEATER_VERSION=1.1.4
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENHOP_REPEATER=${OPENHOP_REPEATER_VERSION}

# --no-deps for the repeater: its openhop_core[hardware]==1.1.3 requirement
# would drag in spidev/periphery (hardware this lab never uses) and could
# replace the local core install. The runtime dependencies are installed
# explicitly below, matching the upstream dependency list minus the
# hardware extra.
RUN python -m pip install --no-cache-dir --timeout=300 --retries=3 \
    --prefix=/install --no-deps /app/repeater \
    && python -m pip install --no-cache-dir --timeout=300 --retries=3 \
    --prefix=/install \
    "pyyaml>=6.0.0" "cherrypy>=18.0.0" "paho-mqtt>=1.6.0" \
    "cherrypy-cors==1.7.0" "psutil>=5.9.0" "pyserial>=3.5" \
    "pyjwt>=2.8.0" "ws4py>=0.6.0"

# Provenance stamp: which checkout each installed distribution came from,
# and the version that was actually installed.
COPY lab/openhop/bake_pin.py /build/bake_pin.py
RUN OPENHOP_CORE_REF="${OPENHOP_CORE_REF}" \
    OPENHOP_REPEATER_REF="${OPENHOP_REPEATER_REF}" \
    python /build/bake_pin.py /install

# Runtime stage
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

RUN groupadd --gid 1000 openhop && \
    useradd --uid 1000 --gid openhop --shell /bin/bash --create-home openhop

RUN apt-get update && apt-get install -y --no-install-recommends \
    procps=2:4.0.2-3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

RUN mkdir -p /app /data /etc/openhop_repeater /var/lib/openhop_repeater && \
    chown -R openhop:openhop /app /data /etc/openhop_repeater /var/lib/openhop_repeater

ARG BUILD_DATE
ARG VCS_REF
LABEL org.opencontainers.image.title="openHop Repeater (hardware-lab, null radio)" \
    org.opencontainers.image.description="openHop repeater + pinned local openhop_core; radio_type null, no hardware capabilities" \
    org.opencontainers.image.revision="${VCS_REF:-unknown}" \
    org.opencontainers.image.created="${BUILD_DATE:-unknown}"

ENV PYTHONUNBUFFERED=1
ENV PATH=/usr/local/bin:/usr/bin:/bin
# Persistent state (SQLite companion store, identity file) lives here.
ENV DATA_DIR=/var/lib/openhop_repeater
ENV CONFIG_DIR=/etc/openhop_repeater
# The pinned checkouts are mounted read-only and shadow the baked install
# (same dev-shadowing pattern the MEDRE image uses), so the running code is
# the pinned checkout, not an index artifact.
ENV PYTHONPATH=/src/openhop-core/src:/src/openhop-repeater

USER 1000:1000

WORKDIR /app

CMD ["python3", "-m", "repeater.plugins.container_supervisor", "--config", "/etc/openhop_repeater/config.yaml"]
