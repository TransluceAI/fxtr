"""SandboxServiceContext impl for the container runtime.

`ContainerServiceContext` wraps the anyio UDS SocketStream that the
container holds open against the host's per-spawn task socket. The
``main_protocol_stream`` property exposes a `SocketJSONStream` over the
underlying byte stream — length-prefixed JSON on send, native
`anyio.EndOfStream` propagation on a clean half-close.

``open_transport_stream`` (stage 3) dials the multiplexed transport
UDS at ``/run/fxtr/transport.sock``, writes the routing header
(``{"token": "..."}``), reads the host's response (``{"ok": true}`` or
``{"ok": false, "error": ...}``), and either returns the (now byte-
bridged) connection or raises :class:`BadTransportTokenError`.
"""

from __future__ import annotations

import anyio
import anyio.abc
from anyio.abc import ObjectStream
from fxtr_sandbox_types.json_object_stream import JSONTypes
from fxtr_sandbox_types.socket_json_stream import SocketJSONStream
from fxtr_sandbox_types.transport_routing import read_routing_response, write_routing_header

# Well-known socket path inside the container; the host-side spawner mounts
# its per-spawn UDS at this path.
SOCKET_PATH = "/run/fxtr/task.sock"

# Well-known transport socket path inside the container; the host-side
# spawner mounts its per-spawn multiplexed transport UDS at this path
# (only when a TransportConnectionServer is configured on the env).
TRANSPORT_SOCKET_PATH = "/run/fxtr/transport.sock"


class BadTransportTokenError(LookupError):
    """Raised when the host rejects a transport token via the routing-header
    response (``{"ok": false, "error": ...}``).

    Mirrors the host-side ``fxtr_sandboxing.transport_server.BadTransportTokenError``
    name so user code can ``except BadTransportTokenError`` regardless of
    which side is raising. They are not the same class — but both inherit
    :class:`LookupError`.
    """


class ContainerServiceContext:
    """Concrete `SandboxServiceContext` for the container runtime.

    Constructed from an already-connected `anyio.SocketStream` (typically
    produced by ``await anyio.connect_unix(SOCKET_PATH)`` in
    `fxtr_service_runner.__main__`). The wrapping `SocketJSONStream` is
    built once and re-served on every property access — adapter-side code
    relies on identity stability for the connection's lifetime.
    """

    def __init__(self, socket_stream: anyio.abc.SocketStream) -> None:
        self._main_protocol_stream = SocketJSONStream(socket_stream)

    @property
    def main_protocol_stream(self) -> ObjectStream[JSONTypes]:
        return self._main_protocol_stream

    async def open_transport_stream(self, token: str) -> anyio.abc.ByteStream:
        """Redeem ``token`` on the multiplexed transport UDS.

        Dials :data:`TRANSPORT_SOCKET_PATH`, writes the
        ``{"token": ...}`` routing header, reads the host's response
        frame, and returns the now-byte-bridged socket on success.
        Raises :class:`BadTransportTokenError` on a host rejection
        response; the underlying socket is closed before the raise.
        """
        sock = await anyio.connect_unix(TRANSPORT_SOCKET_PATH)
        try:
            await write_routing_header(sock, token)
            response = await read_routing_response(sock)
        except BaseException:
            await sock.aclose()
            raise

        if not response.get("ok"):
            error = response.get("error", "bad transport token")
            await sock.aclose()
            raise BadTransportTokenError(str(error))

        return sock
