"""Tests for `fxtr2/services/cap_ir_arena.py` — adapter and arena.

Covers:

- `ArenaIRHandleAdapter` — server-side wrapper exposing a
  `ReadableArena` as an `IRHandle`.
- `CapIRArena` — client-side `ReadableArena` whose remote is a
  Cap-IR handle.
- The full task-1/2/3 acceptance test mirroring substage 3's
  acceptance test, but with cross-task communication routed through
  IRValue handoffs (handles + leases + tagged values).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Mapping, cast

import anyio
import anyio.abc
import pytest

from fxtr2.data.abstract_arena import LeaseGrantMismatchError
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawStrongFxtrID

# Imported at module level so the entity-type registration happens at
# pytest collection time (before the autouse `global_scope` fixture
# snapshots `initial_liveset`); otherwise `TaskNode`'s system entities
# would surface as a fresh leak whenever this test runs before the
# substage-3 sibling. See `tests/conftest.py:global_scope`.
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, EntityRef, FxtrScope
from fxtr2.data.memory_arena import MemoryArena
from fxtr2.leases.lease_bridges import (
    DependentLocalBridge,
    DependentPipelinedBridge,
    IndependentLocalBridge,
)
from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.leased_cap_common import IRHandleLocalImplBase
from fxtr2.leases.leased_cap_ir import (
    Call,
    GetItem,
    IRException,
    IRHandle,
    IRTagged,
    UnwrapForPipelining,
)
from fxtr2.leases.local_lease import (
    STATIC_LIFETIME,
    DerivedLocalLifetime,
    UnconditionalLocalLifetime,
)
from fxtr2.services.cap_ir_arena import (
    ArenaIRHandleAdapter,
    CapIRArena,
    _decode_flat_data,
    _decode_fxtr_id,
    _encode_fxtr_id,
    compute_and_track_precomp_closure,
)
from tests.test_entity_ref_scope.test_derived_scope_acceptance import TaskNode

pytestmark = pytest.mark.anyio


# =============================================================================
# Test helpers
# =============================================================================


def _store_simple(
    arena: MemoryArena[Any], lease: Lease[Any], ground_context: FxtrID, payload: str
) -> FxtrID:
    """Store a simple ground-context d-node carrying a string."""
    entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "data": payload,
    }
    return arena.store(lease.clone(), entity)


# =============================================================================
# ArenaIRHandleAdapter — read methods
# =============================================================================


class TestAdapterConstruction:
    def test_token_aliases_arena(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    ) -> None:
        arena, _ = memory_arena

        async def go() -> None:
            async with anyio.create_task_group() as tg:
                adapter = ArenaIRHandleAdapter(arena, task_group=tg)
                assert adapter.token is arena.token

        anyio.run(go)

    def test_non_local_token_rejected(self) -> None:
        from fxtr2.data.remote.transfer_entity_source import TransferEntitySource

        # TransferEntitySource has a STATIC_LIFETIME token (not a LocalLifetimeToken).
        source = TransferEntitySource({})

        async def go() -> None:
            async with anyio.create_task_group() as tg:
                with pytest.raises(TypeError):
                    ArenaIRHandleAdapter(source, task_group=tg)  # type: ignore[arg-type]

        anyio.run(go)


class TestAdapterLoadFlat:
    async def test_basic_load_flat(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """`load_flat` returns a wire-shaped Mapping with entity data + precomp."""
        arena, lease = memory_arena
        node_id = _store_simple(arena, lease, ground_context, "hello")

        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            result = await adapter.request(
                lease.clone(), Call("load_flat", _encode_fxtr_id(node_id))
            )
            assert isinstance(result, Mapping)
            flat, precomp = _decode_flat_data(result)
            assert flat.entity_id == node_id
            assert flat.entity_bytes is not None
            # The d-node has a $fxtr.ctx ref to the ground context, so deps
            # contain at least that one.
            dep_ids = {d for d, _ in flat.deps}
            assert ground_context in dep_ids
            # `d`-node has no precomp; ground "." is also a leaf — empty.
            assert list(precomp) == []

    async def test_load_flat_through_provenance(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """A c-node load_flat gets its precomp closure (unwraps_to + ctx_node)."""
        arena, lease = memory_arena
        # Build a c-node by lifting some context data into a c-id.
        ctx_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "value": RawStrongFxtrID(inner_id=ground_context),
            "label": "ctx",
        }
        c_id = arena.create_c_node(lease.clone(), ctx_data)

        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            result = await adapter.request(lease.clone(), Call("load_flat", _encode_fxtr_id(c_id)))
            assert isinstance(result, Mapping)
            flat, precomp_raw = _decode_flat_data(result)
            assert flat.entity_id == c_id
            # precomp closure should include c_id -> ctx_node, c_id -> unwraps_to
            entries = {_decode_fxtr_id(e["id"]): e for e in precomp_raw}
            assert c_id in entries
            assert "ctx_node" in entries[c_id]
            # unwraps_to may or may not be present depending on whether the
            # c-node has an explicit unwrap; it should at least be the "." node.
            assert "unwraps_to" in entries[c_id]


def _abandon_entry(node_id: FxtrID, count: int = 1) -> dict[str, Any]:
    """Build a wire-abandon entry for ``(node_id, count)``.

    Wire shape per ``part5bugfix1_plan.md`` Layer 3: each abandon arg
    is a Mapping with ``"id"`` (tagged FxtrID) and ``"count"`` (int —
    the introduction-refcount the client observed for this id).
    """
    return {"id": _encode_fxtr_id(node_id), "count": count}


class TestAdapterAbandon:
    async def test_abandon_removes_node(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        arena, lease = memory_arena
        node_id = _store_simple(arena, lease, ground_context, "doomed")
        assert arena.is_tracked(lease.clone(), node_id)

        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            # Register the introduction refcount this fake "client"
            # would observe in the wire packet (the existing arena
            # liveset, in our case just node_id and ground_context).
            adapter.register_emission([node_id, ground_context])
            result = await adapter.request(
                lease.clone(),
                Call("abandon", [_abandon_entry(node_id, count=1)]),
            )
            assert result is None
            assert not arena.is_tracked(lease.clone(), node_id)

    async def test_abandon_removes_multiple_nodes(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """Wire abandon takes a list — single batched call covers many IDs."""
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "a")
        n2 = _store_simple(arena, lease, ground_context, "b")

        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            adapter.register_emission([n1, n2, ground_context])
            result = await adapter.request(
                lease.clone(),
                Call("abandon", [_abandon_entry(n1), _abandon_entry(n2)]),
            )
            assert result is None
            assert not arena.is_tracked(lease.clone(), n1)
            assert not arena.is_tracked(lease.clone(), n2)

    async def test_abandon_malformed_arg(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    ) -> None:
        """Non-sequence arg is rejected (a bare tagged FxtrID, for instance)."""
        arena, lease = memory_arena
        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            with pytest.raises(IRException) as excinfo:
                await adapter.request(
                    lease.clone(),
                    Call("abandon", _encode_fxtr_id(FxtrID.builtin("x"))),
                )
            assert excinfo.value.category == "MalformedArenaPacket"


class TestAdapterErrorPaths:
    async def test_getitem_rejected(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    ) -> None:
        arena, lease = memory_arena
        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            with pytest.raises(IRException) as excinfo:
                await adapter.request(lease.clone(), GetItem(attr="anything"))
            assert excinfo.value.category == "GetItemUnsupported"

    async def test_unknown_method_rejected(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    ) -> None:
        arena, lease = memory_arena
        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            with pytest.raises(IRException) as excinfo:
                await adapter.request(lease.clone(), Call("not_a_real_method", b""))
            assert excinfo.value.category == "UnknownArenaMethod"

    async def test_method_path_sequence_rejected(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    ) -> None:
        """Adapter expects a string method, not a path."""
        arena, lease = memory_arena
        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            with pytest.raises(IRException) as excinfo:
                await adapter.request(lease.clone(), Call(["load_flat"], b""))
            assert excinfo.value.category == "UnknownArenaMethod"

    async def test_malformed_arg_load_flat(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    ) -> None:
        arena, lease = memory_arena
        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            # Wrong tag
            with pytest.raises(IRException) as excinfo:
                await adapter.request(
                    lease.clone(),
                    Call("load_flat", IRTagged("wrong_tag", b"")),
                )
            assert excinfo.value.category == "MalformedArenaPacket"


class TestAdapterLeaseHandling:
    async def test_input_lease_consumed(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """The lease passed to `request` must be discarded by the time it returns."""
        arena, lease = memory_arena
        node_id = _store_simple(arena, lease, ground_context, "x")

        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            input_lease = lease.clone()
            await adapter.request(input_lease, Call("load_flat", _encode_fxtr_id(node_id)))
            assert not input_lease.is_active()


# =============================================================================
# CapIRArena — client side
# =============================================================================


def _build_adapter_pair(
    memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    tg: anyio.abc.TaskGroup,
    initial_tracked: list[FxtrID] | None = None,
    initial_precomp: list[dict[str, Any]] | None = None,
) -> tuple[ArenaIRHandleAdapter[Any], CapIRArena[Any]]:
    """Build a pair of (server adapter, client CapIRArena.direct in mode 1)
    sharing the same arena token.

    `initial_tracked` defaults to whatever the underlying MemoryArena
    currently tracks (excluding the auto-tracked ground "." builtin).

    Mirrors the wire-packet builder by also registering the
    introduction refcount on the adapter (every id mentioned in
    ``initial_tracked`` ∪ ``initial_precomp`` keys/values contributes
    +1 to ``adapter._total_introductions``). Tests that bypass the
    real builders must do this explicitly so the
    :class:`CapIRArenaRemoteState` and :class:`ArenaIRHandleAdapter`
    counters stay in sync — see
    ``v2/plans/phase3/stage8c_user_caps/part5bugfix1_plan.md``.
    """
    arena, _ = memory_arena
    adapter = ArenaIRHandleAdapter(arena, task_group=tg)
    if initial_tracked is None:
        initial_tracked = list(arena._liveset())
    emission_ids: set[FxtrID] = set(initial_tracked)
    if initial_precomp is not None:
        from fxtr2.services.cap_ir_arena import collect_precomp_ids

        emission_ids |= collect_precomp_ids(initial_precomp)
    adapter.register_emission(emission_ids)
    capir = CapIRArena.direct(
        token=arena.token,
        remote=adapter,
        initial_tracked=initial_tracked,
        initial_precomp=initial_precomp or [],
    )
    return adapter, capir


class TestCapIRArenaConstruction:
    def test_token_aliases(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    ) -> None:
        """Mode 1: `z_token == x_token == y_token`."""
        arena, _ = memory_arena

        async def go() -> None:
            async with anyio.create_task_group() as tg:
                _, capir = _build_adapter_pair(memory_arena, tg)
                assert capir.token is arena.token
                capir.close()

        anyio.run(go)

    def test_validates_x_token_in_derived_inputs(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    ) -> None:
        """The `derived` factory rejects an `x_token` that isn't in
        `derived.inputs` — runtime check, since the type system can't
        catch this (input membership isn't part of `DerivedLocalLifetime`'s
        type)."""
        arena, _ = memory_arena

        async def go() -> None:
            async with anyio.create_task_group() as tg:
                adapter = ArenaIRHandleAdapter(arena, task_group=tg)
                stray = UnconditionalLocalLifetime("stray-x")
                # Derived's inputs include `arena.token` but not `stray.token`.
                derived = DerivedLocalLifetime(
                    "derived",
                    inputs={arena.token},
                )
                # Use unrelated `stray.token` as `x_token`.
                with pytest.raises(ValueError, match="x_token.*derived"):
                    CapIRArena.derived(
                        derived=derived,
                        x_token=stray.token,
                        # remote brand mismatches stray, but the runtime
                        # input check fires first.
                        remote=cast(Any, adapter),
                        initial_tracked=[],
                    )
                derived.revoke("test done")
                stray.revoke("test done")

        anyio.run(go)


class TestCapIRArenaIsTracked:
    async def test_initial_tracked_seeded(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "x")
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[n1, ground_context])
            assert capir.is_tracked(lease.clone(), n1)
            # The ground "." is also tracked.
            assert capir.is_tracked(lease.clone(), ground_context)
            # An unrelated id isn't.
            other = FxtrID.from_tag_and_payload("d", b"\xee" * 32)
            assert not capir.is_tracked(lease.clone(), other)
            capir.close()

    async def test_lease_subject_validated(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """A lease for a different token must be rejected."""
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[ground_context])
            stray = UnconditionalLocalLifetime("stray")
            # `_precheck` raises *before* the lease is taken, so the clone is
            # still active on the error path; the test discards it explicitly.
            stray_clone = stray.root_lease.clone()
            try:
                with pytest.raises(LeaseGrantMismatchError):
                    capir.is_tracked(stray_clone, ground_context)
            finally:
                stray_clone.discard()
            stray.revoke("test done")
            capir.close()


class TestCapIRArenaLoadFlat:
    async def test_load_flat_round_trip(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "hello")
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[n1])
            flat = await capir.load_flat(lease.clone(), n1)
            assert flat.entity_id == n1
            assert flat.entity_bytes is not None
            # ground_context is now in the local liveset (added via load_flat's
            # dep tracking).
            assert capir.is_tracked(lease.clone(), ground_context)
            capir.close()

    async def test_load_flat_unknown_id_rejected(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    ) -> None:
        arena, lease = memory_arena
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[])
            unknown = FxtrID.from_tag_and_payload("d", b"\x00" * 32)
            with pytest.raises(ValueError, match="not tracked"):
                await capir.load_flat(lease.clone(), unknown)
            capir.close()


class TestCapIRArenaProvenanceCache:
    async def test_unwrap_provenance_after_load_flat(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """Loading a c-node populates the provenance cache; subsequent
        `unwrap_provenance` is served locally."""
        arena, lease = memory_arena
        ctx_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "value": RawStrongFxtrID(inner_id=ground_context),
            "label": "ctx-cache",
        }
        c_id = arena.create_c_node(lease.clone(), ctx_data)

        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[c_id])
            await capir.load_flat(lease.clone(), c_id)
            # Now `unwrap_provenance` and `c_node_to_context_data` are
            # cache-served sync calls.
            uw = capir.unwrap_provenance(lease.clone(), c_id)
            assert uw is not None  # c-nodes unwrap to "."
            ctx = capir.c_node_to_context_data(lease.clone(), c_id)
            assert ctx is not None
            capir.close()

    async def test_c_node_to_context_data_miss_raises(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """`c_node_to_context_data` raises if the entry is not cached."""
        arena, lease = memory_arena
        # We track the ground node directly without ever load_flat-ing a c-node.
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[ground_context])
            with pytest.raises(ValueError, match="precomp cache"):
                # ground_context is a "." node, not a c-node, but the API still
                # checks the cache; this should miss.
                capir.c_node_to_context_data(lease.clone(), ground_context)
            capir.close()


class TestPrecompTracksOnServer:
    """Regression for the precomp/liveset divergence documented in
    ``v2/plans/phase3/stage8c_user_caps/part5bugfix1_plan.md``.

    The wire arena packet ships ``(tracked_ids, initial_precomp)``: the
    receiver seeds its local liveset with ``tracked_ids`` and seeds the
    sync provenance caches with ``initial_precomp``. The precomp may
    reference IDs *outside* ``tracked_ids`` — e.g. for a u-node in
    ``tracked_ids``, the precomp says "u(d) unwraps_to d".

    Invariant (c) in the plan: whatever the client thinks it has access
    to, the server's wrapped arena must currently track. The fix has
    three layers:

    1. Server-side: :func:`compute_and_track_precomp_closure` runs on
       the wire-packet's sub_arena fork (not the source), so every
       visited ID lands in the fork's liveset before the wire packet
       ships.
    2. Client-side: every value cited by a precomp entry contributes
       +1 to that value's shared refcount on
       :class:`CapIRArenaRemoteState`. ``_drop_to_zero`` cascades
       through those entries when an id is dropped, so an
       abandon never overshoots into precomp-cited ids.
    3. Defensive: client-side sync ops verify the result already has
       a refcount (a bookkeeping bug otherwise).
    """

    async def test_load_flat_for_precomp_value_succeeds(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """Mirror the workstation persistence-cap restart-durability flow at
        the cap_ir_arena layer, without RPC or services.

        1. Source arena tracks ``d`` and ``u(d)``.
        2. Build a wire packet for ``seed_ids = {u(d)}`` (only the prov
           parent, mimicking ``_arena_id`` on a returned layered FxtrID).
           Fork sub_arena. Run
           :func:`compute_and_track_precomp_closure` **on the fork**
           — that's the contract the wire-packet builders maintain so
           every visited ID lands in the fork's liveset.
        3. Wrap sub_arena in an adapter; build a CapIRArena with
           ``initial_tracked=[u(d)]`` and the precomp.
        4. Client sync-unwraps ``u(d) → d`` (precomp hit). ``d`` enters
           the client's liveset.
        5. Client calls ``load_flat(d)`` over the wire — the server's
           sub_arena tracks ``d`` via the closure walk, so the load
           succeeds.

        Regression for ``part5bugfix1_plan.md``: previously the closure
        ran on the source arena, so visited IDs landed in the source's
        liveset rather than the fork's, and the fork rejected wire
        ``load_flat`` for any precomp value.
        """
        arena, lease = memory_arena
        d_id = _store_simple(arena, lease, ground_context, "leaf")
        u_id = arena.create_u_node(lease.clone(), d_id)
        assert u_id.get_tag() == "u"

        # Mimic the wire packet builder in
        # ``service_cap_common._emit_call_result_packet`` /
        # ``entity_ref_scope.fork_arena_handle_for_ids``: fork over the
        # seed IDs, then walk the precomp closure **on the fork** so
        # every visited ID lands in the fork's liveset.
        seed_ids: list[FxtrID] = [u_id]
        sub_arena = arena.copy_to_new_arena(lease.clone(), seed_ids)
        # Pre-walk: the fork only has u_id, d_id is reachable but not
        # yet tracked.
        assert sub_arena.is_tracked(lease.clone(), u_id)
        assert not sub_arena.is_tracked(lease.clone(), d_id)

        precomp = compute_and_track_precomp_closure(sub_arena, lease.clone(), seed_ids)
        # Post-walk: the fork tracks both u_id (from copy) and d_id
        # (from the closure walk's auto-tracking).
        assert sub_arena.is_tracked(lease.clone(), u_id)
        assert sub_arena.is_tracked(lease.clone(), d_id)

        # The closure should mention u_id → d_id.
        precomp_ids = {_decode_fxtr_id(e["id"]) for e in precomp}
        assert u_id in precomp_ids

        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(sub_arena, task_group=tg)
            # Mirror the wire-packet introduction registration: the
            # union of seed_ids ∪ precomp keys/values bumps the
            # adapter's ``_total_introductions`` so the client-side
            # ``_remote_refcounts`` (seeded the same way at
            # construction) matches.
            adapter.register_emission(set(seed_ids) | {d_id, u_id})
            capir = CapIRArena.direct(
                token=sub_arena.token,
                remote=adapter,
                initial_tracked=seed_ids,
                initial_precomp=precomp,
            )
            try:
                # Client side: sync-unwrap u(d) → d via precomp. d
                # enters the client's local liveset.
                unwrapped = capir.unwrap_provenance(lease.clone(), u_id)
                assert unwrapped == d_id
                assert capir.is_tracked(lease.clone(), d_id)

                # Client side: load_flat(d). This dispatches a wire
                # request to the adapter; the adapter calls
                # sub_arena.load_flat(d). After the fix, sub_arena
                # tracks d (via the precomp closure pre-tracking
                # contract); without the fix, this raises "Node d…
                # is not tracked in the arena!".
                flat = await capir.load_flat(lease.clone(), d_id)
                assert flat.entity_id == d_id
                assert flat.entity_bytes is not None
            finally:
                capir.close()

    async def test_abandon_does_not_reclaim_id_with_recent_introduction(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """Layer 3: a wire ``"abandon"`` ships the introduction
        refcount the *client* observed; the server decrements its
        ``_total_introductions[id]`` by exactly that amount. If the
        server has emitted a more recent introduction for the same id
        that the client hadn't observed when constructing the
        abandon, the post-decrement counter stays > 0 and the id
        stays tracked.

        We simulate the race by stamping an extra introduction on
        the adapter after the client's last observation. The wire
        abandon ships the client's stale count; the server arithmetic
        leaves the id tracked.
        """
        arena, lease = memory_arena
        node_id = _store_simple(arena, lease, ground_context, "raced")

        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            adapter.register_emission([node_id])
            capir = CapIRArena.direct(
                token=arena.token,
                remote=adapter,
                initial_tracked=[node_id],
            )
            try:
                # Client thinks: "I've been introduced to node_id once."
                # _remote_refcounts[node_id] = 1.
                #
                # Race: the server has just emitted a second
                # introduction of node_id (e.g. via a different
                # outgoing arena packet) but the client hasn't
                # observed it yet. We simulate that by directly
                # bumping the adapter's counter.
                adapter.register_emission([node_id])
                # Server-side: _total_introductions[node_id] = 2.

                # Client now abandons. Wire ships count=1 (the only
                # introduction the client knows about).
                capir.abandon(lease.clone(), node_id)
                await anyio.wait_all_tasks_blocked()

                # Server post-decrement: 2 - 1 = 1. Id stays
                # tracked, the underlying arena was *not* abandoned.
                # This is what protects the receiver of the
                # not-yet-observed second introduction from a
                # use-after-free.
                assert arena.is_tracked(lease.clone(), node_id)
                assert adapter._total_introductions.get(node_id) == 1
            finally:
                # capir auto-closed on liveset drain above.
                pass

    async def test_precomp_value_keeps_id_alive_until_cited_by_disappears(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """Layer 2: a precomp entry citing ``d`` as a value contributes
        +1 to ``d``'s shared refcount. So an abandon of ``d`` from the
        client's liveset doesn't fire a wire abandon — the id stays
        tracked on the server until the precomp entry is also dropped.

        Once the cited-by id (``u(d)``) is also abandoned, the
        ``_drop_to_zero`` cascade removes the precomp entry, decrements
        ``d``'s refcount, and the wire abandon fires for both.
        """
        arena, lease = memory_arena
        d_id = _store_simple(arena, lease, ground_context, "leaf")
        u_id = arena.create_u_node(lease.clone(), d_id)

        seed_ids: list[FxtrID] = [u_id]
        sub_arena = arena.copy_to_new_arena(lease.clone(), seed_ids)
        precomp = compute_and_track_precomp_closure(sub_arena, lease.clone(), seed_ids)

        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(sub_arena, task_group=tg)
            adapter.register_emission(set(seed_ids) | {d_id, u_id})
            capir = CapIRArena.direct(
                token=sub_arena.token,
                remote=adapter,
                initial_tracked=seed_ids,
                initial_precomp=precomp,
            )
            try:
                # Sync-unwrap to bring d into the client's liveset
                # (this bumps d's refcount via _track_discovered, on
                # top of the +1 already there from the precomp entry).
                assert capir.unwrap_provenance(lease.clone(), u_id) == d_id
                # Now: refcount[d] = 1 (precomp value) + 1 (sibling
                # liveset) = 2.

                # Abandon d from the sibling: refcount drops to 1,
                # not zero. No wire abandon fires.
                capir.abandon(lease.clone(), d_id)
                await anyio.wait_all_tasks_blocked()
                assert sub_arena.is_tracked(
                    lease.clone(), d_id
                ), "precomp citation should keep d tracked on the server"
                assert sub_arena.is_tracked(lease.clone(), u_id)

                # Abandon u from the sibling: refcount[u] hits zero;
                # _drop_to_zero cascades through u's precomp entry and
                # decrements d's refcount → 0. Both fire in one batched
                # wire abandon.
                capir.abandon(lease.clone(), u_id)
                await anyio.wait_all_tasks_blocked()
                assert not sub_arena.is_tracked(lease.clone(), u_id)
                assert not sub_arena.is_tracked(lease.clone(), d_id)
            finally:
                # capir auto-closed when its liveset emptied above.
                pass


class TestCapIRArenaAbandon:
    async def test_abandon_removes_locally_and_remotely(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "doomed")
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[n1])
            assert capir.is_tracked(lease.clone(), n1)
            capir.abandon(lease.clone(), n1)
            assert not capir.is_tracked(lease.clone(), n1)
            # Server side: also no longer tracked (the wire abandon ran).
            # Drain pending tasks first.
            await anyio.wait_all_tasks_blocked()
            assert not arena.is_tracked(lease.clone(), n1)
            capir.close()


# =============================================================================
# Focused acceptance tests
# =============================================================================


class TestAcceptanceDirectMode:
    """Mode 1 end-to-end: server adapter + client CapIRArena, exercising
    `load_flat`, `unwrap_provenance` (cache hit), `is_tracked`, and
    `abandon` with a clean drain at the end."""

    async def test_full_workflow(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        arena, lease = memory_arena
        # Server-side: store a c-node + a referenced d-node so we have a
        # provenance chain for the precomp cache to populate.
        d_id = _store_simple(arena, lease, ground_context, "data")
        ctx_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "value": RawStrongFxtrID(inner_id=d_id),
            "label": "ctx",
        }
        c_id = arena.create_c_node(lease.clone(), ctx_data)

        async with anyio.create_task_group() as tg:
            adapter = ArenaIRHandleAdapter(arena, task_group=tg)
            # Mirror the wire-packet emission: every id we tell the
            # client about contributes +1 to the adapter's
            # ``_total_introductions``.
            adapter.register_emission([d_id, c_id])
            capir = CapIRArena.direct(
                token=arena.token,
                remote=adapter,
                initial_tracked=[d_id, c_id],
            )

            # is_tracked
            assert capir.is_tracked(lease.clone(), d_id)
            assert capir.is_tracked(lease.clone(), c_id)

            # load_flat populates the precomp cache.
            flat_c = await capir.load_flat(lease.clone(), c_id)
            assert flat_c.entity_id == c_id

            # unwrap_provenance now hits the cache.
            uw = capir.unwrap_provenance(lease.clone(), c_id)
            assert uw is not None  # c-node unwraps to "."

            # abandon: client-side liveset loses d_id, but the
            # precomp cache for c_id still cites d_id (via
            # ``unwraps_to``), which contributes +1 to d_id's shared
            # refcount per ``part5bugfix1_plan.md``. So no wire abandon
            # fires yet — d_id stays tracked on the server.
            capir.abandon(lease.clone(), d_id)
            assert not capir.is_tracked(lease.clone(), d_id)
            await anyio.wait_all_tasks_blocked()
            assert arena.is_tracked(lease.clone(), d_id)

            # Abandoning c_id releases its precomp entry; the cascade
            # decrements d_id's refcount → 0 and dispatches the wire
            # abandon for both ids in one batch.
            capir.abandon(lease.clone(), c_id)
            await anyio.wait_all_tasks_blocked()
            assert not arena.is_tracked(lease.clone(), c_id)
            assert not arena.is_tracked(lease.clone(), d_id)

            # capir auto-closes when its liveset empties.


class TestAcceptanceLocalForks:
    """Forks-of-forks all share one ``CapIRArenaRemoteState``. Local
    refcount bookkeeping is the only thing that distinguishes siblings;
    no wire dispatch happens for the fork itself."""

    async def test_nested_forks(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "level0")
        n2 = _store_simple(arena, lease, ground_context, "alt")

        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[n1, n2])

            f1 = capir.copy_to_new_arena(lease.clone(), [n1])
            assert isinstance(f1, CapIRArena)
            # Siblings share remote state; the parent's refcount for
            # ``n1`` was bumped by the local fork.
            assert f1._remote_state is capir._remote_state
            assert capir._remote_state._refcounts[n1] == 2
            assert capir._remote_state._refcounts[n2] == 1

            # Second fork — same remote state, third holder of ``n1``.
            f2 = f1.copy_to_new_arena(lease.clone(), [n1])
            assert f2._remote_state is capir._remote_state
            assert capir._remote_state._refcounts[n1] == 3

            # Both forks can serve load_flat through the shared remote.
            flat1 = await f1.load_flat(lease.clone(), n1)
            assert flat1.entity_id == n1
            flat2 = await f2.load_flat(lease.clone(), n1)
            assert flat2.entity_id == n1

            f2.close()
            assert capir._remote_state._refcounts[n1] == 2
            f1.close()
            assert capir._remote_state._refcounts[n1] == 1
            capir.close()

    async def test_close_defers_wire_abandon_until_next_release(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """``close()`` has no Z-lease, so wire abandons for IDs whose
        refcount it drops are queued in ``_pending_abandons`` and
        flushed by the next sibling's ``abandon()`` call (in the same
        batched wire abandon as that call's own zero-hits)."""
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "a")
        n2 = _store_simple(arena, lease, ground_context, "b")

        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[n1, n2])

            # Sibling holds only n2; capir holds n1 and n2.
            sibling = capir.copy_to_new_arena(lease.clone(), [n2])
            state = capir._remote_state
            assert state._refcounts == {n1: 1, n2: 2}

            # capir.close() has no Z-lease. n1 is unique to capir, so
            # its refcount drops to 0 — but wire abandon is queued, not
            # dispatched. n2 stays at 1 (sibling still holds it).
            capir.close()
            assert state._refcounts == {n2: 1}
            assert state._pending_abandons == [n1]
            # Server-side liveset still has n1 (queued abandon hasn't
            # been dispatched yet).
            assert arena.is_tracked(lease.clone(), n1)

            # Now sibling abandons n2 with a real lease. This dispatches
            # one batched wire abandon for {n1 (queued), n2 (zero this
            # call)}.
            sibling.abandon(lease.clone(), n2)
            await anyio.wait_all_tasks_blocked()
            assert not arena.is_tracked(lease.clone(), n1)
            assert not arena.is_tracked(lease.clone(), n2)
            assert state._pending_abandons == []


