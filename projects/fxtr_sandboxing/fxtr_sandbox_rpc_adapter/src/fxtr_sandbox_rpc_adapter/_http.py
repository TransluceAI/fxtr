"""HTTP-shaped sandbox transport surface (gated on ``[http]`` extra).

:func:`get_client` returns a normal ``httpx.AsyncClient`` whose backend
mints a fresh single-use transport token + opens one side-channel
connection per ``connect_tcp``. Each connect happens inside its own
short-lived ``FxtrScope``, with the authorizer **ref** resolved fresh
in that scope — so the witness returned by ``get_transport_token()``
lands in the same per-connection scope and gets cleaned up promptly
on connect failure (no-op on success since the token was consumed
before the scope closes).

This module imports ``httpx`` / ``httpcore`` lazily inside
:func:`get_client` so the adapter's import chain works without the
``[http]`` extra installed. Calling :func:`get_client` without the
extra raises a clear ``RuntimeError`` instead of an opaque
``ImportError``.

Why takes a ``ServiceRef`` and not a resolved proxy: the lifetime of
resources returned by a service call is bounded by the **resolution
scope** of the service ref the caller called against, not by the
active scope at the call site. If we accepted a resolved proxy, the
witness's lifetime would be tied to whatever long-lived scope the
user resolved in — defeating the prompt-cleanup the witness pattern
provides. By taking the ref and resolving it per-connection inside
the backend, we guarantee the witness's lifetime is bounded by one
connect_tcp call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto

from ._transport import connect_transport

if TYPE_CHECKING:
    from collections.abc import Iterable

    import httpcore
    import httpx
    from httpcore._backends.base import SOCKET_OPTION


def _import_httpx_or_raise() -> tuple[Any, Any]:
    """Lazy-import ``httpx`` + ``httpcore``; raise a clear error if not installed.

    Returns the modules so the caller can use them without ``import``
    at module scope (which would be an unconditional dependency).
    """
    try:
        import httpcore as _httpcore
        import httpx as _httpx
    except ImportError as e:
        raise RuntimeError(
            "fxtr_sandbox_rpc_adapter.get_client requires httpx + httpcore — "
            "install the [http] extra:\n"
            "  pip install fxtr-sandbox-rpc-adapter[http]\n"
            "(Or use connect_transport(token) directly for raw byte-stream "
            "access without the http stack.)\n"
            f"\nUnderlying error: {e}"
        ) from e
    return _httpx, _httpcore


def get_client(
    authorizer_ref: ServiceRef[NetworkHandleAuthorizerProto],
    *,
    base_url: str = "http://proxy",
) -> httpx.AsyncClient:
    """Build a ``httpx.AsyncClient`` for a network route.

    Each connection the client opens runs through the authorizer's
    side channel: a fresh single-use token is minted via
    ``get_transport_token()``, redeemed via :func:`connect_transport`,
    and the resulting byte stream is wrapped as the connection's
    transport. HTTP semantics live in ``httpx`` / ``httpcore``; the
    custom backend just provides the byte stream.

    Args:
        authorizer_ref: Capability ref bridged from the host. Must be
            a ``ServiceRef`` (not a resolved proxy) so the backend can
            resolve it fresh inside each ``connect_tcp`` child scope —
            see the module docstring for why.
        base_url: Base URL passed to ``httpx.AsyncClient``. The
            request's ``Host`` / actual host is irrelevant — routing
            is decided at token-mint time on the host. Defaults to
            ``"http://proxy"`` as a placeholder.

    Returns:
        An ``httpx.AsyncClient`` ready for use. The caller is
        responsible for ``aclose()``-ing it (or using ``async with``).

    Raises:
        RuntimeError: If httpx / httpcore are not installed (the
            ``[http]`` extra wasn't selected).
    """
    httpx, httpcore = _import_httpx_or_raise()

    backend = _AuthorizingBackend(authorizer_ref, httpcore_module=httpcore)
    pool = httpcore.AsyncConnectionPool(network_backend=backend)
    transport = httpx.AsyncHTTPTransport()
    transport._pool = pool  # type: ignore[attr-defined]
    return httpx.AsyncClient(transport=transport, base_url=base_url)


class _AuthorizingBackend:
    """Custom ``httpcore.AsyncNetworkBackend`` that mints + redeems a
    transport token for each new connection.

    Subclasses ``httpcore.AsyncNetworkBackend`` only at runtime
    (``httpcore`` is a lazy import). Type checking treats this as a
    plain class with an ``async def connect_tcp`` method matching the
    httpcore protocol structurally.
    """

    def __init__(
        self,
        authorizer_ref: ServiceRef[NetworkHandleAuthorizerProto],
        *,
        httpcore_module: Any,
    ) -> None:
        self._authorizer_ref = authorizer_ref
        # Pin the httpcore module so connect_tcp can build an
        # ``AsyncNetworkStream``-shaped wrapper without re-importing.
        self._httpcore = httpcore_module

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Per-connection child scope. Resolving the authorizer ref
        # inside this scope guarantees the witness returned by
        # get_transport_token() lands here too — so when connect_tcp
        # returns (success or failure), the scope drains and any
        # unredeemed token is revoked promptly.
        async with FxtrScope(name="transport_connect") as connect_scope:
            authorizer = await self._authorizer_ref.resolve(connect_scope)
            token, _witness_ref = await authorizer.get_transport_token()
            # _witness_ref is bridged into connect_scope by the
            # framework. We don't need to keep a local handle — when
            # connect_scope drains the bridge drops.
            stream = await connect_transport(token)
            return _ByteStreamHttpcoreStream(stream, self._httpcore)


class _ByteStreamHttpcoreStream:
    """Adapts an ``anyio.abc.ByteStream`` to httpcore's
    ``AsyncNetworkStream`` interface.

    Lives outside the per-connection ``FxtrScope`` — the underlying
    stream is independent of any scope's lifetime, and the stream's
    own ``aclose`` is what closes the side-channel connection.
    """

    def __init__(self, stream: Any, httpcore_module: Any) -> None:
        self._stream = stream
        # ``httpcore.AsyncNetworkStream`` runtime base via duck-typing —
        # we don't subclass it because httpcore is a lazy import.
        # httpcore's pool only checks structural compatibility.
        del httpcore_module  # reserved for future extension hooks

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        import anyio

        try:
            return await self._stream.receive(max_bytes)
        except anyio.EndOfStream:
            return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.send(buffer)

    async def aclose(self) -> None:
        await self._stream.aclose()

    def get_extra_info(self, info: str) -> Any:  # pragma: no cover — httpcore probe
        return None
