# MEDRE hardware lab

One lab for MEDRE and MMRelay: Docker interop, physical radio routes, and mixed
Docker/radio deployments, plus native service qualification. It imports the
retired interop rig and the bounded native-peer checks used to qualify Meshtastic, MeshCore and LXMF.

The lab owns setup, evidence and resource lifetimes. MEDRE and its providers
continue to own routing, rendering, encoding and transport behavior.

## Choose a mode

| Mode            | What it proves                                                          | Requirements                                                                     |
| --------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Docker `base`   | Two MMRelays, independent virtual meshnets and Matrix                   | Docker Compose with BuildKit additional contexts                                 |
| Docker `normal` | MEDRE/MMRelay shared-room interop, virtual Meshtastic, LXMF counterpart | Same, plus exact source locks                                                    |
| Docker `stress` | Deliberate shared-mesh loop topology                                    | Same; a separate diagnostic profile                                              |
| Native services | Chatmail and SimpleX recipient-side publication campaigns               | Dedicated accounts/profiles; [campaign guide](docs/service-qualification.md)     |
| Hardware        | Two-way ordinary-text routing between two physical transport pairs      | Linux, Python 3.12+, two boards per selected transport                           |
| Mixed           | Docker relay with an explicitly mapped physical serial radio            | [Generic serial overlay](examples/hybrid/README.md); live qualification required |

Hardware checks support any two of Meshtastic, MeshCore and LXMF. A role selects
which endpoint in each configured pair MEDRE owns; the other endpoints are
independent native senders/receivers. Swap roles to exercise every board.

A passing short check means one message per direction was independently observed,
appeared once and has a successful MEDRE receipt. LXMF sends must reach terminal
DELIVERED. It does not claim attachment, restart, soak or full release coverage.

Use this checkout as the single lab workspace. Keep campaign environments and
private operator inputs under ignored paths; see [workspace layout](docs/workspace.md).

## Setup

Use the repository checkout as the working directory. The CLI can also select it
with `--root`. Docker files and scenarios stay in the checkout; they are not
bundled into a separately published Python distribution.

```sh
uv sync --locked --extra dev --python 3.12
uv run --no-sync medre-lab init
uv run --no-sync medre-lab doctor
```

`lab.toml` is local and ignored. Configure only the transports you own. The
example uses placeholders, not the author's device addresses. `doctor` inventories
serial paths, controllers and required executables without transmitting or
changing settings. BLE on Linux normally runs on the host through BlueZ; hardware
mode does not require privileged Docker containers.

## Docker interop

```sh
uv run --no-sync medre-lab sources fetch
uv run --no-sync medre-lab docker up --profile normal
uv run --no-sync medre-lab docker test normal-fanout normal-parity normal-replies normal-reactions normal-lxmf normal-untrusted
uv run --no-sync medre-lab docker down
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

Native service campaigns use the selected provider
harnesses and independent peer observations described in the
[service guide](docs/service-qualification.md); they are separate from the
`hardware run` CLI.

## Physical radio qualification

Install the released transport packages, or editable source revisions when
qualifying unreleased provider changes:

```sh
uv sync --locked --extra dev --extra hardware
# Include the published MeshCore adapter:
uv sync --locked --extra dev --extra hardware --extra meshcore
# Or use a checkout at the exact candidate commit:
# uv pip install --python .venv/bin/python --no-deps -e /path/to/medre-adapter-meshcore
```

Fill in `lab.toml`: stable serial by-id paths; MeshCore BLE addresses, channel and
controller; and two existing private Reticulum profiles/identities. Only selected
transport pairs need to be configured. Configure RNode interfaces for independent
physical radios, with no enabled IP/shared-instance alternative if the claim is
RF-only. Keep profiles and identity material outside Git.

```sh
uv run --no-sync medre-lab doctor
uv run --no-sync medre-lab hardware run --pair meshtastic:meshcore --role a
uv run --no-sync medre-lab hardware run --pair meshtastic:meshcore --role b
uv run --no-sync medre-lab hardware run --pair meshcore:lxmf --role a
uv run --no-sync medre-lab hardware run --pair meshcore:lxmf --role b
uv run --no-sync medre-lab hardware run --pair meshtastic:lxmf --role a
uv run --no-sync medre-lab hardware run --pair meshtastic:lxmf --role b
```

Each command runs one bounded startup and one message per direction, stops after
a failed stage and writes private evidence under `evidence/`. Worker processes
isolate SDK state. Native listener startup, observation and teardown run off the
MEDRE event loop. Endpoint locks prevent overlapping lab runs from owning the
same configured devices. Before arming the first peer receiver, the worker checks
adapter health and waits for any reported initial startup drain to pass. The
wait polls the reported epoch and window, follows initial-setup re-arms and
fails within three times the initial window plus five seconds if it cannot
establish retirement. Verdicts record each adapter's wait or skip reason in
`startup_drain_wait`; native send and observation deadlines remain unchanged.
An LXMF receiver is stopped/reaped before its identity
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
uv run --no-sync pytest -q
.trunk/trunk check --all --no-fix --ci
```

CI runs Docker-free tests and validates Compose/shell/Python structure. It does
not assert that physical radios or a full Docker campaign were qualified.

See [source provenance](docs/provenance.md) for what was imported, what
remains local and the distinction between historical and current evidence.

## Development tooling

Use the committed Trunk launcher; local work and CI share its tool versions:

```bash
.trunk/trunk check --fix --all
.trunk/trunk check --all --no-fix --ci
uv sync --locked --extra dev
uv run --no-sync pytest -q
```

Run `.trunk/trunk upgrade` only for an intentional toolchain refresh; review
the resulting pin changes and repeat both checks before committing.

uv owns `.venv` and `uv.lock`; rerun sync after dependency changes. Select every
needed extra on each sync. CI selects only `dev`, so offline tests do not start
radios or install optional hardware SDKs. Black targets Python 3.12.

Core and all seven adapter distributions have published packages from the 0.1.0 line; medre and medre-adapter-meshtastic additionally publish 0.1.1.
The `hardware` and `meshcore` extras resolve released distributions from the
package index, with exact versions and artifact hashes recorded in `uv.lock`.
The current host hardware lock still records core and Meshtastic 0.1.0;
it does not describe a 0.1.1 qualification. Upgrade the host lock and
requalify it separately from Docker checkout selection.
Local editable overrides are development experiments and do not qualify the
committed lock. A later exact sync restores the locked environment. Qualify
package updates against the campaign's actual installed sources and SDK pins;
package availability alone is not radio evidence.

The deployment source manifest and `medre-lab sources` continue to control Docker
checkouts and bake provenance independently of the host uv environment. Report
both when qualifying a mixed Docker/hardware run. Keep local profiles, identities,
`.zcode/`, state and evidence outside Git. See the core
[installation-source plan](https://github.com/jeremiah-k/medre/blob/main/docs/ops/package-management.md).

`scenarios/config.env` is sourced Bash. Its parameter expansions and overrides
are checked by ShellCheck and a defaults/overrides regression; dotenv formatting
is excluded for that file. Renovate tracks the inline upstream-source metadata
on Trunk pins. Routine history belongs in PR descriptions; release notes are
optional and descriptively named.
