"""Guest-side bootstrap, executed inside pyodide via ``runPythonAsync``.

Expects (set up by main.ts before this runs):

- ``/guest-site-packages`` mounted in the emscripten FS, containing
  fxtr2-portable + fxtr-sandbox-types + fxtr-sandbox-rpc-adapter + the user
  package (all pure-python, plus any pyemscripten binary wheels).
- ``fxtr_deno_bridge`` registered as a JS module exposing byte-level
  connection handles (``recv(max_bytes)`` / ``send(Uint8Array)`` /
  ``send_eof()`` / ``close()``): ``main`` (the already-dialed per-spawn task
  socket) and ``connect_transport()`` (dial one fresh connection to the
  host's multiplexed transport socket, when configured).
- ``_ENTRY_POINT`` global — the ``module:function`` string to serve.

This is the pyodide analog of ``fxtr_service_runner.__main__`` (container)
and ``fxtr_sandboxing/_runner.py`` (eryx). Unlike eryx, no socket/loop shims
are needed — pyodide is a complete CPython with a real asyncio loop. Unlike
the container, there is no in-guest UDS: bytes cross the JS boundary through
the bridge module, and the same ``SocketJSONStream`` framing / routing-header
protocol ride on top.

Note the entrypoint style: this file runs as one big ``await``-able body
(pyodide top-level await) rather than ``anyio.run(...)`` — ``asyncio.run``
requires JSPI in pyodide, and running on the already-live WebLoop is the
supported path.
"""

import sys

sys.path[:0] = ["/guest-site-packages"]

import anyio
import anyio.abc
import fxtr_deno_bridge as _bridge
from pyodide.ffi import to_js

import fxtr_sandbox_rpc_adapter
from fxtr_sandbox_types.json_object_stream import JSONTypes
from fxtr_sandbox_types.socket_json_stream import SocketJSONStream
from fxtr_sandbox_types.transport_routing import read_routing_response, write_routing_header


class BadTransportTokenError(LookupError):
    """Host rejected a transport token via the routing-header response.

    Mirrors the host-side / container-side classes of the same name (all
    inherit LookupError; they are deliberately distinct classes per side).
    """


class DenoSocketByteStream(anyio.abc.ByteStream):
    """anyio ByteStream over one deno-side UDS connection handle."""

    def __init__(self, handle) -> None:
        self._handle = handle

    async def receive(self, max_bytes: int = 65536) -> bytes:
        chunk = (await self._handle.recv(max_bytes)).to_bytes()
        if not chunk:
            # Empty read is the bridge's EOF sentinel (see main.ts makeHandle).
            raise anyio.EndOfStream
        return chunk

    async def send(self, item: bytes) -> None:
        await self._handle.send(to_js(item))

    async def send_eof(self) -> None:
        await self._handle.send_eof()

    async def aclose(self) -> None:
        self._handle.close()


class PyodideServiceContext:
    """Concrete ``SandboxServiceContext`` for the pyodide-in-deno runtime."""

    def __init__(self) -> None:
        self._main_protocol_stream = SocketJSONStream(DenoSocketByteStream(_bridge.main))

    @property
    def main_protocol_stream(self) -> "anyio.abc.ObjectStream[JSONTypes]":
        return self._main_protocol_stream

    async def open_transport_stream(self, token: str) -> anyio.abc.ByteStream:
        """Redeem ``token`` on the host's multiplexed transport socket.

        Same handshake as the container runtime's
        ``ContainerServiceContext.open_transport_stream``, over a
        bridge-dialed connection instead of an in-guest UDS.
        """
        stream = DenoSocketByteStream(await _bridge.connect_transport())
        try:
            await write_routing_header(stream, token)
            response = await read_routing_response(stream)
        except BaseException:
            await stream.aclose()
            raise

        if not response.get("ok"):
            error = response.get("error", "bad transport token")
            await stream.aclose()
            raise BadTransportTokenError(str(error))

        return stream


await fxtr_sandbox_rpc_adapter.main(  # noqa: F704  (pyodide top-level await)
    PyodideServiceContext(), entry_point=_ENTRY_POINT  # noqa: F821  (injected global)
)
