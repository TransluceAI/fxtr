"""Tests for fxtr2.rpc.lowering — the LeasedCapIR lowering layer.

Stage D.1 (§2a) coverage: scaffold, handshake-free bootstrap, clean shutdown.

The transport is always an in-process pair of ``anyio.create_memory_object_stream``
stapled together so the two halves of the connection can talk to each other. Later
substages add the per-message-handler tests on top of this fixture.
"""

from __future__ import annotations

import math
from typing import Any, Callable, cast

import anyio
import anyio.abc
import pytest
from anyio.streams.stapled import StapledObjectStream

from fxtr2.leases.lease_types import Lease, LeaseBrokenError
from fxtr2.leases.lease_util import open_lifetime_task_group
from fxtr2.leases.leased_cap_common import IRHandleLocalImplBase, spawn_ir_future_in_task_group
from fxtr2.leases.leased_cap_ir import (
    Call,
    GetItem,
    IRException,
    IRFuture,
    IRValue,
    UnwrapForPipelining,
    WaitAndIgnoreFutureThen,
)
from fxtr2.leases.local_lease import LocalLifetimeToken, UnconditionalLocalLifetime
from fxtr2.rpc.lowering import (
    MAIN_GRANT_ID,
    MAIN_HANDLE_ID,
    MAIN_TOKEN_ID,
    FutureImportEntry,
    GrantImportEntry,
    HandleImportEntry,
    HandshakeError,
    LeasedCapRPCConnection,
    NullMainInterface,
    RemoteGrant,
    RemoteIRFuture,
    RemoteIRHandle,
    RPCConnectionAborted,
    TokenImportEntry,
)
from fxtr2.rpc.lowering_types import (
    AbortMessage,
    AwaitFutureExpr,
    BreakMessage,
    ClosedMessage,
    DiscardMessage,
    ExportedIRFutureExpr,
    ExportedLeaseExpr,
    HandleRequestExpr,
    HelloMessage,
    ImportedIRFutureExpr,
    ImportedLeaseExpr,
    PullMessage,
    PushMessage,
    RejectMessage,
    ResolveMessage,
    RPCMessage,
    StaticExpr,
    UnpullMessage,
    WalkFutureExpr,
    WatchMessage,
)

pytestmark = pytest.mark.anyio


# Wrapped around each §2b test body to convert deadlocks into test failures rather
# than hangs. Five seconds is plenty: all §2b tests involve at most a handful of
# round-trip messages through in-process memory streams.
_TEST_DEADLOCK_TIMEOUT: float = 5.0


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_transport_pair() -> tuple[
    StapledObjectStream[RPCMessage],
    StapledObjectStream[RPCMessage],
]:
    """Construct two stapled memory-object streams wired as a bidirectional RPC pair.

    Returns ``(a_transport, b_transport)`` — A sends → B receives; B sends → A receives.
    """
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    a_transport = StapledObjectStream(a_to_b_send, b_to_a_recv)
    b_transport = StapledObjectStream(b_to_a_send, a_to_b_recv)
    return a_transport, b_transport


class _EchoMain(IRHandleLocalImplBase[Any]):
    """Minimal server-side main interface used by a few tests.

    Implements ``_run_request_async`` by echoing the request's call argument (for
    ``Call``) or indexing into a small dict (for ``GetItem``). Not used by §2a tests
    directly; provided so later substages can share the scaffold.
    """

    def __init__(
        self,
        token: Any,
        task_group: anyio.abc.TaskGroup,
    ) -> None:
        self._token = token
        self._task_group = task_group

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call):
            return {"echoed": request.arg, "method": request.method}
        return {"attr": request.attr}


# ---------------------------------------------------------------------------
# §2a tests — scaffold + handshake-free bootstrap
# ---------------------------------------------------------------------------


class TestClientServerBootstrap:
    """Both halves enter their async CM, come up, and shut down cleanly.

    No application work is performed — just connect and close. The only messages on
    the wire are the two ``HelloMessage``s. Validated by capturing the stream with a
    sniffer-wrapper below.
    """

    async def test_two_connections_come_up_and_shut_down(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        # No explicit rendezvous needed: each side enters its CM, runs its
        # property-access asserts, exits. Both sides' conn drain inside the
        # CM exit waits for the peer to release the grant exports — the
        # drain itself is the rendezvous. ``fail_after`` catches any hang.
        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _EchoMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ) as conn:
                            assert isinstance(conn.their_main_interface, RemoteIRHandle)
                            assert isinstance(conn.their_main_lease.grant, RemoteGrant)

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        assert isinstance(conn.their_main_interface, RemoteIRHandle)
                        assert isinstance(conn.their_main_lease.grant, RemoteGrant)

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestPropertyAccess:
    """The public properties return sensible values immediately after construction."""

    async def test_their_main_interface_is_remote_handle_on_id_1(self) -> None:
        a_transport, b_transport = _make_transport_pair()
        async with anyio.create_task_group() as tg:

            async def side_a() -> None:
                async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                    a_main = _EchoMain(a_lt.token, a_tg)
                    async with LeasedCapRPCConnection.as_server(
                        a_transport, a_main, a_lt.root_lease.clone()
                    ):
                        pass

            async def side_b() -> None:
                async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                    handle = conn.their_main_interface
                    assert isinstance(handle, RemoteIRHandle)
                    assert handle.handle_import_id == MAIN_HANDLE_ID
                    grant = conn.their_main_lease.grant
                    assert isinstance(grant, RemoteGrant)
                    assert grant.grant_import_id == MAIN_GRANT_ID

            tg.start_soon(side_a)
            tg.start_soon(side_b)

    async def test_our_main_interface_preserved(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        a_handle_holder: dict[str, Any] = {}

        async with anyio.create_task_group() as tg:

            async def side_a() -> None:
                async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                    a_main = _EchoMain(a_lt.token, a_tg)
                    async with LeasedCapRPCConnection.as_server(
                        a_transport, a_main, a_lt.root_lease.clone()
                    ) as conn:
                        a_handle_holder["handle"] = conn.our_main_interface

            async def side_b() -> None:
                async with LeasedCapRPCConnection.as_client(b_transport):
                    pass

            tg.start_soon(side_a)
            tg.start_soon(side_b)

        assert isinstance(a_handle_holder["handle"], _EchoMain)


class TestMainInterfaceRegistration:
    """Main-interface export + import entries are set up at reserved IDs with refcount 1.

    Peeks into the connection's internal tables via name-mangled access. This isn't
    ideal but the alternative is exposing a test hook for every internal structure,
    which clutters the public surface. Consider it a transient seam until Stage D's
    message-handler tests exercise the tables end-to-end.
    """

    async def test_main_ids_reserved_symmetrically(self) -> None:
        a_transport, b_transport = _make_transport_pair()
        async with anyio.create_task_group() as tg:

            async def side_a() -> None:
                async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                    a_main = _EchoMain(a_lt.token, a_tg)
                    async with LeasedCapRPCConnection.as_server(
                        a_transport, a_main, a_lt.root_lease.clone()
                    ) as conn:
                        # Export entries at 0/1/2 are kept alive by the
                        # server's backing lease — they stay in ``_exports``
                        # until the peer discards them.
                        assert MAIN_TOKEN_ID in conn._exports
                        assert MAIN_HANDLE_ID in conn._exports
                        assert MAIN_GRANT_ID in conn._exports
                        # Import entries at 0/1 are pinned by
                        # ``_their_main_token_proxy`` and
                        # ``_their_main_interface_proxy``. The main-grant
                        # import entry (id=2) has already been removed at
                        # this point: ``as_server`` discarded its
                        # ``their_main_lease`` at construction, which drains
                        # ``live_leases`` to empty and triggers the new
                        # remove-on-drain path in ``_clean_up_lease_data``.
                        assert MAIN_TOKEN_ID in conn._imports
                        assert MAIN_HANDLE_ID in conn._imports
                        assert MAIN_GRANT_ID not in conn._imports

            async def side_b() -> None:
                async with LeasedCapRPCConnection.as_client(b_transport):
                    pass

            tg.start_soon(side_a)
            tg.start_soon(side_b)


class TestAsClientNullInterface:
    """`as_client` synthesizes a null main interface whose lifetime is kept alive
    implicitly by the connection's backing lease."""

    async def test_client_side_our_main_interface_is_null(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        null_handle_holder: dict[str, Any] = {}

        async with anyio.create_task_group() as tg:

            async def side_a() -> None:
                async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                    a_main = _EchoMain(a_lt.token, a_tg)
                    async with LeasedCapRPCConnection.as_server(
                        a_transport, a_main, a_lt.root_lease.clone()
                    ):
                        pass

            async def side_b() -> None:
                async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                    null_handle_holder["handle"] = conn.our_main_interface

            tg.start_soon(side_a)
            tg.start_soon(side_b)

        assert isinstance(null_handle_holder["handle"], NullMainInterface)


class TestSniffedMessageCount:
    """Clean bootstrap + shutdown puts exactly a Hello and a main-grant
    :class:`DiscardMessage` on each wire direction, in that order.

    Each side emits ``DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1)``
    for the peer's main grant when it drops its ``their_main_lease``
    (``as_server`` at construction time, ``as_client`` on normal-exit
    shutdown). The main-token's accumulated ``introduction_refcount`` is
    retired only when the cached :class:`RemoteLifetimeToken` proxy GCs —
    and the connection's abort path short-circuits that finalizer on clean
    shutdown, so no token discard ever hits the wire for an otherwise-idle
    session. No push/pull/break traffic should appear either.
    """

    async def test_only_handshake_and_main_grant_discard_sent(self) -> None:
        a_to_b: list[RPCMessage] = []
        b_to_a: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(a_to_b, b_to_a)
        async with anyio.create_task_group() as tg:

            async def side_a() -> None:
                async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                    a_main = _EchoMain(a_lt.token, a_tg)
                    async with LeasedCapRPCConnection.as_server(
                        a_transport, a_main, a_lt.root_lease.clone()
                    ):
                        pass

            async def side_b() -> None:
                async with LeasedCapRPCConnection.as_client(b_transport):
                    pass

            tg.start_soon(side_a)
            tg.start_soon(side_b)

        from fxtr2.rpc.lowering_types import DiscardMessage

        for direction, messages in [("A→B", a_to_b), ("B→A", b_to_a)]:
            assert len(messages) == 2, f"{direction} sent {len(messages)} messages: {messages!r}"
            assert isinstance(messages[0], HelloMessage), f"{direction}[0] is {messages[0]!r}"
            assert isinstance(messages[1], DiscardMessage), f"{direction}[1] is {messages[1]!r}"
            assert messages[1].import_id == MAIN_GRANT_ID
            assert messages[1].refcount == 1
            assert not any(
                isinstance(m, DiscardMessage) and m.import_id == MAIN_TOKEN_ID for m in messages
            ), (
                f"{direction} should not have emitted a main-token DiscardMessage"
                f" on a clean idle shutdown (it's deferred to token-proxy GC):"
                f" {messages!r}"
            )


# ---------------------------------------------------------------------------
# Sniffer transport helper
# ---------------------------------------------------------------------------


class _SniffingTransport(anyio.abc.ObjectStream[RPCMessage]):
    """ObjectStream wrapper that records every send to a supplied list.

    Thin wrapper: delegates `send` / `receive` / `aclose` / `send_eof` to an inner
    stapled stream and appends sent messages to ``record``. Used by
    :class:`TestSniffedMessageCount`.
    """

    def __init__(
        self,
        inner: StapledObjectStream[RPCMessage],
        record: list[RPCMessage],
    ) -> None:
        self._inner = inner
        self._record = record

    async def send(self, item: RPCMessage) -> None:
        self._record.append(item)
        await self._inner.send(item)

    async def receive(self) -> RPCMessage:
        return await self._inner.receive()

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def send_eof(self) -> None:
        await self._inner.send_eof()

    @property
    def extra_attributes(self) -> dict[Any, Any]:
        return {}


def _make_sniffing_transport_pair(
    a_to_b: list[RPCMessage],
    b_to_a: list[RPCMessage],
) -> tuple[anyio.abc.ObjectStream[RPCMessage], anyio.abc.ObjectStream[RPCMessage]]:
    """Like `_make_transport_pair` but each send is also appended to the given list."""
    a_inner, b_inner = _make_transport_pair()
    return _SniffingTransport(a_inner, a_to_b), _SniffingTransport(b_inner, b_to_a)


# ---------------------------------------------------------------------------
# §2b tests — Hello / Abort handling + basic abort propagation
# ---------------------------------------------------------------------------


def _collapse_exc_group(exc: BaseException) -> BaseException:
    """If ``exc`` is a BaseExceptionGroup with exactly one non-cancel leaf, return that
    leaf; otherwise return ``exc`` unchanged. Helper for assertions on exceptions
    raised out of anyio task groups, which tend to wrap single-cause failures in an
    exception group alongside cancel exceptions from sibling tasks."""
    if isinstance(exc, BaseExceptionGroup):
        non_cancel = [
            e for e in exc.exceptions if not isinstance(e, anyio.get_cancelled_exc_class())
        ]
        if len(non_cancel) == 1:
            return _collapse_exc_group(non_cancel[0])
    return exc


class TestWrongFirstMessageAborts:
    """If the peer's very first inbound message isn't a HelloMessage, we abort with
    :class:`HandshakeError` and tear down the connection.

    Test shape: each side catches its own exception locally so the outer task group
    exits normally, letting both sides run to completion before assertions — otherwise
    the outer cancellation race prevents the raw peer from draining the messages we
    want to assert on.
    """

    async def test_non_hello_first_message_aborts(self) -> None:
        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            a_transport, b_transport = _make_transport_pair()

            received_on_b: list[RPCMessage] = []
            a_exc: list[BaseException] = []

            async def raw_peer_b() -> None:
                await b_transport.send(PullMessage(future_import_id=999, grant_import_id=1000))
                try:
                    while True:
                        received_on_b.append(await b_transport.receive())
                except anyio.EndOfStream:
                    pass

            async def side_a() -> None:
                try:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _EchoMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()
                except BaseException as exc:
                    a_exc.append(exc)

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_a)
                tg.start_soon(raw_peer_b)

        assert len(a_exc) == 1
        assert isinstance(_collapse_exc_group(a_exc[0]), HandshakeError)
        # Peer saw our outbound Hello (sent on entry) and the AbortMessage (from _abort).
        assert any(isinstance(m, HelloMessage) for m in received_on_b), received_on_b
        assert any(isinstance(m, AbortMessage) for m in received_on_b), received_on_b


class TestDoubleHelloAborts:
    """Receiving a second HelloMessage aborts with :class:`HandshakeError`."""

    async def test_double_hello_aborts(self) -> None:
        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            a_transport, b_transport = _make_transport_pair()
            received_on_b: list[RPCMessage] = []
            a_exc: list[BaseException] = []

            async def raw_peer_b() -> None:
                await b_transport.send(HelloMessage())
                await b_transport.send(HelloMessage())
                try:
                    while True:
                        received_on_b.append(await b_transport.receive())
                except anyio.EndOfStream:
                    pass

            async def side_a() -> None:
                try:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _EchoMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()
                except BaseException as exc:
                    a_exc.append(exc)

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_a)
                tg.start_soon(raw_peer_b)

        assert len(a_exc) == 1
        assert isinstance(_collapse_exc_group(a_exc[0]), HandshakeError)
        assert any(isinstance(m, AbortMessage) for m in received_on_b)


class TestExplicitAbortTearsDownPeer:
    """If one side raises inside the ``async with`` block, the abort propagates to the
    other side as an :class:`RPCConnectionAborted` (peer receives the AbortMessage)."""

    async def test_user_exception_in_a_aborts_b(self) -> None:
        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            a_transport, b_transport = _make_transport_pair()

            class LocalTrigger(Exception):
                pass

            a_exc: list[BaseException] = []
            b_exc: list[BaseException] = []

            async def side_a() -> None:
                try:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _EchoMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            raise LocalTrigger("kaboom")
                except BaseException as exc:
                    a_exc.append(exc)

            async def side_b() -> None:
                try:
                    async with LeasedCapRPCConnection.as_client(b_transport):
                        await anyio.sleep_forever()
                except BaseException as exc:
                    b_exc.append(exc)

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_a)
                tg.start_soon(side_b)

        # Side A: the trigger exception bubbled out (possibly wrapped in an exception
        # group alongside cancel exceptions from the connection's task group).
        assert len(a_exc) == 1
        collapsed_a = _collapse_exc_group(a_exc[0])
        if isinstance(collapsed_a, BaseExceptionGroup):
            assert any(isinstance(e, LocalTrigger) for e in collapsed_a.exceptions)
        else:
            assert isinstance(collapsed_a, LocalTrigger), collapsed_a

        # Side B: received the AbortMessage and surfaced RPCConnectionAborted.
        assert len(b_exc) == 1
        collapsed_b = _collapse_exc_group(b_exc[0])
        assert isinstance(collapsed_b, RPCConnectionAborted), collapsed_b


