# Scenarios layer (cross-instance canonical-interop suite)

Behavioral scenarios for the interop-lab rig. This layer OWNS ONLY:

- `scenarios/` — this directory (config, lib, scenario scripts, `deferred/`)
- `lab/run-scenarios.sh` — the suite runner
- `lab/plugins/echo_observed.py` — the observation test plugin

The rig agent owns `compose.yaml`, `lab/config/`, `lab/up.sh`, `lab/tools/`,
and bring-up. Scenarios never start/stop containers; they run against an
ALREADY-UP rig and only read/write through the contract surfaces listed below.

## Topology contract (owner correction, 2026-11-10)

- **Two nodes per mesh**: `meshtasticd-a` (+ `meshtasticd-a-peer`) on
  AlphaLab, `meshtasticd-b` (+ `meshtasticd-b-peer`) on BetaLab. The relay
  node carries the mmrelay TCP connection; scenario radio actions originate
  from the PEER nodes (realistic third-party radio identity, exercises
  node-db/longname attribution), and far-side delivery is observed on the
  far mesh's PEER node API. Scenarios never attach a second API client to a
  relay-connected node.
- **Rooms are per meshnet x channel**: `#lab-alpha-ch0`, `#lab-alpha-ch2`,
  `#lab-beta-ch0`, `#lab-beta-ch2`; both relays join all four and map each
  to the same channel index on their own radio. `#lab-other` stays unmapped
  (mapped NOWHERE) for the rejection scenario. Cross-mesh flow under test:
  Alpha ch0 radio → `#lab-alpha-ch0` → Beta ch0 radio; the ch2 variants
  exercise the secondary channel on both meshes.
- **Deferred**: `threenode-same-mesh` moved to `scenarios/deferred/` by
  owner decision — same-mesh multi-relay loop handling is a later work item.
  The runner reports it as `DEFERRED` with the policy reason and does not
  count it as a failure.

## Owner decisions recorded here

- **blank-reply-plugin is a diagnostic** (owner, 2026-11-10): empty-body
  Matrix replies are accepted current behavior — real clients and Synapse
  effectively prevent them. The scenario FAILS only if the blank reply
  enters the structured reply branch (radio reply sent); it always REPORTS
  which gate fired per relay (`DIAGNOSTIC:` lines): plugin observation
  (normal path) or the empty-text early-return in `on_room_message`. The
  r13 doc claim is being corrected on the mmrelay branch separately.

## Running

```bash
lab/run-scenarios.sh              # all scenarios (deferred ones reported, not run)
lab/run-scenarios.sh smoke        # one scenario
lab/run-scenarios.sh threenode-same-mesh   # reports the DEFERRED policy note
```

Per-scenario output is teed to `lab/logs/scenarios/<name>-<ts>.log`; the
runner prints a PASS/FAIL/SKIP/DEFERRED summary and exits nonzero on any
FAIL. `SKIP` is neutral: an optional rig feature is absent and the scenario
says exactly which. Scripts exit `0` PASS, `2` SKIP, anything else FAIL.
Every assertion prints the evidence it checked.

## How scenarios access the rig

All helpers execute inside `lab-tools`:

- `scenarios/lib/mx_events.py` — Matrix event polling with MEDRE-envelope
  filters (`/messages`, stateless)
- `scenarios/lib/maps.py` — relay SQLite `message_map` access
  (read-only; the only writes are the packet-id-0 seed in
  `reaction-exact-key`, which its own teardown removes)
- `scenarios/lib/radio.py` — meshtastic TCP injection (text, structured
  replies, protobuf tapbacks with `Data.emoji=1`) and `listen`, a fromRadio
  sniffer used on PEER nodes for radio-side delivery evidence
- `scenarios/lib/mx_user.py` — human-user REST actions (token/send/reply/
  react/emote) for the human-interactions scenario
- `scenarios/lib/observe.py` — echo_observed plugin log reads
- `scenarios/lib/mx_forge.py` / `mx_blank_reply.py` — trusted-user
  forgery and blank-reply senders
