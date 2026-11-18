# checkov:skip=CKV_DOCKER_2:Compose probes service health (urllib versions endpoint).
# checkov:skip=CKV_DOCKER_3:Compose runs the service as 991:991; the upstream
#   start.py entrypoint requires the image default (root) user so a plain
#   `docker run` keeps its documented UID/GID-env demotion path.
# =============================================================================
# Synapse lab image: unprivileged-runtime wrapper around the pinned upstream
# matrixdotorg/synapse image.
#
# The upstream image ships NO /data directory and runs as root: its start.py
# bootstraps state with `chown -R 991:991 /data` + gosu (and in `generate`
# mode writes <server>.log.config BEFORE that chown, leaving root-owned files
# in the data volume). Pre-owning /data as 991:991 (the same user start.py
# demotes to) lets compose run the whole entrypoint as 991:991 instead: with
# a non-root uid and no UID env, start.py skips the chown/gosu entirely and
# Synapse runs directly as the image's documented runtime user. Docker seeds
# fresh named volumes with the image's /data ownership, so no root-mode
# bootstrap exists anywhere in the bring-up path (same mechanism as the
# mmrelay/medre lab images).
#
# The digest pin is tracked by Renovate's dockerfile manager (see
# renovate.json); compose references this build via medre-lab/synapse-lab.
# =============================================================================
# Pinned upstream base (Renovate tracks this FROM digest): v1.149.0.
FROM matrixdotorg/synapse@sha256:f223f5fc532600801284fededefca82288fb71551bf4fcc89cd0b3363a3461cc

RUN mkdir -p /data && chown 991:991 /data
