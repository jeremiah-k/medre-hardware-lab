# Docker service with a physical serial radio

This generic overlay replaces virtual radio A with one physical Meshtastic
serial radio. The second physical radio remains an independent host-owned peer;
relay B stays on virtual mesh B. Matrix is the interop boundary.

1. Bring up the normal Docker topology and verify its smoke tests.
2. Copy generated `lab/state/config/mmrelay-a.yaml` to the ignored sibling
   `mmrelay-physical.yaml`. Retain the generated Matrix credentials/room mapping;
   set its meshtastic connection_type to `serial` and serial_port to
   `/dev/medre-radio`; remove its TCP host and port fields.
3. Set LAB_RADIO_SERIAL to the stable by-id path of the owned relay radio, and
   LAB_RADIO_GID to its numeric device group. Leave the peer radio unmounted.
4. Stop other clients of the relay radio, then apply the overlay:

```sh
docker compose -f compose.yaml -f examples/hybrid/compose.serial.yaml up -d --no-deps mmrelay-a
```

Observe a unique peer nonce in Matrix and at the independent virtual peer, then
reverse direction and verify physical RF observation. The Docker-only standing
suite still names the virtual peer A: it cannot qualify the substituted physical
path without replacing its inject/observe endpoints. Hardware-only CLI tests
must not run concurrently against a radio held by this container.

Restore the virtual relay without deleting state:

```sh
docker compose up -d --no-deps mmrelay-a
```

This is a configurable deployment template. It has Compose validation only in the
initial portable import; perform live qualification on the configured hardware.
It does not change firmware, keys, channel configuration or clock settings.
