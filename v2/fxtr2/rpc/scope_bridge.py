"""Scope-aware wrapper around :class:`LeasedCapRPCConnection`.

Substage 7 of stage 8c. See ``plans/phase3/stage8c_user_caps/part7_plan.md``.

:func:`open_rpc_scope` lets a caller bridge an existing
:class:`fxtr2.data.entity_ref_scope.ServiceRef` (mounted/bridged in
the active scope) over an RPC transport, exchange main-service info
with the peer, and yield a fresh :class:`FxtrScope` containing a
:class:`ServiceRef` pointing at the peer's real main service.

Bootstrap protocol
------------------

Both sides eagerly send their main-service info to each other as the
call argument of a single ``Call("fxtr_export_main_service", packet)``
on the peer's wrapper handle. The packet shape is
``IRTagged("fxtr_main_export", {handle, type_id, handle_lease})`` —
``handle`` is the peer's main-service handle, ``type_id`` is its
wire-portable type-identity bytes (raw data-id, no envelope), and
``handle_lease`` is a fresh ``Lease[handle.token]`` from the peer's
host scope. No arena coverage for the type_id — it's wire-portable
identity, not arena-tracked content; consumers wanting the loadable
form dispatch ``$fxtr.type``. Each side's wrapper handle receives the
inbound packet, validates it, writes it to an in-process
``MemoryObjectStream``, and returns ``None``. The body reads the
peer's packet from the channel and installs the peer's main service
into a freshly-opened mountable scope.

Lifetime nesting (outer → inner): ``host_scope`` (caller's active
scope) → ``wrapper_lt`` + ``wrapper_tg`` (the wrapper handle's own
ULL/task-group via :func:`open_lifetime_task_group`) → conn
(:class:`LeasedCapRPCConnection`) → ``user_scope``
(``include=[host_scope]``). On exit, innermost
first: ``user_scope.aclose`` releases the bridge for the peer's
handle/arena → conn drains → wrapper_lt drains. The wrapper sits
*outside* the conn because the conn needs the wrapper as its main
interface from construction, but it sits *inside* the host_scope
because the wrapper holds a reference to the host scope's resources.
"""

from __future__ import annotations

import contextlib
import typing
from collections.abc import AsyncGenerator, Collection, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import anyio.abc
import anyio.streams.memory

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceKey, ServiceRef, ServiceTypeAnnotation
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.lease_types import Lease
from fxtr2.leases.lease_util import lease_cancel_scope, open_lifetime_task_group
from fxtr2.leases.leased_cap_common import IRHandleLocalImplBase, UnwrappedCallArg, wrap_exception
from fxtr2.leases.leased_cap_ir import (
    Call,
    GetItem,
    IRException,
    IRHandle,
    IRTagged,
    IRValue,
    UnwrapForPipelining,
)
from fxtr2.leases.local_lease import LocalLifetimeToken
from fxtr2.rpc.lowering import LeasedCapRPCConnection
from fxtr2.rpc.lowering_types import RPCMessage
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.untyped_service import NoOpService

__all__ = ["RPCSession", "open_rpc_scope"]


####################################################################################################
# Bootstrap wire-shape constants
####################################################################################################


_BOOTSTRAP_METHOD_NAME = "fxtr_export_main_service"
_FXTR_MAIN_EXPORT_TAG = "fxtr_main_export"

# Field names in the bootstrap packet's body dict.
_KEY_HANDLE = "handle"
_KEY_TYPE_ID = "type_id"
_KEY_HANDLE_LEASE = "handle_lease"


####################################################################################################
# Public surface
####################################################################################################


@dataclass(frozen=True)
class RPCSession[T2]:
    """Result of entering :func:`open_rpc_scope`.

    Attributes:
        scope: The freshly-opened mountable :class:`FxtrScope` whose lifetime is bounded
            by the ``open_rpc_scope`` body. The peer's main service is installed in it as
            an Independent cap-bridge backed by the lease the peer shipped during
            bootstrap. Scope drains on body exit, releasing the peer's lease and
            unblocking the connection's drain.
        remote_service: A :class:`ServiceRef` pointing at the peer's real main service,
            with ``target_type`` set to whatever the caller passed for ``remote_service_type``
            and ``service_type_id`` set to the FxtrID the peer included in its bootstrap
            packet. Resolves through ``scope`` like any other ServiceRef.
    """

    scope: FxtrScope
    remote_service: ServiceRef[T2]


