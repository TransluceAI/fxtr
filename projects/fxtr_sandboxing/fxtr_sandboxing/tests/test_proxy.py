"""Tests for the transport proxy."""

import json
import socket

import aiohttp
import aiohttp.web
import anyio
import httpcore
import httpx
from fxtr_sandboxing.proxy import TransportRoute, _merge_headers, _path_allowed, proxy_connection

# --- Unit tests for helpers ---


class TestPathAllowed:
    def test_none_allows_all(self):
        assert _path_allowed("/anything", None) is True

    def test_matching_prefix(self):
        assert _path_allowed("/v1/messages", ["/v1/"]) is True

    def test_non_matching_prefix(self):
        assert _path_allowed("/v2/other", ["/v1/"]) is False

    def test_multiple_prefixes(self):
        prefixes = ["/v1/messages", "/v1/models"]
        assert _path_allowed("/v1/messages/list", prefixes) is True
        assert _path_allowed("/v1/models", prefixes) is True
        assert _path_allowed("/v1/other", prefixes) is False

    def test_exact_match(self):
        assert _path_allowed("/health", ["/health"]) is True

    def test_empty_prefixes_denies_all(self):
        assert _path_allowed("/anything", []) is False


class TestMergeHeaders:
    def test_basic_merge(self):
        route = TransportRoute(
            upstream_base_url="https://api.example.com",
            injected_headers={"Authorization": "Bearer sk-123"},
        )
        merged = _merge_headers(
            {"Content-Type": "application/json", "Accept": "text/plain"},
            route,
            "api.example.com",
        )
        assert merged["Content-Type"] == "application/json"
        assert merged["Accept"] == "text/plain"
        assert merged["Authorization"] == "Bearer sk-123"
        assert merged["Host"] == "api.example.com"

    def test_host_replaced(self):
        route = TransportRoute(
            upstream_base_url="https://api.example.com",
            injected_headers={},
        )
        merged = _merge_headers(
            {"Host": "proxy", "Accept": "*/*"},
            route,
            "api.example.com",
        )
        assert merged["Host"] == "api.example.com"

    def test_injected_headers_overwrite(self):
        route = TransportRoute(
            upstream_base_url="https://api.example.com",
            injected_headers={"Authorization": "Bearer injected"},
        )
        merged = _merge_headers(
            {"Authorization": "Bearer task-provided"},
            route,
            "api.example.com",
        )
        assert merged["Authorization"] == "Bearer injected"

    def test_hop_by_hop_stripped(self):
        route = TransportRoute(
            upstream_base_url="https://api.example.com",
            injected_headers={},
        )
        merged = _merge_headers(
            {"Transfer-Encoding": "chunked", "Connection": "keep-alive", "Accept": "*/*"},
            route,
            "api.example.com",
        )
        assert "Transfer-Encoding" not in merged
        assert "Connection" not in merged
        assert merged["Accept"] == "*/*"


# --- Helpers for proxy_connection() integration tests ---


async def _make_upstream(handler) -> tuple[aiohttp.web.AppRunner, str]:
    """Create a test upstream HTTP server, returns (runner, base_url)."""
    app = aiohttp.web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    # Get the actual bound port
    sock = site._server.sockets[0]  # type: ignore[union-attr]
    port = sock.getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


class _ProxyBackend(httpcore.AsyncNetworkBackend):
    """Routes httpcore connections through proxy_connection() via a socketpair."""

    def __init__(self, route: TransportRoute, upstream_session: aiohttp.ClientSession) -> None:
        self._route = route
        self._session = upstream_session

    async def connect_tcp(self, host: str, port: int, **kwargs) -> httpcore.AsyncNetworkStream:
        client_sock, proxy_sock = socket.socketpair()
        proxy_stream = await anyio.abc.UNIXSocketStream.from_socket(proxy_sock)
        # Run proxy_connection in background
        task_group = _ProxyBackend._current_tg
        task_group.start_soon(proxy_connection, proxy_stream, self._route, self._session)
        # Return client side as httpcore stream wrapping an anyio stream
        client_stream = await anyio.abc.UNIXSocketStream.from_socket(client_sock)
        return _AnyioNetworkStream(client_stream)

    # Thread-local-ish task group, set by the test
    _current_tg: anyio.abc.TaskGroup = None  # type: ignore[assignment]


class _AnyioNetworkStream(httpcore.AsyncNetworkStream):
    """Wraps an anyio SocketStream as an httpcore AsyncNetworkStream."""

    def __init__(self, stream: anyio.abc.SocketStream) -> None:
        self._stream = stream

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            return await self._stream.receive(max_bytes)
        except anyio.EndOfStream:
            return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.send(buffer)

    async def aclose(self) -> None:
        await self._stream.aclose()


# --- Integration tests for proxy_connection() ---