- `scenarios/config.env` — contract defaults; the rig can override any
  value by writing `lab/state/scenario.env` (sourced last, never committed)

Provisioned accounts/rooms/tokens come from `lab/state/runtime.json`
(written by `lab/tools/provision.py`), not from scenario config.

## Scenario list

| Scenario                    | Purpose                                                                                                                                                                                                                                                                        |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `smoke`                     | AlphaLab peer radio text → canonical Matrix event in #lab-alpha-ch0 (medre.envelope v1, source_channel "0") → Beta radio receives the authored body verbatim (relay B map row + peer sniffer on the far mesh).                                                                 |
| `reply-correlation`         | Beta peer radio reply → Matrix reply event with native relation to the ORIGINAL event + matching channel id both sides + structured reply delivered on Alpha (map rows both sides + sniffer reply_id equality). Primary channel 0 and secondary channel 2 variants.            |
| `reaction-exact-key`        | Beta peer tapback → `message.reacted` event with the EXACT symbol as relation key (incl. skin-tone modifier), radio-native target packet id; plus a tapback whose target packet id is 0 (seeded map row, removed in teardown).                                                 |
| `untargeted-emote`          | Beta peer radio text with an unresolvable reply id relays as ordinary authored text (no relation, non-empty body) and reaches the peer mesh.                                                                                                                                   |
| `untrusted-forged-envelope` | @untrusted sends an event with a real relay's medre envelope, adapted to fake claims; only the visible text may relay — provenance, meshnet label, sender name, packet id must appear in NO map row or relay event (exact UNTRUSTED_EVENT_ID assertions on both relays' maps). |
| `blank-reply-plugin`        | Blank Matrix reply never enters the structured reply branch (no map rows, no radio reply); reports as an owner-accepted diagnostic which gate handled it (plugin observation vs empty-text early return).                                                                      |
| `cross-room-rejection`      | Events in #lab-other (mapped nowhere) — text, reply-to-mapped-event, reaction — produce no map rows and no radio traffic (map counts unchanged).                                                                                                                               |
| `channel-matrix`            | The r14 regression class: alpha-ch0 omitted/explicit, alpha-ch2, beta-ch2; every native ref (source + reply/reaction targets) carries the SAME numeric channel id; per-meshnet rooms make origin mesh+channel unambiguous.                                                     |
| `human-interactions`        | Simulated humans @alice/@bob (plus @untrusted contrast): text/reply/reaction/emote flows against radio-origin and human-origin messages, asserted through both relays' maps and peer-sniffer radio evidence (cases a-h in the script header).                                  |
| `threenode-same-mesh`       | **DEFERRED** (scenarios/deferred/): same-mesh multi-relay loop handling is a later work item.                                                                                                                                                                                  |

## REQUIREMENTS FOR THE RIG

1. **echo_observed plugin mount (needed by `blank-reply-plugin`)** — DONE in
   the committed compose: `lab/plugins/echo_observed.py` binds to
   `/data/plugins/custom/echo_observed.py` with `ECHO_OBSERVED_PATH` env per
   relay. Still needed from the config generator:
   ```yaml
   custom-plugins:
     echo_observed:
       active: true
   ```
2. **scenarios mounted into lab-tools** — DONE (`./scenarios:/scenarios:ro`).
3. **Peer nodes on each mesh** (NEW): services named per the CI convention
   (`meshtasticd-a-peer`, `meshtasticd-b-peer`), each on its relay node's
   mesh network with the SAME channel name/PSK config as its relay node (so
   over-the-air delivery between the pair works), reachable from lab-tools'
   networks. Publish any non-default hostnames/ports via
   `NODE_A_PEER_HOST/PORT`, `NODE_B_PEER_HOST/PORT` in
   `lab/state/scenario.env`. Peer nodes must carry NO relay connection.