@contextlib.asynccontextmanager
async def open_rpc_scope[
    T2
](
    transport: anyio.abc.ObjectStream[RPCMessage],
    our_main_service: ServiceRef[Any] | None = None,
    *,
    isolated: bool = False,
    include: Collection[FxtrScope] = (),
    remote_service_type: ServiceTypeAnnotation[T2] = UnknownService,
    name: str = "rpc_scope",
) -> AsyncGenerator[RPCSession[T2], None]:
    """Open an RPC connection wrapping ``our_main_service`` and bridge in the peer's main.

    Both sides exchange a single bootstrap packet over the transport, then yield an
    :class:`RPCSession` containing a fresh mountable :class:`FxtrScope` and a
    :class:`ServiceRef` for the peer's main service.

    If ``our_main_service`` is provided, the caller must already have it mounted (or
    bridged) in some scope reachable through ``include`` (or, if not ``isolated``, via
    the currently-active scope). The eager precondition lifts the ref's
    :class:`ServiceKey` into the first matching candidate parent — that becomes the
    *host scope* used to mint the bootstrap-packet lease. If no candidate has the ref
    registered, raises ``ValueError`` before opening the transport.

    If ``our_main_service`` is ``None`` (the default), a fresh isolated host scope is
    opened internally and a :class:`NoOpService` is mounted on it. This is useful for
    callers that don't need to expose any callable interface to the peer (the peer
    can read ``ref.service_type_id`` for introspection, but every method invocation
    raises ``IRException(category="UnknownMethod")``).

    The inner ``user_scope`` mirrors :class:`FxtrScope`'s ``isolated`` / ``include``
    semantics: ``include`` is the explicit parent list; ``isolated=False`` (default)
    additionally picks up the currently-active scope as an implicit parent and sets the
    inner scope as active for the body.

    On exit the inner scope drains first (releases its bridge for the peer's handle and
    arena), the connection then drains and closes the transport, and the wrapper's
    lifetime drains last. Each side's drain naturally waits for the peer to release the
    matching grant exports — no explicit cross-task coordination needed.

    The body is wrapped in a :func:`lease_cancel_scope` tied to the connection's main
    grant import; if the connection aborts (peer disconnect, peer-initiated abort, etc.)
    the body is cancelled promptly even if it's parked on a long-lived ``await`` while
    holding the peer's main service. The original abort cause surfaces via the conn's
    standard exception path; the body sees a :class:`LeaseBrokenError`.

    Args:
        transport: Bidirectional RPC-message stream. The caller is responsible for any
            byte-level framing / wrapping (e.g. ``wrap_json_stream``).
        our_main_service: The ServiceRef this side exposes as its main interface, or
            ``None`` to use a freshly-mounted :class:`NoOpService`. If non-None, must be
            reachable from at least one of the candidate parent scopes (``include`` plus
            the active scope when not isolated).
        isolated: Pass-through to the inner ``user_scope``. When True, the inner scope
            does not auto-include the currently-active scope, and does not set itself
            as active for the body. The active-scope lookup for the host_scope
            precondition also skips the active scope when ``isolated=True``.
        include: Pass-through to the inner ``user_scope``. Explicit parent scopes for
            the inner scope; also consulted by the host_scope precondition lookup.
        remote_service_type: The target type the yielded :class:`ServiceRef` will carry.
            Defaults to :data:`UnknownService`. Substage 8 supplies typed protocol
            annotations here without changing this layer.
        name: Used in lifetime / scope names for logging.

    Yields:
        :class:`RPCSession[T2]` containing the inner scope + peer's main ref.
    """
    async with contextlib.AsyncExitStack() as stack:
        # Step 1: resolve host_scope + our_main_service. If the caller passed None,
        # mount a NoOpService on a fresh isolated scope; that scope is registered with
        # ``stack`` and torn down at ``open_rpc_scope`` exit.
        if our_main_service is None:
            noop_host = await stack.enter_async_context(
                FxtrScope(isolated=True, name=f"{name}/noop_host")
            )
            our_main_service = await noop_host.mount(NoOpService())
            host_scope: FxtrScope = noop_host
        else:
            # Create a new scope (a peer to the inner scope we will create) and lift our main
            # service there, ensuring it is reachable.
            host_include = list(include)
            if not isolated:
                host_include.append(FxtrScope.get_active())
            host_scope = await stack.enter_async_context(
                FxtrScope(isolated=True, include=host_include, name=f"{name}/host")
            )
            host_scope.lift(our_main_service)

        assert host_scope.is_tracked(our_main_service)

        # Step 2: open the wrapper lifetime, packet-channel streams, and connection.
        wrapper_lt, wrapper_tg = await stack.enter_async_context(
            open_lifetime_task_group(f"{name}/wrapper")
        )
        packet_send, packet_recv = anyio.create_memory_object_stream[_BootstrapPacket](1)
        await stack.enter_async_context(packet_send)
        await stack.enter_async_context(packet_recv)
        wrapper = _MainServiceWrapperHandle(
            token=wrapper_lt.token,
            task_group=wrapper_tg,
            packet_send_stream=packet_send,
        )
        conn = await stack.enter_async_context(
            LeasedCapRPCConnection(transport, wrapper, wrapper_lt.root_lease.take())
        )

        # Step 3: cloned keepalive on the conn's main grant import. Held throughout the
        # body so a peer-side abort breaks it (via the conn's import-teardown path),
        # tripping ``lease_cancel_scope`` and cancelling the body. The original
        # ``their_main_lease`` is consumed by ``do_send``. Registering ``discard`` as a
        # stack callback (after ``conn``) means it fires before ``conn.__aexit__`` —
        # same ordering as the original explicit try/finally structure.
        their_main_keepalive: Lease[Any] = conn.their_main_lease.clone()
        stack.callback(their_main_keepalive.discard)

        # Step 4: bootstrap exchange — send our packet, receive peer's. Done outside
        # the stack so we can use a one-shot task group.
        our_packet = _build_export_packet(
            host_scope=host_scope,
            our_main_service=our_main_service,
        )
        peer_packet: _BootstrapPacket | None = typing.cast("_BootstrapPacket | None", None)

        async def do_send() -> None:
            await conn.their_main_interface.request(
                conn.their_main_lease.take(),
                Call(method=_BOOTSTRAP_METHOD_NAME, arg=our_packet),
            )

        async def do_recv() -> None:
            nonlocal peer_packet
            peer_packet = await packet_recv.receive()

        try:
            async with anyio.create_task_group() as bootstrap_tg:
                bootstrap_tg.start_soon(do_send)
                bootstrap_tg.start_soon(do_recv)
            assert peer_packet is not None
            # Close the packet channel so any subsequent peer wrapper invocation
            # gets a closed-resource error rather than blocking on the buffer-1 send.
            await packet_send.aclose()

            # Step 5: install peer's main service into a fresh ``user_scope`` and
            # yield. ``user_scope`` is opened *inside* ``lease_cancel_scope`` (and
            # NOT in the stack) so its drain ordering is correct on the abort path:
            # body cancels → user_scope drains first → lease_cancel_scope exits
            # (raises LeaseBrokenError if the keepalive broke) → outer stack walks
            # back through conn, streams, wrapper_lt.
            with lease_cancel_scope(their_main_keepalive):
                async with FxtrScope(
                    isolated=isolated,
                    include=tuple(include) if include else (),
                    name=name,
                ) as user_scope:
                    their_ref = _install_peer_main_service(
                        user_scope, peer_packet, remote_service_type
                    )
                    # Install consumed peer_packet's leases — clear so the cleanup
                    # below doesn't double-discard.
                    peer_packet = None
                    yield RPCSession(scope=user_scope, remote_service=their_ref)
        finally:
            # Failure paths (bootstrap_tg raised, install raised, body raised before
            # clearing peer_packet) leave peer_packet's lease unconsumed. Discard so
            # the conn's import accounting can drain.
            if peer_packet is not None:
                peer_packet.handle_lease.discard()


