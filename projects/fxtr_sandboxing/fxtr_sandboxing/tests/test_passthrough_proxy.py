"""Tests for the host-agnostic HTTP passthrough proxy.

Unit-tests :class:`PassthroughHostsConfig` and ``_extract_host``;
integration-tests :func:`passthrough_proxy_connection` against a
mocked ``aiohttp.ClientSession`` (we don't need a real HTTPS upstream
since we only want to verify the proxy's URL construction, host
extraction, and filter behavior).
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import AsyncIterator
from typing import Any

import anyio
import anyio.abc
import h11
import pytest
from fxtr_sandboxing.proxy import (
    PassthroughHostsConfig,
    _extract_host,
    passthrough_proxy_connection,
)

pytestmark = pytest.mark.anyio


# --- Unit tests ----------------------------------------------------


class TestPassthroughHostsConfig:
    def test_default_allows_all(self):
        cfg = PassthroughHostsConfig()
        assert cfg.is_allowed("anything.example")
        assert cfg.is_allowed("localhost")

    def test_allowlist_filters(self):
        cfg = PassthroughHostsConfig(allowed_hosts=("example.com", "api.foo"))
        assert cfg.is_allowed("example.com")
        assert cfg.is_allowed("api.foo")
        assert not cfg.is_allowed("evil.com")

    def test_blocklist_filters(self):
        cfg = PassthroughHostsConfig(blocked_hosts=("evil.com",))
        assert not cfg.is_allowed("evil.com")
        assert cfg.is_allowed("example.com")

    def test_case_insensitive(self):
        cfg_allow = PassthroughHostsConfig(allowed_hosts=("Example.com",))
        assert cfg_allow.is_allowed("example.com")
        cfg_block = PassthroughHostsConfig(blocked_hosts=("Evil.COM",))
        assert not cfg_block.is_allowed("evil.com")

    def test_both_set_rejected(self):
        with pytest.raises(ValueError, match="at most one"):
            PassthroughHostsConfig(allowed_hosts=("a",), blocked_hosts=("b",))


class TestExtractHost:
    def test_bare_host(self):
        assert _extract_host({"Host": "example.com"}) == "example.com"

    def test_host_with_port_stripped(self):
        assert _extract_host({"Host": "example.com:8080"}) == "example.com"

    def test_lowercased(self):
        assert _extract_host({"Host": "EXAMPLE.COM"}) == "example.com"

    def test_header_lookup_case_insensitive(self):
        assert _extract_host({"host": "example.com"}) == "example.com"

    def test_missing_returns_none(self):
        assert _extract_host({"Content-Type": "text/plain"}) is None

    def test_whitespace_trimmed(self):
        assert _extract_host({"Host": "  example.com  "}) == "example.com"


# --- Integration tests --------------------------------------------


class _FakeResponse:
    def __init__(self, text: str, status: int, headers: dict[str, str] | None = None) -> None:
        self._text = text.encode()
        self.status = status
        self.headers = headers or {"Content-Type": "text/plain"}

    @property
    def content(self) -> Any:
        outer = self

        class _Content:
            async def iter_any(self) -> AsyncIterator[bytes]:
                yield outer._text

        return _Content()


class _FakeSession:
    """Drop-in for ``aiohttp.ClientSession`` for these tests.

    Records each ``request()`` call (method/url/headers/body) and
    returns a configurable :class:`_FakeResponse`."""

    def __init__(
        self,
        *,
        response_text: str = "fake-response",
        response_status: int = 200,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._text = response_text
        self._status = response_status

    @contextlib.asynccontextmanager
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: Any = None,
        **_: Any,
    ) -> AsyncIterator[_FakeResponse]:
        # Drain the body iterator so the proxy's h11 loop advances.
        body = b""
        if data is not None:
            async for chunk in data:
                body += chunk
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "body": body,
            }
        )
        yield _FakeResponse(self._text, self._status)


async def _run_request(
    config: PassthroughHostsConfig,
    session: _FakeSession,
    request_bytes: bytes,
) -> bytes:
    """Run one HTTP request through ``passthrough_proxy_connection``
    over a socketpair; return the response bytes the client saw."""
    client_sock, proxy_sock = socket.socketpair()
    client_stream = await anyio.abc.UNIXSocketStream.from_socket(client_sock)
    proxy_stream = await anyio.abc.UNIXSocketStream.from_socket(proxy_sock)

    response = b""
    async with anyio.create_task_group() as tg:
        # type-ignored because _FakeSession satisfies aiohttp.ClientSession
        # structurally for our use of ``.request(...)``.
        tg.start_soon(passthrough_proxy_connection, proxy_stream, config, session)  # type: ignore[arg-type]
        await client_stream.send(request_bytes)
        # Half-close so the proxy's keep-alive loop returns when this
        # one request is done.
        await client_stream.send_eof()
        try:
            while True:
                chunk = await client_stream.receive(65536)
                if not chunk:
                    break
                response += chunk
        except anyio.EndOfStream:
            pass
        await client_stream.aclose()
        tg.cancel_scope.cancel()
    return response


def _make_request(host: str, target: str = "/foo", method: str = "GET", body: bytes = b"") -> bytes:
    """Hand-assemble a minimal HTTP/1.1 request."""
    headers = (
        f"{method} {target} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode()
    return headers + body


def _status_from_response(resp: bytes) -> int:
    """Extract the status code from a raw HTTP response."""
    # Parse via h11 client-side, which is what the agent's httpx
    # would do.
    conn = h11.Connection(h11.CLIENT)
    conn.send(h11.Request(method="GET", target="/", headers=[("Host", "x")]))
    conn.send(h11.EndOfMessage())
    conn.receive_data(resp)
    event = conn.next_event()
    while event is h11.NEED_DATA:
        event = conn.next_event()
    assert isinstance(event, h11.Response), f"got {event!r}"
    return event.status_code


async def test_passthrough_forwards_to_host_from_header():
    """The proxy picks the upstream from the Host header — different
    hosts go to different upstream URLs (over the same authorizer)."""
    session = _FakeSession()
    config = PassthroughHostsConfig()

    await _run_request(config, session, _make_request("example.com", "/foo"))
    await _run_request(config, session, _make_request("other.test", "/bar"))

    assert len(session.calls) == 2
    assert session.calls[0]["url"] == "https://example.com/foo"
    assert session.calls[1]["url"] == "https://other.test/bar"


async def test_passthrough_preserves_port_in_url():
    """A Host with an explicit port should land in the upstream URL."""
    session = _FakeSession()
    config = PassthroughHostsConfig()
    await _run_request(config, session, _make_request("example.com:8443", "/x"))
    assert session.calls[0]["url"] == "https://example.com:8443/x"


async def test_passthrough_strips_hop_by_hop_headers():
    """The proxy must not forward hop-by-hop headers to the upstream."""
    session = _FakeSession()
    config = PassthroughHostsConfig()
    raw = (
        b"GET /x HTTP/1.1\r\n"
        b"Host: example.com\r\n"
        b"Connection: close\r\n"
        b"Keep-Alive: timeout=5\r\n"
        b"X-Custom: keep-me\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )
    await _run_request(config, session, raw)
    sent_lower = {k.lower(): v for k, v in session.calls[0]["headers"].items()}
    # Host is dropped (aiohttp re-derives from URL); hop-by-hop
    # gone; user headers preserved. h11 lowercases header names on
    # the way through, so the assert is case-insensitive.
    assert "host" not in sent_lower
    assert "connection" not in sent_lower
    assert "keep-alive" not in sent_lower
    assert sent_lower.get("x-custom") == "keep-me"


async def test_passthrough_allowlist_refuses():
    """A host outside an allowlist gets a 403; no upstream call."""
    session = _FakeSession()
    config = PassthroughHostsConfig(allowed_hosts=("good.example",))
    resp = await _run_request(config, session, _make_request("evil.example"))
    assert _status_from_response(resp) == 403
    assert session.calls == []


async def test_passthrough_allowlist_passes_matching_host():
    session = _FakeSession()
    config = PassthroughHostsConfig(allowed_hosts=("good.example",))
    await _run_request(config, session, _make_request("good.example", "/path"))
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == "https://good.example/path"


async def test_passthrough_blocklist_refuses():
    session = _FakeSession()
    config = PassthroughHostsConfig(blocked_hosts=("bad.example",))
    resp = await _run_request(config, session, _make_request("bad.example"))
    assert _status_from_response(resp) == 403
    assert session.calls == []


async def test_passthrough_blocklist_passes_other_hosts():
    session = _FakeSession()
    config = PassthroughHostsConfig(blocked_hosts=("bad.example",))
    await _run_request(config, session, _make_request("good.example", "/path"))
    assert len(session.calls) == 1


# Note on "missing / empty Host" — h11 enforces a present, non-
# empty Host header at the protocol layer and rejects malformed
# requests before our proxy sees them. The ``_extract_host`` /
# ``Missing Host`` 400 path in the proxy is therefore defensive
# (covers future h11 behavior changes) and not directly testable
# end-to-end through h11's parser.
