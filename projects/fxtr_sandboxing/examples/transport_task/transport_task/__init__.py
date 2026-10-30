"""Stage-3 demo service: exercises the token-keyed transport surface.

The entry point takes a ``ServiceRef[NetworkHandleAuthorizerProto]``
directly as its config (no wrapping dataclass — DataclassEntity fields
have to be entity-graph serializable, but ServiceRef only travels
through the service-dialect channel for service-call args). Exposes
``raw_echo(msg)``: sends ``msg`` over the authorizer's transport,
reads everything back, returns the result. The host registers a
``raw_transport`` whose handler echoes received bytes; sending
``"hello"`` should round-trip ``"hello"`` back.

Tests use this to validate the eryx callback path end-to-end:

    user code
      → connect_transport(token)            (free function via contextvar)
      → ctx.open_transport_stream(token)    (EryxServiceContext method)
      → eryx _transport_connect(token)      (host callback)
      → consume_token in TransportConnectionServer
      → handler invocation on the host's launcher
      → byte echo across the socketpair-backed ByteStream
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import anyio.abc
import fxtr_sandbox_rpc_adapter
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto

# Shared with the host-side test module — same (namespace_uuid, name)
# pair produces the same wire-form FxtrID, so encode/decode round-trips
# even though the Python classes are distinct.
TRANSPORT_TASK_NS = uuid.UUID("d4f1a812-7e09-4d3b-b7e8-2a39d6c1e4fa")

TRANSPORTER_TYPE_ID = system_entities.define_type(TRANSPORT_TASK_NS, "transporter")


@service_protocol
class TransporterProto(Protocol):
    """Protocol the transport-task service serves."""

    async def raw_echo(self, msg: str) -> str: ...
    async def http_get(self, path: str) -> str: ...
    async def redeem_explicit_token(self, token: str) -> str: ...


class _Impl(TypedServiceImpl[TransporterProto]):
    def __init__(self, authorizer_ref: ServiceRef[NetworkHandleAuthorizerProto]) -> None:
        self._authorizer_ref = authorizer_ref

    def _target_type_annotation(self) -> Any:
        return TransporterProto

    def _service_type_id(self) -> FxtrID:
        return TRANSPORTER_TYPE_ID

    @service_method
    async def raw_echo(self, msg: str) -> str:
        """Send ``msg`` over the authorizer's transport, return the bytes
        we got back as a string.

        Mints a single-use token via the authorizer, opens one
        byte-stream connection through ``connect_transport``, sends the
        message, half-closes the send side so the echo handler sees EOF,
        reads the entire response, returns it.
        """
        # Resolve the authorizer in the current (dispatch) scope so the
        # witness returned by get_transport_token() lands here and gets
        # cleaned up promptly. See network_authorizer.py docstring.
        authorizer = await self._authorizer_ref.resolve(get_active_scope())
        token, _witness_ref = await authorizer.get_transport_token()
        stream: anyio.abc.ByteStream = await fxtr_sandbox_rpc_adapter.connect_transport(token)
        async with stream:
            await stream.send(msg.encode("utf-8"))
            await stream.send_eof()
            chunks: list[bytes] = []
            try:
                while True:
                    chunk = await stream.receive(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except anyio.EndOfStream:
                pass
            return b"".join(chunks).decode("utf-8")

    @service_method
    async def http_get(self, path: str) -> str:
        """GET ``path`` through the authorizer's HTTP route, return body.

        Uses ``fxtr_sandbox_rpc_adapter.get_client(authorizer_ref)`` to
        build an ``httpx.AsyncClient`` whose backend mints + redeems a
        token per connection. The caller passes a path; the route's
        upstream URL + injected headers are picked up by the host-side
        proxy.
        """
        client = fxtr_sandbox_rpc_adapter.get_client(self._authorizer_ref)
        async with client:
            resp = await client.get(path)
            resp.raise_for_status()
            return resp.text

    @service_method
    async def redeem_explicit_token(self, token: str) -> str:
        """Redeem an explicitly-passed token via ``connect_transport``.

        Used by the bad-token-error test to exercise the failure path
        without going through the authorizer's mint step. The body is
        the same shape as ``raw_echo`` but skips the mint — letting the
        test pass an invalid token.
        """
        stream: anyio.abc.ByteStream = await fxtr_sandbox_rpc_adapter.connect_transport(token)
        async with stream:
            await stream.send(b"hello")
            await stream.send_eof()
            chunks: list[bytes] = []
            try:
                while True:
                    chunk = await stream.receive(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except anyio.EndOfStream:
                pass
            return b"".join(chunks).decode("utf-8")


async def run(
    authorizer_ref: ServiceRef[NetworkHandleAuthorizerProto],
) -> ServiceRef[TransporterProto]:
    """fxtr.service entry point.

    The config is the authorizer ref itself — passed straight through
    the service-dialect channel as a ``ServiceRef[UnknownService]`` and
    re-typed by the adapter via ``decode_from_service_data`` against
    this signature's annotation. The active scope at call time is the
    ``connect`` dispatch scope, so ``authorizer_ref`` is already
    bridged here; mounting on this scope means method calls on the
    impl can resolve the ref through the dispatch hierarchy.
    """
    return await get_active_scope().mount(_Impl(authorizer_ref))