####################################################################################################
# Internal: parsed bootstrap packet
####################################################################################################


@dataclass(frozen=True)
class _BootstrapPacket:
    """Parsed form of an inbound ``IRTagged("fxtr_main_export", ...)`` packet.

    The reference fields (``handle`` and ``handle_lease``) carry the lease /
    handle imported through the connection. The lease is owned by the recv side
    after the wrapper writes the packet to the channel — it must be either
    consumed by ``_install_peer_main_service`` or explicitly discarded on the
    failure path.
    """

    handle: IRHandle[Any]
    type_id: FxtrID
    handle_lease: Lease[Any]


class _MalformedBootstrapError(Exception):
    """Internal exception type for bootstrap-packet shape failures.

    Caught at the wrapper's request boundary and rewrapped as
    ``IRException(category="MalformedBootstrap")`` via ``wrap_exception``.
    """


####################################################################################################
# Internal: wrapper IRHandle
####################################################################################################


class _MainServiceWrapperHandle(IRHandleLocalImplBase[Any]):
    """Tiny IRHandle that hosts the bootstrap method on its own ULL + task group.

    The wrapper is what each side passes to :class:`LeasedCapRPCConnection` as
    ``our_main_interface``. Its only method is :data:`_BOOTSTRAP_METHOD_NAME`, which
    parses the call argument as a :class:`_BootstrapPacket` and writes it to the
    in-process channel that ``open_rpc_scope`` reads from.

    The wrapper does **not** host the user's real main service — that lives on
    ``host_scope`` and is sent inside the bootstrap packet, not exposed as the
    connection's main interface.
    """

    def __init__(
        self,
        *,
        token: LocalLifetimeToken[Any],
        task_group: anyio.abc.TaskGroup,
        packet_send_stream: anyio.streams.memory.MemoryObjectSendStream[_BootstrapPacket],
    ) -> None:
        self._token = token
        self._task_group = task_group
        self._packet_send = packet_send_stream

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[UnwrappedCallArg] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        # The wrapper holds no per-call state; outer dispatch lease is purely a
        # gate against post-drain dispatches and discarded immediately.
        lease.discard()
        if not isinstance(request, Call):
            raise IRException(
                f"_MainServiceWrapperHandle does not support {type(request).__name__} "
                "requests; only `fxtr_export_main_service` is supported.",
                category="UnknownMethod",
            )
        if request.method != _BOOTSTRAP_METHOD_NAME:
            raise IRException(
                "_MainServiceWrapperHandle does not support method "
                f"{request.method!r}; only {_BOOTSTRAP_METHOD_NAME!r} is supported.",
                category="UnknownMethod",
            )
        try:
            packet = _parse_bootstrap_packet(request.arg)
        except _MalformedBootstrapError as exc:
            raise wrap_exception(exc, category="MalformedBootstrap") from exc
        # Hand ownership of the packet's leases to the recv side.
        await self._packet_send.send(packet)
        return None


