# openHop Docker qualifier (MEDRE ⇄ openHop, hardware-free)

**Status:** Phase 2 implemented and verified. This profile qualifies the
MEDRE ⇄ openHop **protocol** link in Docker with **no radio hardware**.

It is **opt-in**: the default lab rig, its five source pins, and its
`base` / `medre` / `normal` profiles are unchanged.

---

## 1. What this profile is

Two services on a private compose network:

| Service            | Image                                                      | Role                                                                                                                   |
| ------------------ | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `openhop-repeater` | `medre-lab/openhop-repeater:latest` (lab-owned recipe)     | The openHop repeater with `radio_type: null`. Serves the MeshCore companion frame protocol over TCP 5000.              |
| `medre-openhop`    | `medre-lab/medre-openhop-dev:latest` (second MEDRE anchor) | The MEDRE runtime whose **meshcore** adapter talks to that companion, plus the existing **MeshC** meshtastic sim node. |

`medre-openhop` also needs `meshtasticd-c-medre` (the existing MeshC sim
node), which therefore carries the `openhop` profile in addition to
`normal`. The two profiles are mutually exclusive, so exactly one MEDRE
instance owns that node at a time.

### Why a second MEDRE image

The default rig pins **medre 0.1.0** (`fd2927d`), which cannot satisfy
`medre-adapter-meshcore 0.2.0`'s `medre>=0.2.0,<0.3.0` requirement. The
openHop profile therefore carries its **own** pins and its **own** image
anchor (`lab/medre-openhop/Dockerfile`); the default
`lab/medre/Dockerfile` is untouched.

### Pins (`sources.lock.json`)

The profile's sources are the five lock keys in `sources.lock.json`:
`medre-openhop`, `medre-adapter-meshcore`,
`medre-adapter-meshtastic-openhop`, `openhop-core` and
`openhop-repeater`. The lock file is the only place the revisions are
named; this page does not duplicate them.

`lab/openhop-bridge/seed_candidate_sources.sh` materialises the lock
into the gitignored `sources/<key>` checkouts: each checkout is
detached at the lock's exact SHA, taken from the local candidate
repository or from a bundle whose advertised branch points at that
exact SHA. `--check` verifies the state without touching anything and
refuses a dirty checkout.

