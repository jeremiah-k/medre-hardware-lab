# Mixed-transport continuity run card: the A-3 proof

Operator card for `medre-lab hardware mixed-continuity` (branch
`feat/mixed-transport-continuity`). One MEDRE owner process holds four
adapters — two Matrix instances (`matrix0` -> room A, `matrix1` -> room B,
the healthy route), one MeshCore BLE board (the faulted transport), and one
Meshtastic serial board (owned, unrouted) — with three routes. The case
proves the A-3 assertion: while the worker-owned MeshCore host link is
disconnected, fresh room-A traffic fans out and an INDEPENDENT Matrix
recipient must observe the healthy route strictly before the MeshCore
session rejoins. Healthy traffic observed only after the rejoin is a
FAILURE. It then captures a reconcilable pending case and reopens the same
database in a fresh process (the committed continuity machinery).

The ordinary `hardware run` short check and the `hardware continuity`
meshcore:lxmf case are unchanged.

## Prerequisites (operator-owned)

1. Boards: two MeshCore boards configured and paired (the lab never flashes
   or rekeys), one RAK4631 Meshtastic board on its serial port. The
   T1000-E is in BOOT loader mode and stays untouched: the campaign runs
   with ONE Meshtastic instance and no Meshtastic route (no independent
   far-side endpoint exists on this bench; rig meshtasticd witness nodes
   are isolated per meshnet and their sibling nodes belong to the relays).
2. The lab Docker rig is part of the topology: `medre-lab docker up
--profile normal` must be green (the campaign's Matrix legs talk to the
   lab synapse on 127.0.0.1:8008). At the 2026-11-18 bring-up a stale
   lab-tools image baked the pre-#7 shared secret and failed provisioning
   with `HMAC incorrect`; rebuilding the image at bring-up fixed it and
   `medre-lab-mmrelay-a` returned to healthy. If mmrelay-a is unhealthy,
   the campaign's Matrix legs are unaffected (dedicated campaign rooms with
   no relay members); record it as an operator prerequisite.
3. Campaign provisioning (private, outside Git): the four campaign Matrix
   accounts (`campaign-m0`, `campaign-m1`, `campaign-sender`,
   `campaign-witness`) and rooms `#mixed-a` / `#mixed-b` exist, recorded in
   `<campaign>/campaign-matrix.json` (0600). The owner adapters' nio
   crypto store lives under `<campaign>/matrix-stores/` and MUST persist
   across runs: a fresh store regenerates one-time keys under indices the
   homeserver already holds, and synapse 400s the re-upload
   ("One time key ... already exists"), failing adapter health with
   `sync_failed`. If that state is lost, delete the owner devices via the
   synapse admin API, log the accounts in again (fresh device ids +
   tokens), update `campaign-matrix.json`, clear `matrix-stores/`, and
   start from a fresh run.
4. `medre-lab --config <campaign>/lab.toml doctor` is clean for `hci0`
   (the campaign TOML overrides the committed `hci1`, which does not exist
   on this host).
5. No other lab run owns the selected endpoints (endpoint locks fail fast);
   boards sit `Connected: no` at rest. A stale BLE link holds a connected
   board silent — clear it via a clean disconnect before starting.

## Campaign environment

A dedicated venv under `.local/campaigns/<name>/.venv` with the RELEASED
core installed from the index (medre 0.1.2, wheel sha256
`bd9987ad7624d1aae211fe444774abe3a8749a6a536fcbad699cbad2ea3de00a`) and
the adapters at the 0.1.2 manifest pins (matrix 0.1.1[e2e] @ d2619ed8,
meshcore 0.1.0 @ ba12d4d, meshtastic 0.1.1 @ e0008b52), plus the lab
checkout installed editable from the campaign worktree. Never `uv sync` /
`uv run` inside or from the worktree.

## Invocation

From the campaign directory with the campaign venv:

```sh
medre-lab --root <worktree> --config <campaign>/lab.toml hardware \
  mixed-continuity --hardware-toml <campaign>/lab.toml \
  --matrix-json <campaign>/campaign-matrix.json --role a \
  --burst-count 3 --run-root <campaign>/runs
```

