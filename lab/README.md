# Docker interop topology

Use `medre-lab docker up --profile normal` from the repository root. All source
paths and revisions are generated from `sources.lock.json` by `docker prepare`.
The Docker topology is imported from the existing interop rig; transport and
room semantics are retained.

- `base`: two MMRelays, each attached to one primary virtual Meshtastic node;
  independent peer nodes inject and observe RF-simulation traffic.
- `normal`: the two relays share a Matrix bridge room with a MEDRE instance that
  owns a separate Gamma mesh and LXMF counterpart. Source and peer API ownership
  stay separate.
- `stress`: a deliberately shared-mesh MEDRE relay for loop diagnosis.
- `openhop`: an opt-in, HARDWARE-FREE qualifier for the MEDRE ⇄ openHop
  protocol link: an openHop repeater with `radio_type: null` serving the
  MeshCore companion over TCP on a private network, plus a MEDRE runtime
  whose meshcore adapter talks to it. It carries its OWN source pins (the
  default rig's medre 0.1.0 pin cannot satisfy the meshcore adapter's
  `medre>=0.2.0` requirement) and its own MEDRE image anchor. The default
  rig and its pins are unchanged. See [the openHop guide](../docs/openhop-qualification.md).

Normal and stress are mutually exclusive: they share the MEDRE account and
must not compete for its sync loop or radio API clients. `docker up` stops the
other profile's relay before changing topology. `openhop` is exclusive with
both of them for the same reason.

Synapse listens on loopback port 8008. Virtual Meshtastic API debug ports are
4403–4410. The internal networks separate Matrix and virtual meshnets. Generated
credentials/configuration live in ignored `lab/state/`; logs in `lab/logs/`.
Synthetic mesh keys are generated in ignored `lab/config/meshnet.env`.

The normal standing suite is:

```sh
medre-lab docker test normal-fanout normal-parity normal-replies normal-reactions normal-lxmf normal-untrusted
```

The broader scenario registry is in `lab/run-scenarios.sh` and detailed scenario
contracts in `scenarios/README.md`. Candidate-specific probes remain available
for targeted regression analysis. Run against an already-up rig; the runner
checks the source manifest and does not start/stop containers. It distinguishes
PASS, FAIL and SKIP. Skipped scenarios are not qualification evidence.

`docker down` stops containers and retains state. `lab/reset.sh` explicitly
removes volumes and clears generated state/logs. Keep physical devices on the
host unless an explicitly configured hybrid deployment needs serial passthrough;
BLE is not enabled by making containers privileged.
