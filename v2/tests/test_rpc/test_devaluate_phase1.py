"""Tests for the §2c devaluator + phase-1 substitution round-trip.

These exercise ``LeasedCapRPCConnection._devaluate`` paired with the peer's
``_phase1_substitute`` — walking an ``IRCallArg`` into an ``Expression[Any]``, sending
it over (logically; we call phase-1 directly in-process), and inspecting the resulting
``ConstantExpr`` tree on the receiver side.

Focus:
- Primitive / container passthrough.
- Fresh lease/handle/future/token exports and their re-export dedup semantics.
- Bounce-back handling for leases (take + defer via ``bounce_back_leases``).
- Bounce-back handling for handles / futures / tokens (pure ``Imported*Expr``).
- Rollback on invalid walk input: tables restored, backing lease discarded.
- IRTagged, UnwrapForPipelining passthrough.

We construct the two connections without entering their ``async with`` — the
devaluator and phase-1 substituter are both synchronous and don't need the reader /
writer / task group to be running. A small cleanup routine discards the main-interface
leases at the end of each test so weakref-finalize leak warnings don't spam the log.

Once the §2c_post1 refactor promotes ``devaluate`` / ``phase1_substitute`` to
module-level functions, these tests transfer over near-verbatim (the
``conn._devaluate(value)`` calls become ``devaluate(conn, value)``).
"""

from __future__ import annotations

import math
from typing import Any, cast

import anyio
import anyio.abc
import pytest
from anyio.streams.stapled import StapledObjectStream

from fxtr2.leases.lease_types import Lease, LeaseBrokenError
from fxtr2.leases.lease_util import open_lifetime_task_group
from fxtr2.leases.leased_cap_common import IRHandleLocalImplBase
from fxtr2.leases.leased_cap_ir import (
    Call,
    GetItem,
    IRCallArg,
    IRTagged,
    IRValue,
    UnwrapForPipelining,
)
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from fxtr2.rpc.lowering import (
    MAIN_GRANT_ID,
    MAIN_HANDLE_ID,
    MAIN_TOKEN_ID,
    ConstantExpr,
    FutureExportEntry,
    FutureImportEntry,
    GrantExportEntry,
    GrantImportEntry,
    HandleExportEntry,
    LeasedCapRPCConnection,
    RemoteGrant,
    RemoteIRFuture,
    RemoteLifetimeToken,
    TokenExportEntry,
    TokenImportEntry,
)
from fxtr2.rpc.lowering_types import (
    AwaitFutureExpr,
    DictExpr,
    ExportedIRFutureExpr,
    ExportedIRHandleExpr,
    ExportedLeaseExpr,
    ImportedIRFutureExpr,
    ImportedIRHandleExpr,
    ImportedLeaseExpr,
    ImportedLifetimeTokenExpr,
    IRTaggedExpr,
    ListExpr,
    RPCMessage,
    StaticExpr,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_transport_pair() -> (
    tuple[StapledObjectStream[RPCMessage], StapledObjectStream[RPCMessage]]
):
    """Two stapled in-memory streams wired as a bidirectional pair."""
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    return (
        StapledObjectStream(a_to_b_send, b_to_a_recv),
        StapledObjectStream(b_to_a_send, a_to_b_recv),
    )


def _make_conn_pair() -> tuple[LeasedCapRPCConnection[Any, Any], LeasedCapRPCConnection[Any, Any]]:
    """Construct two ``as_client`` connections without entering their async CMs.

    Both sides synthesize a null main interface internally (see ``as_client``); the
    constructor registers the three main-interface export/import entries and cache
    proxies — everything the devaluator/phase-1 tests need is in place after this
    returns. No reader/writer tasks are spawned.
    """
    a_t, b_t = _make_transport_pair()
    a = LeasedCapRPCConnection.as_client(a_t)
    b = LeasedCapRPCConnection.as_client(b_t)
    return a, b


def _cleanup_conn(conn: LeasedCapRPCConnection[Any, Any]) -> None:
    """Release the main-interface leases a connection's ``__init__`` took so test
    teardown doesn't emit weakref-finalize leak warnings.

    Discards ``their_main_lease_obj`` (our implicit import of the peer's main grant)
    and the backing lease on our main grant export (which auto-closes the null
    main-interface lifetime ``as_client`` created).
    """
    conn.their_main_lease.discard()
    entry = conn._exports.get(MAIN_GRANT_ID)
    if isinstance(entry, GrantExportEntry):
        entry.backing_lease.discard()


class _DummyHandle(IRHandleLocalImplBase[Any]):
    """Minimal concrete IRHandle for exercising handle-export paths.

    We never actually ``dispatch`` through it in these tests — the devaluator only
    inspects ``handle.token`` + ``id(handle)`` — so the ``_run_request_async`` body
    can stay unimplemented.
    """

    def __init__(self, token: Any, task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        del lease, request
        raise RuntimeError("_DummyHandle.dispatch not supposed to be called in tests")


# ---------------------------------------------------------------------------
# Primitives / containers / tagged / UnwrapForPipelining
# ---------------------------------------------------------------------------


class TestDevaluatePhase1Primitives:
    def test_primitives_pass_through(self) -> None:
        a, b = _make_conn_pair()
        try:
            value = {"x": 42, "y": [1, 2, 3], "z": None, "w": "hello", "b": True}
            expr, bb = a._devaluate(value)
            assert bb == []
            sub, _sub_obligations = b._phase1_substitute(expr)

            assert isinstance(sub, DictExpr)
            assert sub.items["x"] == StaticExpr(42)
            assert sub.items["z"] == StaticExpr(None)
            assert sub.items["w"] == StaticExpr("hello")
            assert sub.items["b"] == StaticExpr(True)
            y_expr = sub.items["y"]
            assert isinstance(y_expr, ListExpr)
            assert y_expr.elements == (StaticExpr(1), StaticExpr(2), StaticExpr(3))
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)

    def test_ir_tagged(self) -> None:
        a, b = _make_conn_pair()
        try:
            value = IRTagged(tag="mytag", value=42)
            expr, bb = a._devaluate(value)
            assert bb == []
            sub, _sub_obligations = b._phase1_substitute(expr)
            assert isinstance(sub, IRTaggedExpr)
            assert sub.tag == "mytag"
            assert sub.value == StaticExpr(42)
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)


