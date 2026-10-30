"""Internal runner script executed inside the eryx sandbox.

This script is NOT part of the user's task module — it's injected by the host
at execution time, after the socket and loop shims. It builds an
`EryxServiceContext` (a :class:`SandboxServiceContext` impl backed by the
eryx ``_send_to_host`` / ``_receive_from_host`` JSON-typed callbacks plus
the four ``_transport_*`` byte-level callbacks) and hands it to
`fxtr_sandbox_rpc_adapter.main`, which resolves the user callable and
owns the RPC bridge.

The host sets ``_ENTRY_POINT`` immediately before this file's contents (e.g.
``_ENTRY_POINT = "my_pkg:run"``), taken from ``TaskBundle.entry_point``.

The eryx callbacks `_receive_from_host` / `_send_to_host` and (when
the host registers them) `_transport_connect` / `_transport_send` /
`_transport_recv` / `_transport_close` live in the global namespace
of the sandbox — registered by the host before ``sandbox.execute(...)``.
This script closes over them via lexically-scoped wrappers.
"""

import base64

import anyio.abc
import fxtr_sandbox_rpc_adapter
from fxtr_sandbox_types.json_object_stream import JSONObjectStream


async def _send(data):
    await _send_to_host(data=data)  # noqa: F821 — host-injected callback


async def _receive():
    return await _receive_from_host()  # noqa: F821 — host-injected callback


class _EryxTransportByteStream(anyio.abc.ByteStream):
    """anyio ByteStream wrapping a single eryx transport connection.

    Backed by the four host-registered callbacks (``_transport_connect``
    / ``_transport_send`` / ``_transport_recv`` / ``_transport_close``).
    The connect callback returned a ``conn_id`` string identifying this
    connection; subsequent ops pass that id back so the host can route
    bytes to the right socketpair end.

    Bytes go over the eryx callback bridge as base64-encoded strings —
    the eryx wire vocabulary is JSON-typed, not raw bytes. Each
    ``send`` / ``receive`` pays one base64 round-trip.
    """

    def __init__(self, conn_id):
        self._conn_id = conn_id
        self._closed = False

    async def receive(self, max_bytes=65536):
        data_b64 = await _transport_recv(conn_id=self._conn_id, max_bytes=max_bytes)  # noqa: F821
        if not data_b64:
            raise anyio.EndOfStream
        return base64.b64decode(data_b64)

    async def send(self, item):
        data_b64 = base64.b64encode(item).decode("ascii")
        await _transport_send(conn_id=self._conn_id, data_b64=data_b64)  # noqa: F821

    async def send_eof(self):
        # Half-close: shut down the write side of the host-side
        # socketpair end, so the handler's read side sees EOF on its
        # next receive. Reads on this stream still work afterwards.
        await _transport_send_eof(conn_id=self._conn_id)  # noqa: F821

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        await _transport_close(conn_id=self._conn_id)  # noqa: F821

    @property
    def extra_attributes(self):
        return {}


class EryxServiceContext:
    """SandboxServiceContext impl backed by eryx JSON-typed callbacks.

    The eryx channel is a pair of JSON-typed callbacks — there's no
    underlying byte stream to half-close — so the main protocol stream
    is `JSONObjectStream`, which uses the ``["!", "eof"]`` escape frame
    to signal EOF in-band.

    Network transports (when registered host-side) are wrapped via
    :class:`_EryxTransportByteStream` — one byte-stream per
    ``open_transport_stream(token)`` call. The token is redeemed
    synchronously via the ``_transport_connect`` callback; bad /
    consumed tokens raise out of the callback and propagate up.
    """

    def __init__(self):
        self._main_protocol_stream = JSONObjectStream(_send, _receive)

    @property
    def main_protocol_stream(self):
        return self._main_protocol_stream

    async def open_transport_stream(self, token):
        conn_id = await _transport_connect(token=token)  # noqa: F821
        return _EryxTransportByteStream(conn_id)


await fxtr_sandbox_rpc_adapter.main(EryxServiceContext(), entry_point=_ENTRY_POINT)  # noqa: F821
