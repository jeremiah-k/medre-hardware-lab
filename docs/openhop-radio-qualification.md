# openHop virtual radio qualifier (MEDRE ⇄ openHop, hardware-free)

**Status:** Phase 3A implemented and verified. This profile proves that a
REAL MeshCore packet crosses between two independent openHop repeaters
without any radio hardware.

It is **opt-in**: the default lab rig, its five source pins, and the
`base` / `medre` / `normal` / `openhop` profiles are unchanged.

---

## 1. What this profile is

Three services on one private compose network:

| Service               | Image                                                     | Role                                                                                                                                                        |
| --------------------- | --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `openhop-radio-relay` | `medre-lab/openhop-radio-relay:latest` (lab-owned recipe) | The lab's **virtual radio**: an asyncio TCP service speaking the openHop **Modem** protocol. It holds exactly two client slots (the two repeaters' radios). |
| `openhop-repeater-a`  | `medre-lab/openhop-repeater:latest` (lab-owned recipe)    | openHop repeater, `radio_type: modem_tcp` → the relay, `mode: monitor`. Serves the MeshCore companion protocol over TCP 5000.                               |
| `openhop-repeater-b`  | same image, own config and volume                         | The peer repeater. Own identity, own companion, own SQLite store.                                                                                           |
| `openhop-radio-probe` | `medre-lab/medre-openhop-dev:latest`                      | One-shot proof container carrying the pinned `meshcore==2.3.15` SDK.                                                                                        |

```text
  subscriber (SDK) ──TCP 5000── repeater A ──┐
                                                ├── openhop-radio-relay ── openHop Modem over TCP 5055
  sender    (SDK) ──TCP 5000── repeater B ──┘
```

**Nothing is published to the host.** The relay port and both companion
ports live only on the private `medre-lab-openhop-radio` network.

### The relay is lab-owned

`lab/openhop-radio/openhop_radio_relay.py` is new code in this repo. It
does **not** import, patch or wrap any openHop radio backend: it
implements the wire protocol that the pinned `openhop_core` driver
expects, so the repeaters run their **unmodified** upstream TCP radio
driver against it.

The wire format (from `openhop_core/hardware/protocol_constants.py`):

```text
SYNC (0xAA) | CMD (1B) | LEN (2B LE) | PAYLOAD (0..N B) | CRC (2B LE)
```

The CRC is CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`, no
reflection, no xor-out) over **CMD + LEN + PAYLOAD**; the SYNC byte is
excluded.

### The relay's rules

- **Exactly two clients.** A third connection takes the slot from the
  least recently active client (a short grace period lets a peer whose
  socket already closed be reaped first). The relay is a shared medium
  with two ends.
- **A TX crosses to the peer, never back to the sender.** The sender
  receives `CMD_TX_DONE`; the _other_ client receives `CMD_RX_PACKET`
  with the payload and a fixed signal header (rssi / snr / signal rssi).
- **Commands handled:** `PING` → `PONG`, `AUTH` → `AUTH_OK` (only when a
  token is configured; **no token is configured**, so the relay is open
  and no secret exists), `SET_CONFIG` → `CONFIG_RESP`, `GET_CONFIG`,
  `RX_START` → `RX_STARTED`, `TX_REQUEST` → `TX_DONE` + peer
  `RX_PACKET`, `STATUS_REQ` → `STATUS_RESP`, `NOISE_REQ` →
  `NOISE_RESP`, `CAD_REQUEST` → `CAD_RESP`, `SET_CAD_PARAMS` →
  `CAD_PARAMS_RESP`, `GET_VERSION` → `VERSION_RESP`, anything else →
  `ERROR`.
- **Bounded.** Each client has a bounded outbound queue (a full queue
  drops the frame, as an RF loss would), and the logs carry only the
  payload length and a short digest — never the payload, never a secret.

---

## 2. Run it

The cold start from a fully-down rig is exactly:

```bash
cd <your medre-hardware-lab checkout>

# Once per checkout: fetch the pinned checkouts and generate .env.
uv run --no-sync python -m medre_lab sources fetch
uv run --no-sync python -m medre_lab docker prepare

# Build + bring up the radio rig (generates the per-instance identities).
bash lab/up.sh --openhop-radio

# Prove the packet path.
bash lab/openhop-radio/packet_proof.sh

