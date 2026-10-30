"""Tests for ``SocketJSONStream``.

Validates the JSON-over-byte-stream framing (4-byte big-endian length prefix
plus UTF-8 JSON body), and the EOF behaviors that distinguish a clean
between-frames close (propagate ``anyio.EndOfStream``) from a mid-frame
truncation (raise ``anyio.BrokenResourceError``).

Uses an anyio UDS listener + connect for the round-trip tests since that's
exactly the production wire shape; the truncation tests sneak partial bytes
through a connected pair before half-closing the writer.
"""

from __future__ import annotations

import struct
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import anyio.abc
import pytest
from fxtr_sandbox_types.socket_json_stream import SocketJSONStream

_LENGTH_STRUCT = struct.Struct("!I")


@asynccontextmanager
async def _connected_unix_pair() -> (
    AsyncIterator[tuple[anyio.abc.SocketStream, anyio.abc.SocketStream]]
):
    """Yield a (server_side, client_side) pair of anyio UDS streams.

    Uses a temp dir for the socket path; both ends are properly closed when
    the CM exits.
    """
    with tempfile.TemporaryDirectory(prefix="fxtr_test_socket_") as tmp:
        path = Path(tmp) / "test.sock"
        listener = await anyio.create_unix_listener(path)
        try:
            client_stream: anyio.abc.SocketStream | None = None
            server_stream: anyio.abc.SocketStream | None = None

            async def _accept() -> None:
                nonlocal server_stream
                server_stream = await listener.accept()

            async with anyio.create_task_group() as tg:
                tg.start_soon(_accept)
                client_stream = await anyio.connect_unix(path)

            assert server_stream is not None
            try:
                yield server_stream, client_stream
            finally:
                await client_stream.aclose()
                await server_stream.aclose()
        finally:
            await listener.aclose()


async def test_round_trip_simple() -> None:
    async with _connected_unix_pair() as (server, client):
        host = SocketJSONStream(server)
        guest = SocketJSONStream(client)

        await host.send({"hello": "world"})
        assert await guest.receive() == {"hello": "world"}


async def test_round_trip_multiple_frames() -> None:
    async with _connected_unix_pair() as (server, client):
        host = SocketJSONStream(server)
        guest = SocketJSONStream(client)

        for i in range(20):
            await host.send({"i": i})
        for i in range(20):
            assert await guest.receive() == {"i": i}


async def test_round_trip_various_types() -> None:
    samples = [
        None,
        True,
        False,
        0,
        -7,
        3.14,
        "hello",
        "",
        [],
        {},
        [1, 2, 3, "four"],
        {"nested": {"deep": [True, None, 42]}},
        "héllo wörld",  # non-ASCII utf-8
    ]
    async with _connected_unix_pair() as (server, client):
        host = SocketJSONStream(server)
        guest = SocketJSONStream(client)

        for item in samples:
            await host.send(item)
        for expected in samples:
            assert await guest.receive() == expected


async def test_send_eof_then_receive_raises_end_of_stream() -> None:
    """Clean half-close between frames: receiver gets EndOfStream."""
    async with _connected_unix_pair() as (server, client):
        host = SocketJSONStream(server)
        guest = SocketJSONStream(client)

        await host.send({"final": "frame"})
        await host.send_eof()

        assert await guest.receive() == {"final": "frame"}
        with pytest.raises(anyio.EndOfStream):
            await guest.receive()


async def test_eof_with_no_in_flight_data_raises_end_of_stream() -> None:
    async with _connected_unix_pair() as (server, client):
        host = SocketJSONStream(server)
        guest = SocketJSONStream(client)

        await host.send_eof()

        with pytest.raises(anyio.EndOfStream):
            await guest.receive()


async def test_truncated_header_raises_broken_resource_error() -> None:
    """Peer writes 2 bytes of the 4-byte header, then closes — receiver
    sees a partial header and must surface a distinguishable error."""
    async with _connected_unix_pair() as (server, client):
        # Send raw partial header bytes through the underlying socket,
        # then half-close. Don't go through SocketJSONStream's send
        # (which always writes a complete frame).
        await server.send(b"\x00\x00")
        await server.send_eof()

        guest = SocketJSONStream(client)
        with pytest.raises(anyio.BrokenResourceError, match="incomplete header"):
            await guest.receive()


async def test_truncated_body_raises_broken_resource_error() -> None:
    """Peer writes the header announcing N bytes, then sends fewer than N
    body bytes before closing. Receiver should see a body truncation."""
    async with _connected_unix_pair() as (server, client):
        # Header says 100 bytes incoming; only 5 actually delivered.
        await server.send(_LENGTH_STRUCT.pack(100) + b"abcde")
        await server.send_eof()

        guest = SocketJSONStream(client)
        with pytest.raises(anyio.BrokenResourceError, match="incomplete body"):
            await guest.receive()


async def test_aclose_then_send_raises() -> None:
    """After aclose, further sends fail — the underlying stream is gone."""
    async with _connected_unix_pair() as (server, client):
        host = SocketJSONStream(server)
        await host.aclose()

        with pytest.raises((anyio.ClosedResourceError, anyio.BrokenResourceError)):
            await host.send({"too": "late"})

        # Drain the receive side so the CM cleanup doesn't trip.
        guest = SocketJSONStream(client)
        with pytest.raises(anyio.EndOfStream):
            await guest.receive()


async def test_async_iteration_stops_on_eof() -> None:
    async with _connected_unix_pair() as (server, client):
        host = SocketJSONStream(server)
        guest = SocketJSONStream(client)

        for i in range(3):
            await host.send(i)
        await host.send_eof()

        received: list[int] = []
        async for item in guest:
            received.append(item)
        assert received == [0, 1, 2]
