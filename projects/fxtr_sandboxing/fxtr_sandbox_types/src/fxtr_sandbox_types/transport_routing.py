"""Routing-header wire protocol for the container transport side channel.

When a sandbox dials the host's multiplexed transport socket
(``/run/fxtr/transport.sock``), the first thing it sends is a routing
header naming the transport token it wants to redeem. The host validates
the token against the env-level :class:`TransportConnectionServer` and
sends back an ``ok``/``error`` response. After ``ok`` the connection is
the byte bridge to whatever handler the token was registered with.

Protocol:

- **Request**: ``[4-byte big-endian length][JSON {"token": "<hex>"}]``.
- **Response**: ``[4-byte big-endian length][JSON]`` —
  ``{"ok": true}`` on success, ``{"ok": false, "error": "<msg>"}`` on
  bad / consumed token.

Helpers in this module are pure-Python, depend only on ``anyio.abc`` for
the byte stream type, and live in ``fxtr_sandbox_types`` so both the
host (``fxtr_sandboxing``) and the in-container shim
(``fxtr_service_runner``) can share the same code.
"""

from __future__ import annotations

import json
import struct

import anyio
import anyio.abc

# Length-prefix struct: 4-byte big-endian unsigned int. Matches the
# framing used by ``SocketJSONStream`` / ``protocol.py``.
_LENGTH_STRUCT = struct.Struct("!I")


async def write_routing_header(stream: anyio.abc.ByteStream, token: str) -> None:
    """Send a ``{"token": token}`` routing header on ``stream``."""
    body = json.dumps({"token": token}).encode("utf-8")
    await stream.send(_LENGTH_STRUCT.pack(len(body)) + body)


async def read_routing_header(stream: anyio.abc.ByteStream) -> str:
    """Read a ``{"token": ...}`` routing header from ``stream``.

    Raises :class:`ValueError` on a malformed header / missing or
    non-string token field. Raises :class:`anyio.EndOfStream` if the
    stream closes mid-header.
    """
    raw_len = await _recv_exact(stream, _LENGTH_STRUCT.size)
    (length,) = _LENGTH_STRUCT.unpack(raw_len)
    body = await _recv_exact(stream, length)
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict) or "token" not in parsed:
        raise ValueError(f"transport routing header missing 'token' field: {parsed!r}")
    token = parsed["token"]
    if not isinstance(token, str):
        raise ValueError(f"transport routing header 'token' is not a string: {token!r}")
    return token


async def write_routing_response(
    stream: anyio.abc.ByteStream, *, ok: bool, error: str | None = None
) -> None:
    """Send a routing-header response on ``stream``.

    Successful response: ``{"ok": true}``. Failure: ``{"ok": false,
    "error": <msg>}``.
    """
    payload: dict[str, object] = {"ok": ok}
    if not ok:
        payload["error"] = error or "bad transport token"
    body = json.dumps(payload).encode("utf-8")
    await stream.send(_LENGTH_STRUCT.pack(len(body)) + body)


async def read_routing_response(stream: anyio.abc.ByteStream) -> dict[str, object]:
    """Read ``[length][JSON]`` response from ``stream``; return the parsed dict.

    Raises :class:`ValueError` if the response isn't a JSON object;
    :class:`anyio.EndOfStream` if the stream closes mid-frame.
    """
    raw_len = await _recv_exact(stream, _LENGTH_STRUCT.size)
    (length,) = _LENGTH_STRUCT.unpack(raw_len)
    body = await _recv_exact(stream, length)
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"transport routing response is not a JSON object: {parsed!r}")
    return parsed


async def _recv_exact(stream: anyio.abc.ByteStream, n: int) -> bytes:
    """Read exactly ``n`` bytes from ``stream`` or raise on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = await stream.receive(n - len(buf))
        if not chunk:
            raise anyio.EndOfStream
        buf.extend(chunk)
    return bytes(buf)