# Tear the whole lab down.
bash lab/down.sh
```

`lab/up.sh --openhop-radio` generates, into the **gitignored**
`lab/state/` tree, one companion identity key and one credential set per
repeater, and substitutes them into
`lab/config/openhop-radio-repeater.yaml` (the committed contract
template) to produce `lab/state/config/openhop-radio-repeater-{a,b}.yaml`.
**No identity or credential is ever committed.**

The `--openhop-radio` profile is **mutually exclusive** with
`--openhop`, `--with-medre` and `--normal`: the relay holds exactly two
client slots and each companion holds exactly one client, so the radio rig
can never share a rig with another profile.

### The proof owns its single-client precondition

The proof's subscriber must be companion A's **only** client, and the
proof enforces that itself: before connecting, `packet_proof.sh` checks
whether a MEDRE runtime (`medre-lab-medre-openhop-bridge`,
`medre-lab-medre-openhop-fault`) holds companion A and **refuses** —
exit non-zero, before any connection is made — naming the container and
the one-client-per-companion reason. A MEDRE client evicted by the
proof's connect would reconnect within seconds and evict the subscriber
back, so a proof run against a MEDRE-held companion can only measure
zero deliveries. The run order above starts the proof on its own rig,
where no MEDRE exists, so the refusal never fires on the documented
path.

---

## 3. What the proof establishes

`lab/openhop-radio/packet_proof.sh` runs
`lab/tools/openhop_radio_probe.py` (pinned `meshcore==2.3.15`) and asserts:

| Leg              | Assertion                                                                                                               |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Sender handshake | The sender's companion answers `APP_START` → `SELF_INFO` (its own identity, not a shared one).                          |
| Channel send     | A unique channel-0 nonce is accepted by companion B (`EventType.OK`).                                                   |
| Crossing         | Exactly **one** packet crosses the medium (the relay's transmission log over the proof window).                         |
| Reception        | Repeater A receives it exactly **once** (`RX GRP_TXT` in its log over the window).                                      |
| Delivery         | The subscriber at A gets a `CHANNEL_MSG_RECV` whose text is **byte-exact** (`<sender node name>: <nonce>`).             |
| No echo          | **Nothing** is echoed back to B (`deliveries_at_b` is empty).                                                           |
| No repeat        | Repeater A's `mode: monitor` suppresses the repeat: a repeat would have crossed the medium a second time and reached B. |

Evidence lands in `lab/logs/openhop-radio/` (`packet-proof.json`,
`relay.log`, `repeater-a.log`, `repeater-b.log`, `verdict.json`).

### The exact delivered text

openHop prefixes an outgoing **group text** with the sending companion's
node name (`<node name>: <text>`), so the text the subscriber receives
is `"MEDRE-RADIO-b: openhop-radio-proof-<uuid>"`. The probe derives that
expected string from the sender's own `SELF_INFO` rather than guessing
it, and compares it byte-exactly.

### A note on the client-level duplicate count

An openHop companion can hand the **same** queued message to a client
twice: the message is persisted to SQLite _and_ kept in the in-memory
queue, so a client that drains aggressively can see it twice. That is an
**upstream openHop** behaviour, not a property of this profile: the
radio path carried exactly one packet. The proof therefore asserts the
**radio-level** single crossing (relay log + repeater A's reception
count) and _reports_ the client-level duplicate count
(`duplicate_client_events_at_a`) instead of hiding it.

---

## 4. Evidence boundary — READ THIS

### What IS real in this profile

- The packets are **real MeshCore packets**: encrypted by repeater B with
  a real channel secret, transmitted by the real openHop TCP radio
  driver, received and **decrypted** by the real repeater A, and
  delivered by the real companion server to a real SDK client.
- The identities, the companion protocol handshake, the channel-0
  message and the exactness of the delivered text are all genuine.

### What IS SIMULATED

- **The RF medium itself.** The relay is a byte pipe: no LoRa PHY, no
  modulation, no spectrum, no antenna, no range, no duty cycle against
  a real band, no regulatory behaviour, and no propagation model.
- The signal values the relay reports (rssi / snr) are **fixed
  constants**, not measurements.

### Therefore

- This profile is **NOT** evidence of physical RF delivery, of range, of
  airtime behaviour on real hardware, or of regulatory compliance.
- It is **NOT** evidence of a MEDRE delivery either: the MEDRE bridge
  route is **Phase 3B** and is **not** wired here. The proof's clients are
  the pinned SDK, not MEDRE.

### The single-client rule

openHop's companion server accepts **one** client at a time and evicts the
previous one, and the relay accepts exactly **two** clients. Therefore:

- **No** healthcheck, readiness probe or test may TCP-probe the
  companion port or the relay port. Every healthcheck in this profile is
  **process liveness only** (`/proc` scans).
- The probe holds exactly one client per companion.

---

## 5. Phase 2 (`openhop` profile) is untouched

The Phase 2 profile keeps its `radio_type: null` rig: `NullRadio` raises
on every send, so **no** packet can be transmitted there. The radio
profile is a separate opt-in profile with its own configs, volumes,
network and containers; `tests/test_openhop_radio_profile.py` asserts
that the Phase 2 services, their profiles and their configuration are
unchanged.

---

## 6. File map

| Path                                       | What it is                                                         |
| ------------------------------------------ | ------------------------------------------------------------------ |
| `lab/openhop-radio/openhop_radio_relay.py` | The lab-owned virtual radio relay (asyncio, stdlib only).          |
| `lab/openhop-radio/Dockerfile`             | The relay image recipe.                                            |
| `lab/openhop-radio/packet_proof.sh`        | The bounded packet proof and its evidence.                         |
| `lab/config/openhop-radio-repeater.yaml`   | The repeater contract template for the radio profile (no secrets). |
| `lab/tools/openhop_radio_probe.py`         | The bounded proof probe (pinned SDK).                              |
| `lab/tests/test_openhop_radio_relay.py`    | Unit tests: framing, CRC, commands, two-client mapping, no echo.   |
| `tests/test_openhop_radio_profile.py`      | Offline topology assertions for the profile.                       |