# ---------------------------------------------------------------------------
# Fresh lease export and phase-1 reconstruction
# ---------------------------------------------------------------------------


class TestDevaluatePhase1FreshLeaseExport:
    def test_fresh_lease_export_round_trip(self) -> None:
        a, b = _make_conn_pair()
        try:
            with UnconditionalLocalLifetime[Any]("local").revoke_after_block() as lt:
                # Pass a clone so the lifetime stays alive under the
                # ``revoke_after_block`` scope's root lease even after devaluate takes
                # the original.
                expr, bb = a._devaluate(lt.root_lease.clone())
                assert bb == []

                assert isinstance(expr, ExportedLeaseExpr)
                grant_id = expr.grant_export_id
                token_id = expr.token_export_id
                assert grant_id < 0  # freshly allocated negative ID
                assert token_id < 0
                assert grant_id != token_id

                # A: export entries registered, reverse-lookup populated,
                # backing_lease taken.
                grant_entry = a._exports[grant_id]
                assert isinstance(grant_entry, GrantExportEntry)
                assert grant_entry.token_export_id == token_id
                assert grant_entry.outstanding_count == 1
                assert grant_entry.backing_lease.grant == lt.root_lease.grant

                assert isinstance(a._exports[token_id], TokenExportEntry)
                assert a._grant_export_id_by_grant[lt.root_lease.grant] == grant_id
                assert a._token_export_id_by_token[lt.token] == token_id

                # B: phase-1 substitute produces a ConstantExpr with a lease on a
                # fresh RemoteGrant at that grant_id, and registers the matching
                # import entries.
                sub, _sub_obligations = b._phase1_substitute(expr)
                assert isinstance(sub, ConstantExpr)
                b_lease = sub.value
                assert isinstance(b_lease, Lease)
                assert b_lease.grant == RemoteGrant(
                    b, grant_id, _token=b._get_or_create_token_proxy(token_id)
                )

                b_grant_entry = b._imports[grant_id]
                assert isinstance(b_grant_entry, GrantImportEntry)
                assert b_grant_entry.token_import_id == token_id
                assert b_grant_entry.introduction_refcount == 1

                b_token_entry = b._imports[token_id]
                assert isinstance(b_token_entry, TokenImportEntry)

                # Cleanup
                b_lease.discard()
                grant_entry.backing_lease.discard()
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)