class TestTransportBreakAborts:
    """Closing the transport abruptly causes the connection to surface an error from
    its ``async with`` block."""

    async def test_transport_close_aborts(self) -> None:
        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            a_transport, b_transport = _make_transport_pair()
            a_exc: list[BaseException] = []

            async def raw_peer_b() -> None:
                await b_transport.send(HelloMessage())
                await anyio.sleep(0)
                await b_transport.aclose()

            async def side_a() -> None:
                try:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _EchoMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()
                except BaseException as exc:
                    a_exc.append(exc)

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_a)
                tg.start_soon(raw_peer_b)

        # The exact surfaced exception depends on whether the reader or writer notices
        # the broken transport first. Both paths surface via the task group; the
        # shutdown-path ``_abort`` we installed catches them and wraps them as
        # RPCConnectionAborted.
        assert len(a_exc) == 1
        collapsed = _collapse_exc_group(a_exc[0])
        # Accept either a plain RPCConnectionAborted or a group containing one (tasks
        # racing to raise can produce either shape).
        if isinstance(collapsed, BaseExceptionGroup):
            assert any(isinstance(e, RPCConnectionAborted) for e in collapsed.exceptions)
        else:
            assert isinstance(collapsed, RPCConnectionAborted), collapsed


# ---------------------------------------------------------------------------
# §2e tests — push / pull / resolve round-trip
#
# Forward-path tests for the basic RPC call cycle. Lease-wire traffic
# (DiscardMessage / BreakMessage / WatchMessage / ClosedMessage) is §2f; these
# tests rely on clean lease discipline (no leftover clones at ``async with``
# exit) so the current scaffold's unilateral main-grant discard + EOF is
# sufficient cleanup.
# ---------------------------------------------------------------------------


async def _receive_until[T](transport: anyio.abc.ObjectStream[RPCMessage], expected: type[T]) -> T:
    """Read from ``transport`` until we get a message of type ``expected``.

    Silently drops any other message types in between. Useful in raw-peer tests where
    we care about one specific message kind (e.g. ``PullMessage``) and want to tolerate
    protocol-level chatter like :class:`WatchMessage` that the real connection sends
    automatically (e.g. when wiring a remote token's end callback).
    """
    while True:
        msg = await transport.receive()
        if isinstance(msg, expected):
            return msg


class _CountingMain(IRHandleLocalImplBase[Any]):
    """Server-side main interface that records received requests.

    Usable variants:

    - ``Call(method="echo", arg=X)`` → returns ``X`` verbatim.
    - ``Call(method="raise", arg={"message": ..., "category": ...})`` → raises
      :class:`IRException` with the given message/category. Used by the
      exception-propagation test.
    - ``GetItem(attr=...)`` → returns ``{"attr": attr}`` mirror.
    """

    def __init__(self, token: Any, task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group
        self.received_calls: list[tuple[str | int | tuple[str | int, ...], Any]] = []

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call):
            method = request.method
            key: str | int | tuple[str | int, ...]
            if isinstance(method, (str, int)):
                key = method
            else:
                key = tuple(method)
            self.received_calls.append((key, request.arg))
            if key == "echo":
                return request.arg
            if key == "raise":
                assert isinstance(request.arg, dict)
                raise IRException(
                    message=str(request.arg["message"]),
                    category=str(request.arg.get("category", "TestError")),
                )
            return {"unknown_method": method}
        return {"attr": request.attr}


class TestMainInterfaceRoundTrip:
    """Primitive args + primitive result over the main interface."""

    async def test_echo_string(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _CountingMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        result = await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg="hello"),
                        )
                        assert result == "hello"
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

    async def test_echo_list_and_dict(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _CountingMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        payload = {"numbers": [1, 2, 3], "flag": True}
                        result = await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg=payload),
                        )
                        assert result == payload
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

    async def test_getitem_round_trip(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _CountingMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        result = await conn.their_main_interface.request(
                            conn.their_main_lease.clone(), GetItem(attr="hello")
                        )
                        assert result == {"attr": "hello"}
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestDispatchThenWait:
    """Explicit dispatch → wait: PullMessage is sent only when wait fires, not at
    dispatch time."""

    async def test_pull_deferred_until_wait(self) -> None:
        # Sniff B's outbound traffic to count PullMessages.
        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _CountingMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        future, result_lease = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg=42),
                        )
                        # No pull yet — just a push.
                        # Wait briefly so the writer has a chance to drain pending
                        # messages (the Hello + the Push), then check.
                        await anyio.sleep(0.05)
                        push_count_pre_wait = sum(
                            1 for m in sniffed_b_outbound if isinstance(m, PushMessage)
                        )
                        pull_count_pre_wait = sum(
                            1 for m in sniffed_b_outbound if isinstance(m, PullMessage)
                        )
                        assert push_count_pre_wait == 1
                        assert pull_count_pre_wait == 0
                        # Now wait → pull should fire.
                        value = await future.wait(result_lease.clone())
                        assert value == 42
                        pull_count_post_wait = sum(
                            1 for m in sniffed_b_outbound if isinstance(m, PullMessage)
                        )
                        assert pull_count_post_wait == 1
                        result_lease.discard()
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)
        # Reference the A-side sniffer too so it's not unused.
        del sniffed_a_outbound


class TestMultipleWaitsDeduplicatePull:
    """Multiple waits on the same future send only one PullMessage; each wait sees a
    fresh deep copy of the result."""

    async def test_two_waits_one_pull(self) -> None:
        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _CountingMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        future, result_lease = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg=[1, 2, 3]),
                        )
                        v1 = await future.wait(result_lease.clone())
                        v2 = await future.wait(result_lease.clone())
                        assert v1 == [1, 2, 3]
                        assert v2 == [1, 2, 3]
                        # Mutating one shouldn't affect the other (deep copy).
                        assert isinstance(v1, list) and isinstance(v2, list)
                        v1.append(999)
                        assert v2 == [1, 2, 3]
                        result_lease.discard()
                        # Only one pull sent.
                        pull_count = sum(
                            1 for m in sniffed_b_outbound if isinstance(m, PullMessage)
                        )
                        assert pull_count == 1
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)
        del sniffed_a_outbound


class TestExceptionPropagation:
    """Server raises an IRException; the caller's wait surfaces a fresh copy of it."""

    async def test_irexception_propagates(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _CountingMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        with pytest.raises(IRException) as exc_info:
                            await conn.their_main_interface.request(
                                conn.their_main_lease.clone(),
                                Call(
                                    method="raise",
                                    arg={"message": "boom", "category": "BoomCat"},
                                ),
                            )
                        assert exc_info.value.message == "boom"
                        assert exc_info.value.category == "BoomCat"
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestDoubleResolveAborts:
    """A second ResolveMessage for the same future aborts the connection.

    The peer sends the two resolves in sequence, gated on a local event so the first
    resolve has fully committed to the box before the second is put on the wire —
    that way ``_on_resolve``'s synchronous ``box.state != "pending"`` check deterministically
    fires the abort path on the second one. Without the gate there's a benign race
    between two spawned deliver tasks; the end behaviour is still a connection abort,
    just via a different internal exception shape.
    """

    async def test_peer_double_resolves_aborts_us(self) -> None:
        a_transport, b_transport = _make_transport_pair()
        b_exc: list[BaseException] = []
        first_resolve_observed = anyio.Event()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):

            async def raw_peer_a() -> None:
                await a_transport.send(HelloMessage())
                await _receive_until(a_transport, HelloMessage)
                await _receive_until(a_transport, PushMessage)
                pull = await _receive_until(a_transport, PullMessage)
                await a_transport.send(
                    ResolveMessage(
                        future_export_id=pull.future_import_id,
                        expression=StaticExpr("first"),
                    )
                )
                # Wait until side_b confirms the first resolve was delivered to
                # the box before sending the second; otherwise the second
                # resolve can arrive while ``deliver`` for the first is still
                # queued in the task group, and we'd hit a set_result-on-
                # already-resolved race inside the deliver task instead of the
                # intended synchronous abort in ``_on_resolve``.
                await first_resolve_observed.wait()
                await a_transport.send(
                    ResolveMessage(
                        future_export_id=pull.future_import_id,
                        expression=StaticExpr("second"),
                    )
                )
                await a_transport.send_eof()
                try:
                    while True:
                        await a_transport.receive()
                except (anyio.EndOfStream, anyio.ClosedResourceError):
                    return

            async def side_b() -> None:
                try:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        future, result_lease = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg="unused"),
                        )
                        value = await future.wait(result_lease.clone())
                        result_lease.discard()
                        assert value == "first"
                        # Signal the peer to send the illegal second resolve,
                        # then sleep; the connection's reader will abort us.
                        first_resolve_observed.set()
                        await anyio.sleep_forever()
                except BaseException as exc:
                    b_exc.append(exc)

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_b)
                tg.start_soon(raw_peer_a)

        assert len(b_exc) == 1, b_exc
        collapsed = _collapse_exc_group(b_exc[0])
        assert isinstance(collapsed, RPCConnectionAborted), collapsed
        assert "double" in str(collapsed).lower() or "already" in str(collapsed).lower()


class TestRejectMessagePath:
    """Peer sends RejectMessage directly (rather than an IRException via resolve);
    wait surfaces the IRException."""

    async def test_reject_surfaces_irexception(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):

            async def raw_peer_a() -> None:
                await a_transport.send(HelloMessage())
                await _receive_until(a_transport, HelloMessage)
                await _receive_until(a_transport, PushMessage)
                pull = await _receive_until(a_transport, PullMessage)
                await a_transport.send(
                    RejectMessage(
                        future_export_id=pull.future_import_id,
                        expression=StaticExpr(IRException(message="nope", category="Nope")),
                    )
                )
                # Real peers discard our main grant before shutdown — without this,
                # side_b's drain wait on exit would block forever. Send after the
                # Reject so B's request has surfaced the IRException first.
                await a_transport.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
                await a_transport.send_eof()
                try:
                    while True:
                        await a_transport.receive()
                except (anyio.EndOfStream, anyio.ClosedResourceError):
                    return

            async def side_b() -> None:
                async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                    with pytest.raises(IRException) as exc_info:
                        await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg="x"),
                        )
                    assert exc_info.value.message == "nope"
                    assert exc_info.value.category == "Nope"

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_b)
                tg.start_soon(raw_peer_a)


# ---------------------------------------------------------------------------
# §2f tests — leases across the wire (regrant, break, discard, watch/closed)
# ---------------------------------------------------------------------------


class _LeaseCaptureMain(IRHandleLocalImplBase[Any]):
    """Server-side main that captures or discards leases passed in call args.

    Supported ``Call`` methods:

    - ``capture``: appends ``request.arg`` (a :class:`Lease`) to ``captured`` and
      returns ``None``. Caller is responsible for discarding the captured leases
      later.
    - ``capture_and_discard``: appends to ``captured``, then immediately discards.
      Returns ``None``.
    - ``echo``: returns ``request.arg`` unchanged.
    """

    def __init__(self, token: Any, task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group
        self.captured: list[Lease[Any]] = []

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call):
            method = request.method
            if method == "capture":
                assert isinstance(request.arg, Lease)
                self.captured.append(request.arg)
                return None
            if method == "capture_and_discard":
                assert isinstance(request.arg, Lease)
                self.captured.append(request.arg)
                request.arg.discard()
                return None
            if method == "echo":
                return request.arg
            return {"unknown_method": method}
        return {"attr": request.attr}


class TestLeaseAcrossWire:
    """Pass a lease across a connection and observe the bookkeeping at both ends.

    Core §2f round-trip: client creates a local lifetime, hands a lease on it to
    the server, server eventually discards it, client's export entry tears down.
    """

    async def test_server_discard_tears_down_client_export(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _LeaseCaptureMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        with UnconditionalLocalLifetime[Any](
                            "carry"
                        ).revoke_after_block() as carry_lt:
                            result = await conn.their_main_interface.request(
                                conn.their_main_lease.clone(),
                                Call(
                                    method="capture_and_discard",
                                    arg=carry_lt.root_lease.clone(),
                                ),
                            )
                            assert result is None
                            # By the time the wait returns, the server has
                            # processed the call (discarding the arg lease →
                            # emitting DiscardMessage) and sent its Resolve.
                            # Because the reader processes messages in order,
                            # our ``_on_discard`` for that grant has already
                            # run — so the export entry is gone.
                            assert carry_lt._grant not in conn._grant_export_id_by_grant
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

    async def test_introduction_refcount_of_two_for_reexport(self) -> None:
        """Sending the same local grant twice bumps ``outstanding_count`` to 2 on
        the exporter and ``introduction_refcount`` to 2 on the importer; a single
        peer-side ``DiscardMessage(refcount=2)`` tears it down on both ends.
        """
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            release_signal = anyio.Event()
            a_main_holder: dict[str, _LeaseCaptureMain] = {}

            async def side_a() -> None:
                async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                    a_main = _LeaseCaptureMain(a_lt.token, a_tg)
                    a_main_holder["a_main"] = a_main
                    async with LeasedCapRPCConnection.as_server(
                        a_transport, a_main, a_lt.root_lease.clone()
                    ):
                        await release_signal.wait()
                        for lease in a_main.captured:
                            lease.discard()
                        a_main.captured.clear()

            async def side_b() -> None:
                from fxtr2.rpc.lowering_state import GrantExportEntry

                async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                    with UnconditionalLocalLifetime[Any]("carry").revoke_after_block() as carry_lt:
                        # First introduction.
                        await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="capture", arg=carry_lt.root_lease.clone()),
                        )
                        grant_export_id = conn._grant_export_id_by_grant[carry_lt._grant]
                        entry_after_first = conn._exports[grant_export_id]
                        assert isinstance(entry_after_first, GrantExportEntry)
                        assert entry_after_first.outstanding_count == 1
                        # Second introduction — same grant; devaluator bumps
                        # outstanding_count rather than allocating a fresh export.
                        await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="capture", arg=carry_lt.root_lease.clone()),
                        )
                        entry_after_second = conn._exports[grant_export_id]
                        assert isinstance(entry_after_second, GrantExportEntry)
                        assert entry_after_second.outstanding_count == 2
                        # Tell the server to release both captured leases; their
                        # ``_clean_up_lease_data`` emits a single DiscardMessage
                        # with refcount=2 since both share the grant import.
                        release_signal.set()
                        for _ in range(500):
                            if carry_lt._grant not in conn._grant_export_id_by_grant:
                                break
                            await anyio.sleep(0.01)
                        assert carry_lt._grant not in conn._grant_export_id_by_grant

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestBreakPropagates:
    """Revoking a locally-exported lifetime propagates to the peer as a broken
    lease via :class:`BreakMessage`.
    """

    async def test_revoke_on_client_breaks_server_lease(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            a_main_holder: dict[str, _LeaseCaptureMain] = {}
            release_after_broken = anyio.Event()

            async def side_a() -> None:
                async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                    a_main = _LeaseCaptureMain(a_lt.token, a_tg)
                    a_main_holder["a_main"] = a_main
                    async with LeasedCapRPCConnection.as_server(
                        a_transport, a_main, a_lt.root_lease.clone()
                    ):
                        await release_after_broken.wait()
                        for lease in a_main.captured:
                            lease.discard()
                        a_main.captured.clear()

            async def side_b() -> None:
                async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                    carry_lt = UnconditionalLocalLifetime[Any]("carry")
                    await conn.their_main_interface.request(
                        conn.their_main_lease.clone(),
                        Call(method="capture", arg=carry_lt.root_lease.clone()),
                    )
                    # Revoke the client-side lifetime — the backing lease's
                    # broken-callback fires and the connection emits a
                    # :class:`BreakMessage` to the peer.
                    carry_lt.revoke("client revoked for test")
                    a_main = a_main_holder["a_main"]
                    for _ in range(500):
                        if a_main.captured and not a_main.captured[0].is_active():
                            break
                        await anyio.sleep(0.01)
                    assert a_main.captured
                    with pytest.raises(LeaseBrokenError):
                        a_main.captured[0].ensure_active()
                    release_after_broken.set()

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_a)
                tg.start_soon(side_b)

    async def test_revoke_fires_server_side_broken_callback(self) -> None:
        """Server-side imported-lease ``register_broken_callback`` fires with the
        peer-supplied reason when the inbound :class:`BreakMessage` is processed."""
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            a_main_holder: dict[str, _LeaseCaptureMain] = {}
            captured = anyio.Event()
            callback_fired_reason: list[str] = []
            release = anyio.Event()

            async def side_a() -> None:
                async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                    a_main = _LeaseCaptureMain(a_lt.token, a_tg)
                    a_main_holder["a_main"] = a_main
                    async with LeasedCapRPCConnection.as_server(
                        a_transport, a_main, a_lt.root_lease.clone()
                    ):
                        await captured.wait()
                        a_main.captured[0].register_broken_callback(
                            lambda reason: callback_fired_reason.append(reason)
                        )
                        await release.wait()
                        for lease in a_main.captured:
                            lease.discard()
                        a_main.captured.clear()

            async def side_b() -> None:
                async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                    carry_lt = UnconditionalLocalLifetime[Any]("carry")
                    await conn.their_main_interface.request(
                        conn.their_main_lease.clone(),
                        Call(method="capture", arg=carry_lt.root_lease.clone()),
                    )
                    captured.set()
                    # Give the server a moment to register its callback before
                    # we revoke — if we revoked first the callback would fire
                    # synchronously on registration (still correct, but not
                    # what the test is trying to exercise).
                    await anyio.sleep(0.02)
                    carry_lt.revoke("client revoke signal")
                    for _ in range(500):
                        if callback_fired_reason:
                            break
                        await anyio.sleep(0.01)
                    assert callback_fired_reason == ["client revoke signal"]
                    release.set()

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestWatchClosed:
    """Register an end-callback on a remote token → peer receives
    :class:`WatchMessage`; when the local token ends on the peer side,
    :class:`ClosedMessage` fires our callback."""

    async def test_watch_main_token_fires_on_closed(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            callback_fired = anyio.Event()
            b_ready = anyio.Event()

            async def raw_peer_a() -> None:
                await a_transport.send(HelloMessage())
                await _receive_until(a_transport, HelloMessage)
                watch = await _receive_until(a_transport, WatchMessage)
                assert watch.token_import_id == MAIN_TOKEN_ID
                await a_transport.send(ClosedMessage(token_export_id=watch.token_import_id))
                # Wait until B confirms the callback fired before sending the
                # main-grant discard that lets B's drain complete.
                await b_ready.wait()
                await a_transport.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
                await a_transport.send_eof()
                try:
                    while True:
                        await a_transport.receive()
                except (anyio.EndOfStream, anyio.ClosedResourceError):
                    return

            async def side_b() -> None:
                async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                    conn.their_main_interface.token.register_end_callback(callback_fired.set)
                    await callback_fired.wait()
                    b_ready.set()

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_b)
                tg.start_soon(raw_peer_a)


