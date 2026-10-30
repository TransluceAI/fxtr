"""Host-side HTTP proxy for sandboxed network transports.

Provides controlled HTTP access from sandboxed tasks to external APIs.

Two flavors:

- :func:`proxy_connection` (route-based): the upstream URL is pinned by a
  :class:`TransportRoute` (one host per authorizer); the proxy applies a
  path allowlist and injects secret headers before forwarding.
- :func:`passthrough_proxy_connection` (host-agnostic): each request's
  ``Host:`` header picks the upstream; an optional
  :class:`PassthroughHostsConfig` allow- or block-lists individual host
  names. All upstream calls go out as HTTPS regardless of what scheme
  the agent's client thought it was using on the proxy side (the proxy
  side is always plain HTTP/1.1 over the byte stream).

Both flavors parse incoming HTTP/1.1 with h11 and use a single
``aiohttp.ClientSession`` for the upstream half.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import AsyncIterator, Mapping
from urllib.parse import urlparse

import aiohttp
import anyio.abc
import h11

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class TransportRoute:
    """Configuration for a single network transport.

    Runtime config (not an entity) — carries secrets, so it must not be
    stored in the entity graph. Identified by content (upstream URL +
    headers + allowlist), not by name; in stage 3 the lookup key for
    side-channel routing is a single-use token, not a route name.
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


async def _next_event(conn: h11.Connection, stream: anyio.abc.ByteStream) -> h11.Event | type:
    """Pull the next h11 event off ``stream``, feeding bytes as needed.

    Returns the event (or ``h11.PAUSED`` / ``h11.ConnectionClosed`` to
    signal connection end). Raises ``h11.ProtocolError`` for malformed
    input — h11's job, not ours, to police the wire format.
    """
    while True:
        event = conn.next_event()
        if event is h11.NEED_DATA:
            try:
                data = await stream.receive(65536)
            except (anyio.EndOfStream, anyio.ClosedResourceError):
                # Tell h11 the peer has closed; the next event will be
                # h11.ConnectionClosed (or h11.PAUSED if mid-message).
                conn.receive_data(b"")
                continue
            conn.receive_data(data)
            continue
        return event


async def _read_request_headers(
    conn: h11.Connection, stream: anyio.abc.ByteStream
) -> h11.Request | None:
    """Read events until we have an ``h11.Request`` (headers only).

    Returns ``None`` if the client closed before sending a request line —
    the keep-alive idle path that has no more requests on the wire.
    """
    event = await _next_event(conn, stream)
    if isinstance(event, h11.Request):
        return event
    # Anything else here means client closed (PAUSED / ConnectionClosed)
    # before sending headers — treat as "no more requests."
    return None


async def _iter_body(conn: h11.Connection, stream: anyio.abc.ByteStream) -> AsyncIterator[bytes]:
    """Yield ``Data`` chunks from the in-progress request body until
    ``EndOfMessage`` (or peer close).

    h11 already knows the body framing from the headers (Content-Length,
    Transfer-Encoding, or "no body for this method"); we just iterate.
    For a request with no body, this yields nothing and returns
    immediately.
    """
    while True:
        event = await _next_event(conn, stream)
        if isinstance(event, h11.Data):
            if event.data:
                yield bytes(event.data)
        elif isinstance(event, h11.EndOfMessage):
            return
        else:
            # h11.PAUSED / h11.ConnectionClosed — peer hung up mid-body.
            return


