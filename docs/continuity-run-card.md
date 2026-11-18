# Continuity run card: meshcore:lxmf disconnect/reopen

Operator card for the opt-in two-phase continuity case introduced by
`feat/reconnect-reopen-campaign`. It qualifies one current-source
MeshCore/MeshCore-BLE host link plus one second transport across a real
client-connection interruption and a MEDRE process reopen. The ordinary
`hardware run` short check is unchanged and stays the prerequisite.

## Status of the physical campaign this card came from (2026-11-18)

- MeshCore boards are healthy: both configured boards advertise, connect
  over BLE, and deliver channel traffic between themselves in both
  directions (`<owned-board>`, `<peer-board>`, channel 1).
- CORRECTION of the earlier "boards refuse rapid BLE reconnection and
  recover only after idle" claim: a bounded diagnostic (scan-find,
  connect, 2 s hold, clean disconnect, repeated at 2-10 s gaps) connected
  five out of five rounds with connect times of 1.4-1.8 s, and the boards
  resumed advertising 1.1-1.2 s after every clean disconnect. Rapid
  reconnect at seconds-level pacing is the normal case; the peer child's
  connect retry budget rides out a transient advertising gap of about
  half a minute before failing honestly. One transient remains
  unexplained and is recorded as an open finding: three consecutive 8 s
  scans found a board invisible roughly sixteen minutes after a clean
  disconnect, with visibility restored by the next check; no lab or board
  cause has been established for that single event.
- The RAK4631 Meshtastic board is connectable; the T1000-E is in BOOT
  loader mode and must be recovered by the operator before any Meshtastic
  pair can run. Do not reflash from the lab.
- CORRECTION of the earlier "RNode RF link is dead" finding: the two RNode
  radios power up with matching firmware-reported radio configuration
  (916.0 MHz, 125 kHz, 8 dBm, SF8, CR5) and the serial-only RNode RF link
  carries two-way LXMF delivery. At the strengthened head the full-pair
  short check passed with one message per direction, terminal lxmf
  `DELIVERED` (state 8), far-side observation of each nonce, and
  failure-free durable receipts; the RNS profiles contain only
  RNodeInterface entries on the two distinct CH340 serial ports, so the
  deliveries crossed the physical RF link. The lxmf transport is usable.
- Probe-design warning for announce-visibility checks: the earlier probe
  that "heard 0 announces both ways" was flawed — each side registered the
  RNS announce handler on its OWN destination identity, so it could never
  observe the other side's announce regardless of RF conditions. The same
  structure existed in the prior campaign's `rf_visibility.py`. Treat
  announce-listen results from that pattern as non-evidence; a passed
  two-way delivery is the authoritative RF evidence. (The flawed pattern
  is not present in the committed lab package; it lived only in private
  campaign probe scripts.)
- A reduced continuity run on the MeshCore board link alone passed in the
  earlier campaign, but the reduced form is not a qualification: current
  lab heads refuse single-transport pairs during validation, and the full
  `meshcore:lxmf` pair is the qualified shape (and is proven usable above).

## Prerequisites (operator-owned)

1. Firmware, matching channel keys, and pairing for both MeshCore boards
   exist and stay unchanged; the lab never flashes or rekeys.
2. A second transport with a working route (lxmf RNode link restored, or a
   recovered Meshtastic second board) is configured in `lab.toml` next to
   the meshcore section. The continuity case needs both transports of one
   pair.
3. A private run root exists (example below uses the campaign workspace);
   the CLI writes run state only there when `--run-root` is passed.
4. No other lab run owns the selected endpoints; the endpoint locks fail
   fast instead.
5. `medre-lab --config <campaign>/lab.toml doctor` is clean for the selected
   controller. Check `ble_adapter` in that same campaign configuration.

## Invocation

From the lab checkout with the campaign environment (exact sources under
`.local/campaigns/<name>/.venv`; see the campaign provenance record):

```sh
# Prerequisite: one message per direction on the selected pair.
medre-lab --config <campaign>/lab.toml hardware run --pair meshcore:lxmf \
  --role a --run-root <campaign>/runs

# The continuity case (two owned worker phases, one CLI process):
medre-lab --config <campaign>/lab.toml hardware continuity --pair meshcore:lxmf \
  --role a --interrupt-wait 300 --run-root <campaign>/runs
```

