# MEDRE hardware lab

One lab for MEDRE and MMRelay: Docker interop, physical radio routes, and mixed
Docker/radio deployments. It imports the existing interop rig and the bounded
native-peer checks used to qualify Meshtastic, MeshCore and LXMF.

The lab owns setup, evidence and resource lifetimes. MEDRE and its providers
continue to own routing, rendering, encoding and transport behavior.

## Choose a mode

| Mode | What it proves | Requirements |
| --- | --- | --- |
| Docker `base` | Two MMRelays, independent virtual meshnets and Matrix | Docker Compose with BuildKit additional contexts |
| Docker `normal` | MEDRE/MMRelay shared-room interop, virtual Meshtastic, LXMF counterpart | Same, plus exact source locks |
| Docker `stress` | Deliberate shared-mesh loop topology | Same; a separate diagnostic profile |
| Hardware | Two-way ordinary-text routing between two physical transport pairs | Linux, Python 3.12+, two boards per selected transport |
| Mixed | Docker relay with an explicitly mapped physical serial radio | [Generic serial overlay](examples/hybrid/README.md); live qualification required |

Hardware checks support any two of Meshtastic, MeshCore and LXMF. A role selects
which endpoint in each configured pair MEDRE owns; the other endpoints are
independent native senders/receivers. Swap roles to exercise every board.

A passing short check means one message per direction was independently observed,
appeared once and has a successful MEDRE receipt. LXMF sends must reach terminal
DELIVERED. It does not claim attachment, restart, soak or full release coverage.

## Setup

Use the repository checkout as the working directory. The CLI can also select it
with `--root`. Docker files and scenarios stay in the checkout; they are not
bundled into a separately published Python distribution.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/medre-lab init
.venv/bin/medre-lab doctor
```

`lab.toml` is local and ignored. Configure only the transports you own. The
example uses placeholders, not the author's device addresses. `doctor` inventories
serial paths, controllers and required executables without transmitting or
changing settings. BLE on Linux normally runs on the host through BlueZ; hardware
mode does not require privileged Docker containers.

## Docker interop

```sh
.venv/bin/medre-lab sources fetch
.venv/bin/medre-lab docker up --profile normal
.venv/bin/medre-lab docker test normal-fanout normal-parity normal-replies normal-reactions normal-lxmf normal-untrusted
.venv/bin/medre-lab docker down
```

`sources.lock.json` selects exact full commits. Fetch clones into ignored
`sources/`; existing checkouts are verified and never reset or cleaned. Edit the
lock intentionally for candidate qualification, then prepare those exact sources.
`docker prepare` checks them against the independent lock, writes a source
manifest and generates the local `.env`. Bind mounts remain read-only, with
`create_host_path: false`; the scenario guard checks actual live imports.

Synthetic mesh keys are generated locally on first preparation. Local credentials,
state, logs and source clones are ignored. Source-baked MMRelay dependencies are
explicit candidate overrides. `lab/mmrelay/Dockerfile-declared` is retained for
separate qualification of MMRelay's own declared dependencies; do not equate the
two evidence classes.

The Docker topology uses `medre-lab-*` container/network/volume names. Stop an
older rig using the same host ports before bringing this one up. The CLI refuses
to take over a topology owned by another checkout. `docker down` retains volumes;
`lab/reset.sh` is the explicit destructive reset operation.

See [Docker topology and scenarios](lab/README.md).

## Physical radio qualification

Install the released transport packages, or editable source revisions when
qualifying an unpublished provider:

```sh
.venv/bin/pip install -e '.[hardware]'
# Once available as a distribution:
.venv/bin/pip install -e '.[meshcore]'
# Or use a checkout at the exact candidate commit:
# .venv/bin/pip install -e /path/to/medre-adapter-meshcore
```

Fill in `lab.toml`: stable serial by-id paths; MeshCore BLE addresses, channel and
controller; and two existing private Reticulum profiles/identities. Only selected
transport pairs need to be configured. Configure RNode interfaces for independent
physical radios, with no enabled IP/shared-instance alternative if the claim is
RF-only. Keep profiles and identity material outside Git.

```sh
.venv/bin/medre-lab doctor
.venv/bin/medre-lab hardware run --pair meshtastic:meshcore --role a
.venv/bin/medre-lab hardware run --pair meshtastic:meshcore --role b
.venv/bin/medre-lab hardware run --pair meshcore:lxmf --role a
.venv/bin/medre-lab hardware run --pair meshcore:lxmf --role b
.venv/bin/medre-lab hardware run --pair meshtastic:lxmf --role a
.venv/bin/medre-lab hardware run --pair meshtastic:lxmf --role b
```

Each command runs one bounded startup and one message per direction, stops after
a failed stage and writes private evidence under `evidence/`. Worker processes
isolate SDK state. Native listener startup, observation and teardown run off the
MEDRE event loop. Endpoint locks prevent overlapping lab runs from owning the
same configured devices. An LXMF receiver is stopped/reaped before its identity
is reused by the sender. Peer teardown interrupts gracefully and falls back to a
bounded kill. A command timeout interrupts the entire owned process group.

Existing channel configuration, matching keys and firmware are operator
prerequisites. The lab does not flash firmware, change radio channels/keys/bonds,
synchronize clocks or reset Bluetooth. Stale clocks or a non-advertising device
are investigation findings, not proof of a firmware defect. Controller numbering
can remain `hci1` after `hci0` is removed.

## Evidence and development

Hardware verdicts include installed versions, available commit provenance,
far-side wire text, unique nonces, event IDs, destination receipts, terminal LXMF
states and maximum event-loop heartbeat delay. Keep raw reports private: they can
contain native identifiers. Publish a sanitized summary separately.

```sh
.venv/bin/pytest -q
.venv/bin/ruff check medre_lab tests lab/tests
```

CI runs Docker-free tests and validates Compose/shell/Python structure. It does
not assert that physical radios or a full Docker campaign were qualified.

See [provenance and migration](docs/provenance.md) for what was imported, what
remains local and the distinction between historical and current evidence.