async def _send_error(
    conn: h11.Connection,
    stream: anyio.abc.ByteStream,
    status_code: int,
    body: bytes,
) -> None:
    """Send an error response, transition h11 to CLOSED, half-close the
    socket write side, and drain any straggling client writes.

    The combined transition-and-drain is what avoids a ``BrokenPipeError``
    on the client side when our socket close races their pending writes.
    Once the client sees our half-close (response done, connection
    closing) and finishes its own write, our ``stream.receive()`` returns
    EOF and we're done.
    """
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
    # h11 state transition. With ``Connection: close`` we're now in
    # MUST_CLOSE; ``ConnectionClosed`` moves us to CLOSED. Returns no
    # bytes — pure state move.
    conn.send(h11.ConnectionClosed())

    # Socket-level half-close + drain. Best-effort: any error here is
    # fine, the caller will close the stream via ``async with`` anyway.
    try:
        await stream.send_eof()
    except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
        return
    try:
        while await stream.receive(65536):
            pass
    except (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
        pass


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
    the upstream via *upstream_session*, and writes responses back. Supports
    HTTP/1.1 keep-alive (multiple request–response cycles per connection).

    The path-allowlist decision happens against the request headers
    alone — we don't buffer the body before deciding. On allow, the body
    streams through to the upstream chunk-by-chunk via h11's incremental
    event stream. On deny, we drain the in-flight body so the client can
    finish its write cleanly before we close.

    Owns the lifetime of *client_stream*: the outer ``async with
    client_stream`` always runs its ``__aexit__`` on the way out —
    normal return, swallowed ``Exception``, or cancellation —
    guaranteeing close. Callers can therefore just
    ``start_soon(proxy_connection, ...)`` without worrying about it.

    Any unhandled ``Exception`` from the body is caught and logged;
    we never propagate. The proxy is meant to be fired-and-forgotten
    on a shared task group (the env's ``env_tg``), so a misbehaving
    upstream or client must not be able to cancel sibling tasks. The
    client just sees a truncated / closed connection on its end.
    Cancellation (``BaseException``) still propagates so the env can
    tear us down.
    """
    async with client_stream:
        try:
            conn = h11.Connection(h11.SERVER)
            upstream_host = urlparse(route.upstream_base_url).netloc

            while True:
                # --- Read just the request line + headers ----------------------
                request = await _read_request_headers(conn, client_stream)
                if request is None:
                    return  # Client closed without sending a new request.

                target = request.target.decode("ascii")
                path_only = urlparse(target).path
                method = request.method.decode("ascii")

                # --- Path allowlist (decide before reading body) ---------------
                if not _path_allowed(path_only, route.allowed_path_prefixes):
                    # Send the error immediately — the client should see the
                    # 403 + ``Connection: close`` and stop writing. Any body
                    # bytes already in flight get absorbed by ``_send_error``'s
                    # socket-level drain.
                    await _send_error(conn, client_stream, 403, b"Path not allowed")
                    return

                # --- Forward to upstream, streaming the body chunk-by-chunk ----
                req_headers = {k.decode(): v.decode() for k, v in request.headers}
                headers = _merge_headers(req_headers, route, upstream_host)
                upstream_url = route.upstream_base_url + target

                try:
                    async with upstream_session.request(
                        method,
                        upstream_url,
                        headers=headers,
                        data=_iter_body(conn, client_stream),
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
                    # Upstream choked mid-forward. Send 502 immediately;
                    # ``_send_error``'s drain absorbs any client body bytes
                    # still in flight after the half-close.
                    await _send_error(conn, client_stream, 502, f"Upstream error: {e}".encode())
                    return

                # --- Prepare for next request (keep-alive) ---------------------
                try:
                    conn.start_next_cycle()
                except h11.LocalProtocolError:
                    return  # Connection not reusable
        except Exception:
            logger.exception(
                "proxy_connection: unhandled exception; closing client connection",
            )


# ---------------------------------------------------------------------------
# Passthrough proxy (host picked per-request from the Host: header)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PassthroughHostsConfig:
    """Optional host filter for :func:`passthrough_proxy_connection`.

    At most one of ``allowed_hosts`` / ``blocked_hosts`` may be set:

    - ``allowed_hosts`` (allowlist): only requests whose ``Host:`` header
      hostname (port stripped, case-folded) matches one of these are
      forwarded; everything else gets a 403.
    - ``blocked_hosts`` (denylist): the opposite — listed hosts are
      refused, everything else is allowed.
    - Both ``None`` (default): no filtering; any host the request asks
      for is forwarded.

    Hostname matching is exact (modulo case and port). Subdomain
    wildcards aren't supported — list each subdomain explicitly.
    """

    allowed_hosts: tuple[str, ...] | None = None
    blocked_hosts: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.allowed_hosts is not None and self.blocked_hosts is not None:
            raise ValueError(
                "PassthroughHostsConfig: set at most one of allowed_hosts / blocked_hosts"
            )

    def is_allowed(self, host: str) -> bool:
        """Return whether ``host`` (already port-stripped and case-
        folded) passes the configured filter."""
        if self.allowed_hosts is not None:
            return host in {h.lower() for h in self.allowed_hosts}
        if self.blocked_hosts is not None:
            return host not in {h.lower() for h in self.blocked_hosts}
        return True


def _extract_host(headers: Mapping[str, str]) -> str | None:
    """Return the lowercase, port-stripped hostname from the request's
    ``Host:`` header, or ``None`` if it's missing.

    Header lookup is case-insensitive (per HTTP) — we iterate keys
    rather than relying on dict ordering."""
    for key, value in headers.items():
        if key.lower() == "host":
            return value.split(":", 1)[0].strip().lower()
    return None


async def passthrough_proxy_connection(
    client_stream: anyio.abc.ByteStream,
    config: PassthroughHostsConfig,
    upstream_session: aiohttp.ClientSession,
) -> None:
    """Proxy HTTP traffic, picking the upstream from each request's
    ``Host:`` header rather than a fixed route.

    Upstream calls always use HTTPS — there's no per-host scheme
    selection in v1. Agents that need plain HTTP upstreams have to
    extend this function (e.g. via an absolute-URI request line, or
    a config flag).

    Same lifecycle / framing as :func:`proxy_connection`: the outer
    ``async with client_stream`` guarantees close on every exit path
    (including the swallowed-exception path below), supports
    keep-alive, drains body bytes cleanly on refusal/error.

    Hosts are filtered via ``config`` — see
    :class:`PassthroughHostsConfig`. A refused host gets a 403; a
    missing ``Host:`` header gets a 400; an upstream error gets a
    502.

    Any unhandled ``Exception`` is caught and logged; we never
    propagate. See :func:`proxy_connection` for the rationale.
    """
    async with client_stream:
        try:
            conn = h11.Connection(h11.SERVER)

            while True:
                request = await _read_request_headers(conn, client_stream)
                if request is None:
                    return  # Client closed.

                target = request.target.decode("ascii")
                method = request.method.decode("ascii")
                req_headers = {k.decode(): v.decode() for k, v in request.headers}

                host = _extract_host(req_headers)
                if host is None:
                    await _send_error(conn, client_stream, 400, b"Missing Host header")
                    return

                if not config.is_allowed(host):
                    await _send_error(
                        conn,
                        client_stream,
                        403,
                        f"Host {host!r} not allowed".encode(),
                    )
                    return

                # Always HTTPS upstream. The Host header value carries any
                # port the client specified (we strip only for filtering).
                host_with_port = req_headers.get("Host") or req_headers.get("host", host)
                upstream_url = f"https://{host_with_port}{target}"

                # Strip hop-by-hop and Host (aiohttp re-derives Host from
                # the URL). No injected headers in passthrough mode.
                skip = {"host", "transfer-encoding", "connection", "keep-alive"}
                outbound_headers = {k: v for k, v in req_headers.items() if k.lower() not in skip}

                try:
                    async with upstream_session.request(
                        method,
                        upstream_url,
                        headers=outbound_headers,
                        data=_iter_body(conn, client_stream),
                        allow_redirects=False,
                    ) as upstream_resp:
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

                try:
                    conn.start_next_cycle()
                except h11.LocalProtocolError:
                    return
        except Exception:
            logger.exception(
                "passthrough_proxy_connection: unhandled exception; " "closing client connection",
            )
