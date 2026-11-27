#!/usr/bin/env python3
"""Lab-owned fault-injecting TCP splice for the lossy-link fault matrix.

WHAT IT IS
----------
A byte pipe between a MeshCore client and an openHop companion.  It forwards
every byte in both directions unchanged, and can inject two families of
fault:

SCRIPTED PER-CONNECTION FAULTS (read once at startup, server -> client):

  delay_ms        delay every server->client chunk by this many milliseconds
  drop_after_n    drop (consume and discard) the first N server->client chunks
  close_after_n   close the connection after N server->client chunks
  stall_after_n   stop forwarding server->client data after N chunks (the
                  client then waits for a response that never arrives)

  The chunk-budget faults are PER CONNECTION: the counter restarts with
  every new connection, so each reconnect's first chunks are let through.
  One case keeps this shape on purpose - a link that answers the small
  queries and swallows the message frames is the documented worst case the
  adapter spec bounds (at most one consumed message per reconnect
  attempt) - and its verdict asserts that bound.

TIME-WINDOW FAULTS (re-read from the fault file on EVERY chunk, both
directions, no restart needed; armed at a point in time and PERSISTENT for
the configured duration across every connection inside the window):

  stall_server_to_client_until   swallow every server->client chunk until
                                 this unix deadline (the reply stall: the
                                 handshake reply, a liveness-probe reply and
                                 a fetched frame are all swallowed like any
                                 other frame; the session stays open)
  close_on_request_until         until this unix deadline, close the
                                 connection the first time the client puts
                                 a request in flight: the frame is
                                 swallowed, nothing is forwarded upstream,
                                 and the connection is closed (on a live
                                 link that frame is the next fetch or
                                 probe; on a fresh connection it is the
                                 handshake).  The close re-arms with every
                                 connection for the rest of the window.

  stall_server_to_client / stall_client_to_server (booleans) hold one
  direction the same way as the deadline form, with no deadline; the two
  together are the lab's silent-dead-link fault: to the two ends the link
  simply stops speaking while every socket stays open.

  drop_message_frames (boolean, content-triggered): parse the companion
  frames on the server -> client side (">" 0x3E + 2-byte LE length +
  data, data[0] = packet type) and construct THE LINK THE SPEC'S
  ELAPSED-WINDOW RULE DESCRIBES: the handshake reply and the
  liveness-probe (device-query) replies are FORWARDED, and the message
  frames (the replies to the client's 0x0a sync command:
  CONTACT/CHANNEL_MSG_RECV 0x07/0x08 and their V3 forms 0x10/0x11,
  plus the empty drain's NO_MORE_MSGS 0x0a) AND the MESSAGES_WAITING
  0x83 notifications that announce them are SWALLOWED.  On this link
  nothing but an answered probe can re-arm a fetch, so it consumes at
  most one queued message per probe interval - deterministically: no
  chunk-count budget whose phasing could race the pump's fetch cycles.
  A dropped message frame or notification logs one JSON line with the
  connection identity and the lab marker the frame carried (null when it
  carries none), and a forwarded probe reply logs one too, so
  the verdict can prove per connection that the same link both
  answered a probe and lost a message frame.  The splice still never
  SPEAKS the protocol: it reads frame boundaries and one packet-type
  byte from the wire, forwards all payload bytes of every forwarded
  frame unchanged, and logs counters and events, never payload bytes.

  stall_server_to_client_on_request (boolean) opens the reply stall at
                                 the FIRST client->server chunk observed
                                 while it is armed: the request itself
                                 passes, and every server->client reply
                                 from that moment is withheld.  On a
                                 parked, quiescent link that first chunk
                                 is the wake-driven fetch for a fresh
                                 backlog, so the companion pops one
                                 message whose reply never arrives - the
                                 killed-with-one-in-flight backlog case
                                 holds the drain in exactly that state.
                                 The splice stays protocol-blind: the
                                 trigger is the existence of
                                 client->server traffic, never its
                                 meaning.  The latch is global across
                                 connections while the flag is armed and
                                 resets when the flag leaves the file, so
                                 disarm and re-arm starts a new stall.

The windowed faults are what a real stall is: they do not reset with a
reconnect, so a reconnect inside the window meets the same swallowed or
closed frames as the connection it replaced.  All of them are re-read per
chunk, so a case arms them AFTER the link is up and clears them later
WITHOUT restarting the splice - a splice restart would close the session,
which is a different fault.

The faults are applied to ONE connection at a time; the counters are
reported in the splice's own log (chunk counts and stall events, never
payload bytes).

The lab drivers always start the splice with OPENHOP_FAULT_PROXY_TRACE=1,
so every forwarded frame is traced (packet type and lab marker) alongside
the fault events; the cases' verdicts read those trace lines.

WHY A PROXY
-----------
The fault question is what the MeshCore receive pump does when the
companion answers slowly, not at all, or goes silent before the connection
drops.  The fault has to be injected BETWEEN the client and the companion,
because that is where the fetch timeout lives.  A radio-level fault would
exercise the mesh path, not the fetch path.

The splice never logs payload bytes, keys or secrets: only counters.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time

STATS = {
    "connections": 0,
    "client_chunks": 0,
    "server_chunks": 0,
    "server_chunks_dropped": 0,
    "server_chunks_forwarded": 0,
    "delayed_ms_total": 0,
    "closed_by_fault": 0,
    "stalled": 0,
    "stalled_server_to_client": 0,
    "stalled_client_to_server": 0,
    "stall_window_chunks_dropped": 0,
    "close_window_closes": 0,
    "request_stall_opens": 0,
    "request_stall_chunks_withheld": 0,
    "message_frames_dropped": 0,
    "frame_resyncs": 0,
    "probe_replies_forwarded": 0,
}

# The dynamic flags live in the fault file; re-read with this pace while a
# direction is held, so clearing the file restores forwarding promptly.


def read_dynamic_fault(path: str) -> dict:
    """Return the dynamic fault flags, or an empty dict when unreadable.

    A missing or mid-write file must never crash the splice: the flags
    simply read as clear for this chunk.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            fault = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(fault, dict):
        return {}
    return fault


