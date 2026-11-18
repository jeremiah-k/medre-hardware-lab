# checkov:skip=CKV_DOCKER_2:Compose probes service health (/proc process scan).
# =============================================================================
# meshtasticd lab image: unprivileged-runtime wrapper around the pinned
# upstream meshtastic/meshtasticd daily image.
#
# The upstream image declares VOLUME /var/lib/meshtasticd, ships NO /data,
# and runs everything as root (Cmd: meshtasticd --fsdir=/var/lib/meshtasticd).
# Pre-owning /data and /var/lib/meshtasticd for uid 1000 (the compose user:)
# lets the lab services run unprivileged: Docker seeds fresh named volumes
# with the image's directory ownership, so meshtasticd writes its state
# (--fsdir=/data) as uid 1000 with no root-mode bootstrap in the bring-up
# path (same mechanism as the mmrelay/medre lab images). Compose mounts a
# tmpfs over /var/lib/meshtasticd (unused when --fsdir=/data; verified empty)
# so the image's VOLUME declaration no longer mints an orphaned anonymous
# volume per container.
#
# The digest pin is tracked by Renovate's dockerfile manager (see
# renovate.json); compose references this build via medre-lab/meshtasticd-lab.
# =============================================================================
# Pinned upstream base (Renovate tracks this FROM digest): daily 2.8.3.b78db83.
FROM meshtastic/meshtasticd@sha256:68694cd2b704a1f4ed3bc6739e8edb7b519ce5798ac2dbab1a6352f90a632e3f

RUN mkdir -p /data /var/lib/meshtasticd && chown -R 1000:1000 /data /var/lib/meshtasticd

USER 1000:1000