4. **Per-meshnet x channel rooms** (NEW): provision `#lab-alpha-ch0`,
   `#lab-alpha-ch2`, `#lab-beta-ch0`, `#lab-beta-ch2` (all users + bots),
   generate the relay configs mapping each room to the room's channel index
   on the relay's own radio (both relays map all four), and publish the four
   aliases in `lab/state/scenario.env` (`ROOM_ALPHA_CH0_ALIAS`,
   `ROOM_ALPHA_CH2_ALIAS`, `ROOM_BETA_CH0_ALIAS`, `ROOM_BETA_CH2_ALIAS`).
   `#lab-other` stays provisioned but unmapped.
5. **Simulated human accounts** (NEW): provision `@alice:lab.test` and
   `@bob:lab.test` (non-admin, members of the four mapped rooms +
   #lab-other) and publish their tokens as `LAB_TOKEN_ALICE` /
   `LAB_TOKEN_BOB` in `lab/state/scenario.env` (or include them in
   `runtime.json` users; `mx_user.py` reads either).
6. **Map volume paths.** Relay state is mounted read-only into lab-tools at
   `/state/mmrelay-{a,b,c}` (DONE in the committed compose); map DB
   convention `<home>/database/meshtastic.sqlite`. Publish overrides as
   `MAP_DB_A/B/C` in `lab/state/scenario.env` if the convention changes.
7. **Radio injection path.** `scenarios/lib/radio.py` connects to the node
   TCP APIs from lab-tools (mtjk is installed there). Sending via a node's
   API makes that node transmit and the connected relay observes the packet
   (same surface the rig's own `lab/tools/smoke.py` and the upstream CI
   rely on). The `listen` sniffer attaches only to PEER nodes — never to a
   node carrying a relay connection.
8. **Scenario.env is the rig's publication point** for anything the
   scenarios must not guess: room aliases, peer hostnames/ports, human
   tokens, map paths. Scenarios read contract defaults from
   `scenarios/config.env` and let `lab/state/scenario.env` win.

## Contract observations (things the scenarios surfaced while authoring)

- **Human replies/reactions resolve radio targets** (extracted from code;
  upstream CI tests 5/6 assert the same): bridging ANY mapped-room message
  — human or radio-origin — writes a `message_map` row keyed by the Matrix
  event id once the radio send confirms. Replies and reactions then resolve
  through each relay's own map, so a human-to-human reply lands as a
  structured radio reply targeting that relay's own bridged packet id, and
  a human reaction relays as `reacted <key> to "<preview>"` on the mapped
  packet. `trusted_relays` gates only peer-metadata authority, never reply
  resolution — the human path works for any sender. The
  `human-interactions` scenario asserts this extracted contract; the owner
  brief's guess ("no radio target exists" for human originals) does not
  hold once the original was bridged, and the scenario header documents the
  divergence so it can be flipped together with any product change.
- **Radio→Matrix re-emission (self-echo) is a meshtasticd behavior.** The
  relay has no own-transmission guard on the radio→Matrix path; whether the
  node delivers its own transmissions to API clients decides if bridged
  traffic re-appears as canonical Matrix events. `smoke` and
  `human-interactions` therefore check conditionally: if relay-authored
  re-emissions appear, they must carry well-formed canonical envelopes;
  if none appear, that is reported as evidence, not a failure. A rig where
  re-emissions DO appear with more than one mapped room per channel will
  see the same packet posted once per matching room (relay code fans out to
  every room mapped to the packet's channel) — the scenarios' room-scoped
  waits and event-id-keyed map assertions are insensitive to that fan-out.
- **Peer replies on a shared meshnet** (deferred with threenode): a peer
  relay's reply event targets a Matrix event id from the PEER relay's own
  rendering; the receiving relay resolves reply targets only through its
  own map, so structured correlation across relays on one meshnet needs a
  relay-internal hook (resolving the peer relation's radio-native source
  ref against the local map by packet id). Recorded for the future
  loop-prevention work in `scenarios/deferred/`.
- **Malformed channel injection.** The meshtastic TCP API cannot produce a
  packet with a malformed channel value (the firmware assigns channels), so
  the channel-matrix malformed sub-case is documented as not live-injectable;
  the parsing branches are covered by mmrelay unit tests.
