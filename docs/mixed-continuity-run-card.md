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
4. Run the read-only mixed preflight (the pair-mode `doctor` requires two
   Meshtastic ports):

   ```sh
   medre-lab --root <worktree> hardware mixed-preflight \
     --hardware-toml <campaign>/lab.toml \
     --matrix-json <campaign>/campaign-matrix.json
   ```

   The pinned MeshCore owner accepts a BLE address, not a scanner controller.
   This qualification therefore requires exactly one host HCI controller,
   matching the configured `ble_adapter` (hci0 on this bench). The worker
   checks again before disconnect. Multi-controller hosts are unsupported for
   this campaign until the owner can select and attest its controller.

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
`uv run` inside or from the worktree. Save the installed host package records
separately from the running rig's actual Docker identities: retain `lab/VERSION`
from bring-up, the selected source/bake provenance (including the MMRelay
`medre-bake-pin.json` stamp where applicable), and current container image IDs
from `docker inspect --format '{{.Name}} {{.Image}} {{.Config.Image}}'` for
each participating container. A declared image tag or source lock is not proof
of the running image. Note any unavailable record rather than inferring it.

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
replacement owner. The CLI freezes both inputs in `effective-mixed-config.json`
inside the private run directory, retains the original persistent Matrix store
path, and passes that snapshot to both workers. Do not publish it: it contains
credentials.

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
   outbox rows). The independent room-B recipient starts incremental observation before the fault and observes every
   burst nonce exactly once inside the conservatively proven disconnected
   interval. Both observers are armed before interruption. Matrix timeline gaps
   or missing coverage invalidate the verdict.
5. `link-rejoined` — diagnostic polls bound the actual transition between the
   last disconnected and first connected readings. Sightings in that uncertainty
   interval do not qualify as in-fault or recovered evidence. The peak
   reconnect-attempt counter is evidence only (the session resets it on success).
6. Affected-route recovery: the burst MeshCore rows reach sent receipts
   through the route's retry policy (transient failures recorded
   honestly); the peer board observes each nonce only after the rejoin.
   Affected-side in-window or ambiguously timed delivery contradicts the silence
   assertion. A-3 itself records the affected outcome (`all_delivered`,
   `partial`, or `not_delivered`); partial recovery alone need not fail the narrow
   A-3 evaluator. The full baseline requires `all_delivered`, with matching
   durable evidence and independent post-rejoin observations. Otherwise no
   reopen starts.
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
A sample must begin and finish within the proven interval to count as fault
coverage. The full campaign also requires complete affected-route recovery and
retirement checks. Historical partial-recovery results cannot qualify it.

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
  before starting another campaign. Record the worker PGID while it is live
  (`ps -eo pid,ppid,pgid,args`); the supervisor starts each worker in a new
  session, so its recorded PID is its original PGID. Check **all** members,
  even if the worker itself has exited:

  ```sh
  ps -eo pid,ppid,pgid,args | awk -v pgid=<recorded-pgid> '$3 == pgid'
  ```

  Separately check that both board links are `Connected: no`.

- No firmware, channel, key, bond, or clock state is written to any board
  at any point. The operator's Docker rig is left in its brought-up state.

## Reading the verdicts

`continuity-verdict.json` merges phase pass flags, exit codes, durations
and log paths. A-3 verdict comparisons use phase-relative monotonic offsets;
raw phase starts retain monotonic readings, and timeline `at` fields are
wall-clock diagnostic timestamps, not ordering authority. The phase files
contain the individual worker identity and detailed evidence. Completed
identities are checked by destination, allowing ordinary sibling settlement. Private evidence (native ids,
tokens, room ids, wire text) stays in the run directory and campaign
files; publish only sanitized summaries.