class TestPeerDiscardOfWatchedToken:
    """If the peer installs a watch and then discards the token export before
    the local token ends, ``_on_discard`` must call ``watch_deregister`` so we
    don't leave a stale closure in the local token's end-callback list (and
    later fire a :class:`ClosedMessage` for an entry we've already torn down).

    Using ``as_server`` on side A keeps the local main token alive throughout
    the assertion — an ``as_client`` null-main setup would have the main
    lifetime auto-close as soon as the main-grant backing lease is discarded,
    which independently drains ``_end_callbacks`` and masks the bug.
    """

    async def test_discard_of_watched_token_clears_watch(self) -> None:
        a_transport, b_transport = _make_transport_pair()
        assertions_done = anyio.Event()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):

            async def side_a() -> None:
                async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                    a_main = _LeaseCaptureMain(a_lt.token, a_tg)
                    async with LeasedCapRPCConnection.as_server(
                        a_transport, a_main, a_lt.root_lease.clone()
                    ) as conn:
                        local_main_token = a_lt.token
                        assert isinstance(local_main_token, LocalLifetimeToken)
                        # Wait for peer's WatchMessage + DiscardMessage
                        # round-trip to land in _on_watch / _on_discard.
                        await anyio.sleep(0.1)
                        # Post-discard invariants:
                        # 1. Token export entry is gone.
                        assert MAIN_TOKEN_ID not in conn._exports
                        # 2. Bookkeeping set doesn't claim a watch any more.
                        assert MAIN_TOKEN_ID not in conn._watched_token_exports
                        # 3. The local token's end-callback list no longer
                        #    contains the ``on_end`` closure — that's the
                        #    ``watch_deregister`` having fired during
                        #    ``_on_discard``. The local main token is still
                        #    alive (``a_lt`` is the active open_lifetime), so
                        #    the list hasn't been drained by token-end.
                        #    Without the fix, the closure would still be
                        #    present, holding a strong ref to a torn-down
                        #    export entry.
                        assert local_main_token._end_callbacks == [], (
                            f"expected empty end_callbacks, got"
                            f" {local_main_token._end_callbacks!r}"
                        )
                        assertions_done.set()

            async def raw_peer_b() -> None:
                await b_transport.send(HelloMessage())
                await _receive_until(b_transport, HelloMessage)
                # Install a watch on A's main token, then immediately discard
                # it — the deliberate racy scenario we're exercising. A's
                # ``token_export[MAIN_TOKEN_ID].outstanding_count`` is 3 at
                # setup (1 implicit + 2 pair-bumps for main handle + main
                # grant; see the pair-bump comment in
                # ``LeasedCapRPCConnection.__init__``), so we need
                # ``refcount=3`` on this Discard to drive it to zero and
                # trigger the watch-deregister teardown path we're testing.
                await b_transport.send(WatchMessage(token_import_id=MAIN_TOKEN_ID))
                await anyio.sleep(0.02)
                await b_transport.send(DiscardMessage(import_id=MAIN_TOKEN_ID, refcount=3))
                await assertions_done.wait()
                # Now drop the main grant so A's drain can complete.
                await b_transport.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
                await b_transport.send_eof()
                try:
                    while True:
                        await b_transport.receive()
                except (anyio.EndOfStream, anyio.ClosedResourceError):
                    return

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_a)
                tg.start_soon(raw_peer_b)


class TestAbortRevokesImports:
    """On connection abort, every imported grant's live leases are broken
    synchronously — their broken-callbacks fire with the abort reason."""

    async def test_peer_abort_fires_broken_callback_on_main_lease(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            broken_reasons: list[str] = []
            b_exc: list[BaseException] = []

            async def raw_peer_a() -> None:
                await a_transport.send(HelloMessage())
                await _receive_until(a_transport, HelloMessage)
                await a_transport.send(
                    AbortMessage(
                        expression=StaticExpr(
                            IRException(message="peer abort for test", category="TestAbort")
                        )
                    )
                )
                try:
                    while True:
                        await a_transport.receive()
                except (anyio.EndOfStream, anyio.ClosedResourceError):
                    return

            async def side_b() -> None:
                try:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        main_lease = conn.their_main_lease.clone()
                        main_lease.register_broken_callback(
                            lambda reason: broken_reasons.append(reason)
                        )
                        await anyio.sleep_forever()
                except BaseException as exc:
                    b_exc.append(exc)

            async with anyio.create_task_group() as tg:
                tg.start_soon(side_b)
                tg.start_soon(raw_peer_a)

        assert len(b_exc) == 1
        assert isinstance(_collapse_exc_group(b_exc[0]), RPCConnectionAborted)
        assert broken_reasons
        assert "peer abort for test" in broken_reasons[0]


class TestBreakRacingDiscard:
    """Edge case: peer discards our exported grant *before* we revoke locally.
    The subsequent revoke finds no export entry (we deregistered the
    broken-callback during ``_on_discard``) and no stale
    :class:`BreakMessage` is emitted for the torn-down export.
    """

    async def test_revoke_after_peer_discard_is_noop(self) -> None:
        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _LeaseCaptureMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        carry_lt = UnconditionalLocalLifetime[Any]("carry")
                        await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(
                                method="capture_and_discard",
                                arg=carry_lt.root_lease.clone(),
                            ),
                        )
                        # Peer discarded; our export entry is torn down.
                        assert carry_lt._grant not in conn._grant_export_id_by_grant
                        # Late local revoke must not fire BreakMessage for the
                        # now-gone grant export.
                        carry_lt.revoke("late local revoke")
                        await anyio.sleep(0.02)
                        breaks = [m for m in sniffed_b_outbound if isinstance(m, BreakMessage)]
                        assert breaks == []
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)
        del sniffed_a_outbound


class TestBreakUnknownIdToleratedAsRace:
    """Per :class:`BreakMessage`'s docstring: "The message may be sent even
    if the recipient does not have any remaining references to the grant,
    in which case the recipient should simply ignore it."

    This covers the teardown race where the receiver has fully drained
    a grant import (entry removed from ``_imports`` via
    ``_maybe_remove_grant_import``) before the sender's BreakMessage
    arrives. The current bookkeeping cannot distinguish "we never had
    this id" from "we drained it"; per the protocol spec, both cases
    silently no-op rather than aborting.

    Driven via a raw peer that injects a synthetic BreakMessage for an
    id the receiver has never seen.
    """

    async def test_break_for_unknown_id_does_not_abort(self) -> None:
        a_transport, b_transport = _make_transport_pair()
        a_exc: list[BaseException] = []

        async def raw_peer_b() -> None:
            # Standard handshake.
            await b_transport.send(HelloMessage())
            assert isinstance(await b_transport.receive(), HelloMessage)
            # Inject a BreakMessage for a grant we never exported. Per
            # the protocol spec the receiver should silently ignore.
            await b_transport.send(BreakMessage(grant_export_id=999, reason="race"))
            # Drain A's main grant cleanly so A's connection CM exit
            # isn't waiting on us.
            await b_transport.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
            # Consume A's outbound traffic until its transport closes
            # (its main-grant DiscardMessage and any token-discard finalize
            # will land here). End-of-stream means A has shut down cleanly.
            async for _msg in b_transport:
                pass
            # A closed its send side; close ours so its reader can unwind.
            await b_transport.aclose()

        async def side_a() -> None:
            try:
                async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                    a_main = _EchoMain(a_lt.token, a_tg)
                    async with LeasedCapRPCConnection.as_server(
                        a_transport, a_main, a_lt.root_lease.clone()
                    ):
                        # Brief wait so the BreakMessage definitely lands
                        # before the CM starts draining.
                        await anyio.sleep(0.05)
            except BaseException as exc:  # noqa: BLE001
                a_exc.append(exc)

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:
                tg.start_soon(side_a)
                tg.start_soon(raw_peer_b)

        # Side A may surface a transport-close error (B closed the transport
        # to end the test), but it must NOT have surfaced an
        # RPCConnectionAborted with the unknown-grant_export_id message —
        # that would mean ``_on_break`` aborted on the teardown race.
        for exc in a_exc:
            collapsed = _collapse_exc_group(exc)
            candidates = (
                collapsed.exceptions if isinstance(collapsed, BaseExceptionGroup) else [collapsed]
            )
            for inner in candidates:
                if isinstance(inner, RPCConnectionAborted):
                    assert "999" not in str(inner), (
                        f"_on_break treated unknown grant_export_id 999 as a"
                        f" protocol violation; should be silent no-op per"
                        f" BreakMessage docstring. Got: {inner!r}"
                    )


def _assert_no_abort_for_unknown_id(a_exc: list[BaseException], unknown_id: int) -> None:
    """Shared assertion for unknown-id tolerance tests. The connection's
    CM exit may surface a transport-close error (we close B's transport
    to end the test), but it must not surface an RPCConnectionAborted
    whose message contains the unknown id — that would indicate the
    handler aborted on the teardown race.
    """
    for exc in a_exc:
        collapsed = _collapse_exc_group(exc)
        candidates = (
            collapsed.exceptions if isinstance(collapsed, BaseExceptionGroup) else [collapsed]
        )
        for inner in candidates:
            if isinstance(inner, RPCConnectionAborted):
                assert str(unknown_id) not in str(inner), (
                    f"handler aborted on unknown id {unknown_id}; should be"
                    f" silent no-op per the protocol's teardown-race"
                    f" tolerance policy. Got: {inner!r}"
                )


async def _drive_unknown_id_tolerance_test(
    inject: Callable[[anyio.abc.ObjectStream[RPCMessage]], Any],
    a_exc: list[BaseException],
) -> None:
    """Open A as a real server, B as a raw peer that runs the
    handshake, calls ``inject(b_transport)`` to send the synthetic
    message, then drains A's outbound and shuts down. ``a_exc``
    accumulates any exception A surfaces.
    """
    a_transport, b_transport = _make_transport_pair()

    async def raw_peer_b() -> None:
        await b_transport.send(HelloMessage())
        assert isinstance(await b_transport.receive(), HelloMessage)
        await inject(b_transport)
        await b_transport.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
        async for _msg in b_transport:
            pass
        await b_transport.aclose()

    async def side_a() -> None:
        try:
            async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                a_main = _EchoMain(a_lt.token, a_tg)
                async with LeasedCapRPCConnection.as_server(
                    a_transport, a_main, a_lt.root_lease.clone()
                ):
                    await anyio.sleep(0.05)
        except BaseException as exc:  # noqa: BLE001
            a_exc.append(exc)

    with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
        async with anyio.create_task_group() as tg:
            tg.start_soon(side_a)
            tg.start_soon(raw_peer_b)


class TestPullUnknownIdsToleratedAsRace:
    """``PullMessage`` for ids the receiver doesn't have should silent
    no-op rather than aborting — same teardown-race tolerance policy
    as :class:`_on_break` / :class:`_on_closed` / :class:`_on_discard`
    / :class:`_on_unpull`.

    The grant-import-id half has a real local-revoke race: our backing
    lease can break locally + tear down the export entry before the
    peer's already-in-flight :class:`PullMessage` lands. The
    future-import-id half cannot race in legitimate execution
    (FIFO ordering with peer's :class:`DiscardMessage` is the proof)
    but is treated identically for policy consistency.
    """

    async def test_pull_for_unknown_ids_does_not_abort(self) -> None:
        a_exc: list[BaseException] = []

        async def inject(t: anyio.abc.ObjectStream[RPCMessage]) -> None:
            await t.send(PullMessage(future_import_id=999, grant_import_id=1000))

        await _drive_unknown_id_tolerance_test(inject, a_exc)
        _assert_no_abort_for_unknown_id(a_exc, 999)
        _assert_no_abort_for_unknown_id(a_exc, 1000)


class TestResolveUnknownIdToleratedAsRace:
    """A :class:`ResolveMessage` arriving for a future-import the
    receiver has already drained (proxy GC + outbound
    :class:`DiscardMessage` while the peer's resolver was still running)
    should silent no-op. The :class:`UnpullMessage` docstring describes
    the symmetric case: "the in-flight resolve will reach the importer
    and discharge everything".
    """

    async def test_resolve_for_unknown_id_does_not_abort(self) -> None:
        a_exc: list[BaseException] = []

        async def inject(t: anyio.abc.ObjectStream[RPCMessage]) -> None:
            await t.send(ResolveMessage(future_export_id=999, expression=StaticExpr(None)))

        await _drive_unknown_id_tolerance_test(inject, a_exc)
        _assert_no_abort_for_unknown_id(a_exc, 999)


class TestRejectUnknownIdToleratedAsRace:
    """Symmetric to :class:`TestResolveUnknownIdToleratedAsRace` for
    :class:`RejectMessage`. Same drain race; same policy."""

    async def test_reject_for_unknown_id_does_not_abort(self) -> None:
        a_exc: list[BaseException] = []

        async def inject(t: anyio.abc.ObjectStream[RPCMessage]) -> None:
            await t.send(
                RejectMessage(
                    future_export_id=999,
                    expression=StaticExpr(
                        IRException(message="late reject from peer", category="late")
                    ),
                )
            )

        await _drive_unknown_id_tolerance_test(inject, a_exc)
        _assert_no_abort_for_unknown_id(a_exc, 999)


class TestPushAllocatedBounceBack:
    """Bounce-back identity for push-allocated token / future / grant.

    A pushes a call to B (call 1) — this allocates ``(token_id, future_id,
    grant_id)`` on A's import side with matching push-allocated exports on B.
    A then makes a *second* push carrying the first call's result-lease as an
    arg. B's phase-1 resolves the ``ImportedLeaseExpr(grant_id)`` to a fresh
    local lease on the push-root grant. The handler echoes that lease back,
    so B's devaluator has to re-export it. Per the protocol's bounce-back
    identity rule, that re-export must land at ``grant_id`` / ``token_id`` —
    not a fresh negative export ID for the same underlying object — or A
    receives it as a stranger and ``grant ==`` comparisons silently break.

    Without registering push-allocated grants / tokens / futures in their
    reverse-lookup dicts (``_grant_export_id_by_grant`` /
    ``_token_export_id_by_token`` / ``_future_export_id_by_future_id``) in
    ``_on_push``, B's devaluator takes the fresh-allocation branch and
    identity breaks without any other visible error.
    """

    async def test_push_grant_round_trips_preserving_identity(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _LeaseCaptureMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        # Call 1: plain push; we don't care about the value, we
                        # keep the result-lease ``lease1`` as our round-trip payload.
                        future1, lease1 = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg="hello"),
                        )
                        v1 = await future1.wait(lease1.clone())
                        assert v1 == "hello"
                        original_grant = lease1.grant
                        original_token = future1.token
                        # Call 2: echo lease1. B's handler returns the lease
                        # unchanged; B's devaluator must re-use the push ids
                        # to preserve bounce-back identity.
                        future2, lease2 = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg=lease1.clone()),
                        )
                        returned = await future2.wait(lease2.clone())
                        assert isinstance(
                            returned, Lease
                        ), f"expected a Lease from echo, got {type(returned).__name__}"
                        assert returned.grant == original_grant, (
                            f"bounce-back grant identity broken:"
                            f" got {returned.grant!r}, expected {original_grant!r}"
                        )
                        # The subject token should also round-trip: the returned
                        # lease's grant subject is a RemoteLifetimeToken on the
                        # same import id we got from ``future1.token``.
                        assert returned.grant.subject == original_token, (
                            f"bounce-back token identity broken:"
                            f" got {returned.grant.subject!r}, expected {original_token!r}"
                        )
                        # Cleanup so drain can complete.
                        returned.discard()
                        lease1.discard()
                        lease2.discard()
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)


# ---------------------------------------------------------------------------
# §2g tests — pipelining + forward-delegation
# ---------------------------------------------------------------------------