class _ExchangeForwardConverter(IRHandleLocalImplBase[Any]):
    """Server-side converter handle.

    Implements ``Call("convert", Sequence[Lease]) -> Lease[Y]`` by calling
    ``derived.exchange_forward({x_token_i: x_lease_i, ..., k_token: k_lease})`` —
    the canonical "issuer of Y mints fresh Y-leases for downstream
    consumers" pattern. The arg sequence is positional in the order
    declared by the constructor's ``x_tokens`` tuple. Used by both
    ``TestAcceptanceConverterPipelined`` (single-test mode-3 smoke
    check) and ``TestThreeTaskAcceptanceCapIR`` (full lifetime cascade).
    """

    def __init__(
        self,
        *,
        derived: DerivedLocalLifetime[Any],
        x_tokens: Sequence[Any],
        k_ull: UnconditionalLocalLifetime[Any],
        task_group: anyio.abc.TaskGroup,
        extra_minted_inputs: Mapping[Any, Callable[[], Lease[Any]]] | None = None,
    ) -> None:
        self._token = k_ull.token
        self._task_group = task_group
        self._derived = derived
        self._x_tokens = tuple(x_tokens)
        self._k_token = k_ull.token
        # Derived inputs not provided by the consumer's `x_leases` — minted
        # fresh per call. Used for never-ending inputs like GLOBAL_SCOPE.main,
        # which is a derived input under the uniform-derived model but is
        # authorized everywhere, so the converter can mint its own lease.
        self._extra_minted_inputs = dict(extra_minted_inputs or {})

    async def _run_request_async(self, lease: Lease[Any], request: Any) -> Any:
        if not isinstance(request, Call) or request.method != "convert":
            lease.discard()
            raise RuntimeError(f"unexpected converter request: {request}")
        x_leases = request.arg
        if not isinstance(x_leases, list) or len(x_leases) != len(self._x_tokens):
            lease.discard()
            for entry in x_leases if isinstance(x_leases, list) else ():
                if isinstance(entry, Lease):
                    entry.discard()
            raise RuntimeError(
                f"unexpected converter arg shape: expected {len(self._x_tokens)} "
                f"leases, got {x_leases!r}"
            )
        for entry in x_leases:
            assert isinstance(entry, Lease)
        # `lease` is the K-lease (outer dispatch authority); the entries
        # of `x_leases` are the base inputs in declared order.
        forward_inputs: dict[Any, Lease[Any]] = {self._k_token: lease}
        for tok, x_lease in zip(self._x_tokens, x_leases, strict=True):
            forward_inputs[tok] = x_lease
        for tok, mint in self._extra_minted_inputs.items():
            forward_inputs[tok] = mint()
        return self._derived.exchange_forward(forward_inputs)


