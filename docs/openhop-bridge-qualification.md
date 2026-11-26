# openHop bridge qualifier (MEDRE ⇄ openHop radio overlay ⇄ MeshC)

## Which revisions the recorded runs used

A recorded run is bound to the content its selection provided to the
rig, never to identifiers: commit IDs are provenance. A re-pin to
identical content, a documentation change or a test change carries a
recorded pass forward through the aggregator's `--verify-selection`,
which appends its record beside the stamp
(`selection-carry-forward.json`) and leaves the stamp untouched. A
change to the content the rig builds or mounts, to the rig tooling or
to the profile means a new evidence directory and a fresh
protocol-simulated run.

**What this profile measured.** In each recorded successful run, ONE
nonce-bearing message per direction reached the client: the arrival count
equalled the distinct arrival count and the client-level duplicate counter
was zero. That is a count observed in a bounded, successful test window on a
virtual medium.

**What it did NOT measure.** The openHop companion protocol has no
application-level acknowledgement of durable admission. A crash between the
frame enqueue and the row delete can replay a message; a crash between the
delete and the receipt can lose it. The openHop core/repeater fixes move the
transaction boundary to local frame admission — they are not a transactional
or exactly-once delivery guarantee, and the upstream protocol cannot be made
one. The counters asserted here (arrival count, distinct arrival count,
duplicate client events) are therefore regression counters for the observed
path, not a crash-recovery proof.

**Status:** Phase 3B implemented and verified. This profile proves that
MEDRE bridges **real** MeshCore traffic in **both** directions between the
openHop radio overlay and the MeshC sim mesh, and that in the observed
run each bridged message reached the client **once**, with no duplicate
client delivery.

It is **opt-in**: the default lab rig, its five source pins, and the
`base` / `medre` / `normal` / `openhop` / `openhop-radio` profiles are
unchanged.

---

## 1. What this profile is

Five services plus one one-shot proof container:

| Service                | Role                                                                                                                                                                                                 |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `openhop-radio-relay`  | The lab's **virtual radio**: an asyncio TCP service speaking the openHop **Modem** protocol. Two client slots: repeater A's radio and repeater B's radio.                                            |
| `openhop-repeater-a`   | openHop repeater, `radio_type: modem_tcp` → the relay, `mode: monitor`. Serves the MeshCore companion over TCP 5000. **MEDRE is its only companion client.**                                         |
| `openhop-repeater-b`   | The peer repeater (same image, own identity, own volume, own companion). **The virtual MeshCore peer is its only companion client.**                                                                 |
| `meshtasticd-c-medre`  | The MeshC sim node MEDRE's meshtastic adapter talks to (TCP 4403). MEDRE is its only API client.                                                                                                     |
| `meshtasticd-c-peer`   | The MeshC sim node the independent observer/injects on (TCP 4403). The proof container is its only API client.                                                                                       |
| `medre-openhop-bridge` | The MEDRE runtime: `meshcore` adapter → `openhop-repeater-a:5000` (channel 0), `meshtastic` adapter → `meshtasticd-c-medre:4403` (channel 0), and the **bidirectional** route `openhop-a ↔ radio-c`. |
| `openhop-bridge-probe` | One-shot proof container: the virtual MeshCore peer (pinned SDK `meshcore==2.3.15`) on companion B, plus the independent MeshC observer/injector on `meshtasticd-c-peer`.                            |

```text
  virtual MC peer (SDK) --TCP 5000--> openhop-repeater-b --+
                                                          | modem relay (lab)
  MEDRE (meshcore) --TCP 5000--------> openhop-repeater-a --+
                                                          | TCP 4403
                              meshtasticd-c-medre -- MeshC -- meshtasticd-c-peer
```

**Nothing is published to the host.** The relay, both companion ports and
the MeshC sim nodes live only on the private `medre-lab-openhop-radio` and
`medre-lab-mesh-c` networks.

### The route is enabled ONLY here

`lab/config/medre-openhop-bridge.yaml` carries the only enabled
bidirectional openHop↔MeshC bridge in the repository:

```yaml
routes:
  openhop-bridge:
    source_adapters: ["openhop-a"]
    dest_adapters: ["radio-c"]
    directionality: bidirectional
    enabled: true
    source_channel: "0"
    dest_channel: "0"
```

The Phase 2 `openhop` profile keeps its own route **disabled**
(`lab/config/medre-openhop.yaml`), and the default lab rig has no such
route at all.

Both adapters carry an **empty `relay_prefix`** so the relayed bytes stay
byte-exact: what MEDRE forwards is exactly the canonical body.

---

## 2. Run it

The cold start from a fully-down rig is exactly:

```bash
cd <your medre-hardware-lab checkout>

# Once per checkout: fetch the pinned checkouts and generate .env.
uv run --no-sync python -m medre_lab sources fetch
uv run --no-sync python -m medre_lab docker prepare

# Bring up the bridge rig (generates the per-instance identities, configures
# the MeshC channels, starts the relay, both repeaters, both MeshC nodes and
# MEDRE).
bash lab/openhop-bridge/bridge_up.sh

# Prove both directions and write the evidence.
bash lab/openhop-bridge/bridge_proof.sh

# Tear the whole lab down.
bash lab/down.sh
```

`bridge_up.sh --skip-build` reuses the images built earlier; without it
the two images are rebuilt from the pinned checkouts. `bridge_up.sh`
dies with a clear message when `prepare` has not run (it needs `.env`
and the source manifests).

The matrices run against the up bridge rig, one bounded invocation at a
time, and `lab/down.sh` takes everything down afterwards (including the
fault splice and the fault MEDRE the fault matrix starts):

```bash
# Backlog/persistence cases c1-c6. They manage the bridge MEDRE
# themselves (c1/c2/c5/c6 stop it so a backlog can persist; c5 runs the
# fault MEDRE behind the fault splice and restores the bridge MEDRE at
# the end).
python3 lab/tools/openhop_backlog_matrix.py                 # all six cases
python3 lab/tools/openhop_backlog_matrix.py --case c5       # one case

# Ordering cases o1-o3.
python3 lab/tools/openhop_ordering_matrix.py                # all cases
python3 lab/tools/openhop_ordering_matrix.py --case o1      # one case

# Fault matrix (starts the fault splice itself; splices the fault MEDRE
# to companion A, so the bridge MEDRE must not hold it — the driver
# stops it per case).
bash lab/openhop-radio/fault_matrix.sh                      # all ten cases
bash lab/openhop-radio/fault_matrix.sh --case reply_stall_drain
```

The candidate source pins (see §6) are seeded by
`lab/openhop-bridge/seed_candidate_sources.sh`; it is idempotent and
`--check` verifies without touching anything.

---

## 3. What the proof establishes