class _PipelineMain(IRHandleLocalImplBase[Any]):
    """Main interface for §2g tests.

    Supported ``Call`` methods:

    - ``echo`` — returns ``request.arg`` unchanged.
    - ``self_handle`` — returns ``self``. Used for pipeline-dispatch tests: a
      push that resolves to the main handle, whose pipelined child call can
      then be dispatched on ``self``.
    - ``bounce`` — ``arg`` is a 2-list ``[future, lease]``; returns
      :class:`UnwrapForPipelining` wrapping them. Used for forward-delegation
      tests.
    """

    def __init__(self, token: Any, task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call):
            if request.method == "echo":
                return request.arg
            if request.method == "self_handle":
                return self
            if request.method == "self_in_dict":
                # Returns ``{"handle": self}`` so callers can walk into the
                # returned future at key ``"handle"`` and pipeline-dispatch on
                # the walked sub-future. Exercises the
                # ``WalkFutureExpr(ImportedIRFutureExpr, path)`` branch of
                # pipeline_dispatch's devaluator.
                return {"handle": self}
            if request.method == "bounce":
                assert (
                    isinstance(request.arg, list) and len(request.arg) == 2
                ), f"bounce: expected a 2-list, got {request.arg!r}"
                future, inner_lease = request.arg
                assert isinstance(
                    future, IRFuture
                ), f"bounce: arg[0] must be IRFuture, got {type(future).__name__}"
                assert isinstance(
                    inner_lease, Lease
                ), f"bounce: arg[1] must be Lease, got {type(inner_lease).__name__}"
                return UnwrapForPipelining(future=future, lease=inner_lease)
            return {"unknown_method": request.method}
        return {"attr": request.attr}


class TestPipelineDispatchBasic:
    """:meth:`RemoteIRFuture.pipeline_dispatch` — push a call on the result of
    an earlier push without awaiting the first.

    Doesn't exercise the §2g forward-delegation path (that's covered by
    :class:`TestBounceBackForwarding`). Just confirms the outbound shape of a
    pipelined :class:`PushMessage` and that the result round-trips correctly.
    """

    async def test_pipeline_dispatch_echoes_through_pipelined_call(self) -> None:
        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _PipelineMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        # Push #1: ask A for its main handle. future_handle
                        # resolves to an IRHandle we can pipeline against.
                        future_handle, lease_handle = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="self_handle", arg=None),
                        )
                        assert isinstance(future_handle, RemoteIRFuture)
                        # Push #2: pipeline_dispatch on future_handle to call
                        # ``echo`` on A's main without waiting for push #1 to
                        # resolve. ``inner_handle_lease`` is a fresh clone of
                        # our existing main lease (same authority, different
                        # physical lease instance).
                        future_echo, lease_echo = future_handle.pipeline_dispatch(
                            lease_handle.clone(),
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg="pipelined"),
                        )
                        result = await future_echo.wait(lease_echo.clone())
                        assert result == "pipelined"
                        lease_echo.discard()
                        lease_handle.discard()
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        # B's outbound: exactly two PushMessages (one for each dispatch). The
        # second's expression has an AwaitFutureExpr in the ``handle`` slot —
        # that's the shape pipeline_dispatch builds.
        pushes = [m for m in sniffed_b_outbound if isinstance(m, PushMessage)]
        assert len(pushes) == 2, f"expected 2 pushes from B, got {len(pushes)}"
        second = pushes[1].expression
        assert isinstance(second, HandleRequestExpr), (
            f"pipelined push's top-level expression should be HandleRequestExpr,"
            f" got {type(second).__name__}"
        )
        assert isinstance(second.handle, AwaitFutureExpr), (
            f"pipelined push's handle slot should be AwaitFutureExpr (handle-from-future),"
            f" got {type(second.handle).__name__}"
        )
        assert isinstance(second.handle.future, ImportedIRFutureExpr), (
            f"pipelined push's handle future should be ImportedIRFutureExpr,"
            f" got {type(second.handle.future).__name__}"
        )

    async def test_pipeline_dispatch_on_walked_future(self) -> None:
        """pipeline_dispatch on ``future.walk("key")`` — the non-empty attr_path
        path through the devaluator. Expected wire shape: the pipelined push's
        ``handle`` slot is ``AwaitFutureExpr(WalkFutureExpr(ImportedIRFutureExpr,
        ("handle",)), <lease>)``.
        """
        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _PipelineMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        # Push #1: ``self_in_dict`` returns ``{"handle": self}``.
                        # future_dict resolves to a dict whose ``"handle"`` key
                        # is the main handle.
                        future_dict, lease_dict = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="self_in_dict", arg=None),
                        )
                        walked = future_dict.walk("handle")
                        # Push #2: pipeline on the walked sub-future. Forces
                        # the devaluator down the WalkFutureExpr branch.
                        future_echo, lease_echo = walked.pipeline_dispatch(
                            lease_dict.clone(),
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg="walked-pipelined"),
                        )
                        result = await future_echo.wait(lease_echo.clone())
                        assert result == "walked-pipelined"
                        lease_echo.discard()
                        lease_dict.discard()
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        pushes = [m for m in sniffed_b_outbound if isinstance(m, PushMessage)]
        assert len(pushes) == 2, f"expected 2 pushes from B, got {len(pushes)}"
        second = pushes[1].expression
        assert isinstance(second, HandleRequestExpr)
        assert isinstance(second.handle, AwaitFutureExpr)
        # The WalkFutureExpr wrap is the new bit vs the basic pipeline test.
        walk_expr = second.handle.future
        assert isinstance(walk_expr, WalkFutureExpr), (
            f"expected WalkFutureExpr wrapping the pipelined future,"
            f" got {type(walk_expr).__name__}"
        )
        assert tuple(walk_expr.attr_path) == (
            "handle",
        ), f"expected attr_path == ('handle',), got {walk_expr.attr_path!r}"
        assert isinstance(walk_expr.future, ImportedIRFutureExpr)


class TestBounceBackForwarding:
    """§2g forward-delegation via :meth:`IRFutureLocalImpl.wait_or_forward`.

    Scenario: A creates ``future_a`` locally and calls ``b_main.bounce(future_a,
    lease_a)``. B's handler returns ``UnwrapForPipelining(future_a_as_B,
    lease_a_as_B)`` — so B's push box resolves-from a :class:`RemoteIRFuture`
    on the same connection that pulled it. B's ``_on_pull`` detects this and
    emits ``ResolveMessage(AwaitFutureExpr(ImportedIRFutureExpr,
    ImportedLeaseExpr))`` instead of eagerly pulling the value back to B.

    Asserts (both variants):

    - B's outbound :class:`ResolveMessage`'s expression is an
      :class:`AwaitFutureExpr` — proves forward-delegation happened rather
      than the value being serialized into the resolve.
    - A sends exactly one :class:`PullMessage` — the one for the push result.
      None for the forward target (``future_a`` is A's own local future).
    """

    async def _run_scenario(self, pre_resolve: bool) -> None:
        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )
        resolve_event = anyio.Event()

        async def work_future_a() -> IRValue:
            await resolve_event.wait()
            return "bounced-value"

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-futures") as (_a_lt, a_tg):
                        future_a, lease_a = spawn_ir_future_in_task_group(
                            name="future_a", work=work_future_a, task_group=a_tg
                        )
                        async with LeasedCapRPCConnection.as_client(a_transport) as conn:
                            if pre_resolve:
                                resolve_event.set()
                            # Dispatch + wait explicitly (rather than `request`)
                            # so we can delay the resolve in the post-resolved
                            # variant between send and await.
                            fut, res_lease = conn.their_main_interface.dispatch(
                                conn.their_main_lease.clone(),
                                Call(method="bounce", arg=[future_a, lease_a]),
                            )
                            if not pre_resolve:
                                # Give B's forwarding resolve time to land on
                                # A before we unblock the local future; then
                                # A's ``_await_forwarded`` has to wait on the
                                # still-pending local box.
                                await anyio.sleep(0)
                                resolve_event.set()
                            result = await fut.wait(res_lease.clone())
                            assert (
                                result == "bounced-value"
                            ), f"bounced-value expected, got {result!r}"
                            res_lease.discard()
                            tg.cancel_scope.cancel()

                async def side_b() -> None:
                    async with open_lifetime_task_group("B-main") as (b_lt, b_tg):
                        b_main = _PipelineMain(b_lt.token, b_tg)
                        async with LeasedCapRPCConnection.as_server(
                            b_transport, b_main, b_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        resolves = [m for m in sniffed_b_outbound if isinstance(m, ResolveMessage)]
        assert (
            len(resolves) == 1
        ), f"expected 1 ResolveMessage from B, got {len(resolves)}: {resolves}"
        expr = resolves[0].expression
        assert isinstance(expr, AwaitFutureExpr), (
            f"forwarding resolve's expression should be AwaitFutureExpr,"
            f" got {type(expr).__name__}"
        )
        assert isinstance(expr.future, ImportedIRFutureExpr), (
            f"forwarding resolve's future slot should be ImportedIRFutureExpr,"
            f" got {type(expr.future).__name__}"
        )
        assert isinstance(expr.lease, ImportedLeaseExpr), (
            f"forwarding resolve's lease slot should be ImportedLeaseExpr,"
            f" got {type(expr.lease).__name__}"
        )
        pulls = [m for m in sniffed_a_outbound if isinstance(m, PullMessage)]
        assert len(pulls) == 1, f"expected exactly 1 PullMessage from A, got {len(pulls)}: {pulls}"

    async def test_future_a_pre_resolved(self) -> None:
        await self._run_scenario(pre_resolve=True)

    async def test_future_a_post_resolved(self) -> None:
        await self._run_scenario(pre_resolve=False)


# ---------------------------------------------------------------------------
# §2h tests — drain semantics + Unpull + no-lease waits
# ---------------------------------------------------------------------------


class _GatedMain(IRHandleLocalImplBase[Any]):
    """Main interface whose ``echo`` blocks on a caller-supplied event before
    returning. Used by the §2h tests to freeze A mid-resolver so we can observe
    the wire-level ordering between B's outbound messages and A's response.
    """

    def __init__(
        self,
        token: Any,
        task_group: anyio.abc.TaskGroup,
        release: anyio.Event,
        *,
        run_count_holder: list[int] | None = None,
    ) -> None:
        self._token = token
        self._task_group = task_group
        self._release = release
        self._run_count_holder = run_count_holder

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call) and request.method == "echo":
            await self._release.wait()
            if self._run_count_holder is not None:
                self._run_count_holder.append(1)
            return request.arg
        raise IRException(message=f"unhandled: {request!r}", category="TestError")


async def _poll_until[T](predicate: Callable[[], T | None]) -> T:
    """Yield to the event loop until ``predicate`` returns a non-``None`` value."""
    while True:
        result = predicate()
        if result is not None:
            return result
        await anyio.sleep(0)


class TestWaitDiscardsLeasePromptly:
    """§2h: B starts a wait holding only one lease copy. The grant's single live
    lease is discarded immediately after the :class:`PullMessage` is sent, so the
    :class:`DiscardMessage` for the push-allocated grant precedes A's eventual
    :class:`ResolveMessage` (rather than being delayed until after the wait
    returns).
    """

    async def test_discard_before_resolve(self) -> None:
        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )

        release = anyio.Event()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _GatedMain(a_lt.token, a_tg, release)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        future, result_lease = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg="hi"),
                        )

                        wait_done = anyio.Event()

                        async def waiter() -> None:
                            value = await future.wait(result_lease)
                            assert value == "hi"
                            wait_done.set()

                        async with anyio.create_task_group() as waiter_tg:
                            waiter_tg.start_soon(waiter)

                            # Deterministic sync: poll the outbound sniffer until
                            # B's DiscardMessage for the push-allocated grant has
                            # been sent — that's the point the wait has yielded.
                            # The main-grant DiscardMessage (import_id=MAIN_GRANT_ID)
                            # emitted by ``as_client`` shutdown would be a different
                            # entry, but we haven't exited yet so the only
                            # non-main DiscardMessage on the wire is this one.
                            def pushed_discard_present() -> bool | None:
                                for m in sniffed_b_outbound:
                                    if (
                                        isinstance(m, DiscardMessage)
                                        and m.import_id != MAIN_GRANT_ID
                                    ):
                                        return True
                                return None

                            await _poll_until(pushed_discard_present)

                            # By here, [Push, Pull, Discard] have been emitted
                            # while A's resolver is still blocked on ``release``.
                            # No ResolveMessage has landed yet on B (A can't send
                            # one until we release).
                            resolves_before = [
                                m for m in sniffed_a_outbound if isinstance(m, ResolveMessage)
                            ]
                            assert resolves_before == [], (
                                "A must not have sent a ResolveMessage yet —"
                                f" got {resolves_before}"
                            )
                            # Assert ordering of the specific traffic we care
                            # about (ignoring :class:`WatchMessage`, which the
                            # box materializes along the way). Push must come
                            # first, then Pull, then Discard — the Discard
                            # preceding A's eventual Resolve is the point of the
                            # whole drain-semantics design.
                            interesting = {
                                "PushMessage",
                                "PullMessage",
                                "DiscardMessage",
                            }
                            seq = [
                                type(m).__name__
                                for m in sniffed_b_outbound
                                if type(m).__name__ in interesting
                            ]
                            assert seq == [
                                "PushMessage",
                                "PullMessage",
                                "DiscardMessage",
                            ], f"expected [Push, Pull, Discard], got {seq}"
                            assert not any(
                                isinstance(m, ResolveMessage) for m in sniffed_a_outbound
                            )

                            # Release A and let the wait complete.
                            release.set()
                            await wait_done.wait()
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)
        del sniffed_a_outbound


class TestResolveSurvivesPeerCloseRace:
    """Regression: a ``wait`` parked on a remote future must succeed even if the
    peer's :class:`ClosedMessage` for the result token arrives before the local
    deliver task's ``set_result`` lands.

    Race scenario:

    - B dispatches on A and parks in ``wait``. ``wait`` sends Pull and immediately
      discards its lease (per the drain-semantics design), so B's
      :class:`DiscardMessage` for the push-allocated grant precedes A's eventual
      :class:`ResolveMessage`.
    - A's handler completes; A sends Resolve. A's call_scope drains; A sends
      Close.
    - B's reader processes Resolve → spawns a ``deliver`` task that runs
      :func:`eval_forwardable`. Async.
    - B's reader continues and processes Close before ``deliver`` finishes
      evaluating.

    Under the historical box-end-on-token-end coupling, Close would discard the
    box mid-delivery and surface :class:`LeaseBrokenError` to the parked wait.
    The fix decouples the box discard from the remote token's end-callbacks and
    moves the policy onto :class:`FutureImportEntry`, deferring the discard
    while a delivery is in flight.

    Determinism comes from a test-only monkey-patch of ``eval_forwardable`` on
    B's side that gates on a token-end-arrived event. The wrapper holds the
    deliver task at its first ``await`` until B has already processed the
    Close, then lets evaluation proceed; this is exactly the wire ordering that
    triggered the original bug.
    """

    async def test_close_during_deliver_does_not_break_wait(self) -> None:
        from fxtr2.rpc import lowering_message_handlers as _lmh

        b_conn_holder: dict[str, LeasedCapRPCConnection[Any, Any]] = {}
        close_arrived = anyio.Event()
        original_eval_forwardable = _lmh.eval_forwardable

        async def gated_eval_forwardable(conn: LeasedCapRPCConnection[Any, Any], expr: Any) -> Any:
            # Only gate on B's connection — A also calls eval_forwardable
            # (for B's incoming Push); we don't want to block that path.
            if conn is b_conn_holder.get("conn"):
                await close_arrived.wait()
                # Yield once so the close-handling reader's synchronous work
                # (in particular the buggy box-discard transition) has a
                # chance to run before we proceed with the original eval.
                await anyio.sleep(0)
            return await original_eval_forwardable(conn, expr)

        _lmh.eval_forwardable = gated_eval_forwardable
        try:
            a_transport, b_transport = _make_transport_pair()

            with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
                async with anyio.create_task_group() as tg:

                    async def side_a() -> None:
                        async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                            a_main = _EchoMain(a_lt.token, a_tg)
                            async with LeasedCapRPCConnection.as_server(
                                a_transport, a_main, a_lt.root_lease.clone()
                            ):
                                await anyio.sleep_forever()

                    async def side_b() -> None:
                        async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                            b_conn_holder["conn"] = conn
                            future, result_lease = conn.their_main_interface.dispatch(
                                conn.their_main_lease.clone(),
                                Call(method="echo", arg="hi"),
                            )
                            # Subscribe to the result token's end *before* we
                            # park in ``wait``. This is what unblocks the
                            # gated ``eval_forwardable`` once Close has been
                            # processed by the reader. The deregister handle
                            # is intentionally dropped — fires once and
                            # ``anyio.Event.set`` is idempotent.
                            future.token.register_end_callback(close_arrived.set)
                            value = await future.wait(result_lease)
                            assert value == {"echoed": "hi", "method": "echo"}
                            tg.cancel_scope.cancel()

                    tg.start_soon(side_a)
                    tg.start_soon(side_b)
        finally:
            _lmh.eval_forwardable = original_eval_forwardable