def window_deadline(fault: dict, key: str) -> float | None:
    """Return the window's deadline for ``key``, or None when not armed.

    A window is armed when the fault file carries the key with a unix
    deadline still in the future.  The file is re-read on every call, so
    the window applies to every connection for exactly as long as the
    file says - a reconnect inside the window meets the same fault as the
    connection it replaced, and the fault is gone the moment the
    deadline passes.
    """
    deadline = fault.get(key)
    if not isinstance(deadline, (int, float)):
        return None
    if time.time() >= float(deadline):
        return None
    return float(deadline)


class Window:
    """The two time-window faults, re-read from the fault file per chunk."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.announced: set[str] = set()

    def stall_s2c_active(self) -> bool:
        return (
            window_deadline(
                read_dynamic_fault(self.path), "stall_server_to_client_until"
            )
            is not None
        )

    def close_active(self) -> bool:
        return (
            window_deadline(read_dynamic_fault(self.path), "close_on_request_until")
            is not None
        )

    def announce(self, event: str, connection: int) -> None:
        """Log one JSON line per event kind and connection."""
        key = f"{connection}:{event}"
        if key not in self.announced:
            self.announced.add(key)
            print(
                json.dumps({"event": event, "connection": connection}),
                flush=True,
            )

    def disarm_announcement(self, event: str, connection: int) -> None:
        self.announced.discard(f"{connection}:{event}")

    def close_connection(self, connection: int, writers: list) -> None:
        """Close every writer of the connection without forwarding anything."""
        self.announce("closed_by_fault_window", connection)
        STATS["close_window_closes"] += 1
        for writer in writers:
            with contextlib.suppress(Exception):
                writer.close()


class Blackhole:
    """The two direction flags, re-read from the fault file per chunk."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.announced: set[str] = set()

    def armed(self, flag: str) -> bool:
        return bool(read_dynamic_fault(self.path).get(flag))

    def announce(self, flag: str, connection: int) -> None:
        """Log one JSON line per armed direction and connection."""
        key = f"{connection}:{flag}"
        if key not in self.announced:
            self.announced.add(key)
            print(
                json.dumps(
                    {
                        "event": "blackhole_armed",
                        "direction": flag,
                        "connection": connection,
                    }
                ),
                flush=True,
            )

    def disarm_announcement(self, flag: str, connection: int) -> None:
        self.announced.discard(f"{connection}:{flag}")

    def drops(self, flag: str, connection: int, counter: str) -> bool:
        """Discard chunks while armed so EOF remains observable."""
        if not self.armed(flag):
            self.disarm_announcement(flag, connection)
            return False
        self.announce(flag, connection)
        STATS[counter] += 1
        return True


