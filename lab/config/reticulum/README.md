# Reticulum configuration templates (normal profile)

These are the two halves of a loopback-only Reticulum link used by the
`normal` compose profile. Nothing here reaches a physical interface or an
external network: both sides bind and forward exclusively on 127.0.0.1
INSIDE the medre-normal container's network namespace (the counterpart
driver runs there via `docker compose exec`).

- `medre.config` -> seeded by lab/up.sh into the medre-normal state volume
  as `/data/reticulum/config` (`reticulum_config_dir` of the medre lxmf0
  adapter). Listens on 127.0.0.1:4242, forwards to 127.0.0.1:4243.
- `peer.config` -> seeded by lab/up.sh into `lab/state/lxmf/reticulum-peer/config`
  (the counterpart driver's RNS config dir, mounted at /data-lxmf).
  Listens on 127.0.0.1:4243, forwards to 127.0.0.1:4242.

`share_instance = No` keeps both RNS instances independent (no shared-instance
listener); `enable_transport = No` keeps them from acting as network
transport. RNS writes its destination-path cache under each config dir's
`storage/` — both dirs are writable (volume / lab-state bind).

Syntax per the pinned rns 1.5.7 UDPInterface (loopback UDPInterface pairs are
the documented local-isolation pattern in the medre-adapter-lxmf local
integration suite).