class TestProxyConnectionIntegration:
    """Test proxy_connection() with httpx going through a socketpair."""

    async def _run_with_proxy(self, route, callback):
        """Set up proxy infrastructure and run callback(client)."""
        upstream_session = aiohttp.ClientSession()
        try:
            backend = _ProxyBackend(route, upstream_session)
            pool = httpcore.AsyncConnectionPool(network_backend=backend)
            transport = httpx.AsyncHTTPTransport()
            transport._pool = pool  # type: ignore[attr-defined]

            async with anyio.create_task_group() as tg:
                _ProxyBackend._current_tg = tg
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://proxy"
                ) as client:
                    await callback(client)
                tg.cancel_scope.cancel()
        finally:
            await upstream_session.close()

    async def test_basic_get(self):
        """Proxy forwards a basic GET and returns the response."""

        async def upstream_handler(request):
            return aiohttp.web.Response(text="hello from upstream")

        upstream_runner, upstream_url = await _make_upstream(upstream_handler)
        try:
            route = TransportRoute(
                upstream_base_url=upstream_url,
                injected_headers={},
            )

            async def check(client):
                resp = await client.get("/hello")
                assert resp.status_code == 200
                assert resp.text == "hello from upstream"

            await self._run_with_proxy(route, check)
        finally:
            await upstream_runner.cleanup()

    async def test_header_injection(self):
        """Proxy injects configured headers into upstream requests."""
        received_headers = {}

        async def upstream_handler(request):
            received_headers.update(dict(request.headers))
            return aiohttp.web.Response(text="ok")

        upstream_runner, upstream_url = await _make_upstream(upstream_handler)
        try:
            route = TransportRoute(
                upstream_base_url=upstream_url,
                injected_headers={"Authorization": "Bearer sk-secret", "X-Custom": "value"},
            )

            async def check(client):
                resp = await client.get("/test")
                assert resp.status_code == 200
                assert received_headers["Authorization"] == "Bearer sk-secret"
                assert received_headers["X-Custom"] == "value"

            await self._run_with_proxy(route, check)
        finally:
            await upstream_runner.cleanup()

    async def test_path_filtering_allowed(self):
        """Proxy allows requests matching path prefixes."""

        async def upstream_handler(request):
            return aiohttp.web.Response(text=f"path: {request.path}")

        upstream_runner, upstream_url = await _make_upstream(upstream_handler)
        try:
            route = TransportRoute(
                upstream_base_url=upstream_url,
                injected_headers={},
                allowed_path_prefixes=["/v1/"],
            )

            async def check(client):
                resp = await client.get("/v1/messages")
                assert resp.status_code == 200
                assert resp.text == "path: /v1/messages"

            await self._run_with_proxy(route, check)
        finally:
            await upstream_runner.cleanup()

    async def test_path_filtering_denied(self):
        """Proxy returns 403 for paths not matching prefixes."""

        async def upstream_handler(request):
            return aiohttp.web.Response(text="should not reach here")

        upstream_runner, upstream_url = await _make_upstream(upstream_handler)
        try:
            route = TransportRoute(
                upstream_base_url=upstream_url,
                injected_headers={},
                allowed_path_prefixes=["/v1/"],
            )

            async def check(client):
                resp = await client.get("/v2/other")
                assert resp.status_code == 403

            await self._run_with_proxy(route, check)
        finally:
            await upstream_runner.cleanup()

    async def test_post_with_body(self):
        """Proxy forwards POST request bodies."""
        received_body = None

        async def upstream_handler(request):
            nonlocal received_body
            received_body = await request.text()
            return aiohttp.web.Response(text="received")

        upstream_runner, upstream_url = await _make_upstream(upstream_handler)
        try:
            route = TransportRoute(
                upstream_base_url=upstream_url,
                injected_headers={},
            )

            async def check(client):
                resp = await client.post("/submit", json={"key": "value"})
                assert resp.status_code == 200
                assert received_body is not None
                assert json.loads(received_body) == {"key": "value"}

            await self._run_with_proxy(route, check)
        finally:
            await upstream_runner.cleanup()

    async def test_streaming_response(self):
        """Proxy streams response body chunks."""

        async def upstream_handler(request):
            resp = aiohttp.web.StreamResponse()
            await resp.prepare(request)
            for i in range(5):
                await resp.write(f"chunk{i}\n".encode())
            await resp.write_eof()
            return resp

        upstream_runner, upstream_url = await _make_upstream(upstream_handler)
        try:
            route = TransportRoute(
                upstream_base_url=upstream_url,
                injected_headers={},
            )

            async def check(client):
                resp = await client.get("/stream")
                assert resp.status_code == 200
                for i in range(5):
                    assert f"chunk{i}" in resp.text

            await self._run_with_proxy(route, check)
        finally:
            await upstream_runner.cleanup()

    async def test_query_string_forwarded(self):
        """Proxy forwards query string parameters."""

        async def upstream_handler(request):
            return aiohttp.web.Response(text=f"qs: {request.query_string}")

        upstream_runner, upstream_url = await _make_upstream(upstream_handler)
        try:
            route = TransportRoute(
                upstream_base_url=upstream_url,
                injected_headers={},
            )

            async def check(client):
                resp = await client.get("/search?q=hello&limit=10")
                assert resp.status_code == 200
                assert resp.text == "qs: q=hello&limit=10"

            await self._run_with_proxy(route, check)
        finally:
            await upstream_runner.cleanup()

    async def test_upstream_error_returns_502(self):
        """Proxy returns 502 when upstream is unreachable."""
        route = TransportRoute(
            upstream_base_url="http://127.0.0.1:1",  # nothing listening
            injected_headers={},
        )

        async def check(client):
            resp = await client.get("/test")
            assert resp.status_code == 502

        await self._run_with_proxy(route, check)

    async def test_httpx_over_proxy(self):
        """httpx can make requests through proxy_connection (Docker path)."""

        async def upstream_handler(request):
            return aiohttp.web.Response(
                text='{"result": "ok"}',
                content_type="application/json",
            )

        upstream_runner, upstream_url = await _make_upstream(upstream_handler)
        try:
            route = TransportRoute(
                upstream_base_url=upstream_url,
                injected_headers={"Authorization": "Bearer secret"},
            )

            async def check(client):
                resp = await client.get("/api/data")
                assert resp.status_code == 200
                assert resp.json() == {"result": "ok"}

            await self._run_with_proxy(route, check)
        finally:
            await upstream_runner.cleanup()
