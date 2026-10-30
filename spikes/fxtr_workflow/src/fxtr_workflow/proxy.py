"""HTTP proxy for network transport routes.

Copied from isolation_spike/isolation_spike/proxy.py. Provides controlled HTTP
access through ``TransportRoute`` configuration: path allowlisting, header
injection, and upstream forwarding.

The ``proxy_connection()`` function takes an anyio byte stream (the client
side), a ``TransportRoute``, and an ``aiohttp.ClientSession`` for upstream.
It parses the incoming HTTP/1.1 request with h11, checks path allowlists,
injects secret headers, forwards to the upstream, and streams the response
back.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from urllib.parse import urlparse

import aiohttp
import anyio.abc
import h11


@dataclasses.dataclass
class TransportRoute:
    """Configuration for a single network transport.

    Runtime config (not an entity) — carries secrets, so it must not be
    stored in the entity graph.
    """

    upstream_base_url: str
    """Base URL for the upstream service (e.g. "https://api.anthropic.com")."""

    injected_headers: dict[str, str]
    """Headers merged into every proxied request (e.g. Authorization)."""

    allowed_path_prefixes: list[str] | None = None
    """If set, only paths starting with one of these prefixes are allowed.
    None means all paths are allowed."""


def _path_allowed(path: str, prefixes: list[str] | None) -> bool:
    """Check whether a request path is allowed by the route's prefix list."""
    if prefixes is None:
        return True
    return any(path.startswith(p) for p in prefixes)


def _merge_headers(
    request_headers: Mapping[str, str],
    route: TransportRoute,
    upstream_host: str,
) -> dict[str, str]:
    """Build the headers to send upstream.

    - Copies task headers (except Host and hop-by-hop headers)
    - Sets Host to the upstream hostname
    - Applies injected_headers (overwrites on conflict)
    """
    # Hop-by-hop headers that shouldn't be forwarded
    skip = {"host", "transfer-encoding", "connection", "keep-alive"}

    merged: dict[str, str] = {}
    for key, value in request_headers.items():
        if key.lower() not in skip:
            merged[key] = value

    merged["Host"] = upstream_host
    merged.update(route.injected_headers)
    return merged


# ---------------------------------------------------------------------------
# h11 helpers
# ---------------------------------------------------------------------------


async def _read_request(
    conn: h11.Connection, stream: anyio.abc.ByteStream
) -> tuple[h11.Request | None, bytes]:
    """Read one HTTP/1.1 request from the client stream.

    Returns (request, body) or (None, b"") if the client closed.
    """
    request: h11.Request | None = None
    body_parts: list[bytes] = []

    while True:
        event = conn.next_event()

        if event is h11.NEED_DATA:
            try:
                data = await stream.receive(65536)
            except (anyio.EndOfStream, anyio.ClosedResourceError):
                return None, b""
            conn.receive_data(data)
            continue

        if isinstance(event, h11.Request):
            request = event
        elif isinstance(event, h11.Data):
            body_parts.append(event.data)
        elif isinstance(event, h11.EndOfMessage):
            break
        elif isinstance(event, (h11.ConnectionClosed, type(h11.PAUSED))):
            return None, b""

    return request, b"".join(body_parts)


async def _send_error(
    conn: h11.Connection,
    stream: anyio.abc.ByteStream,
    status_code: int,
    body: bytes,
) -> None:
    """Send an error response to the client."""
    response = h11.Response(
        status_code=status_code,
        headers=[
            ("Content-Type", "text/plain"),
            ("Content-Length", str(len(body))),
            ("Connection", "close"),
        ],
    )
    await stream.send(conn.send(response))
    await stream.send(conn.send(h11.Data(data=body)))
    await stream.send(conn.send(h11.EndOfMessage()))


# ---------------------------------------------------------------------------
# Main proxy function
# ---------------------------------------------------------------------------


async def proxy_connection(
    client_stream: anyio.abc.ByteStream,
    route: TransportRoute,
    upstream_session: aiohttp.ClientSession,
) -> None:
    """Proxy HTTP traffic from *client_stream* through *route* to upstream.

    Handles the full connection lifetime: reads requests, proxies them to
    the upstream via *upstream_session*, and writes responses back.  Supports
    HTTP/1.1 keep-alive (multiple request-response cycles per connection).

    Does **not** close *client_stream* — the caller owns the stream lifecycle
    and should wrap the call in ``async with stream:`` or close it afterwards.
    """
    conn = h11.Connection(h11.SERVER)
    upstream_host = urlparse(route.upstream_base_url).netloc

    while True:
        # --- Read one request ------------------------------------------
        request, body = await _read_request(conn, client_stream)
        if request is None:
            return  # Client closed

        target = request.target.decode("ascii")
        path_only = urlparse(target).path

        # --- Path allowlist check --------------------------------------
        if not _path_allowed(path_only, route.allowed_path_prefixes):
            await _send_error(conn, client_stream, 403, b"Path not allowed")
            return

        # --- Forward to upstream ---------------------------------------
        req_headers = {k.decode(): v.decode() for k, v in request.headers}
        headers = _merge_headers(req_headers, route, upstream_host)
        upstream_url = route.upstream_base_url + target
        method = request.method.decode("ascii")

        try:
            async with upstream_session.request(
                method,
                upstream_url,
                headers=headers,
                data=body or None,
                allow_redirects=False,
            ) as upstream_resp:
                # Build response headers, filtering hop-by-hop
                resp_headers: list[tuple[str, str]] = []
                skip_resp = {"transfer-encoding", "connection", "keep-alive"}
                for key, value in upstream_resp.headers.items():
                    if key.lower() not in skip_resp:
                        resp_headers.append((key, value))

                response = h11.Response(
                    status_code=upstream_resp.status,
                    headers=resp_headers,
                )
                await client_stream.send(conn.send(response))

                async for chunk in upstream_resp.content.iter_any():
                    await client_stream.send(conn.send(h11.Data(data=chunk)))

                await client_stream.send(conn.send(h11.EndOfMessage()))

        except aiohttp.ClientError as e:
            await _send_error(conn, client_stream, 502, f"Upstream error: {e}".encode())
            return

        # --- Prepare for next request (keep-alive) ---------------------
        try:
            conn.start_next_cycle()
        except h11.LocalProtocolError:
            return  # Connection not reusable