def _make_irhandle_bridge(
    *,
    converter_handle: IRHandle[Any],
    k_lease: Lease[Any],
) -> DependentPipelinedBridge:
    """Build a :class:`DependentPipelinedBridge` for an IRHandle-backed converter.

    Wraps a single-input wire converter (``Call("convert", [base])``)
    into the bridge shape :meth:`CapIRArena.with_converter` expects.
    The converter callable clones the K-lease and dispatches the
    handle, returning ``UnwrapForPipelining`` so the wire pipelining
    threads through the remote arena dispatch (substage 4).

    Takes ownership of ``k_lease``; it ends up as the bridge's at-rest
    K entry (not a clone), so the arena's ``close()`` will release it.
    """
    k_token = k_lease.subject
    taken_k = k_lease.take()

    def bridge_converter(
        clones: dict[Any, Lease[Any] | UnwrapForPipelining[Lease[Any], Any]],
        base: Lease[Any],
    ) -> UnwrapForPipelining[Lease[Any], Any]:
        k_clone = clones.pop(k_token)
        # Discard any extras (the bridge might be merged with other
        # input snapshots in non-test contexts).
        for stray in clones.values():
            if isinstance(stray, UnwrapForPipelining):
                stray.lease.discard()
            else:
                stray.discard()
        if isinstance(k_clone, UnwrapForPipelining):
            raise TypeError("K-lease for an IRHandle bridge must be concrete")
        fut, fut_lease = converter_handle.dispatch(k_clone, Call("convert", [base]))
        return UnwrapForPipelining(fut, fut_lease)

    return DependentPipelinedBridge(leases={k_token: taken_k}, converter=bridge_converter)