# ---------------------------------------------------------------------------
# Bounce-back (imported-back-to-origin) handling
# ---------------------------------------------------------------------------


class TestDevaluatePhase1BounceBackLease:
    def test_bounce_back_emits_imported_lease_expr(self) -> None:
        a, b = _make_conn_pair()
        try:
            bouncing = a.their_main_lease.clone()
            expr, bb = a._devaluate(bouncing)
            assert isinstance(expr, ImportedLeaseExpr)
            assert expr.grant_import_id == MAIN_GRANT_ID
            assert len(bb) == 1
            assert bb[0].grant == RemoteGrant(
                a, MAIN_GRANT_ID, _token=a._get_or_create_token_proxy(MAIN_TOKEN_ID)
            )

            # Cleanup. Pretend the caller enqueued their message and now cleans up.
            for l in bb:
                l.discard()
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)

    def test_bounce_back_take_preserves_live_leases_count(self) -> None:
        """The ``take()`` substitution should keep ``live_leases`` at a constant count
        through the devaluate walk: the caller-provided lease is replaced by a clone,
        so the count never hits zero and no ``DiscardMessage`` trigger fires mid-walk.
        """
        a, b = _make_conn_pair()
        try:
            entry = a._imports[MAIN_GRANT_ID]
            assert isinstance(entry, GrantImportEntry)
            baseline = len(entry.live_leases)

            bouncing = a.their_main_lease.clone()
            assert len(entry.live_leases) == baseline + 1

            expr, bb = a._devaluate(bouncing)
            # take() added a clone AND discarded the original ``bouncing``, so the
            # count is unchanged across the devaluate call.
            assert len(entry.live_leases) == baseline + 1
            assert isinstance(expr, ImportedLeaseExpr)
            assert len(bb) == 1

            # After the caller's post-enqueue discards, count drops back to baseline.
            for l in bb:
                l.discard()
            assert len(entry.live_leases) == baseline
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)

    def test_bounce_back_phase1_clones_backing_lease_on_peer(self) -> None:
        a, b = _make_conn_pair()
        try:
            bouncing = a.their_main_lease.clone()
            expr, bb = a._devaluate(bouncing)
            sub, _sub_obligations = b._phase1_substitute(expr)
            assert isinstance(sub, ConstantExpr)
            b_lease = sub.value
            assert isinstance(b_lease, Lease)
            # The re-minted lease shares B's main-grant identity.
            b_main_entry = b._exports[MAIN_GRANT_ID]
            assert isinstance(b_main_entry, GrantExportEntry)
            assert b_lease.grant == b_main_entry.backing_lease.grant

            b_lease.discard()
            for l in bb:
                l.discard()
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)


class TestDevaluatePhase1BounceBackHandle:
    def test_bounce_back_handle(self) -> None:
        a, b = _make_conn_pair()
        try:
            expr, bb = a._devaluate(a.their_main_interface)
            assert bb == []
            assert isinstance(expr, ImportedIRHandleExpr)
            assert expr.import_id == MAIN_HANDLE_ID

            sub, _sub_obligations = b._phase1_substitute(expr)
            assert isinstance(sub, ConstantExpr)
            # Receiver sees its OWN local main-interface handle.
            entry = b._exports[MAIN_HANDLE_ID]
            assert isinstance(entry, HandleExportEntry)
            assert sub.value is entry.local_handle
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)


