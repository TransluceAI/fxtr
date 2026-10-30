"""Utilities for network transport handles.

- ``TransportRouteConnector``: Adapts a ``TransportRoute`` into a
  ``ByteStreamConnectable`` by running an in-process HTTP proxy per connection.
- ``make_httpx_transport`` / ``make_httpx_client``: Wraps a
  ``NetworkTransportHandle`` into httpx types for convenient HTTP usage.
"""

from __future__ import annotations

import socket
from typing import Any

import aiohttp
import anyio
import anyio.abc
import httpcore
import httpx

from fxtr_workflow.network_transport import NetworkTransportHandle
from fxtr_workflow.proxy import TransportRoute, proxy_connection

# ---------------------------------------------------------------------------
# TransportRoute → ByteStreamConnectable adapter
# ---------------------------------------------------------------------------


class TransportRouteConnector:
    """Adapts a TransportRoute into a ByteStreamConnectable.

    Each ``connect()`` call creates a socketpair, spawns ``proxy_connection``
    as a background task on one end, and returns the other end to the caller.
    The proxy handles path filtering, header injection, and upstream forwarding.

    Must be used as an async context manager — owns an aiohttp session for
    upstream requests and a task group for proxy background tasks.

    Usage::

        async with TransportRouteConnector(route) as connector:
            handle = ep.bind_network_transport(connector, key_id)
            # ... use handle in steps ...
    """

    def __init__(self, route: TransportRoute) -> None:
        self._route = route
        self._session: aiohttp.ClientSession | None = None
        self._task_group: anyio.abc.TaskGroup | None = None
        self._exit_stack: list[Any] = []

    async def __aenter__(self) -> TransportRouteConnector:
        self._session = aiohttp.ClientSession()
        self._task_group_cm = anyio.create_task_group()
        self._task_group = await self._task_group_cm.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool | None:
        # Exit task group first (cancels background proxy tasks)
        try:
            await self._task_group_cm.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None
        return None

    async def connect(self) -> anyio.abc.ByteStream:
        """Open a new proxied connection.

        Returns the client end of a socketpair. The proxy end runs
        ``proxy_connection()`` in a background task, mediating all HTTP
        traffic through the TransportRoute's configuration.
        """
        assert self._session is not None, "Must be used inside async context manager"
        assert self._task_group is not None

        client_sock, proxy_sock = socket.socketpair()
        proxy_stream = await anyio.abc.SocketStream.from_socket(proxy_sock)
        client_stream = await anyio.abc.SocketStream.from_socket(client_sock)

        async def _run_proxy() -> None:
            async with proxy_stream:
                await proxy_connection(proxy_stream, self._route, self._session)

        self._task_group.start_soon(_run_proxy)
        return client_stream


# ---------------------------------------------------------------------------
# httpx bridge
# ---------------------------------------------------------------------------


class _AnyioNetworkStream(httpcore.AsyncNetworkStream):
    """Wraps an anyio ByteStream as an httpcore AsyncNetworkStream."""

    def __init__(self, stream: anyio.abc.ByteStream) -> None:
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


class _HandleNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend that opens connections via a NetworkTransportHandle."""

    def __init__(self, handle: NetworkTransportHandle) -> None:
        self._handle = handle

    async def connect_tcp(self, host: str, port: int, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        stream = await self._handle.connect()
        return _AnyioNetworkStream(stream)


class _AsyncResponseStream(httpx.AsyncByteStream):
    """Adapts an httpcore async iterable to an httpx AsyncByteStream."""

    def __init__(self, httpcore_stream: Any) -> None:
        self._stream = httpcore_stream

    async def __aiter__(self) -> Any:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        if hasattr(self._stream, "aclose"):
            await self._stream.aclose()


class _HandleTransport(httpx.AsyncBaseTransport):
    """httpx transport that routes connections through a NetworkTransportHandle.

    Uses the public AsyncBaseTransport API instead of swapping internal
    pool attributes.
    """

    def __init__(self, handle: NetworkTransportHandle) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=_HandleNetworkBackend(handle),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        resp = await self._pool.handle_async_request(req)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_AsyncResponseStream(resp.stream),
            extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def make_httpx_transport(handle: NetworkTransportHandle) -> httpx.AsyncBaseTransport:
    """Wrap a NetworkTransportHandle as an httpx transport.

    Each HTTP connection opened by httpx will call ``handle.connect()`` to
    get a byte stream. The caller should set ``base_url`` on their httpx
    client as needed.
    """
    return _HandleTransport(handle)


def make_httpx_client(handle: NetworkTransportHandle, **client_kwargs: Any) -> httpx.AsyncClient:
    """Convenience: create an httpx.AsyncClient using a NetworkTransportHandle.

    All keyword arguments are passed to ``httpx.AsyncClient``. Typical usage::

        async with make_httpx_client(handle, base_url="http://proxy") as client:
            resp = await client.get("/v1/models")
    """
    transport = make_httpx_transport(handle)
    return httpx.AsyncClient(transport=transport, **client_kwargs)