| Leg                                        | Assertion                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Direction 1 (MC peer → MEDRE → MeshC peer) | A fresh channel-0 nonce submitted through companion B crosses the virtual radio to repeater A, is decoded by MEDRE's meshcore adapter into **one** canonical event, is relayed over the enabled route to the meshtastic adapter, and reaches the **independent** observer on `meshtasticd-c-peer` **once**, with byte-exact text.                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Direction 2 (MeshC peer → MEDRE → MC peer) | A fresh channel-0 nonce injected at `meshtasticd-c-peer` reaches MEDRE over MeshC, is relayed by the **reverse** route leg to the meshcore adapter, is transmitted by repeater A, and reaches the virtual MeshCore peer on companion B **once**, with byte-exact text.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| No duplicate (bridge level)                | MEDRE's own mapping store holds **one** canonical event per direction, one delivery outbox row per direction and one `sent` receipt per direction.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| No duplicate (radio level)                 | Over the proof window the relay forwarded **two** transmissions (one per direction) and each repeater logged **one** group-text reception. The medium carried each bridged packet once.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| No echo                                    | Companion B received **nothing** back during direction 1 (the repeater's `monitor` mode suppresses the repeat).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| No duplicate (client level)                | **Zero** client-level duplicate deliveries in either direction. The openHop companion could hand a client the **same queued message twice** (it kept the message in its in-memory queue and in SQLite at the same time); that race (D5) is **pinned** in the candidate sources this profile runs on, so a duplicate is a **regression** and the proof **fails** on it. Both counters are asserted: `duplicate_client_events == 0` and `arrival_count == distinct_arrival_count`. The probe drains the queue the way a **real client** does, so the count is the count the companion actually delivered — see [the strict assertion found a duplicate, and it was the probe's fault](#the-strict-assertion-found-a-duplicate--and-it-was-the-probes-fault). |
| Fresh nonces                               | The two directions use different nonces; neither is replayed.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Native ids                                 | Each evidence file carries MEDRE's canonical event id, its native message id and native channel id, plus the observer-side native ids (the MeshCore packet id / the MeshC packet id) and the delivery receipt id.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |

### The latest run

The Phase 3B evidence was re-run against the **strict** assertions (zero
duplicate client events, `arrival_count == distinct_arrival_count` in
both directions) with **fresh nonces**:

- **Direction 1** — 1 arrival, 1 distinct, **0 duplicate client events**, byte-exact.
- **Direction 2** — 1 arrival, 1 distinct, **0 duplicate client events**, byte-exact
  (openHop's wire prefix accounted for).
- **Client commands vs frames** — 3 GET_MSG commands issued, **1** frame received:
  the companion answered the bridged message **once**.
- **Fresh nonces** — yes, the two directions differ.
- **Echo back to companion B** — 0 messages.
- **Radio path** — 2 relay transmissions, 1 reception per repeater.
- **MEDRE's store** — 2 canonical events, 2 outbox rows, 2 `sent` receipts.
- **Verdict** — **PASS**.

The same run repeated **20 times** (fresh nonces each time) passed every
time with the same counters — 1 arrival, 1 distinct, 0 duplicate client
events, 3 GET_MSG commands, 1 frame per direction.

Evidence lands in `lab/logs/openhop-bridge/` — raw observations stay
outside Git by repo policy (`docs/workspace.md`).

### The strict assertion found a duplicate — and it was the PROBE's fault

Turning the client-level duplicate from a reported number into a **failed
assertion** exposed a duplicate delivery. The first hypothesis was a
defect in the openHop candidates; the evidence says otherwise.

The old probe drained the companion's queue with a **polling loop** of
`get_msg(timeout=2.0)`. A `get_msg` that times out leaves its GET_MSG
command **pending at the companion**: the SDK gives up waiting, but the
command was already sent. The loop then issued the next GET_MSG, so the
companion could end up with **two pending GET_MSG commands**. When the
bridged message arrived, the companion served it to **both** commands —
once from the memory queue, once from the persisted row — and the client
saw the same text twice, ~1.8 ms apart.

The SDK's own **real-client** path does not do this:
`start_auto_message_fetching()` fetches when the companion announces
that messages are waiting and calls `get_msg()` with **no timeout**, so
no command is ever left pending.

The probe now uses that real-client path — auto message fetching plus a
dispatcher subscription — and counts both sides so the evidence
separates the two possibilities:

| Counter                 | Meaning                                     |
| ----------------------- | ------------------------------------------- |
| `get_msg_commands`      | the GET_MSG commands the client issued      |
| `max_in_flight_get_msg` | how many of those were in flight at once    |
| `frames_received`       | the frames the companion actually sent back |

**Sweep result (fresh nonces each run, the SDK path with no extra
lock): 10 of 10 completed runs passed**, every one with the same
counters — 1 arrival, 1 distinct, **0 duplicate client events**, 3
GET_MSG commands, `max_in_flight_get_msg` = 1, 1 frame received. The
SDK's auto-fetch path never had two GET_MSG commands in flight on this
rig, so no extra serialization is needed.

Supporting evidence from the companion itself during a run: repeater B
logged **exactly one** `RX GRP_TXT`, the persisted
`companion_messages` table was **empty** afterwards (the row was
written and deleted, i.e. the D5 delete path ran and found a packet
hash), and there was **no** "delivered message has no packet hash"
warning. The D5 fix is doing its job; the old probe was asking twice.

**Conclusion:** the duplicate was a **probe artefact**, not a defect in
the openHop candidates and not in the bridge. The strict assertion is
kept — it is what surfaced the artefact — and the probe now measures the
way a real client behaves, so the number it reports is the number the
companion actually delivered.

A GET_MSG serialization lock is available behind
`OPENHOP_BRIDGE_GET_MSG_LOCK=1` for clients that want the
single-consumer guarantee explicitly; it is **off by default**, so the
SDK's own path is what the proof exercises.

### What a failed run leaves behind

A failed run must not leave the previous run's verdict behind. The proof
therefore:

- clears the artifacts it is about to rewrite before it starts;
- writes the evidence and the verdict **before** it fails, so the
  verdict says `"pass": false` and lists the failures;
- collects the client-level, MEDRE-store and radio-path failures
  together and reports them all at the end, so a failed run's evidence
  is complete rather than truncated at the first problem.

### The exact bytes and the openHop node-name prefix

openHop stamps the **sending** companion's node name onto an outgoing
group text. The two directions therefore carry different prefixes, and
the proof accounts for both:

- **Direction 1** — the sender is the virtual peer on companion **B**, but
  MEDRE's meshcore codec lifts the wire prefix off the body (it keeps the
  raw wire text in the native metadata as `wire_text`), so the MeshC
  observer sees the **bare nonce**.
- **Direction 2** — the sender is MEDRE, and repeater **A** stamps _its_
  companion node name on the wire, so the virtual peer on companion B sees
  `"MEDRE-RADIO-a: <nonce>"`.

The expected prefix is read from the generated repeater config (never
guessed in code) and passed to the probe through
`OPENHOP_BRIDGE_EXPECT_PREFIX`.

### Evidence files

`lab/logs/openhop-bridge/`:

| File                                                                        | Content                                                                                                                                    |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `evidence-mc-to-mesh.json`                                                  | Direction 1: nonce, send result, observer bytes, timestamps, source/destination channel, native ids, MEDRE canonical event id, echo count. |
| `evidence-mesh-to-mc.json`                                                  | Direction 2: the same, plus the openHop wire prefix that was accounted for.                                                                |
| `verdict.json`                                                              | The per-run verdict (arrival counts, echo count, fresh-nonce flag).                                                                        |
| `medre-state.json` / `medre-state-summary.json`                             | MEDRE's own mapping store for the proof window: canonical events, native message refs, delivery receipts, delivery outbox, routing facts.  |
| `bridge-proof.json`                                                         | The raw probe output.                                                                                                                      |
| `medre-openhop-bridge.log`, `repeater-a.log`, `repeater-b.log`, `relay.log` | Container logs for the proof window.                                                                                                       |

Every evidence file is labelled **`"label": "protocol-simulated"`**.

### A note on the client-level duplicate

The **first** Phase 3B run observed the bridged text **twice** at the
virtual peer on companion B (about 1 ms apart). The bridge was not at
fault then: the relay forwarded the packet **once**, repeater B received
it **once**, and MEDRE had created and delivered exactly **one** canonical
event. The second delivery came from openHop's companion serving the same
queued message to its client twice — the upstream behaviour Phase 3A
already documents.

That upstream behaviour is now **pinned** by the revisions this
profile runs on (openhop_core `fix/companion-frame-admission` — a frame
is admitted only into a live client transport; openhop_repeater
`fix/companion-queue-ownership` — the queue is delivered oldest first
across the memory and persisted queues, and a retired row can only land
in the client that is still attached). A duplicate client delivery
is therefore a **regression**, not a known upstream quirk, and the proof
now **fails** on it instead of reporting it:

- `duplicate_client_events` must be **0** in both directions;
- `arrival_count` must equal `distinct_arrival_count` in both directions.

Both assertions live in `lab/tools/openhop_bridge_check.py`, which the
proof script calls and the offline unit tests
(`lab/tests/test_openhop_bridge_check.py`) exercise directly, so the
single-delivery contract is pinned by the test suite and not only by a
live Docker run.

The probe also **settles** after the first arrival (5 s by default,
`--settle`) before it counts, because a duplicate delivery arrives within
milliseconds of the first one; counting immediately would let a late
duplicate slip through unnoticed.

**The assertion found a duplicate, and it was the probe's fault** — see
[above](#the-strict-assertion-found-a-duplicate--and-it-was-the-probes-fault).
The probe's polling drain was stacking GET_MSG commands at the
companion; the real-client drain does not, and the duplicate is gone.

### The fault matrix and what each case asserts

`lab/openhop-radio/fault_matrix.sh` splices a fault-injecting TCP proxy
between MEDRE and companion A and measures the receive pump against a
slow, silent, or closed link. Each case asserts exactly what the adapter
spec promises, and a bounded run reports the counts it measured:

| Case                | Fault                                                                                                               | What a successful run asserts                                                                                                                                                                                                                                                                                                                           |
| ------------------- | ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `latency-under`     | every reply delayed 0.9 s (below the 2 s fetch timeout)                                                             | every message delivered, no reconnect churn                                                                                                                                                                                                                                                                                                             |
| `latency-over`      | every reply delayed 4 s (above the fetch timeout)                                                                   | nothing consumed; delivery or preservation accounts for every accepted message; no idle-window retirement                                                                                                                                                                                                                                               |
| `repeated-waiting`  | twenty notifications in a row                                                                                       | every message delivered once, no duplicates                                                                                                                                                                                                                                                                                                             |
| `reconnect`         | mild delay                                                                                                          | every message delivered without churn                                                                                                                                                                                                                                                                                                                   |
| `reply-stall`       | time window (45 s): every server-to-client frame swallowed, on every connection inside it                           | nothing consumed while the window holds; probe retirements of the silent link are the designed outcome; reconnect attempts continue on backoff without success; after the window ends the client reconnects and everything queued is delivered                                                                                                          |
| `mid-fetch-close`   | time window (45 s): the connection is closed on the first in-flight request, re-armed per connection for the window | same contract as `reply-stall`; nothing is popped into a connection the splice is about to close                                                                                                                                                                                                                                                        |
| `saturation`        | 60-message backlog behind a 60 s reply-stall window                                                                 | designed probe retirements; nothing consumed; the whole backlog delivered once the window ends                                                                                                                                                                                                                                                          |
| `reply-stall-drain` | 45 s reply-stall window armed while a backlog drain is in flight (at the first observed delivery)                   | at most the one in-flight message consumed; every other accepted message delivered once; no reconnect succeeds while the window holds; recovery follows the spec's branches - a survived silence resumes within one probe interval of the window's end, a longer silence retires and recovery follows the reconnect backoff plus one gated probe budget |
| `selective-drop`    | per-connection budget: the probes answered, message frames swallowed                                                | the spec's documented worst case: at most one consumed message per answered liveness probe beyond the unproven first fetch (reported per run), every other accepted message delivered or preserved, zero duplicates                                                                                                                                     |
| `idle-blackhole`    | both directions of an up, idle link blackholed, no close                                                            | exactly one idle-link probe retirement inside its measured window; backoff continues without success; everything queued during the fault delivered once after the link returns                                                                                                                                                                          |

The companion protocol has no application-level acknowledgement of
durable admission, so none of these assertions is a delivery guarantee:
each is a count observed in a bounded, protocol-simulated window. The
backlog matrix's c5 kill case (`c5-kill-mid-drain-one-in-flight`) makes
the stop-and-wait bound concrete and deterministic: the fault splice
withholds replies from the first client-to-server request it sees after
the arm, so the companion pops exactly the backlog's FIRST message whose
frame never reaches MEDRE; the case holds that state — the companion's
queue must stop shrinking for longer than two fetch windows (2 s each)
with nothing delivered — kills MEDRE inside it, clears the fault, and
restarts. Exactly that first message is missing afterwards, every other
accepted message is delivered exactly once, nothing is preserved at the
companion, and the evidence records `in_flight_at_kill: 1`.

---

## 4. Evidence boundary — READ THIS

### What IS real in this profile

- The MeshCore packets are **real**: encrypted by the sending repeater
  with the channel secret, transmitted by the real openHop radio driver,
  received and decrypted by the real repeater, and delivered by the real
  companion server to a real SDK client.
- MEDRE's decode, its canonical event, its route expansion, its delivery
  and its receipts are the real MEDRE code paths against the pinned
  candidate sources.

### What IS SIMULATED

- **The RF medium itself.** The relay is a byte pipe: no LoRa PHY, no
  modulation, no spectrum, no antenna, no range, no duty cycle against a
  real band, no regulatory behaviour and no propagation model.
- The signal values the relay reports (rssi / snr) are fixed constants.

**Therefore** this profile is **NOT** evidence of physical RF delivery,
range, airtime behaviour on real hardware or regulatory compliance. It is
evidence of **protocol-level bidirectional bridging**.

---

## 5. The single-client rule

openHop's companion server accepts **ONE** client at a time and **evicts**
the previous one, and meshtasticd force-closes the previous TCP API
client on every new connection. Therefore:

- MEDRE is the **only** client of companion A; the virtual MeshCore peer
  is the **only** client of companion B.
- **No** healthcheck, readiness probe or test may TCP-probe port 5000
  while a client is attached. Every healthcheck in this profile is
  **process liveness only** (`/proc` scans).
- The proof container holds exactly one client on companion B and one API
  client on `meshtasticd-c-peer`; MEDRE holds one client on companion A
  and one API client on `meshtasticd-c-medre`.
- The MeshC channel configuration runs **before** MEDRE starts, so the
  meshtasticd API client churn never lands on a live MEDRE session.

---

## 6. Source pins

The bridge runs on the five sources selected by `sources.lock.json`.
Every pin the rig actually ran on, and its status, is recorded in the
`openhop_sources` section of `lab/VERSION` (written by
`lab/openhop_pins.py` from the source manifest **and** the live
checkouts). The lock file, the run's `lab/VERSION` section and each
evidence directory's `source-selection.json` are the pin record; this
page does not duplicate the revisions.

A pin's status is derived from the checkout, not asserted by hand:
`origin-tracking` when the checkout's cached `refs/remotes/origin/`
refs contain the revision, `local-candidate` otherwise. The
classification describes local refs; it does not assert remote
publication.

`lab/openhop-bridge/seed_candidate_sources.sh` materialises the lock
into the gitignored `sources/` checkouts: each checkout is detached at
the lock's exact SHA, taken from the local candidate repository or from
a bundle whose advertised branch points at that exact SHA. `--check`
verifies the state without touching anything and refuses a dirty
checkout.

Evidence binds to the content its source selection provides, never to
identifiers: every matrix evidence directory carries a
`source-selection.json` stamp whose selection digest covers the content
of every pinned checkout (`sources/<key>` at the lock's commit) and the
lab tooling. Paths no rig reads are excluded from the digest: CI
scaffolding (`.github/`, `.gitignore`, `.gitlab-ci.yml`, `.trunk/`,
`.circleci/`, the review/editor/lint config files), root `docs/` and
root `tests/`, root `*.md` and `LICENSE*`, the lab's own `lab/tests/`
and `sources.lock.json` (its entries are compared by content), and the
read-side modules `lab/tools/openhop_case_summary.py` and
`lab/openhop_selection.py` — the summary is a pure function rebuilt
from the case files whenever it is read, so a change to the aggregation
or the fence is always in effect and cannot leave a stale judgement
behind. Checkout attributes and submodule maps (`.gitattributes`,
`.gitmodules`) stay in the digest: both are read when the seeder
materialises the working tree, deciding the bytes the Docker builds
consume and the sources a submodule names. A summary counts as a pass
only when every planned case reports a pass.

A stamp that records identifiers only (lab revision, lock digest,
profile) is reconstructed from the recorded lab revision and compared
by content; it is never rewritten. `--verify-selection` carries a
recorded pass forward when the content is unchanged and appends its
record to `selection-carry-forward.json`; a changed pin, a changed
tooling digest or a different profile names itself and requires a new
evidence directory.

### The version record

`lab/VERSION` is the lab's runtime version record. Each rig **replaces**
its own section (via `lab/version_record.py`), so the record carries
exactly one section per rig and never a stale copy from an earlier
bring-up; `lab/version_record.py compact` drops the duplicate keys older
script versions left behind.

The bridge's own section (`openhop_bridge`) records the **image IDs**
that actually ran, the **build args** the images were built with, the
topology, the route and the evidence paths — so the record ties the
evidence to the exact binaries it came from.

---

## 7. Troubleshooting

**`bridge_up.sh` fails with "did not become healthy"**
The repeater's healthcheck is a `/proc` scan for the `repeater.main`
process. Read `lab/logs/openhop-bridge/repeater-*.log`; a companion that
fails to bind usually means the config's `identities.companions[].settings.tcp_port`
is taken or the identity key is malformed.

**MEDRE logs `initial connection unavailable`**
Repeater A is not up yet, or its companion port is unreachable. MEDRE
recovers on its own (1s → 30s backoff) — that is the reconnect path.

**The probe fails with "the nonce was never delivered"**
Check the relay log (`lab/logs/openhop-bridge/relay.log`): a relay with no
"transmitted" line means the packet never left the sending repeater. A
relay transmission without an arrival at the observer usually means the
route is disabled or the channel index does not match (`source_channel` /
`dest_channel` must both be `"0"`).

**The MeshC observer sees nothing but MEDRE's log shows the event**
The MeshC nodes must share the channel-0 secret. `bridge_up.sh` configures
both nodes from `lab/config/meshnet.env`; if the nodes were reconfigured
by hand afterwards, re-run the bring-up.

**`MEDRE recorded N sent receipts, expected 2`**
More than one delivery per direction: the store is scoped to the proof
window, so a leftover delivery from an earlier run inside the window
would show up. Check `medre-state-summary.json` for the event ids.

---

## 8. Teardown

```bash
bash lab/down.sh
```

`down.sh` downs the project, force-removes survivors, removes leftover
networks, and **verifies** that zero project containers and zero project
networks remain. State volumes are **kept** (bring-up is resumable and the
companion identities survive); `lab/reset.sh` wipes them.

---

## 9. File map

| Path                                           | What it is                                                                                        |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `lab/openhop-bridge/bridge_up.sh`              | The bridge bring-up: identities, configs, MeshC channels, services, health waits.                 |
| `lab/openhop-bridge/bridge_proof.sh`           | The bounded two-direction proof and its evidence.                                                 |
| `lab/openhop-bridge/seed_candidate_sources.sh` | Local-candidate source seeding (`--check` verifies).                                              |
| `lab/tools/openhop_bridge_probe.py`            | The probe: virtual MeshCore peer + independent MeshC observer/injector.                           |
| `lab/tools/openhop_bridge_check.py`            | The strict single-delivery assertions the proof enforces (shared with the unit tests).            |
| `lab/tools/openhop_bridge_medre_state.py`      | Reads MEDRE's own mapping store (canonical events, native ids, receipts) read-only.               |
| `lab/openhop_pins.py`                          | Derives the openHop source pins and their upstream/local-candidate status for the version record. |
| `lab/version_record.py`                        | Maintains `lab/VERSION`: one section per rig, no stale duplicates.                                |
| `lab/config/medre-openhop-bridge.yaml`         | The MEDRE bridge config (no secrets); the only enabled bidirectional openHop↔MeshC route.         |
| `tests/test_openhop_bridge_profile.py`         | Offline topology/wiring assertions for the profile.                                               |
| `lab/tests/test_openhop_bridge_check.py`       | Offline assertions for the single-delivery contract.                                              |