class RequestStall:
    """The request-opened reply stall (stall_server_to_client_on_request).

    While the flag is armed in the fault file, the FIRST client->server
    chunk observed opens the stall: the request itself passes, and every
    server->client reply from that moment is withheld.  The latch is one
    instance per splice (shared by every connection, so it survives a
    reconnect while armed) and resets when the flag leaves the file, so
    disarm and re-arm starts a new stall.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.open = False
        self.generation: object = None

    def armed(self) -> bool:
        fault = read_dynamic_fault(self.path)
        generation = fault.get("request_stall_generation")
        if generation != self.generation:
            self.generation = generation
            self.open = False
        if not fault.get("stall_server_to_client_on_request"):
            self.open = False
            return False
        return True

    def observe_request(self, connection: int) -> None:
        """Open the stall at the first client->server chunk while armed."""
        if not self.armed() or self.open:
            return
        self.open = True
        STATS["request_stall_opens"] += 1
        print(
            json.dumps({"event": "request_stall_opened", "connection": connection}),
            flush=True,
        )

    def withholds_replies(self) -> bool:
        """Return whether server->client replies are withheld right now.

        Reading the file per chunk keeps the same contract as the other
        dynamic faults: the stall ends the moment the flag leaves the
        file, and the reset latch re-arms on the next request.
        """
        if not self.armed():
            self.open = False
            return False
        return self.open


# MeshCore companion wire format (this splice's server -> client side):
# ">" (0x3E) + 2-byte little-endian length + data, where data[0] is the
# packet type: the reply to the client's 0x0a sync (GET_MSG) command
# carries the drained message frames (CONTACT_MSG_RECV 0x07,
# CHANNEL_MSG_RECV 0x08, and their V3 forms 0x10/0x11) or the empty
# drain's NO_MORE_MSGS 0x0a; MESSAGES_WAITING pushes carry 0x83; the
# handshake and the idle-link probe replies echo their own commands.
FRAME_OUT_START = 0x3E
MESSAGE_FRAME_TYPES = frozenset({0x07, 0x08, 0x0A, 0x10, 0x11})
MESSAGES_WAITING_PUSH = 0x83
PROBE_REPLY_TYPE = 0x0D
DROP_MESSAGE_FRAMES_KEY = "drop_message_frames"
FRAME_IN_START = 0x3C

# When OPENHOP_FAULT_PROXY_TRACE=1, every server->client frame the splice
# FORWARDS is logged with its inner packet type and, for message-carrying
# frames, the lab marker found in the text.  Both lab drivers
# (openhop_fault_matrix.start_proxy and openhop_backlog_matrix.start_splice)
# start the splice with the variable set, so the trace is always on in rig
# runs; the switch only exists so a manually started splice can run quiet.
TRACE_ENV = "OPENHOP_FAULT_PROXY_TRACE"


def trace_enabled() -> bool:
    import os

    return os.environ.get(TRACE_ENV, "") == "1"


def _marker_in(payload: bytes) -> str | None:
    import re

    match = re.search(rb"p4[a-z0-9]{1,4}-\d+-[0-9a-f]{6,12}", payload)
    return match.group(0).decode("ascii", "replace") if match else None


def trace_c2s_records(connection: int, data: bytes) -> list[dict]:
    """Observational only: describe the client→server command frames.

    Frame format (client → server): "<" (0x3C) + 2-byte LE length + data,
    where data[0] is the command byte (APP_START 0x01, DEVICE_QUERY 0x16,
    the 0x0A sync/GET_MSG drain command).
    """
    records: list[dict] = []
    buf = data
    while len(buf) >= 3 and buf[0] == FRAME_IN_START:
        length = int.from_bytes(buf[1:3], "little")
        if len(buf) < 3 + length:
            break
        payload = buf[3 : 3 + length]
        buf = buf[3 + length :]
        records.append(
            {
                "event": "c2s_frame",
                "connection": connection,
                "command": "0x%02x" % (payload[0] if payload else 0),
                "marker": _marker_in(payload),
            }
        )
    return records


def trace_forward_records(connection: int, data: bytes) -> list[dict]:
    """Observational only: describe the frames inside a forwarded chunk."""
    records: list[dict] = []
    buf = data
    while len(buf) >= 3 and buf[0] == FRAME_OUT_START:
        length = int.from_bytes(buf[1:3], "little")
        if len(buf) < 3 + length:
            break
        payload = buf[3 : 3 + length]
        buf = buf[3 + length :]
        records.append(
            {
                "event": "frame_forwarded",
                "connection": connection,
                "packet_type": "0x%02x" % (payload[0] if payload else 0),
                "marker": _marker_in(payload),
            }
        )
    return records


class MessageFrameDropper:
    """Drop the message frames and their notifications; forward the rest.

    CONTENT-TRIGGERED, never timing-triggered: the frame boundary and
    the packet type are read from the wire, so a link that answers the
    handshake and the liveness probes and swallows exactly the message
    path is constructed, with no chunk-count phasing to race the pump's
    fetch cycles.  Dropped: the replies to the client's 0x0a sync
    command (the drained message frames and the empty drain's
    NO_MORE_MSGS) AND the MESSAGES_WAITING pushes that announce them.
    Swallowing the notifications is what makes the link the documented
    worst case the spec bounds at one consumed message per answered
    probe: the spec arms one extra drain per delivered notification, so
    a link that delivers notifications would consume one message per
    notification instead - a different (and unspec-bounded) shape.  A
    dropped message frame or notification logs one JSON line with the
    connection identity, so the verdict can attribute the lost frames
    to the connection they belonged to.  Per connection: one instance,
    one buffer; partial frames are held until complete and a byte that
    cannot start a frame is counted as a resync and skipped.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.buf = b""

    def active(self) -> bool:
        return bool(read_dynamic_fault(self.path).get(DROP_MESSAGE_FRAMES_KEY))

    def feed(self, data: bytes, connection: int) -> bytes:
        """Parse frames out of ``data`` and return what must be forwarded."""
        self.buf += data
        out = bytearray()
        while self.buf:
            if len(self.buf) < 3:
                break
            if self.buf[0] != FRAME_OUT_START:
                STATS["frame_resyncs"] += 1
                self.buf = self.buf[1:]
                continue
            length = int.from_bytes(self.buf[1:3], "little")
            if len(self.buf) < 3 + length:
                break
            frame = self.buf[: 3 + length]
            self.buf = self.buf[3 + length :]
            if len(frame) > 3 and (
                frame[3] in MESSAGE_FRAME_TYPES or frame[3] == MESSAGES_WAITING_PUSH
            ):
                STATS["message_frames_dropped"] += 1
                print(
                    json.dumps(
                        {
                            "event": "message_frame_dropped",
                            "connection": connection,
                            "packet_type": frame[3],
                            "marker": _marker_in(frame[3:]),
                        }
                    ),
                    flush=True,
                )
                continue
            if len(frame) > 3 and frame[3] == PROBE_REPLY_TYPE:
                # The link's proof evidence: a liveness-probe reply this
                # connection FORWARDED, recorded right next to the message
                # frames it swallows.
                STATS["probe_replies_forwarded"] += 1
                print(
                    json.dumps(
                        {
                            "event": "probe_reply_forwarded",
                            "connection": connection,
                            "packet_type": frame[3],
                        }
                    ),
                    flush=True,
                )
            out += frame
        return bytes(out)