class TestUnpullOnCancelledWait:
    """§2h: cancelling ``wait`` while the result is still pending emits a
    :class:`UnpullMessage` and prevents A from sending a spurious
    :class:`ResolveMessage`.

    The A-side handler's dispatched work continues to run (per the "wait is a
    read, not a control op" contract), but A's resolver task is cancelled by
    ``_on_unpull`` so no :class:`ResolveMessage` / :class:`RejectMessage` ever
    reaches B. Both sides drain cleanly.
    """

    async def test_unpull_emitted_and_nothing_comes_back(self) -> None:
        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )

        release = anyio.Event()
        run_count: list[int] = []
        a_conn_holder: dict[str, LeasedCapRPCConnection[Any, Any]] = {}

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _GatedMain(a_lt.token, a_tg, release, run_count_holder=run_count)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ) as a_conn:
                            a_conn_holder["conn"] = a_conn
                            # Hold until b finishes its assertions and signals done.
                            await _b_done.wait()

                _b_done = anyio.Event()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        wait_scope = anyio.CancelScope()

                        async def waiter(
                            *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED
                        ) -> None:
                            future, result_lease = conn.their_main_interface.dispatch(
                                conn.their_main_lease.clone(),
                                Call(method="echo", arg="hi"),
                            )
                            with wait_scope:
                                task_status.started()
                                # ``wait`` runs its setup (enqueue Pull, discard
                                # user lease) synchronously, then yields on the
                                # box's await_resolution. Cancellation lands on
                                # the yield and the ``finally`` path emits the
                                # :class:`UnpullMessage`.
                                try:
                                    await future.wait(result_lease)
                                except anyio.get_cancelled_exc_class():
                                    raise

                        async with anyio.create_task_group() as waiter_tg:
                            await waiter_tg.start(waiter)

                            # Deterministic sync: poll until B's Pull is on the
                            # wire. ``wait`` enqueues the Pull synchronously
                            # before its first await, so the first event-loop
                            # iteration after ``start`` returns gets us there.
                            def pull_present() -> bool | None:
                                for m in sniffed_b_outbound:
                                    if isinstance(m, PullMessage):
                                        return True
                                return None

                            await _poll_until(pull_present)

                            # Cancel the wait. The cancel propagates through
                            # ``await future.wait(result_lease)`` → the wait's
                            # ``finally`` block decrements local_consumer_count,
                            # which emits :class:`UnpullMessage` because the
                            # obligation refcount is still non-zero and no
                            # :class:`ResolveMessage` has arrived.
                            wait_scope.cancel()
                        # waiter_tg exited.

                        # Wait for A to actually process the Unpull. We observe
                        # this via ``_on_unpull``'s side effect on A's future
                        # export entry: ``resolve_task_cancel_scope`` is nulled
                        # when the count drops to zero. Polling the sniffer
                        # alone only tells us the Unpull is on the wire, not
                        # that A's reader has picked it up.
                        from fxtr2.rpc.lowering_state import FutureExportEntry

                        def a_cancelled_resolver() -> bool | None:
                            a_conn = a_conn_holder.get("conn")
                            if a_conn is None:
                                return None
                            for entry in a_conn._exports.values():
                                if (
                                    isinstance(entry, FutureExportEntry)
                                    and entry.export_id >= 3  # skip main reserves
                                    and entry.resolve_task_cancel_scope is None
                                    and entry.resolve_obligation_count == 0
                                    and not entry.resolved_sent
                                ):
                                    return True
                            return None

                        await _poll_until(a_cancelled_resolver)

                        # Release A's handler. Its dispatched work completes,
                        # but the resolver task was cancelled by _on_unpull so
                        # no ResolveMessage is sent.
                        release.set()

                        # Give A time to finish its work + would-be emission.
                        # Poll on the run counter instead of a wall-clock sleep.
                        def handler_completed() -> bool | None:
                            return True if run_count else None

                        await _poll_until(handler_completed)
                        # Yield a few more times to let any racing emission
                        # surface (it shouldn't).
                        for _ in range(10):
                            await anyio.sleep(0)
                        # Force collection so the cancelled-waiter's ``future``
                        # proxy (held via the torn-down Task's coroutine frame)
                        # finalizes deterministically before we inspect the
                        # wire — otherwise §2j's best-effort future
                        # :class:`DiscardMessage` can land after the assertion.
                        import gc

                        gc.collect()

                        # Inspect the wire. Filter to just the messages we care
                        # about (WatchMessage may interleave from the box's
                        # token subscription). Keep the message objects, not
                        # just type names, so we can assert on the IDs each
                        # one references and verify they line up with the
                        # push's allocations.
                        interesting_types = (
                            PushMessage,
                            PullMessage,
                            DiscardMessage,
                            UnpullMessage,
                        )
                        filtered = [
                            m for m in sniffed_b_outbound if isinstance(m, interesting_types)
                        ]
                        # Pin down the shape first: six messages in this
                        # exact order. The trailing ``Discard(token)`` is
                        # the full accumulated token refcount, emitted
                        # when the push token's cached proxy itself GCs —
                        # with no dependents left holding a pin, the
                        # WeakValueDictionary cache doesn't keep it alive,
                        # and its finalizer retires every introduction
                        # (standalone +1 from the push, +1 each from the
                        # future and grant deps) in a single discard.
                        #   [Push, Pull,
                        #    Discard(grant),    # from wait's lease cleanup
                        #    Unpull(future),    # cancelled wait
                        #    Discard(future),   # from future proxy GC
                        #    Discard(token)]    # from token proxy GC
                        assert [type(m).__name__ for m in filtered] == [
                            "PushMessage",
                            "PullMessage",
                            "DiscardMessage",
                            "UnpullMessage",
                            "DiscardMessage",
                            "DiscardMessage",
                        ], f"unexpected outbound sequence: {filtered!r}"
                        (
                            push_msg,
                            pull_msg,
                            grant_discard,
                            unpull_msg,
                            future_discard,
                            token_discard,
                        ) = filtered
                        assert isinstance(push_msg, PushMessage)
                        assert isinstance(pull_msg, PullMessage)
                        assert isinstance(grant_discard, DiscardMessage)
                        assert isinstance(unpull_msg, UnpullMessage)
                        assert isinstance(future_discard, DiscardMessage)
                        assert isinstance(token_discard, DiscardMessage)

                        # The push allocates three consecutive positive ids:
                        # (token, future, grant) = (N, N+1, N+2). For this
                        # test N=3 (the first positive id after the reserved
                        # main-interface triple), but we assert the
                        # relationship rather than the absolute value so a
                        # future change to the allocator doesn't silently
                        # break the invariant.
                        pulled_future_id = pull_msg.future_import_id
                        pulled_grant_id = pull_msg.grant_import_id
                        push_token_id = pulled_future_id - 1
                        assert pulled_grant_id == pulled_future_id + 1, (
                            "push-allocated (future, grant) must be adjacent;"
                            f" got future={pulled_future_id}, grant={pulled_grant_id}"
                        )

                        # 1. The first Discard is for the *grant* we named in
                        #    the Pull. Emitted when wait()'s setup block discards
                        #    the user-taken lease (last live lease on that grant
                        #    → _clean_up_lease_data enqueues DiscardMessage).
                        assert grant_discard.import_id == pulled_grant_id, (
                            "first DiscardMessage should target the grant from"
                            f" the push/pull triple (id={pulled_grant_id}),"
                            f" got {grant_discard.import_id}"
                        )
                        assert grant_discard.refcount == 1
                        # 2. The Unpull is for the *future* we pulled. Emitted
                        #    from wait()'s cancelled finally block.
                        assert unpull_msg.future_import_id == pulled_future_id, (
                            "UnpullMessage should target the pulled future"
                            f" (id={pulled_future_id}), got"
                            f" {unpull_msg.future_import_id}"
                        )
                        assert unpull_msg.refcount == 1
                        # 3. After the waiter task tears down, the waiter's
                        #    ``future`` proxy finalizes and its GC callback
                        #    emits Discard(future).
                        assert future_discard.import_id == pulled_future_id, (
                            "trailing DiscardMessage should target the pulled"
                            f" future (id={pulled_future_id}), got"
                            f" {future_discard.import_id}"
                        )
                        assert future_discard.refcount == 1
                        # 4. With no dependents left to pin the push token
                        #    proxy (and no box keeping it alive — the
                        #    ``LocalPendingResultBox`` is freed by plain
                        #    refcounting now that it no longer stores its
                        #    own end-callback deregister closure), the
                        #    cached token proxy GCs and its finalizer
                        #    retires the full accumulated refcount in one
                        #    discard.
                        assert token_discard.import_id == push_token_id, (
                            "trailing DiscardMessage should target the push"
                            f" token (id={push_token_id}), got"
                            f" {token_discard.import_id}"
                        )
                        # Standalone +1 from the push's implicit token
                        # introduction plus +1 each from the future and
                        # grant deps' Exported…Expr pair-bumps.
                        assert token_discard.refcount == 3, (
                            f"expected Discard(token={push_token_id},"
                            f" refcount=3) retiring the full accumulated"
                            f" refcount (1 standalone + 1 per dep); got"
                            f" refcount={token_discard.refcount}"
                        )
                        assert not any(isinstance(m, ResolveMessage) for m in sniffed_a_outbound), (
                            "A must not have sent a ResolveMessage after Unpull; "
                            f"sniffed_a_outbound={sniffed_a_outbound!r}"
                        )
                        assert not any(
                            isinstance(m, RejectMessage) for m in sniffed_a_outbound
                        ), "A must not have sent a RejectMessage after Unpull"
                        assert run_count == [1], (
                            "A's handler dispatched work must still complete — it's a"
                            f" read, not a control op; got run_count={run_count}"
                        )
                        _b_done.set()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        del sniffed_a_outbound


# ---------------------------------------------------------------------------
# §2i tests — bounce-back identity + as_client / as_server conveniences
# ---------------------------------------------------------------------------


class _IdentityCheckMain(IRHandleLocalImplBase[Any]):
    """Main interface that records the ``request.arg`` it was called with.

    Used by :class:`TestHandleBounceBackIdentity` to verify that a
    :class:`RemoteIRHandle` sent back to its origin resolves — on the origin
    side — to the original local :class:`IRHandle` object, not a fresh export.
    """

    def __init__(self, token: Any, task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group
        self.last_received: Any = None

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call) and request.method == "receive_handle":
            self.last_received = request.arg
            return None
        return None


class TestHandleBounceBackIdentity:
    """B imports A's main handle as a :class:`RemoteIRHandle`, then sends that
    proxy back to A inside a :class:`Call` argument. A's phase-1 substitution
    resolves the :class:`ImportedIRHandleExpr` by looking up the matching
    :class:`HandleExportEntry` and returning ``entry.local_handle`` — the
    original :class:`IRHandle` object A constructed. ``is``-identity preserved.
    """

    async def test_round_tripped_handle_resolves_to_original_local_handle(self) -> None:
        a_transport, b_transport = _make_transport_pair()
        a_main_holder: dict[str, _IdentityCheckMain] = {}

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _IdentityCheckMain(a_lt.token, a_tg)
                        a_main_holder["main"] = a_main
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        # ``conn.their_main_interface`` is a RemoteIRHandle
                        # pointing at A's main handle import. Devaluator sees
                        # a RemoteIRHandle on this connection → emits
                        # ImportedIRHandleExpr(1). A's phase-1 resolves that
                        # to the original local handle via _exports[1].local_handle.
                        await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="receive_handle", arg=conn.their_main_interface),
                        )
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        a_main = a_main_holder["main"]
        assert a_main.last_received is a_main, (
            "bounce-back handle identity broken: A's handler should have received"
            " the exact original local handle via `is` identity; got"
            f" {a_main.last_received!r}, expected {a_main!r}"
        )


class TestNullMainInterfaceRejection:
    """An inbound :class:`Call` targeting an ``as_client`` connection's null
    main interface surfaces to the caller as an :class:`IRException` with
    category ``"NullMainInterface"``.

    Covers the until-now-unreachable dispatch branch in
    :class:`fxtr2.rpc.lowering_null_main.NullMainInterface`: A runs ``as_client``
    so its main handle is the :class:`NullMainInterface` placeholder; B is
    constructed directly (not ``as_server``, so B keeps its handle on A's main
    interface) and calls ``their_main_interface.request``. The dispatched work
    on A raises :class:`IRException`; :func:`spawn_ir_future_in_task_group`
    routes it via ``set_exception``; A emits a :class:`RejectMessage`; B's
    ``wait`` re-raises a copy on the caller.
    """

    async def test_inbound_call_to_null_main_rejects(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    # ``as_client`` synthesizes a NullMainInterface placeholder
                    # for our main handle + keeps our hold on the peer's main
                    # lease until normal shutdown.
                    async with LeasedCapRPCConnection.as_client(a_transport):
                        await anyio.sleep_forever()

                async def side_b() -> None:
                    async with open_lifetime_task_group("B-main") as (b_lt, b_tg):
                        b_main = _EchoMain(b_lt.token, b_tg)
                        # Raw constructor — we want B to retain its lease on
                        # A's (null) main interface so we can actually call it.
                        # ``as_server`` would discard it at construction time.
                        async with LeasedCapRPCConnection(
                            b_transport, b_main, b_lt.root_lease.clone()
                        ) as conn:
                            with pytest.raises(IRException) as excinfo:
                                await conn.their_main_interface.request(
                                    conn.their_main_lease.clone(),
                                    Call(method="anything", arg="hello"),
                                )
                            assert excinfo.value.category == "NullMainInterface", (
                                "expected IRException(category='NullMainInterface'),"
                                f" got {excinfo.value!r}"
                            )
                            tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)


# ---------------------------------------------------------------------------
# §2j tests — best-effort DiscardMessage for handle/future imports on proxy GC
# ---------------------------------------------------------------------------


class _HandleServerMain(IRHandleLocalImplBase[Any]):
    """Main interface for §2j handle-discard tests.

    Exposes ``get_child``: returns a stable secondary :class:`IRHandle`
    (``same_token_child``) — a separate object from ``self`` but deliberately
    *sharing our lifetime token*, so no extra lifetime plumbing is needed.
    The stability lets us exercise the re-export / re-introduction path:
    the *same* local handle object devaluates to a fresh export ID after a
    previous discard has cleared it from ``_handle_export_id_by_handle_id``.

    Note: since the child shares the main token, dropping a proxy for this
    child doesn't on its own cause the subject-token entry to GC — the main
    token is still pinned by ``_their_main_token_proxy`` and friends for
    the connection's lifetime. Tests that need a fresh subject-token
    lifecycle should use a push result (which allocates a fresh token id)
    instead — see ``TestPinChainKeepsTokenAlive``.
    """

    def __init__(self, token: Any, task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group
        # Named ``same_token_child`` (rather than ``child``) to make the
        # shared-lifetime design choice explicit at every call site.
        self.same_token_child: IRHandleLocalImplBase[Any] = _EchoMain(token, task_group)

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call) and request.method == "get_child":
            return self.same_token_child
        return None


class TestFutureProxyGCEmitsDiscard:
    """After a :class:`RemoteIRFuture` proxy is garbage-collected with no
    remaining references, the connection enqueues a :class:`DiscardMessage`
    for the future's import ID. This is §2j's best-effort cleanup signal —
    holding onto the proxy doesn't pin any cross-process resource (only
    grants do, and those are handled in §2f), but emitting the discard lets
    the peer free its export-table slot promptly.
    """

    async def test_future_discard_emitted_after_proxy_gc(self) -> None:
        import gc

        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )

        future_id_holder: dict[str, int] = {}

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _EchoMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        future, lease = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg="hello"),
                        )
                        assert isinstance(future, RemoteIRFuture)
                        future_id_holder["id"] = future.future_import_id
                        # Wait first so the future resolves + the push
                        # machinery stabilizes; then drop the proxy.
                        result = await future.wait(lease.clone())
                        assert result == {"echoed": "hello", "method": "echo"}
                        lease.discard()
                        # The discard for the grant is already on the wire
                        # at this point (from lease.discard() → _clean_up_lease_data
                        # emitting DiscardMessage for the push-allocated grant).
                        # Now release the future proxy and let its finalizer
                        # enqueue the future discard.
                        del future
                        gc.collect()
                        # Give the finalizer's enqueued message a chance to
                        # land on the sniffed outbound list (anyio sleep yields
                        # to the writer loop, which pulls from the outbox).
                        for _ in range(5):
                            await anyio.sleep(0)
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        future_id = future_id_holder["id"]
        discards_for_future = [
            m
            for m in sniffed_b_outbound
            if isinstance(m, DiscardMessage) and m.import_id == future_id
        ]
        assert len(discards_for_future) == 1, (
            f"expected exactly one DiscardMessage for future import id {future_id},"
            f" got {discards_for_future!r}"
        )
        assert discards_for_future[0].refcount == 1, (
            "future was introduced exactly once (via the push allocation);"
            f" expected refcount=1, got {discards_for_future[0].refcount}"
        )


class TestHandleProxyGCEmitsDiscard:
    """After a :class:`RemoteIRHandle` proxy is garbage-collected with no
    remaining references, the connection enqueues a :class:`DiscardMessage`
    for the handle's import ID. Main-interface handles (ID 1) are cached
    indefinitely on the connection itself, so this path naturally only fires
    for secondary handle imports — which is the case we test here.
    """

    async def test_handle_discard_emitted_after_proxy_gc(self) -> None:
        import gc

        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )

        child_id_holder: dict[str, int] = {}

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _HandleServerMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        child_handle = await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="get_child", arg=None),
                        )
                        assert isinstance(child_handle, RemoteIRHandle), (
                            f"expected a RemoteIRHandle for the child, got"
                            f" {type(child_handle).__name__}"
                        )
                        child_id_holder["id"] = child_handle.handle_import_id
                        # Drop the only reference to the child proxy. Finalizer
                        # fires synchronously (refcount drop → 0); callback
                        # enqueues DiscardMessage.
                        del child_handle
                        gc.collect()
                        for _ in range(5):
                            await anyio.sleep(0)
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        child_id = child_id_holder["id"]
        discards_for_handle = [
            m
            for m in sniffed_b_outbound
            if isinstance(m, DiscardMessage) and m.import_id == child_id
        ]
        assert len(discards_for_handle) == 1, (
            f"expected exactly one DiscardMessage for handle import id {child_id},"
            f" got {discards_for_handle!r}"
        )
        assert discards_for_handle[0].refcount == 1, (
            "child handle was introduced exactly once (via the receive_handle"
            f" call's result); expected refcount=1, got"
            f" {discards_for_handle[0].refcount}"
        )
        # No paired token discard: handle proxy GC emits only its own
        # ``Discard(handle, 1)``. The handle's share of the subject token's
        # introduction_refcount (one per ExportedIRHandleExpr) stays
        # accumulated on the token import entry and is retired only when
        # the cached ``RemoteLifetimeToken`` proxy itself GCs. In this test
        # the subject token is B's main token, which stays pinned via
        # ``_their_main_token_proxy`` / ``_their_main_interface_proxy``
        # for the connection's lifetime — so no main-token discard should
        # appear at all while the connection is alive.
        main_token_discards = [
            m
            for m in sniffed_b_outbound
            if isinstance(m, DiscardMessage) and m.import_id == MAIN_TOKEN_ID
        ]
        assert main_token_discards == [], (
            "main token is pinned by _their_main_token_proxy, so no"
            f" DiscardMessage for it should appear; got {main_token_discards!r}"
        )