class TestDevaluatePhase1BounceBackToken:
    def test_bounce_back_token(self) -> None:
        a, b = _make_conn_pair()
        try:
            # Construct the proxy directly — ``RemoteGrant.subject`` is still a
            # stub from the scaffold, but that's unrelated to what this test is
            # exercising (identity-check in ``_devaluate_token``).
            their_main_token: Any = RemoteLifetimeToken(conn=a, token_import_id=MAIN_TOKEN_ID)

            expr, bb = a._devaluate(their_main_token)
            assert bb == []
            assert isinstance(expr, ImportedLifetimeTokenExpr)
            assert expr.import_id == MAIN_TOKEN_ID

            sub, _sub_obligations = b._phase1_substitute(expr)
            assert isinstance(sub, ConstantExpr)
            b_entry = b._exports[MAIN_TOKEN_ID]
            assert isinstance(b_entry, TokenExportEntry)
            assert sub.value is b_entry.local_token
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)


# ---------------------------------------------------------------------------
# Re-export dedup
# ---------------------------------------------------------------------------


class TestDevaluatePhase1ReExportDedup:
    def test_same_handle_sent_twice_uses_same_export_id(self) -> None:
        a, b = _make_conn_pair()
        try:

            async def _run() -> None:
                async with open_lifetime_task_group("local") as (lt, tg):
                    handle = _DummyHandle(lt.token, tg)
                    expr, bb = a._devaluate([handle, handle])
                    assert bb == []
                    assert isinstance(expr, ListExpr)
                    e1, e2 = expr.elements
                    assert isinstance(e1, ExportedIRHandleExpr)
                    assert isinstance(e2, ExportedIRHandleExpr)
                    # Same export id on both references.
                    assert e1.handle_export_id == e2.handle_export_id
                    assert e1.token_export_id == e2.token_export_id
                    # outstanding_count bumped once per reference → 2 total.
                    entry = a._exports[e1.handle_export_id]
                    assert isinstance(entry, HandleExportEntry)
                    assert entry.outstanding_count == 2

            anyio.run(_run)
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)

    def test_same_local_lease_sent_twice_uses_same_grant_id(self) -> None:
        """Two fresh leases on the same grant dedupe to a single ``GrantExportEntry``;
        only the first take lands in ``backing_lease``, subsequent references just bump
        ``outstanding_count``.
        """
        a, b = _make_conn_pair()
        try:
            with UnconditionalLocalLifetime[Any]("local").revoke_after_block() as lt:
                expr, bb = a._devaluate([lt.root_lease.clone(), lt.root_lease.clone()])
                assert bb == []
                assert isinstance(expr, ListExpr)
                e1, e2 = expr.elements
                assert isinstance(e1, ExportedLeaseExpr)
                assert isinstance(e2, ExportedLeaseExpr)
                assert e1.grant_export_id == e2.grant_export_id
                entry = a._exports[e1.grant_export_id]
                assert isinstance(entry, GrantExportEntry)
                assert entry.outstanding_count == 2
                entry.backing_lease.discard()
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


class TestDevaluatePhase1Rollback:
    def test_invalid_value_rolls_back_tables(self) -> None:
        """An unsupported value type in the middle of a list triggers ``TypeError``
        from the walker. Tables should be fully restored: every pre-call allocation is
        gone, every taken lease is discarded, ``_next_negative_export_id`` is reset.
        """
        a, b = _make_conn_pair()
        try:
            exports_before = dict(a._exports)
            grant_dict_before = dict(a._grant_export_id_by_grant)
            token_dict_before = dict(a._token_export_id_by_token)
            counter_before = a._next_negative_export_id

            with UnconditionalLocalLifetime[Any]("local").revoke_after_block() as lt:
                lease_to_send = lt.root_lease.clone()

                class _NotAnIRValue:
                    pass

                # Cast bypasses pyright's IRCallArg check — the test is exercising
                # exactly the runtime "unsupported type" fallback path in the walker.
                with pytest.raises(TypeError):
                    a._devaluate(cast(IRCallArg, [lease_to_send, _NotAnIRValue()]))

                assert a._exports == exports_before
                assert a._grant_export_id_by_grant == grant_dict_before
                assert a._token_export_id_by_token == token_dict_before
                assert a._next_negative_export_id == counter_before
                # The lease we passed in was taken and then discarded on rollback.
                assert not lease_to_send.is_active()
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)