async def pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    direction: str,
    blackhole: Blackhole,
    connection: int,
    window: Window,
    all_writers: list,
    stall: RequestStall,
) -> None:
    flag = "stall_client_to_server"
    counter = "stalled_client_to_server"
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            if trace_enabled():
                for record in trace_c2s_records(connection, data):
                    print(json.dumps(record), flush=True)
            STATS[f"{direction}_chunks"] += 1
            # The request-opened reply stall: the request itself keeps
            # passing; the stall opens on it and withholds the replies.
            stall.observe_request(connection)
            if window.close_active():
                # The close-window fault: the client has put a request in
                # flight (on a live link the next fetch or probe, on a
                # fresh connection the handshake).  Swallow the frame,
                # forward nothing upstream, close the connection.  The
                # window is read from the fault file per chunk, so the
                # close re-arms with every connection for the rest of
                # the window.
                window.close_connection(connection, all_writers)
                break
            if blackhole.drops(flag, connection, counter):
                continue
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    host: str,
    port: int,
    delay_ms: int,
    drop_after_n: int,
    close_after_n: int,
    stall_after_n: int,
    fault_file: str,
    stall: RequestStall,
) -> None:
    STATS["connections"] += 1
    connection = STATS["connections"]
    blackhole = Blackhole(fault_file)
    dropper = MessageFrameDropper(fault_file)
    try:
        server_reader, server_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10
        )
    except Exception as exc:
        print(
            json.dumps(
                {"event": "upstream_connect_failed", "error": type(exc).__name__}
            ),
            flush=True,
        )
        client_writer.close()
        return

    async def server_to_client() -> None:
        nonlocal drop_after_n, close_after_n, stall_after_n
        seen = 0
        try:
            while True:
                data = await server_reader.read(65536)
                if not data:
                    break
                seen += 1
                if drop_after_n > 0 and seen <= drop_after_n:
                    STATS["server_chunks_dropped"] += 1
                    continue
                if close_after_n > 0 and seen >= close_after_n:
                    STATS["closed_by_fault"] += 1
                    print(
                        json.dumps(
                            {"event": "closed_by_fault", "connection": connection}
                        ),
                        flush=True,
                    )
                    break
                if stall_after_n > 0 and seen >= stall_after_n:
                    STATS["stalled"] += 1
                    print(
                        json.dumps({"event": "stalled", "connection": connection}),
                        flush=True,
                    )
                    # Hold the connection open but send nothing further.
                    await asyncio.sleep(3600)
                if window.stall_s2c_active():
                    # The reply-stall window: every server->client frame -
                    # a handshake reply, a liveness-probe reply, a fetched
                    # message frame - is swallowed like any other frame,
                    # on every connection inside the window.
                    window.announce("reply_stall_window", connection)
                    STATS["stall_window_chunks_dropped"] += 1
                    continue
                window.disarm_announcement("reply_stall_window", connection)
                if stall.withholds_replies():
                    # The request-opened reply stall: the reply is
                    # withheld while the stall holds (re-read per chunk,
                    # so clearing the flag restores forwarding at once).
                    STATS["request_stall_chunks_withheld"] += 1
                    continue
                if blackhole.drops(
                    "stall_server_to_client",
                    connection,
                    "stalled_server_to_client",
                ):
                    continue
                if dropper.active():
                    # The content-triggered message-frame drop: parse the
                    # companion frames in this chunk and forward everything
                    # except GET_MSG replies, so the handshake and the
                    # liveness-probe replies pass and the message frames are
                    # swallowed on this same connection.
                    data = dropper.feed(data, connection)
                    if not data:
                        continue
                if trace_enabled():
                    for record in trace_forward_records(connection, data):
                        print(json.dumps(record), flush=True)
                if delay_ms:
                    STATS["delayed_ms_total"] += delay_ms
                    await asyncio.sleep(delay_ms / 1000)
                STATS["server_chunks_forwarded"] += 1
                client_writer.write(data)
                await client_writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                server_writer.close()

    window = Window(fault_file)
    tasks = [
        asyncio.create_task(
            pipe(
                client_reader,
                server_writer,
                "client",
                blackhole,
                connection,
                window,
                [client_writer, server_writer],
                stall,
            )
        ),
        asyncio.create_task(server_to_client()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        client_writer.close()
        server_writer.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # The splice binds all interfaces because the container's interface IS
    # the private compose network; it publishes no host port.
    parser.add_argument(
        "--listen-host",
        default="0.0.0.0",  # nosec B104 # The splice serves the private lab Docker network only; the compose file publishes no host port.
    )
    parser.add_argument("--listen-port", type=int, default=5100)
    parser.add_argument("--upstream-host", default="openhop-repeater-a")
    parser.add_argument("--upstream-port", type=int, default=5000)
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--drop-after-n", type=int, default=0)
    parser.add_argument("--close-after-n", type=int, default=0)
    parser.add_argument("--stall-after-n", type=int, default=0)
    parser.add_argument("--stats-file", default="/tmp/openhop-fault-proxy-stats.json")
    parser.add_argument(
        "--fault-file",
        default="",
        help="read the fault arguments from this JSON file instead of the "
        "command line, so the splice can be started by compose (which owns "
        "the network alias the MEDRE client resolves); the dynamic "
        "blackhole flags in this file are re-read on every chunk, so a "
        "case arms and clears them without restarting the splice",
    )
    args = parser.parse_args()
    if args.fault_file:
        try:
            fault = json.loads(open(args.fault_file, encoding="utf-8").read())
        except (OSError, ValueError) as exc:
            print(
                json.dumps({"event": "fault_file_unreadable", "error": str(exc)}),
                flush=True,
            )
            raise SystemExit(2)
        args.delay_ms = int(fault.get("delay_ms", args.delay_ms))
        args.drop_after_n = int(fault.get("drop_after_n", args.drop_after_n))
        args.close_after_n = int(fault.get("close_after_n", args.close_after_n))
        args.stall_after_n = int(fault.get("stall_after_n", args.stall_after_n))

    async def report() -> None:
        while True:
            await asyncio.sleep(5)
            payload = dict(STATS)
            payload["observed_at"] = time.time()
            with open(args.stats_file, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

    asyncio.create_task(report())
    stall = RequestStall(args.fault_file)
    server = await asyncio.start_server(
        lambda r, w: handle(
            r,
            w,
            args.upstream_host,
            args.upstream_port,
            args.delay_ms,
            args.drop_after_n,
            args.close_after_n,
            args.stall_after_n,
            args.fault_file,
            stall,
        ),
        args.listen_host,
        args.listen_port,
    )
    print(
        json.dumps(
            {
                "event": "listening",
                "listen": f"{args.listen_host}:{args.listen_port}",
                "upstream": f"{args.upstream_host}:{args.upstream_port}",
                "delay_ms": args.delay_ms,
                "drop_after_n": args.drop_after_n,
                "close_after_n": args.close_after_n,
                "stall_after_n": args.stall_after_n,
            }
        ),
        flush=True,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