class TestHandleReIntroductionAfterDiscard:
    """After a handle import's :class:`DiscardMessage` has propagated to the
    exporter and the exporter has torn its :class:`HandleExportEntry` down,
    re-introducing the same local handle on the exporter allocates a fresh
    export ID. The importer therefore observes two *different* import IDs
    for the two successive introductions, even though they name the same
    underlying local handle on the exporter.
    """

    async def test_re_introduction_allocates_fresh_export_id(self) -> None:
        import gc

        a_transport, b_transport = _make_transport_pair()
        first_id_holder: dict[str, int] = {}
        second_id_holder: dict[str, int] = {}

        a_conn_holder: dict[str, LeasedCapRPCConnection[Any, Any]] = {}
        _first_discard_processed = anyio.Event()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _HandleServerMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ) as a_conn:
                            a_conn_holder["conn"] = a_conn
                            # Hold until B's first discard has landed on A
                            # AND B has made the second call; then keep A
                            # alive so the second round-trip can complete.
                            await _first_discard_processed.wait()
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        # First call: receive child handle at some import ID.
                        child1 = await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="get_child", arg=None),
                        )
                        assert isinstance(child1, RemoteIRHandle)
                        first_id_holder["id"] = child1.handle_import_id
                        # Drop the proxy; finalizer emits DiscardMessage.
                        del child1
                        gc.collect()
                        # Poll A's export table until the tear-down has
                        # landed (A's reader removes the entry on receipt of
                        # DiscardMessage). This eliminates the race where
                        # B's second call outruns A's processing of the
                        # discard — without it, A could still have the old
                        # entry in ``_handle_export_id_by_handle_id`` when
                        # devaluating the second response, reusing the id.
                        for _ in range(200):
                            a_conn = a_conn_holder.get("conn")
                            if a_conn is not None and first_id_holder["id"] not in a_conn._exports:
                                break
                            await anyio.sleep(0)
                        # Signal A we're past the race window; A's hold
                        # releases so we can exit cleanly at the end.
                        _first_discard_processed.set()
                        # Second call: same underlying local handle on A,
                        # but A's devaluator allocates a fresh export ID
                        # since the old entry has been torn down.
                        child2 = await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="get_child", arg=None),
                        )
                        assert isinstance(child2, RemoteIRHandle)
                        second_id_holder["id"] = child2.handle_import_id
                        del child2
                        gc.collect()
                        for _ in range(5):
                            await anyio.sleep(0)
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        first_id = first_id_holder["id"]
        second_id = second_id_holder["id"]
        assert first_id != second_id, (
            "re-introducing the same local handle after a discard should"
            f" allocate a fresh export id on the remote; got {first_id} twice"
        )


class TestImportEntryRemoval:
    """When an import entry is fully drained (no proxies, no refcount, no
    pending obligations), it is removed from ``_imports`` so long-running
    sessions don't leak entry objects. A subsequent re-introduction at the
    same id allocates a fresh entry via the ``_introduce_*_import`` path.
    """

    async def test_handle_entry_removed_after_proxy_gc(self) -> None:
        import gc

        a_transport, b_transport = _make_transport_pair()
        child_id_holder: dict[str, int] = {}
        b_conn_holder: dict[str, LeasedCapRPCConnection[Any, Any]] = {}

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _HandleServerMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        b_conn_holder["conn"] = conn
                        child = await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="get_child", arg=None),
                        )
                        assert isinstance(child, RemoteIRHandle)
                        child_id_holder["id"] = child.handle_import_id
                        # Before drop: entry is present.
                        assert child.handle_import_id in conn._imports
                        # Drop + GC + settle so the finalizer fires and
                        # emits Discard(child_handle_id, 1).
                        del child
                        gc.collect()
                        for _ in range(5):
                            await anyio.sleep(0)
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        b_conn = b_conn_holder["conn"]
        child_id = child_id_holder["id"]
        assert child_id not in b_conn._imports, (
            f"handle import entry id {child_id} should have been removed from"
            f" _imports after its proxy GC'd and the discard was emitted;"
            f" got {sorted(b_conn._imports)}"
        )

    async def test_grant_entry_removed_after_wait_completes(self) -> None:
        """A push-allocated grant import is removed from ``_imports`` once
        both drain conditions hold: ``live_leases`` empty AND
        ``wait_break_callbacks`` empty.

        The ``wait`` path exercises both in sequence — it takes the user
        lease (populating ``live_leases``), registers a pin-style
        wait-break callback, discards the lease (draining
        ``live_leases`` and emitting :class:`DiscardMessage`), awaits
        resolution, and in ``finally`` deregisters the pin. The
        deregister closure is the site that calls
        :meth:`_maybe_remove_grant_import` after both conditions have
        flipped.
        """
        b_conn_holder: dict[str, LeasedCapRPCConnection[Any, Any]] = {}
        grant_id_holder: dict[str, int] = {}

        a_transport, b_transport = _make_transport_pair()
        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _CountingMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        b_conn_holder["conn"] = conn
                        future, result_lease = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg="hi"),
                        )
                        assert isinstance(future, RemoteIRFuture)
                        grant_id = cast(Any, result_lease.grant).grant_import_id
                        grant_id_holder["id"] = grant_id
                        # Pre-wait: grant entry is present with one live lease.
                        entry = conn._imports.get(grant_id)
                        assert isinstance(entry, GrantImportEntry)
                        assert len(entry.live_leases) == 1
                        assert entry.wait_break_callbacks == {}
                        # Wait consumes ``result_lease``. During the wait
                        # the pin wait-break callback keeps the entry alive
                        # past the in-wait ``live_leases`` drain; the
                        # ``finally`` deregister flips the entry's final
                        # drain state and removes it.
                        result = await future.wait(result_lease)
                        assert result == "hi"
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        b_conn = b_conn_holder["conn"]
        grant_id = grant_id_holder["id"]
        assert grant_id not in b_conn._imports, (
            f"grant import entry id {grant_id} should have been removed"
            f" from _imports once ``wait`` completes (live_leases drained"
            f" plus wait-break pin deregistered); got"
            f" {sorted(b_conn._imports)}"
        )

    async def test_future_entry_removed_after_proxy_gc(self) -> None:
        """A push-allocated future import is removed from ``_imports``
        once its proxy GCs with no pending obligations.

        Setup: dispatch + wait completes the round-trip, which retires
        every obligation on the future entry
        (``resolve_obligation_refcount``, ``local_consumer_count``). At
        that point dropping the proxy fires the GC finalizer, which
        decrements ``live_proxy_count`` and routes through
        :func:`_dependent_decrement_and_maybe_discard` →
        :func:`_maybe_remove_future_import` → ``del _imports[id]``.
        """
        import gc

        b_conn_holder: dict[str, LeasedCapRPCConnection[Any, Any]] = {}
        future_id_holder: dict[str, int] = {}

        a_transport, b_transport = _make_transport_pair()
        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _CountingMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        b_conn_holder["conn"] = conn
                        future, result_lease = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg="ok"),
                        )
                        assert isinstance(future, RemoteIRFuture)
                        future_id = future.future_import_id
                        future_id_holder["id"] = future_id
                        pre_entry = conn._imports.get(future_id)
                        assert isinstance(pre_entry, FutureImportEntry)
                        # Complete the round-trip; this drains every
                        # obligation on the future entry and consumes
                        # ``result_lease``.
                        result = await future.wait(result_lease)
                        assert result == "ok"
                        # Drop every local reference to the push triple
                        # (future proxy + consumed lease) and cycle the
                        # event loop so the finalizer callback enqueues
                        # + the connection's writer can observe the
                        # decrement.
                        del future
                        del result_lease
                        gc.collect()
                        for _ in range(5):
                            await anyio.sleep(0)
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        b_conn = b_conn_holder["conn"]
        future_id = future_id_holder["id"]
        assert future_id not in b_conn._imports, (
            f"future import entry id {future_id} should have been removed"
            f" from _imports after its proxy GC'd and wait had already"
            f" drained resolve_obligation_refcount + local_consumer_count;"
            f" got {sorted(b_conn._imports)}"
        )

    async def test_token_entry_removed_after_final_discard(self) -> None:
        """A push-allocated token import is removed from ``_imports``
        once the cached token proxy itself GCs (after every dependent
        has already retired its own proxy), the final accumulated
        :class:`DiscardMessage` is emitted, and no end-callbacks are
        registered.

        This is the last link in the pin-chain cascade: B dispatches →
        discards the lease (grant discard + grant entry removed) →
        completes the wait → drops the future proxy (future discard +
        future entry removed) → at that point the subject-token proxy
        is no longer pinned by any dependent and its own GC finalizer
        fires, emitting the accumulated token :class:`DiscardMessage`
        and removing the token entry.
        """
        import gc

        b_conn_holder: dict[str, LeasedCapRPCConnection[Any, Any]] = {}
        token_id_holder: dict[str, int] = {}

        a_transport, b_transport = _make_transport_pair()
        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _CountingMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        b_conn_holder["conn"] = conn
                        future, result_lease = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="echo", arg=42),
                        )
                        assert isinstance(future, RemoteIRFuture)
                        # Push layout: token is future_id - 1.
                        token_id = future.future_import_id - 1
                        token_id_holder["id"] = token_id
                        entry = conn._imports.get(token_id)
                        assert isinstance(entry, TokenImportEntry)
                        # Drain: complete wait (retires grant entry +
                        # future obligations), drop future proxy (retires
                        # future entry + releases the token pin via
                        # ``RemoteIRFuture._token``), gc.
                        result = await future.wait(result_lease)
                        assert result == 42
                        # Drop every local reference that pins the
                        # token proxy via the ``RemoteGrant._token`` /
                        # ``RemoteIRFuture._token`` pin chain. Both the
                        # consumed lease (via ``result_lease.grant``)
                        # and the future proxy hold strong references,
                        # so both must go before the token proxy
                        # finalizer can fire.
                        del future
                        del result_lease
                        gc.collect()
                        for _ in range(5):
                            await anyio.sleep(0)
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        b_conn = b_conn_holder["conn"]
        token_id = token_id_holder["id"]
        assert token_id not in b_conn._imports, (
            f"token import entry id {token_id} should have been removed"
            f" from _imports after the cached token proxy GC'd and the"
            f" final DiscardMessage was emitted; got"
            f" {sorted(b_conn._imports)}"
        )


class TestImporterReIntroductionAfterDiscard:
    """Mirror-image complement to
    :class:`TestHandleReIntroductionAfterDiscard`: the exporter-side
    variant verifies that A allocates a fresh *export* id after its
    previous entry was torn down; this importer-side variant verifies
    that B's :meth:`_introduce_handle_import` registers a fresh *import*
    entry cleanly after the earlier import entry has been removed from
    B's ``_imports`` dict.

    The interesting invariant is that the removal-on-drain path
    (``_maybe_remove_handle_import``) doesn't leave stale state
    (wrong-kind entry, residual refcount, etc.) that would cause the
    next introduction to trip
    :class:`lowering_errors.ImportEntryKindMismatch` or otherwise
    misregister.
    """

    async def test_fresh_entry_registered_after_removal(self) -> None:
        import gc

        a_transport, b_transport = _make_transport_pair()
        first_id_holder: dict[str, int] = {}
        second_id_holder: dict[str, int] = {}
        b_conn_holder: dict[str, LeasedCapRPCConnection[Any, Any]] = {}
        _first_discard_processed = anyio.Event()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _HandleServerMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ) as a_conn:
                            # Hold until B has completed both calls and
                            # asserted on its _imports shape.
                            await _first_discard_processed.wait()
                            # Touch to silence the unused-var warning.
                            _ = a_conn

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        b_conn_holder["conn"] = conn
                        # First call: receive an imported handle.
                        child1 = await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="get_child", arg=None),
                        )
                        assert isinstance(child1, RemoteIRHandle)
                        first_id = child1.handle_import_id
                        first_id_holder["id"] = first_id
                        first_entry = conn._imports.get(first_id)
                        assert isinstance(first_entry, HandleImportEntry)
                        assert first_entry.live_proxy_count == 1
                        assert first_entry.introduction_refcount == 1
                        # Drop the proxy, GC, settle, wait for A's
                        # export-side teardown so the next call is
                        # guaranteed to allocate a fresh export id.
                        del child1
                        gc.collect()
                        for _ in range(20):
                            if first_id not in conn._imports:
                                break
                            await anyio.sleep(0)
                        assert first_id not in conn._imports, (
                            f"first-call handle import entry id {first_id}"
                            f" should have been removed from _imports after"
                            f" the proxy GC'd; got {sorted(conn._imports)}"
                        )
                        # Second call: fresh exporter-side id → fresh
                        # importer-side entry. The crucial invariant is
                        # that ``_introduce_handle_import`` registers
                        # cleanly (no kind-mismatch, no stale refcount).
                        child2 = await conn.their_main_interface.request(
                            conn.their_main_lease.clone(),
                            Call(method="get_child", arg=None),
                        )
                        assert isinstance(child2, RemoteIRHandle)
                        second_id = child2.handle_import_id
                        second_id_holder["id"] = second_id
                        second_entry = conn._imports.get(second_id)
                        assert isinstance(second_entry, HandleImportEntry), (
                            f"second-call import entry should be a fresh"
                            f" HandleImportEntry; got {second_entry!r}"
                        )
                        assert second_entry is not first_entry
                        assert second_entry.live_proxy_count == 1
                        assert second_entry.introduction_refcount == 1
                        # Keep child2 alive until we're done asserting —
                        # otherwise its finalizer could race our check.
                        _ = child2
                        _first_discard_processed.set()
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        # Sanity: the two calls saw different ids (the exporter-side
        # re-intro is covered by TestHandleReIntroductionAfterDiscard;
        # asserted here only to make the test self-contained against
        # the shared setup).
        assert first_id_holder["id"] != second_id_holder["id"]


# ---------------------------------------------------------------------------
# §2j.5 tests — paired token discards + pin chain + NullMainInterface cleanup
# ---------------------------------------------------------------------------


