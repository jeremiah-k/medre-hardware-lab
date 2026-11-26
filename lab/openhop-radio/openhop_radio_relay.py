#!/usr/bin/env python3
"""Lab-owned virtual radio relay: the openHop Modem protocol over TCP.

This service is the LAB side of openHop's modem transport. It speaks the
openHop Modem wire protocol exactly as the repeater's TCP radio driver
expects it (see the pinned openhop_core):

    openhop_core/src/openhop_core/hardware/protocol_constants.py
    openhop_core/src/openhop_core/hardware/tcp_radio.py

    SYNC (0xAA) | CMD (1B) | LEN (2B LE) | PAYLOAD (0..N B) | CRC (2B LE)

The CRC is CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection,
no xor-out) computed over CMD + LEN + PAYLOAD; the SYNC byte is excluded.

WHAT THIS IS (AND IS NOT)
-------------------------
The relay models a shared, deterministic RF medium with exactly two
clients: a payload transmitted by one client is RECEIVED BY THE OTHER
client, and is never echoed back to the sender. There is no RF, no
airtime, no spectrum, no antenna and no hardware: "the air" is a byte
pipe between two TCP peers.

EVIDENCE BOUNDARY
-----------------
A packet that crosses this relay is a REAL MeshCore packet produced by a
real openHop repeater: the repeater encrypted it with a real channel
secret, and the peer repeater decrypted it with a real channel secret.
What is SIMULATED is the radio hop itself (the LoRa PHY, the air, the
range). No claim of physical RF delivery may be derived from this
service.

SECURITY / HYGIENE
-------------------
- No secret is ever logged. The optional AUTH token is compared in
  memory and never printed.
- Packet payloads are never logged (only their length and a short
  digest), and the per-client log/event buffers are bounded.
- Every client has a bounded outbound queue; a full queue drops the
  packet (an RF loss) instead of growing without limit.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import struct
import time
from typing import List, Optional, Tuple

LOGGER = logging.getLogger("openhop-radio-relay")

# ---------------------------------------------------------------------------
# Framing (mirrors openhop_core.hardware.protocol_constants)
# ---------------------------------------------------------------------------

PROTO_SYNC = 0xAA
MAX_FRAME_PAYLOAD = 1024

# Host -> Modem
CMD_TX_REQUEST = 0x01
CMD_SET_CONFIG = 0x10
CMD_GET_CONFIG = 0x11
CMD_STATUS_REQ = 0x20
CMD_NOISE_REQ = 0x22
CMD_CAD_REQUEST = 0x30
CMD_RX_START = 0x31
CMD_SET_CAD_PARAMS = 0x34
CMD_AUTH = 0x50
CMD_GET_VERSION = 0x70
CMD_PING = 0xFF

# Modem -> Host
CMD_TX_DONE = 0x02
CMD_TX_FAIL = 0x03
CMD_RX_PACKET = 0x04
CMD_CONFIG_RESP = 0x12
CMD_STATUS_RESP = 0x21
CMD_NOISE_RESP = 0x23
CMD_CAD_RESP = 0x32
CMD_RX_STARTED = 0x33
CMD_CAD_PARAMS_RESP = 0x35
CMD_AUTH_OK = 0x51
CMD_VERSION_RESP = 0x71
CMD_ERROR = 0xFE
CMD_PONG = 0xFF  # same wire byte as CMD_PING

# CMD_ERROR payload[0]
ERR_CRC_MISMATCH = 0x01
ERR_INVALID_CMD = 0x02
ERR_UNAUTHORIZED = 0x09

# RadioConfig: freq_hz(u32) | bandwidth_hz(u32) | sf(u8) | cr(u8)
#              | power_dbm(i8) | syncword(u16) | preamble_len(u8)
RADIO_CONFIG_FMT = "<IIBBbHB"
RADIO_CONFIG_SIZE = struct.calcsize(RADIO_CONFIG_FMT)

# StatusResp: uptime | rx_count | tx_count | crc_errors | last_rssi
#             | snr x10 | noise x10 | temp_c | radio_state
STATUS_RESP_FMT = "<IIIIhhhbB"
STATUS_RESP_SIZE = struct.calcsize(STATUS_RESP_FMT)

# The relay is a virtual medium, not a chip: the reported signal values
# are fixed so a run is reproducible.
SIM_RSSI_DBM = -40
SIM_SNR_X10 = 80
SIM_SIGNAL_RSSI_DBM = -40
SIM_NOISE_X10 = -1200
SIM_TEMP_C = 25
SIM_AIRTIME_US = 100_000

MAX_CLIENTS = 2
CLIENT_QUEUE_MAX = 128
# Grace period before a third connection takes a slot: it lets a peer that
# already closed its socket be reaped (EOF) instead of evicted.
RECONNECT_GRACE_SECONDS = 2.0
RX_HEADER_SIZE = 6  # rssi(i16) | snr_x10(i16) | signal_rssi(i16)


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE, bit-for-bit as the openHop firmware."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    """SYNC | CMD | LEN | PAYLOAD | CRC."""
    length = len(payload)
    if length > 0xFFFF:
        raise ValueError(f"payload too long: {length}")
    header = struct.pack("<BH", cmd, length)
    crc = crc16_ccitt(header + payload)
    return bytes([PROTO_SYNC]) + header + payload + struct.pack("<H", crc)


class FrameDecoder:
    """Incremental decoder for the modem framing.

    Resynchronises on a bad CRC or an implausible length by dropping the
    SYNC byte, exactly like the openHop driver does on the receive side.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.crc_errors = 0

    def feed(self, data: bytes) -> List[Tuple[int, bytes]]:
        frames: List[Tuple[int, bytes]] = []
        self._buf.extend(data)
        while True:
            index = self._buf.find(PROTO_SYNC)
            if index < 0:
                self._buf.clear()
                break
            if index:
                del self._buf[:index]
            if len(self._buf) < 4:
                break
            cmd = self._buf[1]
            length = self._buf[2] | (self._buf[3] << 8)
            if length > MAX_FRAME_PAYLOAD:
                self.crc_errors += 1
                del self._buf[0]
                continue
            total = 4 + length + 2
            if len(self._buf) < total:
                break
            payload = bytes(self._buf[4 : 4 + length])
            received = int.from_bytes(self._buf[4 + length : total], "little")
            if received != crc16_ccitt(bytes(self._buf[1 : 4 + length])):
                self.crc_errors += 1
                del self._buf[0]
                continue
            frames.append((cmd, payload))
            del self._buf[:total]
        return frames