# ---------------------------------------------------------------------------
# Operations: UnwrapForPipelining + IRFuture
# ---------------------------------------------------------------------------


class TestDevaluateHandleRequestPipelinedLease:
    """Substage 4: devaluate_handle_request accepts an
    ``UnwrapForPipelining`` in the ``handle_lease`` slot, encoding it as
    an ``AwaitFutureExpr`` in the resulting ``HandleRequestExpr.lease``
    slot. The ``_build_handle_request_expr`` already supported this
    shape; substage 4 just exposes it on the public devaluator API.
    """

    def test_pipelined_handle_lease_emits_await_future_in_lease_slot(self) -> None:
        from fxtr2.leases.local_lease import LocalLifetimeLease
        from fxtr2.rpc.lowering_devaluate import devaluate_handle_request
        from fxtr2.rpc.lowering_types import HandleRequestExpr

        a, b = _make_conn_pair()
        try:
            # Synthesize a future + grant import on `a` so we can build a
            # pipelined-lease wrapper referencing them.
            future_import_id, lease_import_id = 200, 201
            a._imports[future_import_id] = FutureImportEntry(
                import_id=future_import_id,
                token_import_id=MAIN_TOKEN_ID,
                introduction_refcount=1,
                resolve_obligation_refcount=0,
                local_consumer_count=0,
                resolution=None,
            )
            a._imports[lease_import_id] = GrantImportEntry(
                import_id=lease_import_id,
                token_import_id=MAIN_TOKEN_ID,
                introduction_refcount=1,
                live_leases={},
                wait_break_callbacks={},
                broken_reason=None,
            )
            token_proxy = a._get_or_create_token_proxy(MAIN_TOKEN_ID)
            future_proxy: Any = RemoteIRFuture(
                conn=a, future_import_id=future_import_id, _token=token_proxy
            )
            lease_proxy = LocalLifetimeLease(
                owner=a,
                grant=RemoteGrant(conn=a, grant_import_id=lease_import_id, _token=token_proxy),
            )
            handle_lease = UnwrapForPipelining(future=future_proxy, lease=lease_proxy)

            expr, bb = devaluate_handle_request(
                a,
                handle_import_id=MAIN_HANDLE_ID,
                handle_lease=handle_lease,
                request=Call(method="echo", arg=None),
            )
            assert isinstance(expr, HandleRequestExpr)
            # Lease slot is an AwaitFutureExpr referencing our synthetic
            # future + lease imports.
            assert isinstance(
                expr.lease, AwaitFutureExpr
            ), f"expected AwaitFutureExpr in lease slot, got {type(expr.lease).__name__}"
            assert isinstance(expr.lease.future, ImportedIRFutureExpr)
            assert expr.lease.future.import_id == future_import_id
            assert isinstance(expr.lease.lease, ImportedLeaseExpr)
            assert expr.lease.lease.grant_import_id == lease_import_id
            # The inner lease was a bounce-back so its clone is deferred.
            assert len(bb) == 1
            for l in bb:
                l.discard()
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)