The pins the rig actually ran on are recorded in the `openhop_sources`
section of `lab/VERSION` (`lab/openhop_pins.py`), which compares the
generated source manifest against the live checkouts; see the
[bridge qualification](openhop-bridge-qualification.md#6-source-pins).
A pin's status is derived from the checkout: `origin-tracking` when the
checkout's cached `refs/remotes/origin/` refs contain the revision,
`local-candidate` otherwise. The classification describes local refs
and does not assert remote publication.

Evidence binds to the content its source selection provides, never to
identifiers: every matrix evidence directory carries a
`source-selection.json` stamp whose selection digest covers the content
of every pinned checkout (`sources/<key>` at the lock's commit) and the
lab tooling. Paths no rig reads are excluded from the digest — CI
scaffolding (`.github/`, `.gitignore`, `.gitlab-ci.yml`, `.trunk/`,
`.circleci/`, the review/editor/lint config files), root `docs/` and
`tests/`, root `*.md` and `LICENSE*`, the lab's own `lab/tests/` and
`sources.lock.json`, and the read-side summary and selection modules;
`.gitattributes` and `.gitmodules` stay in, since checkout attributes
and submodule maps decide what a materialised checkout produces — and
a directory refuses cases produced under different content. A stamp
that records identifiers only is reconstructed from the recorded lab
revision and compared by content; it is never rewritten.
`--verify-selection` carries a recorded pass forward when the content
is unchanged and appends its record to `selection-carry-forward.json`;
a changed pin, a changed tooling digest or a different profile names
itself and requires a new directory. A summary counts as a pass only
when every planned case reports a pass.

---

## 2. Setup

```bash
cd <your medre-hardware-lab checkout>

# Fetch the pinned checkouts (the openHop ones are new).
uv run --no-sync python -m medre_lab sources fetch

# Generate .env (source paths + build refs) and the source manifests.
uv run --no-sync python -m medre_lab docker prepare
```

`prepare` writes two manifests:

- `lab/state/sources.json` — the **default** rig. Unchanged content.
- `lab/state/sources-openhop.json` — the **openHop** rig. Checked
  separately, and only while the openHop profile is up (the default
  scenario suite inspects the default manifest with live container
  inspection, so the openHop containers must not be in it).

---

## 3. Run

The cold start from a fully-down rig is exactly the sequence above plus
one command — fetch the sources, run `prepare`, then bring the profile
up:

```bash
# Bring up the openHop profile (builds the two images on first run).
bash lab/up.sh --openhop

# Or through the CLI (it runs the SAME script with prepare's environment):
uv run --no-sync python -m medre_lab docker up --profile openhop
```

`lab/up.sh --openhop` starts the profile's three services **by name**
(`meshtasticd-c-medre`, `openhop-repeater`, `medre-openhop`). The profile
needs neither Synapse nor the mmrelay relays, and neither it nor the CLI
command touches them.

**A raw `docker compose --profile openhop up -d` is not this profile's
bring-up.** Without service names it also starts every default-profile
service, and `mmrelay-a`, `mmrelay-b` and `lab-tools` depend on a
**healthy** synapse; a synapse volume without a generated `homeserver.yaml`
never becomes healthy (the synapse image requires its config to be
generated explicitly — the default rig's `lab/up.sh` runs
`compose run synapse generate` for exactly this reason), so the raw
command aborts with `dependency failed to start: container
medre-lab-synapse is unhealthy`. The openHop profile does not need that
fix or that command: use `bash lab/up.sh --openhop`.

`--openhop` is **mutually exclusive** with `--normal` and `--with-medre`:
both MEDRE topologies run `@medre:lab.test` and own the same sim node.

### What bring-up generates

`lab/up.sh --openhop` generates the openHop configs into
`lab/state/config/` from the committed templates in `lab/config/`:

- `openhop-repeater.yaml` — the repeater config with the identity and
  credential placeholders substituted.
- `medre-openhop.yaml` — the MEDRE config (no secrets; copied verbatim).

The **identities and credentials** are generated into
`lab/state/openhop/secrets.env` (mode 0600, gitignored) and reused on
later bring-ups, so the companion keeps the same identity across restarts.
**No identity or credential is ever committed.**

---

## 4. The lifecycle check

```bash
bash lab/openhop/lifecycle_check.sh
```

or, as an opt-in scenario:

```bash
bash scenarios/scenario-openhop-qualifier.sh
```

The scenario is deliberately **not** in `ALL_SCENARIOS` and **SKIPs**
(exit 2) when the openHop profile is not up.

### What it proves

| Leg                          | Evidence                                                                                                                                                                                                                                      |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Container startup            | Both containers reach their healthchecks.                                                                                                                                                                                                     |
| Protocol handshake           | A dedicated probe (pinned upstream SDK `meshcore==2.3.15`) does APP_START → SELF_INFO → channel read, then a clean disconnect.                                                                                                                |
| MEDRE adapter connection     | MEDRE's own logs report `MeshCoreSession openhop-companion started (mode=tcp, connected=True)`, and its startup banner carries the companion's identity (`device name MEDRE-OPENHOP-QUAL · pubkey aabfaf3f960a`) — i.e. MEDRE read SELF_INFO. |
| Reconnect                    | The repeater is restarted underneath the running MEDRE; MEDRE's adapter logs `reconnected successfully` (the reconnect lines are scoped to the post-restart log window).                                                                      |
| Identity and default channel | The probe compares the public key and channel-0 name across restart. The backlog matrix separately checks persisted queue survival.                                                                                                           |
| Clean stop                   | MEDRE stops on request (compose exit 0, container not running) and logs `MEDRE stopped`.                                                                                                                                                      |

Evidence lands in `lab/logs/openhop/` (`verdict.json`, the probe JSONs,
the MEDRE and repeater logs).

### Evidence boundary — READ THIS

**This profile proves the PROTOCOL handshake and the adapter lifecycle. It
proves nothing about delivery.**

The repeater runs with `radio_type: null`, so its `NullRadio` **raises on
every send**. No packet can be transmitted by this profile, and no
assertion here may be read as a delivery claim. "The adapter connected" is
a protocol fact, never a delivery fact.

Phase 3 (virtual packets) needs its own evidence rules and is out of scope
here. See §6.

### The single-client rule

openHop's companion server accepts **one client at a time** and **evicts
the previous one**. Therefore:

- **No** healthcheck, readiness probe or test may TCP-probe port 5000
  while MEDRE holds the companion. Both openHop healthchecks are
  **process-liveness only** (`/proc` scans), never TCP.
- The protocol probe runs **only while MEDRE is stopped**. The lifecycle
  check stops MEDRE before each probe and restarts it afterwards, and it
  restores the rig to the state it found it in.

---

## 5. Troubleshooting

**`docker compose --profile openhop config` fails on a missing variable**
The `openhop` services need `${OPENHOP_CORE_SRC}`, `${OPENHOP_REPEATER_SRC}`,
`${MEDRE_OPENHOP_SRC}`, `${MEDRE_MESHCORE_SRC}` and
`${MEDRE_MESHTASTIC_OPENHOP_SRC}`. Run `medre-lab docker prepare`; the CI
job exports the same variables explicitly.

**The repeater container is unhealthy**
The healthcheck is a `/proc` scan for the repeater process. Read
`lab/logs/openhop-repeater-*.log`. A companion that fails to bind usually
means the config's `identities.companions[].settings.tcp_port` is taken or
the identity key is malformed.

**MEDRE logs `initial connection unavailable`**
The repeater is not up yet, or the companion port is unreachable. MEDRE
recovers on its own (1s → 30s backoff) — that is the reconnect path the
lifecycle check exercises.

**The lifecycle check fails at "protocol probe produced no output"**
The probe runs as a one-shot container of the `medre-openhop` image. Check
`lab/logs/openhop/probe-*.json.err`; a failure there is usually the
companion not being up yet.

**"MEDRE did not report a CONNECTED MeshCore session"**
MEDRE's meshcore adapter could not reach the companion. Confirm the
repeater is healthy and that nothing else is holding port 5000 on the
`openhop` network.

**Channel 0 shows `0/0 channel(s)` restored**
Expected on a **first** start: nothing was persisted yet. The repeater
auto-provisions public channel 0 ("Public") for every new companion. After
a restart the log reports `1/1 channel(s)` restored — that is the
persistence evidence.

---

## 6. Teardown

```bash
bash lab/down.sh
```

`down.sh` downs the project, force-removes survivors, removes leftover
networks, and **verifies** that zero project containers and zero project
networks remain. State volumes are **kept** (bring-up is resumable and the
companion identity survives); `lab/reset.sh` wipes them.

---

## 7. Phase 3 notes (documented, NOT enabled)

Phase 3 (virtual packet path) needs two more switches. They are
**documented in `lab/config/openhop-repeater.yaml`** and deliberately
**not** flipped here, because Phase 2 must not be able to transmit:

1. `repeater.mode: "no_tx"` → `"forward"` — `no_tx` turns **all** TX off.
2. `mesh.unscoped_flood_allow` / `mesh.global_flood_allow`: `false` →
   `true` — unscoped flooding is denied in this template.

The MEDRE side has its own switch: the `openhop-companion` route in
`lab/config/medre-openhop.yaml` is `enabled: false`.

**Phase 3 must provision its own test channel** (a non-zero index with the
lab's channel secret). Channel 0 is the repeater's auto-provisioned
default public channel, not the lab's test channel.

There is **no virtual RF transport in openHop** (`NullRadio.send` raises,
`WsRadio.send` fails, `TCPLoRaRadio` is a client of a real modem), so
Phase 3 needs a lab-owned modem-protocol relay at that boundary.

---

## 8. File map

| Path                                      | What it is                                                                                          |
| ----------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `lab/openhop/Dockerfile`                  | Lab-owned openHop image recipe (installs the pinned local `openhop_core`, then `openhop_repeater`). |
| `lab/openhop/bake_pin.py`                 | Stamps the baked provenance (source context + commit + installed version) into the image.           |
| `lab/medre-openhop/Dockerfile`            | The second MEDRE image (0.2.3 + meshcore 0.2.0 + meshtastic 0.2.1; no matrix/lxmf).                 |
| `lab/openhop/lifecycle_check.sh`          | The bounded lifecycle check.                                                                        |
| `lab/tools/openhop_probe.py`              | The bounded MeshCore protocol probe (pinned SDK).                                                   |
| `lab/config/openhop-repeater.yaml`        | Repeater config template (no secrets).                                                              |
| `lab/config/medre-openhop.yaml`           | MEDRE config template (no secrets).                                                                 |
| `scenarios/scenario-openhop-qualifier.sh` | The opt-in scenario (SKIPs without the profile).                                                    |
| `tests/test_openhop_profile.py`           | Offline topology/profile-exclusivity tests.                                                         |
