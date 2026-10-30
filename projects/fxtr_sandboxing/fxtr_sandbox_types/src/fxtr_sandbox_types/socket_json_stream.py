"""JSON ObjectStream over an anyio byte stream, using native half-close for EOF.

For runtimes whose host channel is a real byte stream (UDS, TCP) we can use
the underlying transport's `send_eof()` / `EndOfStream` directly, instead of
the escape-frame trick used by `JSONObjectStream` for JSON-callback channels.

Wire format: each frame is a 4-byte big-endian length prefix followed by the
UTF-8 JSON body. `send_eof()` performs a real half-close on the underlying
stream; the peer's next `receive()` (after consuming any in-flight bytes)
raises `anyio.EndOfStream`. `aclose()` closes the stream.

EOF semantics on the read side:

- ``EndOfStream`` raised between frames (no header bytes buffered yet) →
  graceful peer close. Propagate; the RPC reader treats this as the
  "peer closed cleanly" signal.
- ``EndOfStream`` raised mid-header or mid-body → peer crash / abrupt drop.
  Translate to ``anyio.BrokenResourceError`` with a message including how
  many bytes we got vs. expected, since this isn't a clean shutdown.

Both behaviors keep `EndOfStream` as a clean-close signal in the layers
above us.
"""

from __future__ import annotations

import json
import struct

import anyio
import anyio.abc

from .json_object_stream import JSONTypes

# 4-byte big-endian unsigned int — same wire framing the old
# fxtr_service_runner.protocol module used for length-prefixed JSON.
_LENGTH_STRUCT = struct.Struct("!I")
_HEADER_SIZE = _LENGTH_STRUCT.size


class SocketJSONStream(anyio.abc.ObjectStream[JSONTypes]):
    """Anyio-compatible ObjectStream[JSONTypes] backed by an
    `anyio.abc.ByteStream`.

    Length-prefixed JSON on send. Receive reads the 4-byte header, then the
    body, then `json.loads`. Lets ``anyio.EndOfStream`` propagate naturally
    on a clean peer half-close (between frames). A mid-frame EOF translates
    to ``anyio.BrokenResourceError``.
    """

    def __init__(self, stream: anyio.abc.ByteStream) -> None:
        self._stream = stream

    async def send(self, item: JSONTypes) -> None:
        encoded = json.dumps(item).encode("utf-8")
        header = _LENGTH_STRUCT.pack(len(encoded))
        await self._stream.send(header + encoded)

    async def receive(self) -> JSONTypes:
        header = await self._read_exact(_HEADER_SIZE, frame_kind="header")
        (length,) = _LENGTH_STRUCT.unpack(header)
        body = await self._read_exact(length, frame_kind="body")
        return json.loads(body.decode("utf-8"))

    async def _read_exact(self, n: int, *, frame_kind: str) -> bytes:
        """Read exactly `n` bytes.

        EOF before any bytes have been buffered → propagate `EndOfStream`
        (clean between-frame close). EOF after some bytes have been read →
        translate to `BrokenResourceError` (mid-frame truncation).
        """
        if n == 0:
            return b""
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = await self._stream.receive(n - len(buf))
            except anyio.EndOfStream:
                if not buf and frame_kind == "header":
                    raise
                raise anyio.BrokenResourceError(
                    f"incomplete {frame_kind}: got {len(buf)} of {n} bytes"
                ) from None
            if not chunk:
                # anyio is supposed to raise EndOfStream rather than return
                # b"" on EOF; defensive fallback in case any wrapper differs.
                if not buf and frame_kind == "header":
                    raise anyio.EndOfStream
                raise anyio.BrokenResourceError(
                    f"incomplete {frame_kind}: got {len(buf)} of {n} bytes"
                )
            buf.extend(chunk)
        return bytes(buf)

    def __aiter__(self) -> "SocketJSONStream":
        return self

    async def __anext__(self) -> JSONTypes:
        try:
            return await self.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            raise StopAsyncIteration

    async def send_eof(self) -> None:
        await self._stream.send_eof()

    async def aclose(self) -> None:
        await self._stream.aclose()
