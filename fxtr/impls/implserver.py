"""RPC server for hub impls.

The server object starts serving on localhost with a random free port.
At first, it has no routes. However, it has a method `serve_impl(FxtrCallTarget)`
which gives a context manager that serves the implementation at some prefix like
`{host}:{port}/{index}/...`. The context manager should produce a URL that can be used to access
the implementation.

If the same implementation is passed to `serve_impl` multiple times, it should detect this and
return the same URL. It should also track this in a counter, so that the implementation is only
torn down when all context managers for the implementation have been exited.

The server object itself should be usable as a context manager, and when exited it should shut down
the server and wait for it to close.

The server should use `anyio` for async concurrency, NOT asyncio.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import sys
import traceback
import typing
from dataclasses import dataclass
from typing import AsyncGenerator, Generator

import anyio
import anyio.abc
import hypercorn.asyncio
import logfire
from starlette.routing import Mount, Router, WebSocketRoute
from starlette.websockets import WebSocket

from ..object_model import locator
from ..object_model.core import FxtrCallTarget
from . import rpc

logger = logging.getLogger(__name__)


def _find_available_port(host: str, start_port: int) -> int:
    """Find an available TCP port on host, starting at start_port or any if 0."""
    if start_port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            return s.getsockname()[1]

    for port in range(start_port, start_port + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return port
        except OSError:
            continue

    raise RuntimeError(f"No available ports found starting from {start_port}")


@dataclass
class _ImplEntry:
    ref_count: int
    mount: Mount
    impl: FxtrCallTarget


class ImplServer:
    """Server that hosts servable protocol implementations under unique prefixes.

    Usage:
        async with ImplServer().serving() as server:
            with server.serve_impl(impl) as url:
                ...
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        default_port: int = 0,
        known_loader: locator.CachingEntityLoader | None = None,
    ):
        self.server_active = False

        self._host = host
        self._port = _find_available_port(host, default_port)
        self._known_loader = known_loader or locator.CachingEntityLoader.empty()

        # Router is the ASGI app. We'll mount sub-apps under unique prefixes.
        self._router = Router(lifespan=self._server_app_lifespan)
        self._server_started_event = anyio.Event()

        # Concurrency and lifecycle
        # self._lock = anyio.Lock()
        self._server_tg: anyio.abc.TaskGroup | None = None
        self._graceful_shutdown_event: anyio.Event | None = None

        # Impl tracking
        self._next_index = 1
        self._impl_id_to_entry: dict[int, _ImplEntry] = {}

    @contextlib.asynccontextmanager
    async def _server_app_lifespan(self, _app) -> AsyncGenerator[None, None]:
        self._server_started_event.set()
        yield

    @contextlib.asynccontextmanager
    async def serving(self) -> AsyncGenerator[ImplServer, None]:
        """Context manager that serves the router on the server."""
        assert self._server_tg is None
        try:
            async with anyio.create_task_group() as tg:
                self._server_tg = tg
                server_cancel_scope: anyio.CancelScope | None = None
                graceful_shutdown_event = anyio.Event()
                server_done_event = anyio.Event()

                async def _run_server() -> None:
                    nonlocal server_cancel_scope
                    try:
                        with anyio.CancelScope(shield=True) as cscope:
                            server_cancel_scope = cscope
                            config = hypercorn.Config()
                            # TODO (rfriel): receive very large collections in multiple messages?
                            # I have a collection that is ~50MB, and I just set this to 512MB
                            # because that's a lot bigger than the collection I have now, but
                            # in principle we need to be able to support arbitrarily large collections.
                            config.websocket_max_message_size = 1024 * 1024 * 512  # 512MB
                            config.bind = [f"{self._host}:{self._port}"]

                            app = self._router
                            app = logfire.instrument_asgi(app)  # type: ignore

                            await hypercorn.asyncio.serve(
                                app, config, shutdown_trigger=graceful_shutdown_event.wait  # type: ignore
                            )
                    finally:
                        server_done_event.set()

                tg.start_soon(_run_server)
                await self._server_started_event.wait()
                # needed for type checker:
                server_cancel_scope = typing.cast(anyio.CancelScope | None, server_cancel_scope)
                assert server_cancel_scope is not None
                self.server_active = True
                self._graceful_shutdown_event = graceful_shutdown_event
                try:
                    yield self
                finally:
                    self.server_active = False
                    self._graceful_shutdown_event = None
                    self._server_started_event = anyio.Event()
                    logger.info("Shutting down ImplServer")
                    # Try to shut down the server cleanly.
                    graceful_shutdown_event.set()
                    with anyio.move_on_after(5, shield=True):
                        await server_done_event.wait()
                        logger.debug("Server finished")
                    if not server_done_event.is_set():
                        logger.warning("Server did not finish in time! need to fix")
                    # Cancel the server task if it is still running.
                    server_cancel_scope.cancel()
                    self.server_cancel_scope = None
                    logger.debug("Exiting task group")
            logger.debug("ImplServer exited serving")
        finally:
            self._server_tg = None

    def _create_impl_router(self, impl: FxtrCallTarget) -> Router:
        """Create a router with RPC endpoints for the given implementation."""

        # Create handler closure that captures the impl
        async def handle_websocket(websocket: WebSocket) -> None:
            await self._rpc_handle_websocket(impl, websocket)

        return Router(
            routes=[
                WebSocketRoute(
                    "/.well_known/org.transluce.fxtr.rpc/ws",
                    handle_websocket,
                ),
            ]
        )

    async def _rpc_handle_websocket(self, impl: FxtrCallTarget, websocket: WebSocket) -> None:
        """Handle WebSocket connection for RPC."""
        await websocket.accept()

        try:
            # Create task group for this connection
            async with anyio.create_task_group() as connection_tg:
                # Create RPC connection with the impl as root target
                connection = rpc.FxtrRPCConnection(
                    websocket=websocket,
                    known_loader=self._known_loader,
                    background_task_group=connection_tg,
                    root_target=impl,
                )

                # Start the connection (sends info message)
                await connection.start()

                # Run the receive loop until server shutdown
                assert self._graceful_shutdown_event is not None
                await connection._receive_until_cancelled(self._graceful_shutdown_event)

        except Exception as e:
            logger.exception("Error in WebSocket handler")
        finally:
            with anyio.move_on_after(5, shield=True):
                await websocket.close()

    @contextlib.contextmanager
    def serve_impl(self, impl: FxtrCallTarget) -> Generator[str, None]:
        """Context manager that serves the implementation."""
        if not self.server_active:
            raise RuntimeError("ImplServer is not active!")

        entry = self._impl_id_to_entry.get(id(impl))
        if entry is None:
            impl_router = self._create_impl_router(impl)
            entry = _ImplEntry(
                ref_count=0,
                mount=Mount(f"/{self._next_index}", impl_router),
                impl=impl,
            )
            self._impl_id_to_entry[id(impl)] = entry
            self._next_index += 1
            self._router.routes.append(entry.mount)
        entry.ref_count += 1

        try:
            yield f"http://{self._host}:{self._port}{entry.mount.path}"

        finally:
            entry.ref_count -= 1
            if entry.ref_count == 0:
                self._router.routes.remove(entry.mount)
                self._impl_id_to_entry.pop(id(impl))


__all__ = ["ImplServer"]
