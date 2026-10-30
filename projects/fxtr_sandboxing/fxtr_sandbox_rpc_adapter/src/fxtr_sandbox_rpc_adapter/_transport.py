"""Sandbox-side network transport surface.

User code in the sandbox calls :func:`connect_transport` (or the
higher-level :func:`get_client` from ``_http.py``) to open a byte-stream
connection through a host-side network route. Both functions look up
the active :class:`SandboxServiceContext` via a contextvar that
``fxtr_sandbox_rpc_adapter.main`` populates before the user entrypoint
runs — so user code can stay free of plumbing.

The runtime-specific dialing logic lives on
:class:`SandboxServiceContext.open_transport_stream`: the eryx context
adapts the four byte-level ``_transport_*`` callbacks into an
``anyio.abc.ByteStream``; the container context dials the multiplexed
UDS at ``/run/fxtr/transport.sock``, writes the routing header, and
reads back the host's ``{"ok": ...}`` response.

Errors raised by the runtime's ``open_transport_stream`` (e.g. bad or
already-consumed token) propagate up to the caller of
:func:`connect_transport` unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    import anyio.abc
    from fxtr_sandbox_types.service_context import SandboxServiceContext


# Holds the active :class:`SandboxServiceContext` for the current
# adapter session. Populated by ``fxtr_sandbox_rpc_adapter.main`` before
# the user entrypoint runs; reset on exit. ``None`` outside an adapter
# session.
_ctx_var: ContextVar["SandboxServiceContext | None"] = ContextVar(
    "_fxtr_sandbox_service_ctx", default=None
)


@contextmanager
def _set_active_ctx(ctx: "SandboxServiceContext") -> "Iterator[None]":
    """Internal: install ``ctx`` as the active service context for the
    duration of the block. Used by ``main()`` only."""
    token = _ctx_var.set(ctx)
    try:
        yield
    finally:
        _ctx_var.reset(token)


async def connect_transport(token: str) -> "anyio.abc.ByteStream":
    """Redeem ``token`` for a single byte-stream connection.

    ``token`` must be a fresh, unredeemed transport token minted by a
    host-side ``NetworkHandleAuthorizerProto.get_transport_token()``
    call. The runtime dials the appropriate side channel, validates
    the token with the host's transport-connection server, and returns
    the raw byte stream.

    The caller owns the returned stream's lifecycle — close via
    ``aclose()`` or wrap in ``async with``. The host closes its end
    when the underlying connection ends or when the env tears down,
    so a still-open stream surfaces as ``EndOfStream`` /
    ``BrokenResourceError`` at that point.

    Raises ``RuntimeError`` if called outside a
    ``fxtr_sandbox_rpc_adapter.main`` session (no active service
    context). Raises whatever the runtime's
    ``open_transport_stream`` raises on bad / consumed token —
    typically a ``BadTransportTokenError`` from the host's
    ``TransportConnectionServer`` for the eryx path, or a routing-
    header error response surfaced as the same exception type for the
    container path.
    """
    ctx = _ctx_var.get()
    if ctx is None:
        raise RuntimeError(
            "connect_transport called outside a fxtr_sandbox_rpc_adapter.main "
            "session — no active SandboxServiceContext. This function must be "
            "called from inside a sandbox entry point that the adapter is "
            "currently running."
        )
    return await ctx.open_transport_stream(token)
