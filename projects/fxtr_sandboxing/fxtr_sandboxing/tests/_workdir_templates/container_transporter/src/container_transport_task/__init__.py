"""Container transporter service fixture for the transport E2E tests.

Ported from ``examples/container_transport_task/task_src`` (the deprecated
registry-hosted demo) onto the nix-built image path. Same
``TransporterProto`` shape + TRANSPORT_TASK_NS UUID as the other transport
demos, so host tests can mount one shared definition and use it against any
runtime.
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

# Same UUID + define_type strings as examples/transport_task — produces
# the same wire-form fxtr_type FxtrIDs even though the Python classes
# are distinct.
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
        client = fxtr_sandbox_rpc_adapter.get_client(self._authorizer_ref)
        async with client:
            resp = await client.get(path)
            resp.raise_for_status()
            return resp.text

    @service_method
    async def redeem_explicit_token(self, token: str) -> str:
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

    The config is the authorizer ref itself — same shape as
    examples/transport_task.run.
    """
    return await get_active_scope().mount(_Impl(authorizer_ref))