####################################################################################################
# Internal: packet build / parse
####################################################################################################


def _build_export_packet(
    *,
    host_scope: FxtrScope,
    our_main_service: ServiceRef[Any],
) -> IRTagged[str, Any]:
    """Build the outbound ``IRTagged("fxtr_main_export", ...)`` packet.

    Mints a fresh lease on ``our_main_service.handle.token`` via host_scope's bridge
    graph and packages it with the handle and the type_id raw bytes. The
    type_id is wire-portable identity (no arena coverage needed); the peer
    dispatches ``$fxtr.type`` to fetch the loadable form on demand.

    Caller is responsible for the precondition lift (done at ``open_rpc_scope`` entry).
    """
    # Mint Lease[our_main_service.handle.token] via host_scope's bridge graph.
    # Inline the same logic as ``ServiceProxyContext.mint_lease_for_key`` rather
    # than adding a public scope helper or building a synthetic context — the
    # precondition lift was already done by ``open_rpc_scope``, so the entry is
    # guaranteed to exist locally.
    own_key = ServiceKey(our_main_service.handle, our_main_service.service_type_id)
    entry = host_scope._cap_bridges[own_key.handle.token]
    handle_lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any] = (
        host_scope._mint_lease_via_ancestor_bridge(host_scope.scope_token, entry.bridge)
    )

    body: dict[str, Any] = {
        _KEY_HANDLE: our_main_service.handle,
        _KEY_TYPE_ID: bytes(our_main_service.service_type_id),
        _KEY_HANDLE_LEASE: handle_lease,
    }
    return IRTagged(tag=_FXTR_MAIN_EXPORT_TAG, value=body)