class TestAcceptanceConverterPipelined:
    """Mode 3 end-to-end: a real `_ExchangeForwardConverter` bridges
    `(K, X)` leases into a fresh `Lease[Y]` via
    `DerivedLocalLifetime.exchange_forward`, and the remote arena
    dispatch is pipelined on the converter's eventual result.

    A focused single-test version of the converter half of
    `TestThreeTaskAcceptanceCapIR`. Useful when the full task-1/2/3
    test breaks and you want to isolate "is the wire chain working at
    all?" from "is the lifetime cascade working?".
    """

    async def test_converter_chain(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        arena_X, X_lease = memory_arena
        store = arena_X._store

        async with anyio.create_task_group() as tg:
            # ─── Server side: an arena under a derived Y = derive({X, K}) ──
            k_ull = UnconditionalLocalLifetime[Any]("converter-key")
            derived_Y = DerivedLocalLifetime[Any]("server-Y", inputs={X_lease.subject, k_ull.token})
            store.register_token(derived_Y.token, name="server-Y")
            y_lease = derived_Y.exchange_forward(
                {
                    X_lease.subject: X_lease.clone(),
                    k_ull.token: k_ull.root_lease.clone(),
                }
            )
            arena_Y = store.new_arena(y_lease.clone())
            arena_Y.track_builtin(y_lease.clone(), ground_context)
            ent_y = _store_simple(arena_Y, y_lease, ground_context, "Y-data")

            adapter_Y = ArenaIRHandleAdapter(arena_Y, task_group=tg)
            converter = _ExchangeForwardConverter(
                derived=derived_Y,
                x_tokens=(X_lease.subject,),
                k_ull=k_ull,
                task_group=tg,
            )

            # ─── Client side: a *distinct* derived Z over X only ──
            # The bridge holds K_lease internally, so Z's only input
            # is X (Z is purely the "we're alive" envelope; the bridge
            # handles the K plumbing).
            derived_Z = DerivedLocalLifetime[Any]("client-Z", inputs={X_lease.subject})
            bridge = _make_irhandle_bridge(
                converter_handle=converter,
                k_lease=k_ull.root_lease.clone(),
            )
            capir = CapIRArena.with_converter(
                derived=derived_Z,
                base_token=X_lease.subject,
                remote=adapter_Y,
                bridge=bridge,
                initial_tracked=[ent_y],
            )

            # ─── Exercise the chain: load_flat through Z → reverse → Y ────
            z_lease = derived_Z.exchange_forward({X_lease.subject: X_lease.clone()})
            flat = await capir.load_flat(z_lease, ent_y)
            assert flat.entity_id == ent_y
            assert flat.entity_bytes is not None

            # ─── Cleanup ──
            capir.close()  # discards the bridge's K-lease clone
            arena_Y.close()
            y_lease.discard()
            derived_Y.revoke("test done")
            derived_Z.revoke("test done")
            k_ull.revoke("test done")


# =============================================================================
# Full task-1/2/3 acceptance test (substage 5 mirror of substage 3)
# =============================================================================


class TestThreeTaskAcceptanceCapIR:
    """Substage-5 mirror of `tests/test_entity_ref_scope/test_derived_scope_acceptance.py`.

    Three concurrent tasks exchange Cap-IR-marshalled artifacts through
    in-process streams and exercise the same lifetime cascade story as
    substage 3 — but with `MemoryArena` direct refs and Python-callable
    converters replaced by their Cap-IR equivalents:

    - Cross-task arena handoff: `MemoryArena` → `ArenaIRHandleAdapter`
      (server) wrapped by `CapIRArena` (client).
    - Cross-task converter handoff: Python `(key, base) -> Lease[Y]`
      closure → an `IRHandle[K]` whose `Call("convert", x_lease)`
      runs `derived.exchange_forward(...)` on the server side.
    - The receiver builds a *local* sync `LifetimeConverter` for the
      memory store, mapping its own `(key, base)` to a *local*
      derived token D; the CapIRArena translates D to the remote Y via
      the IR-converter handle internally (mode 3, substage 4
      pipelining).

    The three lifetime-conversion patterns from substage 3's test
    show up unchanged:
    - scope2's attach of t1's arena → **case 3**.
    - scope1's attach of scope2's arena → **case 2**, with the
      CapIRArena's z_token = local derived D, memory-store-level
      converter local-only.
    - scope3's attach of scope2's arena → **case 1**, mode-1
      CapIRArena with z=x=y=scope2's token (Y dispatched once
      synchronously, then default converter).
    """

    @pytest.mark.anyio
    async def test_three_tasks_drain_in_lockstep(self) -> None:
        async def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
            with anyio.fail_after(timeout):
                while not predicate():
                    await anyio.sleep(0)

        scopes: dict[str, Any] = {}

        # Stream pairs.
        t1_to_t2_send, t1_to_t2_recv = anyio.create_memory_object_stream[Any](max_buffer_size=1)
        t2_to_t1_send, t2_to_t1_recv = anyio.create_memory_object_stream[Any](max_buffer_size=1)
        t2_to_t3_send, t2_to_t3_recv = anyio.create_memory_object_stream[Any](max_buffer_size=1)
        t1_to_t3_send, t1_to_t3_recv = anyio.create_memory_object_stream[Any](max_buffer_size=1)

        # Task 3 drives the timing: triggers task 1's drain.
        t1_may_drain = anyio.Event()

        # Outer task group used for adapter/converter task groups (must
        # outlive the scopes' tokens).
        async with anyio.create_task_group() as tg:

            async def task1() -> None:
                async with FxtrScope(name="t1", isolated=True, include=[GLOBAL_SCOPE]) as scope1:
                    scopes["scope1"] = scope1
                    t1_token: LifetimeToken[Any] = scope1._token

                    ent1 = TaskNode(children=(), value="from t1").store(scope=scope1)
                    t1_ent_id = ent1.entity_id

                    # Slice an arena view containing just t1's entity, wrap
                    # as an adapter for the wire.
                    arena_for_t2 = scope1._main_arena.copy_to_new_arena(
                        scope1.clone_authorization(), [t1_ent_id]
                    )
                    adapter_for_t2 = ArenaIRHandleAdapter(arena_for_t2, task_group=tg)
                    lease_for_t2_derived = scope1.clone_authorization()

                    await t1_to_t2_send.send((adapter_for_t2, lease_for_t2_derived, t1_ent_id))
                    await t1_to_t2_send.aclose()

                    # Receive task 2's bridge: B-key lease, IR-converter
                    # handle (B → maps A-leases to scope2-token leases),
                    # adapter for the new arena (under scope2's token), IDs.
                    (
                        key_lease_t1,
                        converter_handle_t1,
                        adapter_t1,
                        t2_ids,
                    ) = await t2_to_t1_recv.receive()

                    # Build a *local* derived D = derive({t1_token}) so we
                    # have a token of our own bounded by t1's lifetime.
                    # The CapIRArena's z_token is D; the bridge holds
                    # K_t1 internally and dispatches the wire converter
                    # on every Z→Y conversion.
                    derived_D = DerivedLocalLifetime[Any](
                        "task1-derived-D",
                        inputs={t1_token},
                    )
                    bridge_t1 = _make_irhandle_bridge(
                        converter_handle=converter_handle_t1,
                        k_lease=key_lease_t1.take(),
                    )
                    capir_t1 = CapIRArena.with_converter(
                        derived=derived_D,
                        base_token=t1_token,
                        remote=adapter_t1,
                        bridge=bridge_t1,
                        initial_tracked=t2_ids,
                    )

                    # Local sync LifetimeConverter for the memory store:
                    # `(key=STATIC, base=Lease[A]) -> Lease[D]` via
                    # local exchange_forward. K_t1 lives inside the
                    # bridge now, so the row's key is just STATIC.
                    a_token = t1_token

                    def t1_local_converter(key: Lease[Any], base: Lease[Any]) -> Lease[Any]:
                        key.discard()  # STATIC, unused
                        return derived_D.exchange_forward({a_token: base})

                    # Lazily attach each of task 2's new entities into
                    # scope1's main arena, bridging via the local converter.
                    # Wrap as a DependentLocalBridge with empty inputs.
                    def t1_dep_converter(
                        inputs: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
                    ) -> Lease[Any]:
                        for v in inputs.values():
                            v.discard()
                        return t1_local_converter(STATIC_LIFETIME.lease(), base)

                    for new_id in t2_ids:
                        scope1._main_arena.lazily_attach_from_arena(
                            scope1.clone_authorization(),
                            new_id,
                            capir_t1,
                            DependentLocalBridge(leases={}, converter=t1_dep_converter),
                        )

                    # Loading each new entity transitively reaches t1's
                    # original entity via its child reference.
                    for new_id in t2_ids:
                        loaded = await scope1.load(EntityRef(new_id, TaskNode))
                        assert loaded.value.startswith("from t2")
                        assert len(loaded.children) == 1
                        assert loaded.children[0].entity_id == t1_ent_id

                    # Hand task 3 a clone of our main lease.
                    await t1_to_t3_send.send(scope1.clone_authorization())
                    await t1_to_t3_send.aclose()

                    # capir_t1's close() discards the bridge's K_t1
                    # clone — that's our only remaining stake in K_t1.
                    capir_t1.close()

                    # Wait for task 3 to release t1's lease before draining.
                    await t1_may_drain.wait()

                # Scope1 has now drained. derived_D's input ended (t1_token),
                # so its output token closed too — no explicit cleanup
                # needed.

            async def task2() -> None:
                (
                    adapter_from_t1,
                    lease_for_derived,
                    t1_ent_id,
                ) = await t1_to_t2_recv.receive()
                t1_token: LifetimeToken[Any] = lease_for_derived.subject

                # scope2's lifetime derives from t1_token — an *external* token
                # (arrived over a stream; no FxtrScope object). The entry
                # primitive expresses this via ``extra_derived_leases``;
                # GLOBAL_SCOPE is an ordinary parent (never ends).
                scope2 = FxtrScope(name="t2", isolated=True, include=[GLOBAL_SCOPE])
                try:
                    scopes["scope2"] = scope2
                    async with scope2.open_with_explicit_leases(
                        parent_leases={GLOBAL_SCOPE: GLOBAL_SCOPE.clone_authorization()},
                        extra_derived_leases={t1_token: lease_for_derived},
                    ):
                        derived = scope2._derived
                        assert derived is not None
                        assert scope2._key_ull is not None
                        key_token = scope2._key_ull.token
                        # Wrap t1's adapter as a mode-1 CapIRArena
                        # (z=x=y=t1_token) so we can use it as a source
                        # for `lazily_attach_from_arena`.
                        capir_from_t1 = CapIRArena.direct(
                            token=t1_token,
                            remote=adapter_from_t1,
                            initial_tracked=[t1_ent_id],
                        )

                        # Case-3 attach: scope2's token is derived from
                        # t1_token, so a live Lease[scope2_token]
                        # structurally witnesses t1 alive. Use
                        # exchange_reverse to extract the t1-lease.
                        def t2_attach_converter(key: Lease[Any], base: Lease[Any]) -> Lease[Any]:
                            key.discard()
                            inputs = derived.exchange_reverse(base)
                            inputs[key_token].discard()
                            # GLOBAL_SCOPE is a derived input of scope2 now; drop
                            # its via too (only the t1 via is returned).
                            inputs[GLOBAL_SCOPE.scope_token].discard()
                            return inputs[t1_token]

                        def t2_dep_converter(
                            inputs: dict[LifetimeToken[Any], Lease[Any]],
                            base: Lease[Any],
                        ) -> Lease[Any]:
                            for v in inputs.values():
                                v.discard()
                            return t2_attach_converter(STATIC_LIFETIME.lease(), base)

                        scope2._main_arena.lazily_attach_from_arena(
                            scope2.clone_authorization(),
                            t1_ent_id,
                            capir_from_t1,
                            DependentLocalBridge(leases={}, converter=t2_dep_converter),
                        )

                        # Store new entities, each referencing t1's original.
                        new_refs: list[Any] = []
                        for i in range(2):
                            ref = TaskNode(
                                children=(EntityRef(t1_ent_id, TaskNode),),
                                value=f"from t2 #{i}",
                            ).store(scope=scope2)
                            new_refs.append(ref)
                        new_ids = [r.entity_id for r in new_refs]

                        # Two arena slices, one per recipient. Each gets
                        # wrapped as an `ArenaIRHandleAdapter` for the wire.
                        new_arena_a = scope2._main_arena.copy_to_new_arena(
                            scope2.clone_authorization(), new_ids
                        )
                        new_arena_b = scope2._main_arena.copy_to_new_arena(
                            scope2.clone_authorization(), new_ids
                        )
                        adapter_a = ArenaIRHandleAdapter(new_arena_a, task_group=tg)
                        adapter_b = ArenaIRHandleAdapter(new_arena_b, task_group=tg)

                        # Build the IR-converter handle: `Call("convert",
                        # x_lease)` under k_lease → Lease[scope2_token]
                        # via `derived.exchange_forward`.
                        converter_handle = _ExchangeForwardConverter(
                            derived=derived,
                            x_tokens=(t1_token,),
                            k_ull=scope2._key_ull,
                            task_group=tg,
                            # GLOBAL_SCOPE is a derived input of scope2 but the
                            # consumer doesn't supply it — mint it per call.
                            extra_minted_inputs={
                                GLOBAL_SCOPE.scope_token: GLOBAL_SCOPE.clone_authorization
                            },
                        )

                        await t2_to_t1_send.send(
                            (
                                scope2._current_key_lease(),
                                converter_handle,
                                adapter_a,
                                new_ids,
                            )
                        )
                        await t2_to_t1_send.aclose()
                        await t2_to_t3_send.send(
                            (
                                scope2._current_key_lease(),
                                converter_handle,
                                adapter_b,
                                new_ids,
                            )
                        )
                        await t2_to_t3_send.aclose()
                        # capir_from_t1 stays referenced by scope2's
                        # foreign-lease entry for t1_ent_id; it'll be
                        # released when scope2's token-end callback
                        # discards the foreign-lease record.
                        # Falls through: scope2's CM exit waits for the
                        # derived output to drain.
                except BaseException:
                    scope2.force_close()
                    raise

            async def task3() -> None:
                (
                    key_lease_t3,
                    converter_handle_t3,
                    adapter_t3,
                    t2_ids,
                ) = await t2_to_t3_recv.receive()
                lease_from_t1 = await t1_to_t3_recv.receive()

                # ─── Prediction 1: mid-run invariants ────────────────────
                scope1 = scopes["scope1"]
                scope2 = scopes["scope2"]
                assert scope2._key_ull is not None
                assert not scope1._token.ended()
                assert not scope2._token.ended()
                assert not scope2._key_ull.token.ended()

                # Mint Lease[scope2_token] by dispatching the IR-converter.
                # Same role as substage 3's `converter_t3(key, base)` —
                # but now it's a Cap-IR dispatch (await the future).
                async def _mint_y_lease() -> Lease[Any]:
                    fut, fut_lease = converter_handle_t3.dispatch(
                        key_lease_t3.clone(),
                        Call("convert", [lease_from_t1.clone()]),
                    )
                    try:
                        result = await fut.wait(fut_lease.clone())
                    finally:
                        fut_lease.discard()
                    assert isinstance(result, Lease)
                    return result

                t2_output_lease_for_attach = await _mint_y_lease()
                t2_output_lease_extra = await _mint_y_lease()

                async with FxtrScope(name="t3", isolated=True, include=[GLOBAL_SCOPE]) as scope3:
                    # Wrap t2's adapter as a mode-1 CapIRArena (Y == Z).
                    capir_t3 = CapIRArena.direct(
                        token=adapter_t3.token,
                        remote=adapter_t3,
                        initial_tracked=t2_ids,
                    )

                    # Case 1 attach: store an IndependentLocalBridge holding
                    # Lease[scope2_token] directly.
                    scope3._main_arena.lazily_attach_from_arena(
                        scope3.clone_authorization(),
                        t2_ids[0],
                        capir_t3,
                        IndependentLocalBridge(lease=t2_output_lease_for_attach),
                    )
                    loaded = await scope3.load(EntityRef(t2_ids[0], TaskNode))
                    assert loaded.value.startswith("from t2")

                    # capir_t3 will be released when scope3's foreign-lease
                    # cleanup runs at token-end.

                # scope3 has drained. The forked arena underneath adapter_t3
                # is GC'd (Python ref dropped); MemoryStore's existing
                # finalize reclaims its liveset row.

                # ─── Prediction 2: drop our extra t2 output-lease clone ──
                # Nothing should close.
                t2_output_lease_extra.discard()
                assert not scope1._token.ended()
                assert not scope2._token.ended()
                assert not scope2._key_ull.token.ended()

                # ─── Prediction 3: drop lease_from_t1 → scope1 + scope2 close ─
                lease_from_t1.discard()
                t1_may_drain.set()

                await _wait_for(lambda: scope1._token.ended() and scope2._token.ended())
                assert scope1._token.ended()
                assert scope2._token.ended()
                # Key ULL still dangles — task 3 still holds key_lease_t3.
                assert not scope2._key_ull.token.ended()

                # ─── Prediction 4: drop key_lease_t3 → key ULL ends ──────
                key_ull = scope2._key_ull
                assert key_ull is not None
                key_lease_t3.discard()
                await _wait_for(lambda: key_ull.token.ended())
                assert key_ull.token.ended()

            tg.start_soon(task1)
            tg.start_soon(task2)
            tg.start_soon(task3)


class TestCapIRArenaCopyToNewArena:
    async def test_fork_returns_capir_arena(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """`copy_to_new_arena` returns a CapIRArena (fork-form) seeded with
        the requested IDs."""
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "a")
        n2 = _store_simple(arena, lease, ground_context, "b")
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[n1, n2])
            forked = capir.copy_to_new_arena(lease.clone(), [n1])
            assert isinstance(forked, CapIRArena)
            assert forked.token is capir.token
            # Liveset seeded only with n1 (not n2).
            assert forked.is_tracked(lease.clone(), n1)
            assert not forked.is_tracked(lease.clone(), n2)
            forked.close()
            capir.close()

    async def test_fork_load_flat_pipelined(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """Dispatching `load_flat` on a fork-form arena uses
        `pipeline_dispatch` against the future and produces a valid
        result without ever waiting synchronously on the fork to
        materialise."""
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "pipelined")
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[n1])
            forked = capir.copy_to_new_arena(lease.clone(), [n1])
            flat = await forked.load_flat(lease.clone(), n1)
            assert flat.entity_id == n1
            forked.close()
            capir.close()

    async def test_fork_of_fork_composes(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """A second `copy_to_new_arena` against a fork-form arena pipelines
        on top of the existing future."""
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "nested")
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[n1])
            f1 = capir.copy_to_new_arena(lease.clone(), [n1])
            f2 = f1.copy_to_new_arena(lease.clone(), [n1])
            # Nested fork can still load through the pipelined chain.
            flat = await f2.load_flat(lease.clone(), n1)
            assert flat.entity_id == n1
            f2.close()
            f1.close()
            capir.close()

    async def test_fork_untracked_id_rejected(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        """Forking with an unknown ID raises before dispatching."""
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "valid")
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[n1])
            unknown = FxtrID.from_tag_and_payload("d", b"\xfe" * 32)
            fork_lease = lease.clone()
            try:
                with pytest.raises(ValueError, match="not tracked"):
                    capir.copy_to_new_arena(fork_lease, [unknown])
            finally:
                # On error, the lease is taken inside the `with` block
                # (precheck passes — only the loop validation fails after
                # `lease.take()`), so the with-exit handles cleanup. No
                # extra discard needed.
                pass
            capir.close()


class TestCapIRArenaClose:
    async def test_close_clears_state(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ) -> None:
        arena, lease = memory_arena
        n1 = _store_simple(arena, lease, ground_context, "x")
        async with anyio.create_task_group() as tg:
            _, capir = _build_adapter_pair(memory_arena, tg, initial_tracked=[n1])
            assert capir.is_tracked(lease.clone(), n1)
            capir.close()
            # After close, ``is_tracked`` returns False (mirrors
            # ``MemoryArena.is_tracked`` — closed arenas trivially track
            # nothing). Other ops raise from ``_precheck``.
            assert not capir.is_tracked(lease.clone(), n1)
            # Idempotent.
            capir.close()
