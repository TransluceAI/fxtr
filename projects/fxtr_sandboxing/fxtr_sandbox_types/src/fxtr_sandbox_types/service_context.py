"""Runtime ↔ adapter contract for sandboxed services.

`SandboxServiceContext` is the typing.Protocol that each runtime (eryx WASM,
Docker container, future runtimes) implements with its own concrete class.
``fxtr_sandbox_rpc_adapter.main(ctx, *, entry_point=...)`` accepts any
object satisfying this protocol; that's how the same adapter code drives
two very different transports.

The protocol exposes two pieces of runtime-specific machinery:

- ``main_protocol_stream`` — the JSON-typed ObjectStream backing the
  RPC connection between the sandbox and the host. Each runtime picks
  the JSON ObjectStream impl that matches its transport: eryx uses
  ``JSONObjectStream`` over JSON-typed callbacks; containers use
  ``SocketJSONStream`` over an anyio UDS byte stream.
- ``open_transport_stream`` — opens one byte-stream connection to a
  host-side network transport, identified by a single-use token
  (minted by ``NetworkHandleAuthorizerProto.get_transport_token`` over
  the main RPC connection). Each runtime dials its own side channel:
  eryx uses the four byte-level ``_transport_*`` callbacks; containers
  dial the multiplexed UDS at ``/run/fxtr/transport.sock`` and write a
  ``{"token": ...}`` routing header. The adapter exposes this as a
  contextvar-backed free function ``connect_transport(token)`` so user
  code can call it without threading the ctx through every call site.

Concrete impls satisfy the protocol structurally, so adding a new method
on the protocol will surface as a static-typing error in any concrete
impl that hasn't been updated.
"""

from __future__ import annotations

from typing import Protocol

import anyio.abc
from anyio.abc import ObjectStream

from .json_object_stream import JSONTypes


class SandboxServiceContext(Protocol):
    """Runtime-provided context handed to ``fxtr_sandbox_rpc_adapter.main``."""

    @property
    def main_protocol_stream(self) -> ObjectStream[JSONTypes]:
        """ObjectStream backing the sandbox↔host RPC connection.

        Same instance is returned each time — the adapter caches it for the
        connection's lifetime, so identity matters. Implementations should
        construct the stream once (typically at ``__init__`` time) and
        return it as a stable attribute.
        """
        ...

    async def open_transport_stream(self, token: str) -> anyio.abc.ByteStream:
        """Redeem a transport token; return one byte-stream connection.

        ``token`` is a single-use string minted by a host-side
        ``NetworkHandleAuthorizerProto.get_transport_token`` call. The
        runtime dials its own side channel (eryx callbacks for WASM,
        the multiplexed UDS for containers), validates the token with
        the host's transport-connection server, and returns the
        resulting raw byte stream.

        The caller owns the stream's lifecycle — close it via
        ``aclose()`` or ``async with``. The host side closes its end
        when the underlying connection ends or when the env tears
        down, so a still-open stream surfaces as ``EndOfStream`` /
        ``BrokenResourceError`` at that point.

        Raises ``BadTransportTokenError`` (defined in the host package
        and re-exported by ``fxtr_sandbox_rpc_adapter``) when the
        token is missing / already consumed.
        """
        ...