def _parse_bootstrap_packet(arg: UnwrappedCallArg) -> _BootstrapPacket:
    """Validate + extract a :class:`_BootstrapPacket` from an inbound call argument.

    Raises :class:`_MalformedBootstrapError` on any shape mismatch — the caller
    rewraps as ``IRException(category="MalformedBootstrap")`` via ``wrap_exception``
    so the original failing line is captured in ``traceback_info``.

    On a malformed packet the inbound leases (if any) are *not* explicitly discarded
    here; the runtime's standard ``IRHandleLocalImplBase`` cleanup path discards any
    leases inside the rejected request.
    """
    if not isinstance(arg, IRTagged):
        raise _MalformedBootstrapError(f"bootstrap arg must be IRTagged; got {type(arg).__name__}")
    if arg.tag != _FXTR_MAIN_EXPORT_TAG:
        raise _MalformedBootstrapError(
            f"bootstrap arg tag must be {_FXTR_MAIN_EXPORT_TAG!r}; got {arg.tag!r}"
        )
    body = arg.value
    if not isinstance(body, Mapping):
        raise _MalformedBootstrapError(
            f"bootstrap arg body must be a mapping; got {type(body).__name__}"
        )
    handle = body.get(_KEY_HANDLE)
    type_id_raw = body.get(_KEY_TYPE_ID)
    handle_lease = body.get(_KEY_HANDLE_LEASE)
    if not isinstance(handle, IRHandle):
        raise _MalformedBootstrapError(
            f"bootstrap body[{_KEY_HANDLE!r}] must be IRHandle; got {type(handle).__name__}"
        )
    if not isinstance(handle_lease, Lease):
        raise _MalformedBootstrapError(
            f"bootstrap body[{_KEY_HANDLE_LEASE!r}] must be Lease; "
            f"got {type(handle_lease).__name__}"
        )
    if not isinstance(type_id_raw, bytes):
        raise _MalformedBootstrapError(
            f"bootstrap body[{_KEY_TYPE_ID!r}] must be raw bytes; got {type_id_raw!r}"
        )
    type_id = FxtrID(raw_data_id=type_id_raw)
    if handle_lease.subject is not handle.token:
        raise _MalformedBootstrapError(
            "bootstrap handle_lease.subject does not match handle.token."
        )
    return _BootstrapPacket(
        handle=handle,
        type_id=type_id,
        handle_lease=handle_lease,
    )


####################################################################################################
# Internal: peer main-service install
####################################################################################################


def _install_peer_main_service[
    T2
](
    user_scope: FxtrScope,
    packet: _BootstrapPacket,
    remote_service_type: ServiceTypeAnnotation[T2],
) -> ServiceRef[T2]:
    """Install the peer's main service into ``user_scope``.

    Installs the handle bridge via :meth:`FxtrScope.attach_bridged_lowlevel_cap`
    as an :class:`IndependentLocalBridge` (the peer's lifetime is independent
    of ours, anchored by ``packet.handle_lease``).

    Takes ownership of ``packet.handle_lease`` — it's consumed by the bridge
    install. Caller's ``finally`` only needs to discard it on the *failure*
    path before this function returns.

    The peer's ``service_type_id`` is wire-portable identity; it isn't tracked
    in ``user_scope``, and consumers wanting the loadable form invoke
    ``$fxtr.type`` over the handle.
    """
    # Install the peer's main handle as a service. handle_lease lives on
    # handle.token (verified in _parse_bootstrap_packet), so the IndependentLocalBridge
    # subject-match check inside attach_bridged_lowlevel_cap passes.
    return user_scope.attach_bridged_lowlevel_cap(
        handle=packet.handle,
        service_type=remote_service_type,
        service_type_id=packet.type_id,
        bridge=IndependentLocalBridge(lease=packet.handle_lease),
    )
