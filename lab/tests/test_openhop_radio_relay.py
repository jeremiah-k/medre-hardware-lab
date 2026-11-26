"""Unit tests for the lab's virtual radio relay.

These tests exercise the relay in-process (no Docker, no network): the
framing, the CRC, the command responses and the two-client mapping with
its no-echo rule.

The relay is the LAB side of the openHop Modem protocol, so its framing
must match the pinned openHop implementation exactly. The upstream
constants are used as the reference where the checkout is available
(sources/openhop-core), and the hard-coded vectors below are the
reference otherwise.
"""

from __future__ import annotations

import asyncio
import importlib.util
import struct
from pathlib import Path

import pytest

RELAY_PATH = (
    Path(__file__).resolve().parents[1] / "openhop-radio" / "openhop_radio_relay.py"
)
UPSTREAM_CONSTANTS = (
    Path(__file__).resolve().parents[2]
    / "sources"
    / "openhop-core"
    / "src"
    / "openhop_core"
    / "hardware"
    / "protocol_constants.py"
)


def _load_relay():
    spec = importlib.util.spec_from_file_location("openhop_radio_relay", RELAY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay_mod = _load_relay()


# ---------------------------------------------------------------------------
# Framing and CRC
# ---------------------------------------------------------------------------


def test_crc16_is_ccitt_false():
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xor-out."""
    assert relay_mod.crc16_ccitt(b"123456789") == 0x29B1
    assert relay_mod.crc16_ccitt(b"") == 0xFFFF


def test_crc_matches_upstream_when_the_checkout_is_present():
    """The relay's CRC must equal the pinned openHop implementation's."""
    if not UPSTREAM_CONSTANTS.exists():
        pytest.skip("the pinned openhop-core checkout is not available")
    spec = importlib.util.spec_from_file_location(
        "upstream_protocol_constants", UPSTREAM_CONSTANTS
    )
    upstream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upstream)
    for payload in (b"", b"\x00", b"meshcore", bytes(range(64))):
        assert relay_mod.crc16_ccitt(payload) == upstream.crc16_ccitt(payload)


def test_frame_layout():
    """SYNC | CMD | LEN (u16 LE) | PAYLOAD | CRC (u16 LE) over CMD+LEN+PAYLOAD."""
    frame = relay_mod.build_frame(relay_mod.CMD_PING)
    assert frame[0] == relay_mod.PROTO_SYNC == 0xAA
    assert frame[1] == relay_mod.CMD_PING
    assert struct.unpack("<H", frame[2:4])[0] == 0
    assert len(frame) == 6
    crc = struct.unpack("<H", frame[-2:])[0]
    assert crc == relay_mod.crc16_ccitt(frame[1:4])


def test_frame_length_is_little_endian():
    frame = relay_mod.build_frame(relay_mod.CMD_TX_REQUEST, b"abcd")
    assert struct.unpack("<H", frame[2:4])[0] == 4
    assert frame[4:8] == b"abcd"
    assert relay_mod.crc16_ccitt(frame[1:8]) == struct.unpack("<H", frame[-2:])[0]


def test_frame_matches_upstream_when_the_checkout_is_present():
    if not UPSTREAM_CONSTANTS.exists():
        pytest.skip("the pinned openhop-core checkout is not available")
    spec = importlib.util.spec_from_file_location(
        "upstream_protocol_constants", UPSTREAM_CONSTANTS
    )
    upstream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upstream)
    for cmd, payload in (
        (upstream.CMD_PING, b""),
        (upstream.CMD_TX_REQUEST, b"\x01\x02"),
    ):
        assert relay_mod.build_frame(cmd, payload) == upstream.build_frame(cmd, payload)


def test_decoder_round_trips_and_resyncs():
    decoder = relay_mod.FrameDecoder()
    frames = relay_mod.build_frame(relay_mod.CMD_SET_CONFIG, b"cfg")
    assert decoder.feed(frames) == [(relay_mod.CMD_SET_CONFIG, b"cfg")]

    # A split frame is buffered until it is complete.
    decoder = relay_mod.FrameDecoder()
    assert decoder.feed(frames[:3]) == []
    assert decoder.feed(frames[3:]) == [(relay_mod.CMD_SET_CONFIG, b"cfg")]

    # Leading junk before the SYNC byte is skipped.
    decoder = relay_mod.FrameDecoder()
    assert decoder.feed(b"\x00\x11" + frames) == [(relay_mod.CMD_SET_CONFIG, b"cfg")]

    # A bad CRC drops the frame and resynchronises on the next one.
    decoder = relay_mod.FrameDecoder()
    broken = bytearray(frames)
    broken[-1] ^= 0xFF
    assert decoder.feed(bytes(broken) + frames) == [(relay_mod.CMD_SET_CONFIG, b"cfg")]
    assert decoder.crc_errors == 1


# ---------------------------------------------------------------------------
# Command handling and the two-client mapping
# ---------------------------------------------------------------------------


class _Peer:
    """A test client speaking the modem protocol over a TCP socket."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.decoder = relay_mod.FrameDecoder()

    async def send(self, cmd: int, payload: bytes = b"") -> None:
        self.writer.write(relay_mod.build_frame(cmd, payload))
        await self.writer.drain()

    async def recv(self, timeout: float = 5.0) -> tuple[int, bytes]:
        async def _read() -> tuple[int, bytes]:
            while True:
                data = await self.reader.read(4096)
                if not data:
                    raise AssertionError("the relay closed the connection")
                frames = self.decoder.feed(data)
                if frames:
                    return frames[0]

        return await asyncio.wait_for(_read(), timeout=timeout)

    async def expect(self, cmd: int, timeout: float = 5.0) -> bytes:
        got_cmd, payload = await self.recv(timeout=timeout)
        assert got_cmd == cmd, f"expected command 0x{cmd:02X}, got 0x{got_cmd:02X}"
        return payload

    def close(self) -> None:
        self.writer.close()


async def _connect(port: int) -> _Peer:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    return _Peer(reader, writer)


def test_two_client_mapping_and_no_echo():
    """A TX on one client is RX'd by the OTHER client, never echoed back."""

    async def scenario():
        relay = relay_mod.VirtualRadioRelay()
        server = await asyncio.start_server(relay.handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            serve_task = asyncio.create_task(server.serve_forever())
            try:
                a = await _connect(port)
                b = await _connect(port)

                # Both ends handshake like the openHop radio driver does.
                await a.send(relay_mod.CMD_PING)
                assert await a.expect(relay_mod.CMD_PONG) == b""
                await b.send(relay_mod.CMD_PING)
                assert await b.expect(relay_mod.CMD_PONG) == b""

                payload = b"meshcore-group-text-payload"
                await b.send(relay_mod.CMD_TX_REQUEST, payload)

                # The peer receives the payload as an RX_PACKET frame with the
                # documented 6-byte signal header.
                cmd, rx_payload = await a.recv()
                assert cmd == relay_mod.CMD_RX_PACKET
                rssi, snr_x10, signal_rssi = struct.unpack("<hhh", rx_payload[:6])
                assert rx_payload[6:] == payload
                assert (rssi, snr_x10, signal_rssi) == (
                    relay_mod.SIM_RSSI_DBM,
                    relay_mod.SIM_SNR_X10,
                    relay_mod.SIM_SIGNAL_RSSI_DBM,
                )

                # The transmitter is told the TX completed.
                await b.expect(relay_mod.CMD_TX_DONE)

                # NO echo: the sender must not receive its own packet.
                with pytest.raises(asyncio.TimeoutError):
                    await b.recv(timeout=0.6)

                a.close()
                b.close()
            finally:
                serve_task.cancel()
                try:
                    await serve_task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())


def test_rx_start_is_acknowledged_and_config_is_echoed():
    async def scenario():
        relay = relay_mod.VirtualRadioRelay()
        server = await asyncio.start_server(relay.handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            serve_task = asyncio.create_task(server.serve_forever())
            try:
                peer = await _connect(port)
                config = struct.pack(
                    relay_mod.RADIO_CONFIG_FMT, 869618000, 62500, 8, 8, 14, 0x12, 32
                )
                await peer.send(relay_mod.CMD_SET_CONFIG, config)
                assert await peer.expect(relay_mod.CMD_CONFIG_RESP) == config

                await peer.send(relay_mod.CMD_RX_START)
                assert await peer.expect(relay_mod.CMD_RX_STARTED) == b""

                await peer.send(relay_mod.CMD_STATUS_REQ)
                status = await peer.expect(relay_mod.CMD_STATUS_RESP)
                assert len(status) == relay_mod.STATUS_RESP_SIZE

                peer.close()
            finally:
                serve_task.cancel()
                try:
                    await serve_task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())


def test_unknown_command_is_rejected_with_an_error_frame():
    async def scenario():
        relay = relay_mod.VirtualRadioRelay()
        server = await asyncio.start_server(relay.handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            serve_task = asyncio.create_task(server.serve_forever())
            try:
                peer = await _connect(port)
                await peer.send(0x77)
                payload = await peer.expect(relay_mod.CMD_ERROR)
                assert payload == bytes([relay_mod.ERR_INVALID_CMD])
                peer.close()
            finally:
                serve_task.cancel()
                try:
                    await serve_task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())


def test_auth_is_optional_and_rejected_when_a_token_is_configured():
    """No token is preferred: the relay stays open. A configured token must match."""

    async def scenario(relay):
        server = await asyncio.start_server(relay.handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            serve_task = asyncio.create_task(server.serve_forever())
            try:
                peer = await _connect(port)
                await peer.send(relay_mod.CMD_AUTH, b"wrong-token")
                if relay.token:
                    payload = await peer.expect(relay_mod.CMD_ERROR)
                    assert payload == bytes([relay_mod.ERR_UNAUTHORIZED])
                else:
                    assert await peer.expect(relay_mod.CMD_AUTH_OK) == b""
                peer.close()
            finally:
                serve_task.cancel()
                try:
                    await serve_task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario(relay_mod.VirtualRadioRelay()))
    asyncio.run(scenario(relay_mod.VirtualRadioRelay(token="secret")))