`--no-auto-disconnect` leaves the interruption to the operator: the baseline
writes `interrupt-armed.json` with `owned_address` and `controller`. For a
selected controller (e.g. `hci1`), run
`busctl call org.bluez /org/bluez/hci1/dev_<ADDRESS_WITH_UNDERSCORES> org.bluez.Device1 Disconnect`
using the exact values in that file (uppercase BLE address, colons replaced
with underscores). Without a selected controller, use
`bluetoothctl disconnect <owned_address>`. The baseline polls actual adapter
diagnostics; removal of the sentinel indicates the interruption stage ended,
not acknowledgement of the initial disconnect. Without the flag the
lock-owning worker performs the action once and logs it. Both workers consume
the same private, resolved `effective-config.toml`.

Role `a` makes MEDRE own the first configured endpoint of each transport;
role `b` swaps ownership to exercise the other boards.

## Expected status transitions

Baseline phase (`baseline-verdict.json`):

1. `owner-started` — runtime adapters healthy, startup drains skipped or
   retired (recorded per adapter).
2. Two legs, one per direction, each with exactly one correlated far-side
   delivery and a failure-free `sent` receipt at the destination adapter.
3. `link-disconnected` — observed from the adapter's own connection
   diagnostics (sentinel remains until the interruption/rejoin stage ends).
4. `link-rejoined` — connected again with the recovery loop finished; the
   peak reconnect-attempt counter is recorded as evidence only (the session
   resets it to zero on success).
5. Two fresh legs on the rejoined link; compare durable effects of previous
   markers. Ingress re-observation is not instrumented, so absence of a
   duplicate is not proof that a stale packet was received and suppressed.
6. `pending-captured` — two burst messages toward the MeshCore destination:
   the burst tail is released only after the burst head's claimed, reserved,
   pre-handoff outbox row is observed, and the boundary (tail pending and
   never claimed behind that head) is polled while the tail send is still in
   flight, so the honest state is observable on links where the independent
   sender's own teardown takes seconds. Recheck their identities and stopped
   evidence after retirement.
7. `owner-stopped` — bounded graceful stop with a bounded in-flight drain
   (the stop abandons the captured head mid-window and core records its own
   `shutdown_rejection` receipt; the head row stays claimed, pre-handoff and
   resumable); the independent peer listeners close afterwards.
   `surviving_sdk_children` must be empty.

Reopen phase (`reopen-verdict.json`), a fresh process:

1. Run lock acquired (fails fast while the baseline owner lives).
2. `persisted-verified` — completed sibling identities match the baseline
   record exactly (same canonical events, outbox rows and receipts); the
   successful baseline verdict, selected pair, role, configuration digest
   and installed-package provenance must also match.
3. `owner-restarted` — the same campaign database and profiles.
4. `pending-disposition` — the pending case progresses only if core's own
   replay authority allows it; the uncertain case's outcome is recorded
   without assertion. Both final outbox/receipt states are captured.
5. One fresh marker per direction after reopen proves peer observation
   still functions. After retirement, recheck completed and replayed
   obligations and the fresh legs for unexpected durable changes.

## Safe stop

- Ctrl-C the CLI once: the owned worker process group gets SIGINT, then a
  bounded SIGKILL ladder; phase verdicts are already written when a phase
  completes, so partial runs keep honest per-phase evidence.
- If the CLI is killed hard, preserve the run directory and inspect the
  process group using `ps -eo pid,ppid,pgid,args` (match the private run
  path and worker command). Signal only that verified worker process group
  with `kill -INT -- -<pgid>`, escalate to TERM/KILL if needed, and verify
  the worker and children have exited before starting another campaign.
  Deleting the lock file or disconnecting hardware does not retire an owner.
- No firmware, channel, key, bond, or clock state is written at any point.

## Reading the verdicts

`continuity-verdict.json` records phase pass flags, exit codes, timing,
timeouts and log paths. Inspect the individual phase verdict for detailed
evidence and worker PID when initialization reached that point. A phase that dies before
writing its verdict gets an explicit `worker-exited-before-verdict` record
instead of a pass. Private evidence (native ids, wire text) stays in the
run directory; publish only sanitized summaries.

Each direction arms only its destination listener; the source SDK must own
its exclusive peer endpoint for sending. Observe the full declared recipient
window; this is not continuous two-peer observation across the interruption.
Durable pre-interrupt marker equality detects duplicate durable effects, not
whether an ingress packet was re-observed and suppressed. A failed/cancelled
baseline does not start reopen. The historical reduced result does not qualify
these revised checks; rerun on the exact selected full hardware pair.