class RelayClient:
    """One connected repeater radio."""

    def __init__(
        self, index: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        self.index = index
        self.reader = reader
        self.writer = writer
        self.decoder = FrameDecoder()
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=CLIENT_QUEUE_MAX)
        self.authenticated = False
        self.rx_armed = False
        self.config: Optional[bytes] = None
        self.tx_count = 0
        self.rx_count = 0
        self.dropped = 0
        self.peer = None
        # Monotonic timestamp of the last frame this client sent: the relay
        # uses it to pick the least recently active client when it must free
        # a slot for a reconnecting peer.
        self.last_activity = time.monotonic()

    def close(self) -> None:
        """Drop this client's queue and close its socket."""
        try:
            self.writer.close()
        except Exception:  # pragma: no cover - best effort close
            pass

    def enqueue(self, cmd: int, payload: bytes = b"") -> None:
        self.last_activity = time.monotonic()
        frame = build_frame(cmd, payload)
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1
            LOGGER.warning(
                "client %d outbound queue full; dropping a frame (dropped=%d)",
                self.index,
                self.dropped,
            )

    async def writer_loop(self) -> None:
        while True:
            frame = await self.queue.get()
            try:
                self.writer.write(frame)
                await self.writer.drain()
            except (ConnectionError, OSError) as exc:
                LOGGER.info("client %d write failed: %s", self.index, exc)
                return