class TestNullMainInterfaceCleansUpRequestLeases:
    """Calling a :class:`NullMainInterface` with a :class:`Lease` inside
    ``Call.arg`` must not leak — the dispatch path that raises
    :class:`IRException` also has to walk the request and discard every
    lease it consumed, or drain blocks indefinitely waiting for
    :class:`DiscardMessage` emissions that never fire.

    We exercise the path by handing the placeholder main a :class:`Lease`
    and asserting that (a) the caller sees the expected ``IRException``,
    and (b) both halves drain cleanly on normal shutdown. The drain on B's
    side would hang if the inbound lease ended up stranded with no paired
    discard.
    """

    async def test_inbound_call_with_lease_arg_drains_cleanly(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with LeasedCapRPCConnection.as_client(a_transport):
                        await anyio.sleep_forever()

                async def side_b() -> None:
                    async with open_lifetime_task_group("B-main") as (b_lt, b_tg):
                        b_main = _EchoMain(b_lt.token, b_tg)
                        async with LeasedCapRPCConnection(
                            b_transport, b_main, b_lt.root_lease.clone()
                        ) as conn:
                            # Pass a clone of our own main lease as the call
                            # arg — the null main on A will have to walk
                            # ``request.arg`` and discard this lease before
                            # raising, or drain won't complete.
                            with pytest.raises(IRException) as excinfo:
                                await conn.their_main_interface.request(
                                    conn.their_main_lease.clone(),
                                    Call(method="noop", arg=b_lt.root_lease.clone()),
                                )
                            assert excinfo.value.category == "NullMainInterface"
                            tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestBrokenGrantSuppressesItsOwnDiscard:
    """When a :class:`BreakMessage` arrives for an imported grant, the
    :meth:`_mark_grant_import_broken` path emits *no* outbound
    :class:`DiscardMessage` — not for the grant (peer already tore down
    their ``grant_export`` before sending the break) and not for the
    subject token (the token's share of introductions stays accumulated
    and is retired later, in one go, when the cached token proxy itself
    GCs — see the "Proxy-GC tracking" comment in
    ``lowering_remote_proxies.py``).
    """

    async def test_break_suppresses_own_and_token_discard(self) -> None:
        sniffed_a: list[RPCMessage] = []
        sniffed_b: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(sniffed_a, sniffed_b)

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):

            async def raw_peer_a() -> None:
                await a_transport.send(HelloMessage())
                await _receive_until(a_transport, HelloMessage)
                # Emit BreakMessage for B's main grant. B's ``_on_break``
                # marks the grant import broken, fires the user's
                # broken-callbacks, and emits the paired token discard.
                await a_transport.send(
                    BreakMessage(grant_export_id=MAIN_GRANT_ID, reason="test break")
                )
                # Also retire B's own main-interface exports so B's drain
                # can complete when it exits its CM. (In a real peer these
                # would come from the LeasedCapRPCConnection's shutdown
                # path; since we're emulating raw, we send them manually.)
                # Refcounts match the pair-bumps performed at init:
                # grant/handle each start at 1, token at 3 (1 implicit +
                # 2 pair-bumps for handle + grant).
                await a_transport.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
                await a_transport.send(DiscardMessage(import_id=MAIN_HANDLE_ID, refcount=1))
                await a_transport.send(DiscardMessage(import_id=MAIN_TOKEN_ID, refcount=3))
                # Drain anything B sends back until EOF (from B's shutdown).
                try:
                    while True:
                        await a_transport.receive()
                except (anyio.EndOfStream, anyio.ClosedResourceError):
                    return

            async def side_b() -> None:
                # Raw constructor — ``as_server`` would discard our main
                # lease at construction (removing the grant import before
                # A's break could land); ``as_client`` would emit its own
                # main-grant discard at shutdown (overlapping the signal
                # we want to measure).
                async with open_lifetime_task_group("B-main") as (b_lt, b_tg):
                    b_main = _EchoMain(b_lt.token, b_tg)
                    broke = anyio.Event()
                    async with LeasedCapRPCConnection(
                        b_transport, b_main, b_lt.root_lease.clone()
                    ) as conn:

                        def on_broken(_reason: str) -> None:
                            broke.set()

                        conn.their_main_lease.register_broken_callback(on_broken)
                        await broke.wait()
                        # Let ``_on_break`` fully flush its outbound
                        # messages before we exit the CM.
                        for _ in range(10):
                            await anyio.sleep(0)
                        tg.cancel_scope.cancel()

            async with anyio.create_task_group() as tg:
                tg.start_soon(raw_peer_a)
                tg.start_soon(side_b)

        # B's outbound must not contain *any* DiscardMessage for the main
        # grant or the main token from the broken-grant path. The grant's
        # own discard is suppressed (peer already tore their grant_export
        # down); the token's share is left on the import entry for the
        # eventual token-proxy GC, which on a clean shutdown is short-
        # circuited by the connection's abort path.
        token_discards = [
            m for m in sniffed_b if isinstance(m, DiscardMessage) and m.import_id == MAIN_TOKEN_ID
        ]
        grant_discards = [
            m for m in sniffed_b if isinstance(m, DiscardMessage) and m.import_id == MAIN_GRANT_ID
        ]
        assert not token_discards, (
            "broken-grant path should NOT emit a paired token DiscardMessage"
            f" (deferred to token-proxy GC); got {token_discards!r}"
        )
        assert not grant_discards, (
            "broken-grant path should suppress the grant's own DiscardMessage"
            f" (peer already tore it down), got {grant_discards!r}"
        )


class TestPinChainKeepsTokenAlive:
    """The pin chain (``RemoteIRFuture._token`` / ``RemoteGrant._token``
    strongly reference the cached subject-token proxy) guarantees that
    when a push's future + grant both drop, the resulting GC cascade
    emits the dep discards *before* the subject-token discard, and the
    token's ``introduction_refcount`` — which accumulates one-per-dep on
    top of the standalone token introduction — is retired in a *single*
    :class:`DiscardMessage` rather than paired piecewise with each dep.

    Setup: A is a raw peer that never resolves the push, so the push's
    token/future/grant import triple just sits on B's side waiting to
    be discarded. B calls ``dispatch``, drops the lease (→ grant
    discard), then drops the future proxy (→ future discard and, via
    pin-chain release, token discard).
    """

    async def test_push_triple_discard_sequence(self) -> None:
        import gc

        sniffed_b_outbound: list[RPCMessage] = []
        sniffed_a_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )
        holder: dict[str, int] = {}

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):

            async def raw_peer_a() -> None:
                await a_transport.send(HelloMessage())
                await _receive_until(a_transport, HelloMessage)
                await _receive_until(a_transport, PushMessage)
                # Don't resolve: we want the push triple to retire purely
                # via B's own proxy-GC + lease-discard cascade.
                #
                # Retire B's exported main grant so B's drain can
                # complete on CM exit (real peers do this themselves at
                # shutdown). Send it after the Push so B's ``dispatch``
                # has committed its three import allocations before we
                # start pulling messages towards EOF.
                await a_transport.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
                await a_transport.send_eof()
                try:
                    while True:
                        await a_transport.receive()
                except (anyio.EndOfStream, anyio.ClosedResourceError):
                    return

            async def side_b() -> None:
                async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                    future, result_lease = conn.their_main_interface.dispatch(
                        conn.their_main_lease.clone(),
                        Call(method="ping", arg=None),
                    )
                    assert isinstance(future, RemoteIRFuture)
                    # Record the push's three allocated ids. The push
                    # allocator lays out (token, future, grant) at three
                    # consecutive positive ids; assert the relationship
                    # rather than the absolute value.
                    grant_id = cast(Any, result_lease.grant).grant_import_id
                    future_id = cast(Any, future).future_import_id
                    assert grant_id == future_id + 1
                    token_id = future_id - 1
                    holder["grant_id"] = grant_id
                    holder["future_id"] = future_id
                    holder["token_id"] = token_id

                    # Step 1: discard the lease. Emits Discard(grant, 1);
                    # the grant proxy itself is still alive (referenced
                    # from ``result_lease.grant``) so it continues to pin
                    # the token proxy.
                    result_lease.discard()
                    # Step 2: drop every strong reference to the future
                    # and the lease so their proxies can GC. The future's
                    # finalizer emits Discard(future, 1); after both dep
                    # proxies (future + grant) have GC'd, their ``_token``
                    # pins release and the token proxy — whose only other
                    # reference is the connection's
                    # :class:`weakref.WeakValueDictionary` cache — can
                    # GC, its finalizer emitting
                    # Discard(token_id, <accumulated refcount>).
                    del future
                    del result_lease
                    gc.collect()
                    for _ in range(5):
                        await anyio.sleep(0)

            async with anyio.create_task_group() as tg:
                tg.start_soon(raw_peer_a)
                tg.start_soon(side_b)

        grant_id = holder["grant_id"]
        future_id = holder["future_id"]
        token_id = holder["token_id"]
        # Collect the three push-triple discards from B's outbound stream.
        triple_discards = [
            m
            for m in sniffed_b_outbound
            if isinstance(m, DiscardMessage) and m.import_id in (grant_id, future_id, token_id)
        ]
        kinds = [m.import_id for m in triple_discards]
        # Expect exactly one of each.
        assert kinds.count(grant_id) == 1, f"expected one Discard(grant={grant_id}); got {kinds!r}"
        assert (
            kinds.count(future_id) == 1
        ), f"expected one Discard(future={future_id}); got {kinds!r}"
        assert kinds.count(token_id) == 1, f"expected one Discard(token={token_id}); got {kinds!r}"
        # Token discard retires the full accumulated refcount: 1 for the
        # push's implicit token introduction plus 1 each from the future
        # and grant dependents. Under the paired-discard scheme we'd see
        # three Discards of refcount=1 for the token; under the deferred
        # scheme we see exactly one Discard of refcount=3.
        token_discard = next(m for m in triple_discards if m.import_id == token_id)
        assert token_discard.refcount == 3, (
            f"expected Discard(token={token_id}, refcount=3) retiring the"
            f" full accumulated refcount (1 standalone + 1 per dep); got"
            f" refcount={token_discard.refcount}"
        )
        # Pin-chain ordering: token discard must come after both dep discards.
        token_idx = next(i for i, m in enumerate(triple_discards) if m.import_id == token_id)
        grant_idx = next(i for i, m in enumerate(triple_discards) if m.import_id == grant_id)
        future_idx = next(i for i, m in enumerate(triple_discards) if m.import_id == future_id)
        assert grant_idx < token_idx and future_idx < token_idx, (
            f"token discard should come after both dep discards; got"
            f" grant@{grant_idx}, future@{future_idx}, token@{token_idx}"
        )


# ---------------------------------------------------------------------------
# Substage 4 — pipelined outer-lease for IRHandle.dispatch / IRHandle.request
# ---------------------------------------------------------------------------


class _LeaseProducerMain(IRHandleLocalImplBase[Any]):
    """Main interface for substage-4 pipelined-outer-lease tests.

    Supported ``Call`` methods:

    - ``echo`` — returns ``arg`` unchanged.
    - ``give_me_a_lease`` — returns a fresh clone of our own main lease
      (a Lease that, when used as the outer lease for a subsequent
      dispatch on this handle, has the same authority as the original
      ``their_main_lease`` on the caller's side). Used as the upstream
      for pipelined-outer-lease tests.
    - ``fail_to_make_lease`` — raises ``IRException(category="LeaseProduceFailed")``.
    """

    def __init__(self, token: Any, task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group
        self._our_main_lease: Lease[Any] | None = None

    def set_our_main_lease(self, lease: Lease[Any]) -> None:
        """Stash a clone source of our main-side lease so ``give_me_a_lease`` can
        return clones of it. Caller is responsible for the lifetime of the
        lease they pass in (a ULL root_lease is typical).
        """
        self._our_main_lease = lease

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call):
            if request.method == "echo":
                return request.arg
            if request.method == "give_me_a_lease":
                assert self._our_main_lease is not None, "set_our_main_lease not called"
                return self._our_main_lease.clone()
            if request.method == "fail_to_make_lease":
                raise IRException(
                    message="upstream lease production failed",
                    category="LeaseProduceFailed",
                )
            return {"unknown_method": request.method}
        return {"attr": request.attr}


class TestDispatchPipelinedOuterLease:
    """Substage 4: outer dispatch lease may be pipelined as
    `UnwrapForPipelining[Lease, ...]`. Wire encoding is the existing
    `AwaitFutureExpr` in the `HandleRequestExpr.lease` slot.
    """

    async def test_outbound_pipelined_lease_wire_shape(self) -> None:
        """Pipelining the outer lease produces an AwaitFutureExpr in the
        lease slot of the second push. The first push goes out first; the
        second goes out before any resolve from the peer arrives.
        """
        sniffed_a_outbound: list[RPCMessage] = []
        sniffed_b_outbound: list[RPCMessage] = []
        a_transport, b_transport = _make_sniffing_transport_pair(
            sniffed_a_outbound, sniffed_b_outbound
        )

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _LeaseProducerMain(a_lt.token, a_tg)
                        a_main.set_our_main_lease(a_lt.root_lease.clone())
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        # Push #1: ask A for a lease.
                        lease_future, lease_future_lease = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="give_me_a_lease", arg=None),
                        )
                        # Push #2: dispatch on the main handle with the
                        # pipelined outer lease.
                        echo_future, echo_lease = conn.their_main_interface.dispatch(
                            cast(
                                "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                                UnwrapForPipelining(lease_future, lease_future_lease),
                            ),
                            Call(method="echo", arg="pipelined-lease"),
                        )
                        result = await echo_future.wait(echo_lease.clone())
                        assert result == "pipelined-lease"
                        echo_lease.discard()
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        # B's outbound: at least two PushMessages.
        pushes = [m for m in sniffed_b_outbound if isinstance(m, PushMessage)]
        assert len(pushes) >= 2, f"expected >=2 pushes from B, got {len(pushes)}"
        # The second push's expression is a HandleRequestExpr with an
        # AwaitFutureExpr in the lease slot — that's the pipelined-lease
        # wire shape.
        second = pushes[1].expression
        assert isinstance(
            second, HandleRequestExpr
        ), f"expected HandleRequestExpr, got {type(second).__name__}"
        assert isinstance(second.lease, AwaitFutureExpr), (
            f"expected pipelined-lease's lease slot to be AwaitFutureExpr,"
            f" got {type(second.lease).__name__}"
        )
        # The future inside the AwaitFutureExpr should be ImportedIRFutureExpr
        # pointing at push #1's allocated future import.
        assert isinstance(second.lease.future, ImportedIRFutureExpr), (
            f"expected AwaitFutureExpr.future = ImportedIRFutureExpr,"
            f" got {type(second.lease.future).__name__}"
        )

    async def test_pipelined_lease_future_rejection_propagates(self) -> None:
        """The first call rejects → the pipelined second dispatch's future
        rejects with the same IRException (the resolved-lease future never
        produces a value).
        """
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _LeaseProducerMain(a_lt.token, a_tg)
                        a_main.set_our_main_lease(a_lt.root_lease.clone())
                        async with LeasedCapRPCConnection.as_server(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ):
                            await anyio.sleep_forever()

                async def side_b() -> None:
                    async with LeasedCapRPCConnection.as_client(b_transport) as conn:
                        # Push #1: ask A to fail to produce a lease.
                        lease_future, lease_future_lease = conn.their_main_interface.dispatch(
                            conn.their_main_lease.clone(),
                            Call(method="fail_to_make_lease", arg=None),
                        )
                        # Push #2: pipelined outer lease references the
                        # rejecting future.
                        echo_future, echo_lease = conn.their_main_interface.dispatch(
                            cast(
                                "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                                UnwrapForPipelining(lease_future, lease_future_lease),
                            ),
                            Call(method="echo", arg="never-runs"),
                        )
                        with pytest.raises(IRException) as excinfo:
                            await echo_future.wait(echo_lease.clone())
                        # The IRException should propagate from the rejecting
                        # upstream — exact category preserved through the
                        # AwaitFutureExpr resolution path.
                        assert excinfo.value.category == "LeaseProduceFailed"
                        echo_lease.discard()
                        tg.cancel_scope.cancel()

                tg.start_soon(side_a)
                tg.start_soon(side_b)

    async def test_inbound_pipelined_lease_round_trip(self) -> None:
        """Symmetric setup: A pipelines an outer-lease dispatch on B's main
        interface. Exercises the inbound `_eval_lease_or_pipelined`
        AwaitFutureExpr branch: B's evaluator must pipeline-through to
        handle.dispatch rather than awaiting the lease eagerly.
        """
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        # A is the *caller*: it has a non-null main only so
                        # B can't reach back; B is the *server* with the
                        # lease producer.
                        a_main = _LeaseProducerMain(a_lt.token, a_tg)
                        a_main.set_our_main_lease(a_lt.root_lease.clone())
                        async with LeasedCapRPCConnection(
                            a_transport,
                            our_main_interface=a_main,
                            our_main_lease=a_lt.root_lease.clone(),
                        ) as conn:
                            # A pipelines a dispatch on B's main interface.
                            lease_future, lease_future_lease = conn.their_main_interface.dispatch(
                                conn.their_main_lease.clone(),
                                Call(method="give_me_a_lease", arg=None),
                            )
                            echo_future, echo_lease = conn.their_main_interface.dispatch(
                                cast(
                                    "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                                    UnwrapForPipelining(lease_future, lease_future_lease),
                                ),
                                Call(method="echo", arg="from-A"),
                            )
                            result = await echo_future.wait(echo_lease.clone())
                            assert result == "from-A"
                            echo_lease.discard()

                async def side_b() -> None:
                    async with open_lifetime_task_group("B-main") as (b_lt, b_tg):
                        b_main = _LeaseProducerMain(b_lt.token, b_tg)
                        b_main.set_our_main_lease(b_lt.root_lease.clone())
                        async with LeasedCapRPCConnection(
                            b_transport,
                            our_main_interface=b_main,
                            our_main_lease=b_lt.root_lease.clone(),
                        ):
                            await anyio.sleep_forever()

                tg.start_soon(side_a)
                tg.start_soon(side_b)
                # side_a's CM exits when its async-with closes (after A's
                # work completes); cancel the group then to tear down B.
                await anyio.sleep(0.5)
                tg.cancel_scope.cancel()