The CLI runs the two owned worker phases in one process:
`mixed-baseline` (healthy legs -> worker-owned busctl disconnect of the
MEDRE-owned MeshCore board -> fault-window burst through both route
classes -> in-window independent observation -> rejoin -> affected-route
recovery -> pending capture -> bounded stop) and `mixed-reopen` (fresh
process on the same database and matrix stores: persisted identity
verification, pending disposition via core's replay authority, fresh
legs, final stopped-snapshot equality). `--role b` swaps the owned and
peer MeshCore boards. A failed or cancelled baseline never opens a
replacement owner.

## Expected status transitions

Baseline (`mixed-baseline-verdict.json`):

1. `owner-started` — all four adapters healthy (startup drains recorded).
2. Three healthy legs: mesh->matrix0, matrix0->mesh (24 s pre-send pacing),
   matrix0->matrix1; each with exactly one correlated far-side observation
   and a failure-free sent receipt at the destination.
3. `link-disconnected` — worker-owned `busctl` disconnect of the owned
   board, observed from the adapter's own diagnostics.
4. Fault-window burst: `burst-count` room-A messages, each fanning out to
   the healthy route (matrix1, room B) and the faulted route (meshcore
   outbox rows). The independent room-B recipient observes every burst
   nonce; its offsets share the phase clock and must fall strictly inside
   [disconnect, rejoin].
5. `link-rejoined` — observed from diagnostics; the peak reconnect-attempt
   counter is evidence only (the session resets it on success).
6. Affected-route recovery: the burst MeshCore rows reach sent receipts
   through the route's retry policy (transient failures recorded
   honestly); the peer board observes each nonce only after the rejoin.
   Affected-side in-window delivery would be a contradiction and fails.
7. Generation binding: every burst nonce binds to exactly one canonical
   event with exact outbox ids read from durable state; a mismatch fails.
8. `pending-captured` — the committed event-gated capture, keyed on the
   MeshCore rows (sibling Matrix fan-out receipts are expected, not
   boundary violations): head claimed/reserved/pre-handoff, tail never
   claimed behind it.
9. `owner-stopped` — bounded stop with a 1 s drain deadline; the captured
   head stays claimed and pre-handoff; `surviving_sdk_children` empty.

Reopen (`mixed-reopen-verdict.json`), a fresh process:

1. Run lock acquired only after the baseline owner retired.
2. `persisted-verified` — completed leg identities match exactly.
3. `owner-restarted` — same database, same campaign matrix stores.
4. `pending-disposition` — the pending case progresses only through core's
   own replay authority; the uncertain case's outcome is recorded without
   assertion.
5. Fresh legs prove peer observation still functions; completed deliveries
   stay settled; the final stopped snapshot matches every completed
   delivery.

## The A-3 verdict

`a3.evaluation` in the baseline verdict applies `medre_lab.a3_eval` to the
captured transcript. It fails on: no disconnect/rejoin pair, a fault
action that was not worker-owned, healthy-route traffic missing, duplicated,
or arriving outside the declared window or after the rejoin, affected-route
delivery inside the window, generation/outbox binding mismatches, and
sampler coverage below three successful work-status samples per phase.

## Sustained-load sampler

The owner shells the RELEASED core's read-only CLI on a 5 s cadence into
`work-status-series.jsonl`, phase-tagged (`baseline`, `interruption`,
`recovery`, `pending-capture`, `reopen`): `medre inspect work-status
--json` each cycle and `medre inspect pressure` every other cycle (the
requested `medre storage status` does not exist in the 0.1.2 CLI; the
substitution is recorded in the series header). Sampler failures are
recorded, never fabricated.

## Safe stop

- Ctrl-C the CLI once: the worker process group gets SIGINT, then a
  bounded SIGKILL ladder; completed phases keep their verdicts.
- If the CLI is killed hard, verify the worker process group is gone
  (`ps -eo pid,ppid,pgid,args | grep medre_lab.mixed_worker`) before
  starting another campaign; boards must sit `Connected: no` at rest.
- No firmware, channel, key, bond, or clock state is written to any board
  at any point. The operator's Docker rig is left in its brought-up state.

## Reading the verdicts

`continuity-verdict.json` merges phase pass flags, exit codes, durations
and log paths. Verdict offsets are durations relative to the phase start;
no wall-clock claims appear in evidence. Private evidence (native ids,
tokens, room ids, wire text) stays in the run directory and campaign
files; publish only sanitized summaries.
