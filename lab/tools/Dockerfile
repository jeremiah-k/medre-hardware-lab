# checkov:skip=CKV_DOCKER_2:Compose and scenario probes own service health; tools are one-shot commands.
# checkov:skip=CKV_DOCKER_3:One-shot lab tools require host device access; Compose selects the caller UID where supported.
# Debian packages follow the pinned distribution security repository.
# hadolint global ignore=DL3008
# =============================================================================
# lab-tools: administration and assertion utility container.
#
# Provides curl, sqlite3, psql (unused with the SQLite-backed Synapse default,
# kept per lab contract), jq, python3 with requests + the mtjk Meshtastic
# Python CLI (same pinned version mmrelay depends on, so channel configuration
# behaves exactly like CI) for provisioning, smoke tests, and state queries.
# =============================================================================

# Tag pinned; digest recorded in lab/VERSION after verification.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    jq \
    postgresql-client \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# mtjk pinned to the same version mmrelay's pyproject depends on so channel
# configuration matches CI behavior exactly; vodozemac matches the branch venv.
RUN pip install --no-cache-dir \
    "requests==2.34.2" \
    "mtjk==2.7.11.post14" \
    "vodozemac==0.9.0.post2"

COPY lab/tools/ /opt/lab-tools/

ENV PYTHONPATH=/opt/lab-tools
ENV PYTHONUNBUFFERED=1

CMD ["sleep", "infinity"]
