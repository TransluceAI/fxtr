"""Tests for transport utilities: TransportRouteConnector and httpx bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import aiohttp.web
import httpx
import pytest

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.network_transport import ExternalNetworkTransportHandle, NetworkTransportHandle
from fxtr_workflow.proxy import TransportRoute
from fxtr_workflow.step_context import StepContext
from fxtr_workflow.transport_utils import (
    TransportRouteConnector,
    make_httpx_client,
    make_httpx_transport,
)

from .conftest import StepKeyEntity

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Test upstream server helpers
# ---------------------------------------------------------------------------


async def _make_upstream(handler: Any) -> tuple[aiohttp.web.AppRunner, str]:
    """Create a test upstream HTTP server, returns (runner, base_url)."""
    app = aiohttp.web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sock = site._server.sockets[0]  # type: ignore[union-attr]
    port = sock.getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def _echo_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Echo back method, path, headers, and body as JSON."""
    body = await request.read()
    return aiohttp.web.json_response(
        {
            "method": request.method,
            "path": request.path,
            "headers": dict(request.headers),
            "body": body.decode("utf-8", errors="replace"),
        }
    )


# ---------------------------------------------------------------------------
# TransportRouteConnector tests
# ---------------------------------------------------------------------------


class TestTransportRouteConnector:
    async def test_basic_proxy(self):
        """Connect through proxy, verify request reaches upstream."""
        runner, base_url = await _make_upstream(_echo_handler)
        try:
            route = TransportRoute(
                upstream_base_url=base_url,
                injected_headers={"X-Secret": "hunter2"},
            )
            async with TransportRouteConnector(route) as connector:
                handle = ExternalNetworkTransportHandle(
                    connector,
                    StepKeyEntity(label="test_transport").store().entity_id,
                )
                async with make_httpx_client(handle, base_url="http://proxy") as client:
                    resp = await client.get("/hello")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["path"] == "/hello"
                    assert data["method"] == "GET"
                    # Verify injected header reached upstream
                    assert data["headers"]["X-Secret"] == "hunter2"
        finally:
            await runner.cleanup()

    async def test_header_injection(self):
        """Injected headers overwrite client-provided ones."""
        runner, base_url = await _make_upstream(_echo_handler)
        try:
            route = TransportRoute(
                upstream_base_url=base_url,
                injected_headers={"Authorization": "Bearer secret-token"},
            )
            async with TransportRouteConnector(route) as connector:
                handle = ExternalNetworkTransportHandle(
                    connector,
                    StepKeyEntity(label="test_transport").store().entity_id,
                )
                async with make_httpx_client(handle, base_url="http://proxy") as client:
                    # Client tries to set its own auth — proxy should overwrite
                    resp = await client.get(
                        "/api/v1/test",
                        headers={"Authorization": "Bearer wrong-token"},
                    )
                    data = resp.json()
                    assert data["headers"]["Authorization"] == "Bearer secret-token"
        finally:
            await runner.cleanup()

    async def test_path_filtering(self):
        """Requests to disallowed paths get 403."""
        runner, base_url = await _make_upstream(_echo_handler)
        try:
            route = TransportRoute(
                upstream_base_url=base_url,
                injected_headers={},
                allowed_path_prefixes=["/api/"],
            )
            async with TransportRouteConnector(route) as connector:
                handle = ExternalNetworkTransportHandle(
                    connector,
                    StepKeyEntity(label="test_transport").store().entity_id,
                )
                async with make_httpx_client(handle, base_url="http://proxy") as client:
                    # Allowed path
                    resp = await client.get("/api/v1/models")
                    assert resp.status_code == 200

                    # Disallowed path — proxy returns 403
                    resp = await client.get("/admin/secrets")
                    assert resp.status_code == 403
        finally:
            await runner.cleanup()

    async def test_post_with_body(self):
        """POST requests forward the body correctly."""
        runner, base_url = await _make_upstream(_echo_handler)
        try:
            route = TransportRoute(
                upstream_base_url=base_url,
                injected_headers={},
            )
            async with TransportRouteConnector(route) as connector:
                handle = ExternalNetworkTransportHandle(
                    connector,
                    StepKeyEntity(label="test_transport").store().entity_id,
                )
                async with make_httpx_client(handle, base_url="http://proxy") as client:
                    resp = await client.post(
                        "/api/submit",
                        json={"message": "hello"},
                    )
                    data = resp.json()
                    assert data["method"] == "POST"
                    assert "hello" in data["body"]
        finally:
            await runner.cleanup()


# ---------------------------------------------------------------------------
# httpx bridge tests (direct connector, no proxy)
# ---------------------------------------------------------------------------


class TestHttpxBridge:
    async def test_make_httpx_transport_direct(self):
        """httpx transport works with a direct TCP-like connector."""
        runner, base_url = await _make_upstream(_echo_handler)
        try:
            # Use TransportRouteConnector with no header injection as a
            # "pass-through" to test the httpx bridge plumbing
            route = TransportRoute(
                upstream_base_url=base_url,
                injected_headers={},
            )
            async with TransportRouteConnector(route) as connector:
                handle = ExternalNetworkTransportHandle(
                    connector,
                    StepKeyEntity(label="direct").store().entity_id,
                )
                transport = make_httpx_transport(handle)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://proxy"
                ) as client:
                    resp = await client.get("/test/path")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["path"] == "/test/path"
        finally:
            await runner.cleanup()

    async def test_make_httpx_client_convenience(self):
        """make_httpx_client returns a working pre-configured client."""
        runner, base_url = await _make_upstream(_echo_handler)
        try:
            route = TransportRoute(
                upstream_base_url=base_url,
                injected_headers={},
            )
            async with TransportRouteConnector(route) as connector:
                handle = ExternalNetworkTransportHandle(
                    connector,
                    StepKeyEntity(label="conv").store().entity_id,
                )
                async with make_httpx_client(handle, base_url="http://proxy") as client:
                    resp = await client.get("/convenience")
                    assert resp.status_code == 200
        finally:
            await runner.cleanup()


# ---------------------------------------------------------------------------
# End-to-end: step uses transport handle with proxy
# ---------------------------------------------------------------------------


async def test_step_uses_proxied_transport(tmp_path: Path):
    """A step receives a proxied transport handle and makes HTTP requests."""
    runner, base_url = await _make_upstream(_echo_handler)
    try:
        route = TransportRoute(
            upstream_base_url=base_url,
            injected_headers={"X-Api-Key": "test-key-123"},
        )
        async with TransportRouteConnector(route) as connector:
            db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
            async with SQLiteFxtrNamespace(db, "test") as ns:
                async with TopLevelEntrypoint() as ep:
                    step_key = StepKeyEntity(label="api_step").store().entity_id

                    async def api_step(ctx: StepContext, transport: NetworkTransportHandle) -> dict:
                        async with make_httpx_client(transport, base_url="http://proxy") as client:
                            resp = await client.get("/v1/data")
                            return resp.json()

                    step = ep.bind_external_step(api_step, step_key)
                    transport_key = StepKeyEntity(label="api_service").store().entity_id
                    handle = ep.bind_network_transport(connector, transport_key)

                    slot = ep.open_cache_slot(ns, "root")
                    result = await slot.run(step, handle)
                    assert result["path"] == "/v1/data"
                    assert result["headers"]["X-Api-Key"] == "test-key-123"
    finally:
        await runner.cleanup()