class VirtualRadioRelay:
    """A two-client virtual RF medium.

    Client 0 and client 1 are the two ends of one shared medium: a
    transmission on one end is received on the other end, never echoed
    back to the transmitter.
    """

    def __init__(self, token: str = "") -> None:
        self.token = token
        self.clients: List[RelayClient] = []
        self.started_at = time.monotonic()
        self.tx_forwarded = 0
        self.tx_without_peer = 0

    # -- connection handling -------------------------------------------------

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Exactly two clients live on the medium. When a THIRD connection
        # arrives the slot is taken from the client that has been quiet
        # longest: the openHop radio driver reconnects with backoff by opening
        # a new socket, so a stale socket can still be open here for a moment
        # and must not keep the medium full.
        #
        # A short grace period lets a genuinely closed peer be reaped first
        # (its EOF removes it), so a healthy peer is never evicted in favour
        # of a stale one.
        if len(self.clients) >= MAX_CLIENTS:
            await asyncio.sleep(RECONNECT_GRACE_SECONDS)
        while len(self.clients) >= MAX_CLIENTS:
            stale = min(self.clients, key=lambda c: c.last_activity)
            self.clients.remove(stale)
            LOGGER.warning(
                "evicting client %d (least recently active) to keep exactly two "
                "clients on the medium",
                stale.index,
            )
            stale.close()
        client = RelayClient(len(self.clients), reader, writer)
        self.clients.append(client)
        writer_task = asyncio.create_task(client.writer_loop())
        LOGGER.info("client %d connected (clients=%d)", client.index, len(self.clients))
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for cmd, payload in client.decoder.feed(data):
                    await self.handle_command(client, cmd, payload)
        except (ConnectionError, OSError):
            pass
        finally:
            if client in self.clients:
                self.clients.remove(client)
            LOGGER.info(
                "client %d disconnected (tx=%d rx=%d dropped=%d crc_errors=%d)",
                client.index,
                client.tx_count,
                client.rx_count,
                client.dropped,
                client.decoder.crc_errors,
            )
            writer_task.cancel()
            try:
                await writer_task
            except (asyncio.CancelledError, Exception):
                pass
            writer.close()

    # -- command handling ---------------------------------------------------

    async def handle_command(
        self, client: RelayClient, cmd: int, payload: bytes
    ) -> None:
        if cmd == CMD_PING:
            client.enqueue(CMD_PONG)
        elif cmd == CMD_AUTH:
            self._handle_auth(client, payload)
        elif cmd == CMD_SET_CONFIG:
            client.config = payload
            client.rx_armed = True
            client.enqueue(CMD_CONFIG_RESP, payload[:RADIO_CONFIG_SIZE])
            LOGGER.info(
                "client %d configured (%dB) rx_armed=1", client.index, len(payload)
            )
        elif cmd == CMD_GET_CONFIG:
            client.enqueue(CMD_CONFIG_RESP, client.config or b"")
        elif cmd == CMD_RX_START:
            client.rx_armed = True
            client.enqueue(CMD_RX_STARTED)
        elif cmd == CMD_TX_REQUEST:
            await self._handle_tx(client, payload)
        elif cmd == CMD_STATUS_REQ:
            client.enqueue(CMD_STATUS_RESP, self._status_payload(client))
        elif cmd == CMD_NOISE_REQ:
            client.enqueue(CMD_NOISE_RESP, struct.pack("<h", SIM_NOISE_X10))
        elif cmd == CMD_CAD_REQUEST:
            # The virtual medium is never busy: the repeaters run with
            # LBT disabled, so this is only a fallback answer.
            client.enqueue(CMD_CAD_RESP, b"\x00")
        elif cmd == CMD_SET_CAD_PARAMS:
            client.enqueue(CMD_CAD_PARAMS_RESP)
        elif cmd == CMD_GET_VERSION:
            client.enqueue(CMD_VERSION_RESP, b"openhop-lab-radio-relay 1")
        else:
            LOGGER.warning(
                "client %d sent an unknown command 0x%02X", client.index, cmd
            )
            client.enqueue(CMD_ERROR, bytes([ERR_INVALID_CMD]))

    def _handle_auth(self, client: RelayClient, payload: bytes) -> None:
        if not self.token:
            client.authenticated = True
            client.enqueue(CMD_AUTH_OK)
            return
        if payload == self.token.encode("utf-8"):
            client.authenticated = True
            client.enqueue(CMD_AUTH_OK)
            LOGGER.info("client %d authenticated", client.index)
        else:
            client.enqueue(CMD_ERROR, bytes([ERR_UNAUTHORIZED]))
            LOGGER.warning("client %d failed authentication", client.index)

    async def _handle_tx(self, client: RelayClient, payload: bytes) -> None:
        """Forward a transmission to the OTHER client. Never to the sender."""
        client.tx_count += 1
        peer = self._peer_of(client)
        if peer is None:
            self.tx_without_peer += 1
            LOGGER.warning(
                "client %d transmitted with no peer on the medium", client.index
            )
            client.enqueue(CMD_TX_DONE, struct.pack("<I", SIM_AIRTIME_US))
            return
        digest = hashlib.sha256(payload).hexdigest()[:12]
        LOGGER.info(
            "client %d transmitted %dB (sha256:%s) -> client %d",
            client.index,
            len(payload),
            digest,
            peer.index,
        )
        rx_payload = (
            struct.pack("<hhh", SIM_RSSI_DBM, SIM_SNR_X10, SIM_SIGNAL_RSSI_DBM)
            + payload
        )
        peer.rx_count += 1
        peer.enqueue(CMD_RX_PACKET, rx_payload)
        self.tx_forwarded += 1
        client.enqueue(CMD_TX_DONE, struct.pack("<I", SIM_AIRTIME_US))

    def _peer_of(self, client: RelayClient) -> Optional[RelayClient]:
        for candidate in self.clients:
            if candidate is not client:
                return candidate
        return None

    def _status_payload(self, client: RelayClient) -> bytes:
        return struct.pack(
            STATUS_RESP_FMT,
            int(time.monotonic() - self.started_at),
            client.rx_count,
            client.tx_count,
            client.decoder.crc_errors,
            SIM_RSSI_DBM,
            SIM_SNR_X10,
            SIM_NOISE_X10,
            SIM_TEMP_C,
            0,
        )


async def serve(host: str, port: int, token: str) -> None:
    relay = VirtualRadioRelay(token=token)
    server = await asyncio.start_server(relay.handle_client, host, port)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    LOGGER.info("virtual radio relay listening on %s", addresses)
    LOGGER.info(
        "medium: %d clients max, TX crosses to the peer, no echo to the sender",
        MAX_CLIENTS,
    )
    async with server:
        await server.serve_forever()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default="0.0.0.0",  # nosec B104 # The relay serves the private lab Docker network only; the compose file publishes no host port.
    )
    parser.add_argument("--port", type=int, default=5055)
    parser.add_argument(
        "--token",
        default=os.environ.get("OPENHOP_RADIO_TOKEN", ""),
        help="optional AUTH token; empty means the relay is open (preferred)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(serve(args.host, args.port, args.token))
    except KeyboardInterrupt:
        LOGGER.info("relay stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
