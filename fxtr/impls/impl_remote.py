"""Remote client for an implementation of a protocol."""

from __future__ import annotations

import contextlib
import functools
import logging
from typing import Any, AsyncIterator

import anyio
import anyio.abc
import httpx
import logfire
from httpx_ws import aconnect_ws

from ..object_model import core, locator
from ..object_model.core import FxtrCallTarget
from . import rpc

logger = logging.getLogger(__name__)

# Re-export for backwards compatibility
RemoteRPCError = rpc.RemoteRPCError


async def _websocket_connection_worker(
    base_url: str,
    known_loader: locator.CachingEntityLoader,
    shutdown_event: anyio.Event,
    send_stream: anyio.abc.ObjectSendStream[rpc.FxtrRPCConnection],
) -> None:
    """Background worker that establishes WebSocket connection and runs receive loop.

    Args:
        base_url: Base URL of the remote server
        known_loader: Loader for known entities
        shutdown_event: Event that signals when to shut down
        send_stream: Stream to send the connection object through once established
    """
    ws_url = f"{base_url}/.well_known/org.transluce.fxtr.rpc/ws"

    logger.debug(f"WS connection worker starting at {ws_url}")
    async with httpx.AsyncClient() as client:
        try:
            async with aconnect_ws(
                ws_url,
                client,
                # TODO (rfriel): determine why this is needed / why the default of 20 seconds is not enough
                keepalive_ping_timeout_seconds=None,
            ) as websocket:
                # Create task group for this connection
                async with anyio.create_task_group() as connection_tg:
                    # Create RPC connection
                    connection = rpc.FxtrRPCConnection(
                        websocket=websocket,
                        known_loader=known_loader,
                        background_task_group=connection_tg,
                        root_target=None,
                    )

                    # Start the connection (sends info message)
                    await connection.start()

                    # Send the connection to the caller
                    async with send_stream:
                        await send_stream.send(connection)

                    # Run the receive loop until shutdown
                    logger.debug(f"WS connection worker entering receive loop for {ws_url}")
                    await connection._receive_until_cancelled(shutdown_event)
                    logger.debug(f"WS connection worker exiting receive loop for {ws_url}")
        except BaseException as e:
            # Sometimes, inside aconnect_ws, a cancellation exception gets set as its own context.
            # This leads to an infinite loop in anyio, so we patch it here.
            while e.__context__:
                if e is e.__context__:
                    e.__context__ = None
                    break
                else:
                    e = e.__context__
            raise
    logger.debug(f"WS connection worker exiting for {ws_url}")


class RemoteImpl(FxtrCallTarget):
    """Remote client for an implementation of an interface.

    This class directly implements the interface methods by proxying them to a remote server via RPC
    over WebSocket. The lifecycle is managed by a task group that must be provided at construction.

    For standalone usage, use the RemoteImpl.connect() class method which manages its own task group.
    """

    def __init__(
        self,
        task_group: anyio.abc.TaskGroup,
        interface_spec: core.InterfaceSpec,
        base_url: str,
        shutdown_event: anyio.Event,
        known_loader: locator.CachingEntityLoader | None = None,
    ):
        """Initialize the remote implementation.

        Args:
            task_group: Task group to use for background tasks
            interface_spec: The interface that this implementation implements.
            base_url: The base URL of the remote server.
            shutdown_event: Event to signal when to shut down the connection.
            known_loader: A loader for entities that this program considers "well known".
        """
        self._task_group = task_group
        self._interface_spec = interface_spec
        self._base_url = base_url.rstrip("/")  # Remove trailing slash
        self._shutdown_event = shutdown_event
        self._known_loader = known_loader or locator.CachingEntityLoader.empty()
        self._connection: rpc.FxtrRPCConnection | None = None
        self._connection_lock = anyio.Lock()

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(
        cls,
        interface_spec: core.InterfaceSpec,
        base_url: str,
        known_loader: locator.CachingEntityLoader | None = None,
    ) -> AsyncIterator[RemoteImpl]:
        """Create a RemoteImpl with its own managed task group.

        This is the recommended way to use RemoteImpl for standalone usage.

        Args:
            interface_spec: The interface that this implementation implements
            base_url: The base URL of the remote server
            known_loader: A loader for entities that this program considers "well known"

        Yields:
            A RemoteImpl instance
        """
        shutdown_event = anyio.Event()
        async with anyio.create_task_group() as tg:
            try:
                impl = cls(
                    task_group=tg,
                    interface_spec=interface_spec,
                    base_url=base_url,
                    shutdown_event=shutdown_event,
                    known_loader=known_loader,
                )
                yield impl
            finally:
                shutdown_event.set()

    def interface_spec(self) -> core.InterfaceSpec:
        return self._interface_spec

    def __getattr__(self, method: str) -> Any:
        """Proxy method calls to the remote server."""
        # Only allow public methods and __call__
        if not ((not method.startswith("_")) or (method == "__call__")):
            raise AttributeError(f"Method {method} is not exposed to RPC!")
        return functools.partial(self._method_call, method)

    async def _ensure_connection(self) -> rpc.FxtrRPCConnection:
        """Ensure we have an active WebSocket connection."""
        async with self._connection_lock:
            if self._connection is not None:
                return self._connection

            # Create memory stream for connection passing
            send_stream, receive_stream = anyio.create_memory_object_stream[rpc.FxtrRPCConnection](
                max_buffer_size=1
            )

            # Start worker to establish connection
            self._task_group.start_soon(
                _websocket_connection_worker,
                self._base_url,
                self._known_loader,
                self._shutdown_event,
                send_stream,
            )

            # Receive the connection from the worker
            async with receive_stream:
                connection = await receive_stream.receive()

            # Validate that the remote protocol matches what we expect
            await connection._counterparty_info_received.wait()
            if connection._counterparty_root_protocol is not None:
                if connection._counterparty_root_protocol != self._interface_spec:
                    raise RuntimeError(
                        f"Remote interface mismatch: expected {self._interface_spec}, "
                        f"got {connection._counterparty_root_protocol}"
                    )

            self._connection = connection
            return connection

    async def _method_call(self, method: str, *args, **kwargs) -> Any:
        """Call a method on the remote server."""
        connection = await self._ensure_connection()

        # Make the RPC call
        with logfire.span(
            "{interface_name}.{method}(...)",
            interface_name=self._interface_spec.defn.interface_id,
            method=method,
        ):
            result = await connection.call(0, method, *args, **kwargs)

        return result
