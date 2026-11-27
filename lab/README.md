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

## Rig presets

A rig configuration is picked from a library of named, committed records, not
rebuilt by hand and not overwritten by the next bring-up. A preset is one JSON
file, `lab/presets/<name>.json`, recording the CHOICES a rig configuration is
made of and nothing else:

- `sources`: the full ten-entry source pin map, identical in schema to
  `sources.lock.json`;
- `topology`: the bring-up choice — `base`, `normal`, `stress`, `openhop` or
  `openhop-radio` (the docker-up profile names);
- `suite`: what runs against that rig — `scenarios` (the compose scenario suite
  via `lab/run-scenarios.sh`), `scenario:<name>` (one scenario script), or
  `qual:<stage>` (one stage of `lab/tools/openhop_qualify.py`, which sequences
  its own up/down transitions across the openHop topologies);
- `name` and a one-line `description`.

It deliberately records nothing generated. Tokens, PSKs and room ids rotate on
every provision and live in private state (`lab/state/`,
`lab/config/meshnet.env`); `.env`, the manifests and `scenario.env` regenerate
deterministically from pins + topology. Loading a preset therefore re-derives
everything instead of copying stale or secret-bearing state.

Commands:

- `medre-lab preset list` — the library, with each preset's pins-matched state.
- `medre-lab preset show NAME` — one preset's recorded choices, as committed.
- `medre-lab preset save NAME [--description D] [--force]` — record the current
  configuration: the live lock's pins, the topology the LAST bring-up actually
  ran (read from `lab/state/last-bringup.json`, which `lab/up.sh` writes at the
  end of every bring-up that leaves a rig UP), and the suite that topology runs
  by default. Refuses to overwrite an existing preset without `--force`, and
  refuses a bring-up recorded against different lock content: a preset must
  record a pairing that ran.
- `medre-lab preset load NAME [--up] [--force]` — switch the rig: write
  `sources.lock.json` from the preset, run `sources fetch` + `docker prepare`,
  then verify every checkout HEAD against its pin and fail loudly on any
  mismatch (the lab never resets existing checkouts; a stale checkout is the
  operator's to fix). If the current lock's pin set differs from the target AND
  matches no saved preset, the load is refused — save the current configuration
  first, or pass `--force` to discard it. With `--up`, the recorded topology is
  brought up through the existing docker-up path. Load never runs suites:
  running stays the existing commands.

The library rule is membership, not uniqueness: distinct presets may share the
same pins (they differ in topology, suite or intent — the seeds do exactly
that). The guard test requires the lock's pin set to be recorded by at least
one preset, so a preset edit cannot silently orphan the rig's own lock. Two
presets ship seeded: `base` (the plain two-relay rig and the compose suite) and
`openhop-companion` (the openHop qualification rig with the driver's stages);
both hold the same pins by design.

## Suite evidence

A `lab/run-scenarios.sh` run (with at least one scenario that will actually
execute) stamps a per-run evidence directory — `evidence/scenarios/<ts>` by
default, or `--evidence-dir DIR` — with the same source-selection record the
openHop drivers use, BEFORE any scenario runs. Each executed scenario becomes a
case file and the run writes an aggregate summary embedding the stamp; a bind
refusal (dirty lab tree, moved content) stops the run before any scenario can
execute. Skipped scenarios write no case file: they are not qualification
evidence. Carry a recorded pass forward with the shared verifier:

```sh
python3 lab/tools/openhop_case_summary.py --verify-selection <dir> --profile scenarios
```

It regenerates the summary from the case files, appends an entry to
`selection-carry-forward.json`, and refuses on any content change.

`docker down` stops containers and retains state. `lab/reset.sh` explicitly
removes volumes and clears generated state/logs. Keep physical devices on the
host unless an explicitly configured hybrid deployment needs serial passthrough;
BLE is not enabled by making containers privileged.