class TestDevaluatePhase1UnwrapForPipelining:
    def test_unwrap_for_pipelining_emits_await_future_expr(self) -> None:
        """``UnwrapForPipelining`` in the IRCallArg becomes ``AwaitFutureExpr`` in the
        devaluated expression. Its ``future`` and ``lease`` fields recurse through the
        walker.
        """
        a, b = _make_conn_pair()
        try:
            # Use a's remote future-proxy to avoid needing a real local future. Set up
            # a synthetic import entry at a fresh positive ID so the proxy has
            # something valid to reference.
            future_import_id, lease_import_id = 100, 101
            a._imports[future_import_id] = FutureImportEntry(
                import_id=future_import_id,
                token_import_id=MAIN_TOKEN_ID,
                introduction_refcount=1,
                resolve_obligation_refcount=0,
                local_consumer_count=0,
                resolution=None,
            )
            a._imports[lease_import_id] = GrantImportEntry(
                import_id=lease_import_id,
                token_import_id=MAIN_TOKEN_ID,
                introduction_refcount=1,
                live_leases={},
                wait_break_callbacks={},
                broken_reason=None,
            )

            from fxtr2.leases.local_lease import LocalLifetimeLease

            token_proxy = a._get_or_create_token_proxy(MAIN_TOKEN_ID)
            future_proxy: Any = RemoteIRFuture(
                conn=a, future_import_id=future_import_id, _token=token_proxy
            )
            lease_proxy = LocalLifetimeLease(
                owner=a,
                grant=RemoteGrant(conn=a, grant_import_id=lease_import_id, _token=token_proxy),
            )
            unwrap = UnwrapForPipelining(future=future_proxy, lease=lease_proxy)

            expr, bb = a._devaluate(unwrap)
            assert isinstance(expr, AwaitFutureExpr)
            assert isinstance(expr.future, ImportedIRFutureExpr)
            assert expr.future.import_id == future_import_id
            assert isinstance(expr.lease, ImportedLeaseExpr)
            assert expr.lease.grant_import_id == lease_import_id
            # The lease inside the UnwrapForPipelining was a bounce-back; its clone is
            # in bounce_backs.
            assert len(bb) == 1
            for l in bb:
                l.discard()
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)


# ---------------------------------------------------------------------------
# Fresh future export (to cover _devaluate_future's weakref-finalizer path)
# ---------------------------------------------------------------------------


class TestDevaluatePhase1FreshFutureExport:
    def test_fresh_future_export_registers_entry(self) -> None:
        a, b = _make_conn_pair()
        try:

            async def _run() -> None:
                async with open_lifetime_task_group("local") as (_lt, tg):
                    from fxtr2.leases.leased_cap_common import spawn_ir_future_in_task_group

                    async def work() -> IRValue:
                        return 42

                    future, fut_lease = spawn_ir_future_in_task_group(
                        name="test-future", work=work, task_group=tg
                    )
                    try:
                        expr, bb = a._devaluate(future)
                        assert bb == []
                        assert isinstance(expr, ExportedIRFutureExpr)
                        future_id = expr.future_export_id
                        token_id = expr.token_export_id
                        assert future_id < 0
                        entry = a._exports[future_id]
                        assert isinstance(entry, FutureExportEntry)
                        assert entry.local_future is future
                        assert entry.outstanding_count == 1
                        assert entry.token_export_id == token_id
                        assert isinstance(a._exports[token_id], TokenExportEntry)
                        assert a._future_export_id_by_future_id[id(future)] == future_id

                        # Phase-1 reconstructs a RemoteIRFuture on B.
                        sub, _sub_obligations = b._phase1_substitute(expr)
                        assert isinstance(sub, ConstantExpr)
                        assert isinstance(sub.value, RemoteIRFuture)
                        assert sub.value.future_import_id == future_id
                    finally:
                        fut_lease.discard()

            anyio.run(_run)
        finally:
            _cleanup_conn(a)
            _cleanup_conn(b)


# ---------------------------------------------------------------------------
# Stage-13 teardown-crash regression tests (see
# v2/plans/phase3/stage13_output_channels/teardown_crash_investigation.md)
# ---------------------------------------------------------------------------


def _cleanup_conn_tolerant(conn: LeasedCapRPCConnection[Any, Any]) -> None:
    """Like :func:`_cleanup_conn`, but tolerant of a connection that
    already ran ``_abort`` (which discards its own table state) — used
    by the protocol-violation tests below."""
    import contextlib as _contextlib

    with _contextlib.suppress(Exception):
        conn.their_main_lease.discard()
    entry = conn._exports.get(MAIN_GRANT_ID)
    if isinstance(entry, GrantExportEntry):
        with _contextlib.suppress(Exception):
            entry.backing_lease.discard()


