# checkov:skip=CKV_DOCKER_2:Compose owns service health; the healthcheck is process liveness only.
# =============================================================================
# Virtual radio relay image for the hardware lab (profile `openhop-radio`).
#
# LAB-OWNED. This image contains NO openHop code and NO radio code: it runs
# the lab's virtual-radio relay, an asyncio TCP service that speaks the
# openHop Modem wire protocol (SYNC | CMD | LEN | PAYLOAD | CRC-16/CCITT-FALSE)
# so that two openHop repeaters can be wired to each other without any
# hardware.
#
# The relay simulates the RF MEDIUM only. The MeshCore packets that cross it
# are real: they are produced and consumed by real openHop repeaters. See
# docs/openhop-radio-qualification.md for the evidence boundary.
#
# No hardware: no /dev mounts, no CAP_SYS_RAWIO, no GPIO/USB groups, and no
# host ports. The relay is reachable only on the private compose network.
# =============================================================================

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ARG VCS_REF
LABEL org.opencontainers.image.title="openHop virtual radio relay (hardware-lab)" \
    org.opencontainers.image.description="Lab-owned asyncio service speaking the openHop Modem protocol; simulates the RF medium between two repeaters, no hardware" \
    org.opencontainers.image.revision="${VCS_REF:-unknown}"

RUN mkdir -p /app && chown 1000:1000 /app

COPY lab/openhop-radio/openhop_radio_relay.py /app/openhop_radio_relay.py

ENV PYTHONUNBUFFERED=1
ENV PATH=/usr/local/bin:/usr/bin:/bin

USER 1000:1000

WORKDIR /app

CMD ["python3", "/app/openhop_radio_relay.py", "--host", "0.0.0.0", "--port", "5055"]