class TestOutboundAwaitFutureExprOnLocalFutureExport:
    """Red test exposing a layer-level resolver-spawn gap.

    The intended invariant the lowering layer wants to maintain is:

        While ``FutureExportEntry.resolve_obligation_count > 0`` and
        ``not resolved_sent``, exactly one resolver task is alive.

    The resolver task is the *only* code path that emits the
    ``ResolveMessage`` we owe the peer (see ``_enqueue_resolve_for``
    callers). Two code paths bump ``resolve_obligation_count``:

    1. ``_on_pull`` on incoming ``PullMessage`` — spawns a resolver on
       the 0→1 transition.
    2. The devaluator's ``_bump_await_future_obligation`` on outbound
       ``AwaitFutureExpr`` referencing one of our exports — currently
       does **not** spawn a resolver.

    The ``_on_pull`` gate (``if resolve_obligation_count > 1: return``)
    is a proxy for "a resolver is already running". The proxy is only
    correct if every 0→1 transition spawns a resolver. Because the
    devaluator path violates that, the proxy mis-fires: an inbound Pull
    that lands while the devaluator-only bump is still on the count
    sees ``ROC_before == 1``, bumps to 2, and skips spawning — but no
    resolver was ever running.

    This test races a devaluator bump (via an outbound
    ``RemoteIRHandle.dispatch`` with a pipelined outer lease whose
    future is local to us) with an inbound ``PullMessage`` injected
    directly through the transport, hitting the SKIP-SPAWN branch.
    With the bug, no ``ResolveMessage`` is ever enqueued and the test
    times out. With the invariant fixed (either spawn path 0→1, or
    track resolver liveness explicitly in the gate), the
    ``ResolveMessage`` lands.
    """

    async def test_devaluator_bump_alone_must_produce_resolve(self) -> None:
        """Cleanest statement of the invariant: A devalues an outbound
        ``AwaitFutureExpr`` referencing one of A's exports. Per the
        protocol contract (``FutureExportEntry.resolve_obligation_count``
        docstring: "each such expression implicitly pulls on the
        exporter"), A is now obligated to deliver a ``ResolveMessage``
        — regardless of whether the peer ever sends a ``PullMessage``.

        No Pull is injected in this test. We only:
        1. Bring up A's connection.
        2. Dispatch with a pipelined outer lease whose future is local
           to A. Devaluator bumps ROC 0→1.
        3. Assert A enqueues a ``ResolveMessage`` for F's export.
        4. Send the discards A needs to drain cleanly + EOF, so the
           connection CM exits without timing out.

        Under the bug, no resolver was spawned by the devaluator path
        and no ``ResolveMessage`` ever landed.
        """
        a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
        b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
        a_transport = StapledObjectStream(a_to_b_send, b_to_a_recv)

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            # Bootstrap A's connection. ``as_client`` mints a null main and
            # sends a Hello; we feed back a Hello so A is "connected".
            await b_to_a_send.send(HelloMessage())
            async with LeasedCapRPCConnection.as_client(a_transport) as conn:
                # Drain A's HelloMessage.
                first = await a_to_b_recv.receive()
                assert isinstance(first, HelloMessage)

                async with open_lifetime_task_group("A-future") as (_a_lt, a_tg):
                    # F's resolved value doesn't matter for this test;
                    # any small static suffices. We use ``None`` so
                    # devaluating the resolve doesn't introduce extra
                    # lease bookkeeping.
                    async def work_F() -> IRValue:
                        return cast(IRValue, None)

                    future_F, lease_F = spawn_ir_future_in_task_group(
                        name="F", work=work_F, task_group=a_tg
                    )

                    # Trigger the devaluator-side bump by dispatching with
                    # a pipelined outer lease. The dispatch sends a
                    # PushMessage carrying
                    # ``AwaitFutureExpr(ExportedIRFutureExpr(<future_F>),
                    # ExportedLeaseExpr(<grant_id>, ...))`` and
                    # ``_bump_await_future_obligation`` bumps
                    # ``resolve_obligation_count`` on F's fresh export
                    # entry 0→1.
                    echo_fut, echo_lease = conn.their_main_interface.dispatch(
                        cast(
                            "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                            UnwrapForPipelining(future_F, lease_F),
                        ),
                        Call(method="echo", arg="hi"),
                    )
                    # The dispatch returns a remote future + lease that A
                    # imports (positive IDs allocated by A and to be
                    # mirrored by the receiver). Discard the lease so the
                    # corresponding ``_active_grant_imports`` entry tears
                    # down at shutdown; the future stub is unused.
                    del echo_fut
                    echo_lease.discard()

                    # Consume A's outbound PushMessage and extract the
                    # export ids allocated for F + its grant.
                    push = await a_to_b_recv.receive()
                    assert isinstance(push, PushMessage)
                    push_expr = push.expression
                    assert isinstance(push_expr, HandleRequestExpr)
                    assert isinstance(push_expr.lease, AwaitFutureExpr)
                    assert isinstance(push_expr.lease.future, ExportedIRFutureExpr)
                    assert isinstance(push_expr.lease.lease, ExportedLeaseExpr)
                    f_export_id = push_expr.lease.future.future_export_id
                    f_grant_export_id = push_expr.lease.lease.grant_export_id

                    # Per the protocol, A owes a ResolveMessage for
                    # ``f_export_id``. Drain outbound and assert one
                    # shows up within a short timeout. Under the bug
                    # this drain blocks forever (no resolver was ever
                    # spawned).
                    with anyio.fail_after(2.0):
                        while True:
                            msg = await a_to_b_recv.receive()
                            if isinstance(msg, ResolveMessage) and (
                                msg.future_export_id == f_export_id
                            ):
                                break  # ✓ invariant holds

                    # Clean shutdown: simulate the peer discards needed
                    # so A's drain wait completes naturally rather than
                    # timing out. A holds two grant exports (the main
                    # grant introduced at handshake + the F-lease grant
                    # from the UnwrapForPipelining devaluation), each
                    # with introduction refcount 1.
                    await b_to_a_send.send(DiscardMessage(import_id=f_grant_export_id, refcount=1))
                    await b_to_a_send.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
                    # Close the peer's send half so A's reader sees EOF
                    # and the connection task group can unwind once drain
                    # completes.
                    await b_to_a_send.aclose()

    async def test_devaluator_spawned_resolver_cancelled_by_unpull(self) -> None:
        """Symmetry test: a resolver spawned via the devaluator path is cancelled
        by an inbound :class:`UnpullMessage`, just like a Pull-spawned resolver.

        Confirms that the unified ``_increment`` / ``_decrement`` primitives wire
        the cancel path correctly regardless of which call site originally bumped
        ROC 0→1. After the Unpull lands, no :class:`ResolveMessage` should be
        emitted for F (the resolver is cancelled before it can complete).
        """
        a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
        b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
        a_transport = StapledObjectStream(a_to_b_send, b_to_a_recv)

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            await b_to_a_send.send(HelloMessage())
            async with LeasedCapRPCConnection.as_client(a_transport) as conn:
                first = await a_to_b_recv.receive()
                assert isinstance(first, HelloMessage)

                async with open_lifetime_task_group("A-future") as (_a_lt, a_tg):
                    # F never resolves on its own — we want to observe
                    # that the resolver stays parked and gets cancelled
                    # by the Unpull. Set the event at the end of the
                    # test so work_F can exit cleanly.
                    never_resolve_event = anyio.Event()

                    async def work_F() -> IRValue:
                        await never_resolve_event.wait()
                        return cast(IRValue, None)

                    future_F, lease_F = spawn_ir_future_in_task_group(
                        name="F", work=work_F, task_group=a_tg
                    )
                    echo_fut, echo_lease = conn.their_main_interface.dispatch(
                        cast(
                            "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                            UnwrapForPipelining(future_F, lease_F),
                        ),
                        Call(method="echo", arg="hi"),
                    )
                    del echo_fut
                    echo_lease.discard()

                    push = await a_to_b_recv.receive()
                    assert isinstance(push, PushMessage)
                    push_expr = push.expression
                    assert isinstance(push_expr, HandleRequestExpr)
                    assert isinstance(push_expr.lease, AwaitFutureExpr)
                    assert isinstance(push_expr.lease.future, ExportedIRFutureExpr)
                    assert isinstance(push_expr.lease.lease, ExportedLeaseExpr)
                    f_export_id = push_expr.lease.future.future_export_id
                    f_grant_export_id = push_expr.lease.lease.grant_export_id

                    # Verify the resolver was actually spawned via the
                    # devaluator path: the cancel scope must be live on
                    # the export entry.
                    from fxtr2.rpc.lowering_state import FutureExportEntry

                    entry = conn._exports[f_export_id]
                    assert isinstance(entry, FutureExportEntry)
                    assert entry.resolve_obligation_count == 1
                    assert entry.resolve_task_cancel_scope is not None

                    # Send an UnpullMessage decrementing the obligation
                    # to zero; this must cancel the resolver and clear
                    # the cancel scope.
                    await b_to_a_send.send(UnpullMessage(future_import_id=f_export_id, refcount=1))
                    deadline = anyio.current_time() + 1.0
                    while entry.resolve_obligation_count != 0:
                        if anyio.current_time() > deadline:
                            raise AssertionError("UnpullMessage did not drive ROC to 0")
                        await anyio.sleep(0.01)
                    assert entry.resolve_task_cancel_scope is None

                    # Unblock work_F so the local future task can exit.
                    never_resolve_event.set()

                    # Drain a short window and assert no ResolveMessage
                    # for F.
                    with anyio.move_on_after(0.2):
                        while True:
                            msg = await a_to_b_recv.receive()
                            assert not (
                                isinstance(msg, ResolveMessage)
                                and msg.future_export_id == f_export_id
                            ), "resolver fired despite being cancelled by Unpull"

                    # Clean shutdown: peer-side discards so A drains.
                    await b_to_a_send.send(DiscardMessage(import_id=f_grant_export_id, refcount=1))
                    await b_to_a_send.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
                    await b_to_a_send.aclose()

    async def test_wait_for_devaluator_bump_alone_must_produce_resolve(self) -> None:
        """Same invariant as ``test_devaluator_bump_alone_must_produce_resolve``,
        but routes F through the ``wait_for`` slot of a
        :class:`WaitAndIgnoreFutureThen` instead of the dispatch's pipelined
        outer lease.

        The protocol contract on
        :attr:`FutureExportEntry.resolve_obligation_count` is "+1 per outbound
        ``AwaitFutureExpr`` the devaluator emits referencing this export" — *every*
        outbound ``AwaitFutureExpr``, not just those in the lease slot of
        :class:`HandleRequestExpr`. The receiver's symmetric rule
        (:attr:`FutureImportEntry.resolve_obligation_refcount`) does honor "every"
        uniformly via ``_phase1_walk`` → ``_phase1_bump_await_future_obligation``,
        so the wire shape is asymmetric: peer's refcount goes up, ours doesn't,
        peer skips its Pull, no resolver runs.
        """
        a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
        b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
        a_transport = StapledObjectStream(a_to_b_send, b_to_a_recv)

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            await b_to_a_send.send(HelloMessage())
            async with LeasedCapRPCConnection.as_client(a_transport) as conn:
                first = await a_to_b_recv.receive()
                assert isinstance(first, HelloMessage)

                async with open_lifetime_task_group("A-future") as (_a_lt, a_tg):

                    async def work_F() -> IRValue:
                        return cast(IRValue, None)

                    future_F, lease_F = spawn_ir_future_in_task_group(
                        name="F", work=work_F, task_group=a_tg
                    )

                    # Outer lease is a plain (non-pipelined) clone of A's
                    # main lease. F is routed through the wait_for slot
                    # via WaitAndIgnoreFutureThen — this is the only
                    # outbound AwaitFutureExpr referencing F.
                    echo_fut, echo_lease = conn.their_main_interface.dispatch(
                        conn.their_main_lease.clone(),
                        WaitAndIgnoreFutureThen(
                            wait_for=UnwrapForPipelining(future_F, lease_F),
                            then=Call(method="echo", arg="hi"),
                        ),
                    )
                    del echo_fut
                    echo_lease.discard()

                    push = await a_to_b_recv.receive()
                    assert isinstance(push, PushMessage)
                    push_expr = push.expression
                    assert isinstance(push_expr, HandleRequestExpr)
                    # Sanity: F appears in wait_for, not in the lease slot.
                    assert isinstance(push_expr.lease, ImportedLeaseExpr)
                    assert len(push_expr.wait_for) == 1
                    wait_entry = push_expr.wait_for[0]
                    assert isinstance(wait_entry.future, ExportedIRFutureExpr)
                    assert isinstance(wait_entry.lease, ExportedLeaseExpr)
                    f_export_id = wait_entry.future.future_export_id
                    f_grant_export_id = wait_entry.lease.grant_export_id

                    with anyio.fail_after(2.0):
                        while True:
                            msg = await a_to_b_recv.receive()
                            if isinstance(msg, ResolveMessage) and (
                                msg.future_export_id == f_export_id
                            ):
                                break  # ✓ invariant holds

                    await b_to_a_send.send(DiscardMessage(import_id=f_grant_export_id, refcount=1))
                    await b_to_a_send.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
                    await b_to_a_send.aclose()

    async def test_late_reference_to_already_resolved_export_is_noop(self) -> None:
        """Regression test: a second outbound ``AwaitFutureExpr`` referencing a
        future export whose ``ResolveMessage`` is already on the wire
        (``resolved_sent is True``) must be a silent no-op in
        ``_bump_await_future_obligation`` — no obligation bump, no resolver spawn.

        ``_increment_resolve_obligation_count``'s contract requires every caller
        to short-circuit on ``resolved_sent`` (asserting otherwise); ``_on_pull``
        has the guard, but the devaluator path was missing it, so the second
        dispatch below used to die with ``AssertionError("increment after
        resolved_sent: caller must short-circuit")``. The peer-side semantics of
        the no-op are sound: the peer evaluates the late ``AwaitFutureExpr``
        against the Resolve already delivered for that import.

        Shape of the original incident (workstation → wasm guest): a parked
        dispatch kept the connection's export tables warm so the resolved
        future export outlived its Resolve, and the next invoke's outbound
        expression referenced it again.
        """
        a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
        b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
        a_transport = StapledObjectStream(a_to_b_send, b_to_a_recv)

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            await b_to_a_send.send(HelloMessage())
            async with LeasedCapRPCConnection.as_client(a_transport) as conn:
                first = await a_to_b_recv.receive()
                assert isinstance(first, HelloMessage)

                async with open_lifetime_task_group("A-future") as (_a_lt, a_tg):

                    async def work_F() -> IRValue:
                        return cast(IRValue, None)

                    future_F, lease_F = spawn_ir_future_in_task_group(
                        name="F", work=work_F, task_group=a_tg
                    )
                    # Second credential for the late reference below, taken
                    # before the first dispatch consumes ``lease_F``.
                    lease_F_late = lease_F.clone()

                    # --- First dispatch: exports F, devaluator bumps ROC 0→1,
                    # the eager resolver emits the ResolveMessage.
                    echo_fut, echo_lease = conn.their_main_interface.dispatch(
                        cast(
                            "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                            UnwrapForPipelining(future_F, lease_F),
                        ),
                        Call(method="echo", arg="hi"),
                    )
                    del echo_fut
                    echo_lease.discard()

                    push = await a_to_b_recv.receive()
                    assert isinstance(push, PushMessage)
                    push_expr = push.expression
                    assert isinstance(push_expr, HandleRequestExpr)
                    assert isinstance(push_expr.lease, AwaitFutureExpr)
                    assert isinstance(push_expr.lease.future, ExportedIRFutureExpr)
                    assert isinstance(push_expr.lease.lease, ExportedLeaseExpr)
                    f_export_id = push_expr.lease.future.future_export_id
                    f_grant_export_id = push_expr.lease.lease.grant_export_id

                    with anyio.fail_after(2.0):
                        while True:
                            msg = await a_to_b_recv.receive()
                            if isinstance(msg, ResolveMessage) and (
                                msg.future_export_id == f_export_id
                            ):
                                break

                    # The export entry survives its Resolve (the peer hasn't
                    # discarded the future import) with ``resolved_sent`` set —
                    # the precondition for the regression.
                    from fxtr2.rpc.lowering_state import FutureExportEntry

                    entry = conn._exports[f_export_id]
                    assert isinstance(entry, FutureExportEntry)
                    assert entry.resolved_sent
                    assert entry.resolve_obligation_count == 0

                    # --- Second dispatch: late reference to the resolved
                    # export. Used to raise AssertionError out of the
                    # devaluator; must now devaluate cleanly.
                    echo_fut2, echo_lease2 = conn.their_main_interface.dispatch(
                        cast(
                            "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                            UnwrapForPipelining(future_F, lease_F_late),
                        ),
                        Call(method="echo", arg="hi again"),
                    )
                    del echo_fut2
                    echo_lease2.discard()

                    push2 = await a_to_b_recv.receive()
                    assert isinstance(push2, PushMessage)
                    push2_expr = push2.expression
                    assert isinstance(push2_expr, HandleRequestExpr)
                    assert isinstance(push2_expr.lease, AwaitFutureExpr)
                    assert isinstance(push2_expr.lease.future, ExportedIRFutureExpr)
                    assert isinstance(push2_expr.lease.lease, ExportedLeaseExpr)
                    # Same export entry is re-used for the late reference.
                    assert push2_expr.lease.future.future_export_id == f_export_id
                    f_grant_export_id_2 = push2_expr.lease.lease.grant_export_id

                    # The no-op contract: no obligation added. (Resolver
                    # respawn is excluded by the same observation — the spawn
                    # only happens on an ROC 0→positive crossing — plus the
                    # no-second-Resolve wire check below.
                    # ``resolve_task_cancel_scope`` is deliberately not
                    # inspected: its value is unspecified once
                    # ``resolved_sent`` is set.)
                    assert entry.resolve_obligation_count == 0

                    # And no second ResolveMessage shows up for F.
                    with anyio.move_on_after(0.2):
                        while True:
                            msg = await a_to_b_recv.receive()
                            assert not (
                                isinstance(msg, ResolveMessage)
                                and msg.future_export_id == f_export_id
                            ), "second ResolveMessage for an already-resolved export"

                    # Clean shutdown: discard the grant exports introduced by
                    # the two dispatches (the second may have re-used the first
                    # entry with a bumped introduction refcount) plus the main
                    # grant, then EOF so A's reader unwinds after drain.
                    if f_grant_export_id_2 == f_grant_export_id:
                        await b_to_a_send.send(
                            DiscardMessage(import_id=f_grant_export_id, refcount=2)
                        )
                    else:
                        await b_to_a_send.send(
                            DiscardMessage(import_id=f_grant_export_id, refcount=1)
                        )
                        await b_to_a_send.send(
                            DiscardMessage(import_id=f_grant_export_id_2, refcount=1)
                        )
                    await b_to_a_send.send(DiscardMessage(import_id=MAIN_GRANT_ID, refcount=1))
                    await b_to_a_send.aclose()