class TestPhase1BreakRaceTolerance:
    """A peer's reference to a grant export that was torn down by a
    *backing-lease break* must degrade request-scoped, not abort the
    connection. (Issue 1 in
    ``v2/plans/phase3/stage13_output_channels/teardown_crash_investigation.md``.)

    This is the deterministic, frozen form of the cross-wire race that
    crashed the demo server on ``close_session``: the owner side (A)
    breaks the grant's subject lifetime — ``on_break`` tears the export
    down (leaving a break tombstone) and enqueues a ``BreakMessage`` —
    while the holder side (B), which has not yet processed that message
    (no reader runs in this harness), bounces its still-live-from-its-view
    imported lease back in a request. Phase 1 consults the tombstones and
    substitutes an already-broken lease on the original grant — the same
    "state we no longer have" tolerance ``_on_pull`` / ``_on_discard``
    implement.
    """

    def test_bounce_back_after_owner_side_break_degrades_request_scoped(self) -> None:
        a, b = _make_conn_pair()
        try:
            # A exports a grant on a revocable lifetime; B imports it.
            with UnconditionalLocalLifetime[Any]("local").revoke_after_block() as lt:
                original_grant = lt.root_lease.grant
                expr, bb = a._devaluate(lt.root_lease.clone())
                assert bb == []
                assert isinstance(expr, ExportedLeaseExpr)
                grant_id = expr.grant_export_id
                sub, _ = b._phase1_substitute(expr)
                assert isinstance(sub, ConstantExpr)
                b_lease = sub.value
                assert isinstance(b_lease, Lease)
            # Lifetime revoked: A's on_break tore the export down (leaving a
            # break tombstone) and enqueued a BreakMessage B has not processed
            # (the frozen cross-wire window).
            assert grant_id not in a._exports
            assert grant_id in a._broken_grant_export_tombstones
            assert b_lease.is_active()  # B doesn't know yet

            # B bounces the lease back (the shape of an unsubscribe()
            # request's lease slot during the close cascade).
            back_expr, back_bb = b._devaluate(b_lease)
            try:
                assert isinstance(back_expr, ImportedLeaseExpr)
                assert back_expr.grant_import_id == grant_id

                # Request-scoped degradation — the substitute yields an
                # already-broken lease (the requester then observes the
                # LeaseBrokenError it already tolerates). It must NOT
                # abort the connection.
                res, _obligations = a._phase1_substitute(back_expr)
                assert isinstance(res, ConstantExpr)
                res_lease = res.value
                assert isinstance(res_lease, Lease)
                assert not res_lease.is_active()
                # Same grant identity as the torn-down export's backing lease
                # — observably "a lease that broke", not a lease on some
                # synthetic substitute lifetime — and it carries the original
                # break reason.
                assert res_lease.grant == original_grant
                with pytest.raises(LeaseBrokenError, match="block exit"):
                    res_lease.ensure_active()
                res_lease.discard()
            finally:
                for l in back_bb:
                    l.discard()
        finally:
            _cleanup_conn_tolerant(a)
            _cleanup_conn_tolerant(b)


class TestPhase1ViolationDiagnostics:
    """The phase-1 protocol-violation message names the referencing
    expression and the expected entry kind verbatim. (Issue 3 in
    ``v2/plans/phase3/stage13_output_channels/teardown_crash_investigation.md``;
    it used to build the name as ``f"Imported{kind.__name__[1:]}Expr"``
    — the ``[1:]`` expected a leading-underscore class name, so
    ``GrantExportEntry`` rendered as ``ImportedrantExportEntryExpr``.)

    Exercises the *never-had* case (an id that was never allocated),
    which legitimately stays connection-fatal even after the
    break-tombstone tolerance (issue 1 in the same doc, the test class
    above).
    """

    def test_missing_export_violation_names_the_entry_kind(self) -> None:
        from fxtr2.rpc.lowering_errors import RPCConnectionAborted

        a, b = _make_conn_pair()
        try:
            with pytest.raises(RPCConnectionAborted) as excinfo:
                a._phase1_substitute(ImportedLeaseExpr(grant_import_id=-9999))
            assert str(excinfo.value) == (
                "ImportedLeaseExpr references export id -9999,"
                " expected a GrantExportEntry: no such export"
            )
        finally:
            _cleanup_conn_tolerant(a)
            _cleanup_conn_tolerant(b)
