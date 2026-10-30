"""Tests for FxtrID-level provenance flow through scope ops (Stage 9 / Stage 2).

These exercise the per-op behavior end-to-end:

- ``store_cbor`` projects all child IDs to provenance form (lifting
  pre-provenanced IDs, u-wrapping bare ones), stores the n-node, and
  returns a layered ``FxtrID(d_id, _provenance_parent=n_id)``.
- ``load_cbor`` / ``load`` rewraps children using
  ``arena.unwrap_provenance``, so deserialization handlers see layered
  refs that propagate the trace through sub-loads.
- ``is_tracked`` returns True iff both halves are tracked; ``abandon``
  releases both halves.
- ``lift`` / ``copy_from_scope`` / ``track_from_scope`` flow only the
  provenance parent — the arena's ``unwraps_to`` propagation pulls the
  data ID along.
- The ref cache is keyed on ``(data_id, provenance_parent)``: the same
  data ID under two different provenance traces caches separately and
  abandoning one trace does not invalidate the other.
"""

from __future__ import annotations

import pytest

from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope

# =============================================================================
# Helpers
# =============================================================================

GROUND = FxtrID.from_tag_and_payload(".", b"")


def _make_c_node(scope: FxtrScope, ctx_key: str = "test_ctx") -> FxtrID:
    """Build a c-node in ``scope``'s arena that unwraps to ``"."``."""
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    context_data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": ctx_key,
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), context_data)


def _store_simple(scope: FxtrScope, *, name: str = "test", value: int = 42) -> FxtrID:
    """Store a tiny d-node-shaped entity in ``scope`` and return its FxtrID.

    The cbor uses ``scope.get_context_marker_id()`` as the marker, so it
    flows through whichever path the marker dictates: bare ``.`` markers
    take the unprovenanced path; c-node-bearing markers go through the
    provenance branch.
    """
    builtin_type = scope.track_builtin(FxtrID.builtin("test_simple_entity"))
    cbor: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
        "$fxtr.type": RawWeakFxtrID(inner_id=builtin_type),
        "name": name,
        "value": value,
    }
    return scope.store_cbor(cbor)


# =============================================================================
# store_cbor / load_cbor round-trip
# =============================================================================


class TestStoreCborProvenanceBranch:
    """``store_cbor`` projects children and returns a layered FxtrID."""

    @pytest.mark.anyio
    async def test_bare_scope_returns_bare_id(self):
        """Without a provenance_context, store_cbor returns a bare data ID."""
        async with FxtrScope() as scope:
            stored = _store_simple(scope)
            assert stored.get_tag() == "d"
            assert stored._provenance_parent is None

    @pytest.mark.anyio
    async def test_provenance_scope_returns_layered_id(self):
        """A scope whose marker has a c-node prov parent yields a layered FxtrID.

        The data ID is a d-node; the provenance parent is an n-node.
        """
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                stored = _store_simple(scope)
                assert stored.get_tag() == "d"
                assert stored._provenance_parent is not None
                assert stored._provenance_parent.get_tag() == "n"
                # Both halves are tracked.
                assert scope.is_tracked(stored)

    @pytest.mark.anyio
    async def test_layered_id_unwraps_to_data_id(self):
        """The arena's ``unwraps_to`` chain links the n-node to the d-node."""
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                stored = _store_simple(scope)
                assert stored._provenance_parent is not None
                unwrapped = scope._main_arena.unwrap_provenance(
                    scope.clone_authorization(), stored._provenance_parent
                )
                assert unwrapped is not None
                # FxtrID equality is provenance-blind; the arena's
                # unwraps_to result is bare and matches our data ID.
                assert unwrapped == stored

    @pytest.mark.anyio
    async def test_two_stores_with_same_data_share_data_id(self):
        """Same data + same provenance scope → same layered FxtrID."""
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                a = _store_simple(scope, name="x", value=1)
                b = _store_simple(scope, name="x", value=1)
                assert a == b
                assert a._provenance_parent == b._provenance_parent

    @pytest.mark.anyio
    async def test_two_stores_with_different_provenance_share_data_id(self):
        """Same data, different provenance → same data ID, different prov parent."""
        async with FxtrScope() as builder:
            c_a = _make_c_node(builder, ctx_key="trace_a")
            c_b = _make_c_node(builder, ctx_key="trace_b")
            async with FxtrScope(
                provenance_context=c_a,
                provenance_context_from_scope=builder,
            ) as scope_a:
                async with FxtrScope(
                    provenance_context=c_b,
                    provenance_context_from_scope=builder,
                ) as scope_b:
                    a = _store_simple(scope_a, name="x", value=1)
                    b = _store_simple(scope_b, name="x", value=1)
                    # Same data ID (FxtrID equality is provenance-blind).
                    assert a == b
                    # But distinct provenance parents.
                    assert a._provenance_parent is not None
                    assert b._provenance_parent is not None
                    assert a._provenance_parent != b._provenance_parent


class TestLoadCborProvenanceBranch:
    """``load_cbor`` returns cbor with layered children when called with a layered ID."""

    @pytest.mark.anyio
    async def test_bare_load_returns_bare_children(self):
        """Loading a bare ID yields cbor with bare children."""
        async with FxtrScope() as scope:
            stored = _store_simple(scope)
            cbor = await scope.load_cbor(stored)
            assert isinstance(cbor, dict)
            ctx = cbor["$fxtr.ctx"]
            assert isinstance(ctx, RawFxtrEntityMarkerID)
            # The cbor's marker is bare ".".
            assert ctx.inner_id == GROUND
            assert ctx.inner_id._provenance_parent is None

    @pytest.mark.anyio
    async def test_layered_load_returns_layered_children(self):
        """Loading a layered ID yields cbor whose every child carries its prov parent."""
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                stored = _store_simple(scope)
                cbor = await scope.load_cbor(stored)
                assert isinstance(cbor, dict)
                ctx = cbor["$fxtr.ctx"]
                assert isinstance(ctx, RawFxtrEntityMarkerID)
                # The marker's data is "." and its prov parent is the c-node.
                assert ctx.inner_id == GROUND
                assert ctx.inner_id._provenance_parent == c_node
                # The type field is also layered (its prov parent is a u-node
                # because the type was a bare builtin when stored).
                type_field = cbor["$fxtr.type"]
                assert isinstance(type_field, RawWeakFxtrID)
                assert type_field.inner_id._provenance_parent is not None
                assert type_field.inner_id._provenance_parent.get_tag() == "u"

    @pytest.mark.anyio
    async def test_round_trip_preserves_provenance(self):
        """Store → load → re-store yields the same layered FxtrID."""
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                stored = _store_simple(scope, name="rt", value=7)
                cbor = await scope.load_cbor(stored)
                # Re-store the loaded cbor (same scope, same marker).
                restored = scope.store_cbor(cbor)
                assert restored == stored
                assert restored._provenance_parent == stored._provenance_parent


# =============================================================================
# is_tracked / abandon
# =============================================================================


class TestIsTrackedProvenance:
    """``is_tracked`` returns True iff both halves are tracked."""

    @pytest.mark.anyio
    async def test_layered_id_tracked_iff_both_halves_tracked(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                stored = _store_simple(scope)
                assert scope.is_tracked(stored)
                # Construct a fake layered ID whose data half isn't tracked.
                fake_data = FxtrID.from_tag_and_payload("d", b"x" * 32)
                fake_layered = FxtrID(
                    raw_data_id=fake_data._raw_data_id,
                    provenance_parent=stored._provenance_parent,
                )
                assert not scope.is_tracked(fake_layered)
                # Construct a fake layered ID whose prov half isn't tracked.
                fake_prov = FxtrID.from_tag_and_payload("n", b"y" * 32)
                fake_layered_2 = FxtrID(
                    raw_data_id=stored._raw_data_id, provenance_parent=fake_prov
                )
                assert not scope.is_tracked(fake_layered_2)


class TestAbandonProvenance:
    """``abandon`` releases both halves of a layered ID."""

    @pytest.mark.anyio
    async def test_abandon_releases_both_halves(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                stored = _store_simple(scope)
                assert stored._provenance_parent is not None
                assert scope.is_tracked(stored)
                scope.abandon(stored)
                # After abandon, neither the data ID nor the prov ID is tracked.
                assert not scope._main_arena.is_tracked(scope.clone_authorization(), stored)
                assert not scope._main_arena.is_tracked(
                    scope.clone_authorization(), stored._provenance_parent
                )


# =============================================================================
# lift / copy_from_scope / track_from_scope
# =============================================================================


class TestLiftProvenance:
    """``lift`` brings layered IDs into a child scope via the prov parent."""

    @pytest.mark.anyio
    async def test_lift_layered_id_from_parent(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as parent:
                stored = _store_simple(parent)
                async with FxtrScope() as child:
                    # Initially the child doesn't track the layered ID
                    # directly, but lift pulls it in.
                    child.lift(stored)
                    assert child.is_tracked(stored)


class TestCopyFromScopeProvenance:
    """``copy_from_scope`` round-trips a layered ID."""

    @pytest.mark.anyio
    async def test_copy_layered_id(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as source:
                stored = _store_simple(source)
                async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE]) as dest:
                    await dest.copy_from_scope(stored, source)
                    assert dest.is_tracked(stored)


class TestTrackFromScopeProvenance:
    """``track_from_scope`` round-trips a layered ID (sync transfer)."""

    @pytest.mark.anyio
    async def test_track_layered_id(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as source:
                stored = _store_simple(source)
                # Build a sibling dest with the same parents as source so
                # the transfer can resolve all inheritance bridges.
                async with FxtrScope() as dest:
                    dest.track_from_scope(stored, source)
                    assert dest.is_tracked(stored)


# =============================================================================
# Cache-key isolation across provenance traces
# =============================================================================


class TestProvenanceCacheKeys:
    """Cache is keyed on ``(data_id, provenance_parent)``."""

    @pytest.mark.anyio
    async def test_same_data_distinct_provenance_caches_independently(self):
        """The same data ID under two prov traces should occupy two cache slots."""
        async with FxtrScope() as builder:
            c_a = _make_c_node(builder, ctx_key="trace_a")
            c_b = _make_c_node(builder, ctx_key="trace_b")
            async with FxtrScope(
                provenance_context=c_a, provenance_context_from_scope=builder
            ) as scope_a:
                async with FxtrScope(
                    provenance_context=c_b, provenance_context_from_scope=builder
                ) as scope_b:
                    a = _store_simple(scope_a, name="dup", value=99)
                    b = _store_simple(scope_b, name="dup", value=99)
                    # Same data ID, different prov parents.
                    assert a == b
                    assert a._provenance_parent != b._provenance_parent
                    # Distinct cache slots in their respective scopes.
                    assert (a, a._provenance_parent) != (b, b._provenance_parent)

    @pytest.mark.anyio
    async def test_abandon_one_trace_doesnt_affect_the_other(self):
        async with FxtrScope() as builder:
            c_a = _make_c_node(builder, ctx_key="abandon_a")
            c_b = _make_c_node(builder, ctx_key="abandon_b")
            async with FxtrScope(
                provenance_context=c_a, provenance_context_from_scope=builder
            ) as scope_a:
                async with FxtrScope(
                    provenance_context=c_b, provenance_context_from_scope=builder
                ) as scope_b:
                    a = _store_simple(scope_a, name="dup", value=99)
                    b = _store_simple(scope_b, name="dup", value=99)
                    scope_a.abandon(a)
                    assert not scope_a.is_tracked(a)
                    # scope_b still tracks its own (data, prov_b) pair.
                    assert scope_b.is_tracked(b)


# =============================================================================
# Strip-mode scope must strip provenance from child IDs on store
# =============================================================================


class TestStripModeStripsLayeredChildren:
    """When a ``store_provenance="strip"`` scope stores cbor that
    contains layered child FxtrIDs, the children must be stripped to
    their bare data form before they're handed to the underlying
    arena. The scope is the abstraction barrier — arenas only ever see
    bare data ids per their wire-form invariant
    (``cap_ir_arena._encode_fxtr_id`` asserts bare-only).

    The asymmetry to guard against: the provenance-mode branch of
    ``store_cbor`` correctly projects every child via ``_project()``
    (bare → u-wrap; layered → strip layering and use the prov parent).
    The strip branch historically just passed the cbor straight
    through to ``arena.store``, so a layered child id ended up in the
    arena's ``requires`` set verbatim. That's what these tests
    exercise. The default ``"auto"`` mode would *preserve* layering via
    fallback-c-node minting, so this test pins ``store_provenance="strip"``
    explicitly.
    """

    @pytest.mark.anyio
    async def test_strip_scope_load_flat_returns_bare_deps_after_copy(self):
        """A strip-mode parent over a layered child: ``load_flat``'s deps
        must be all bare.

        Why this matters: the cap-IR-arena adapter (the cross-process
        wire bridge used by the persistence cap and any other remote
        arena consumer) takes ``load_flat``'s deps as seeds for a
        precomp-closure walk and asserts each id is bare (the
        arena-internal wire encoding doesn't carry provenance). When
        the scope's strip ``store_cbor`` leaks a layered child id into
        the parent's arena ``requires``, that layered id surfaces as a
        ``load_flat`` dep and breaks every downstream arena consumer
        that walks the deps.
        """
        async with FxtrScope(store_provenance="strip") as outer:
            assert outer.get_context_marker_id()._provenance_parent is None
            async with FxtrScope(isolated=True, include=[outer]) as builder:
                c_node = _make_c_node(builder, ctx_key="loadflat_test_ctx")
                async with FxtrScope(
                    provenance_context=c_node,
                    provenance_context_from_scope=builder,
                ) as prov_scope:
                    child_layered = _store_simple(prov_scope, name="lfchild", value=11)
                    assert child_layered._provenance_parent is not None
                    await outer.copy_from_scope(child_layered, prov_scope)

            parent_type = outer.track_builtin(FxtrID.builtin("loadflat_test_parent"))
            parent_cbor: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=outer.get_context_marker_id()),
                "$fxtr.type": RawWeakFxtrID(inner_id=parent_type),
                "child": RawWeakFxtrID(inner_id=child_layered),
            }
            parent_id = outer.store_cbor(parent_cbor)
            assert parent_id._provenance_parent is None

            # Fork: open a fresh strip-mode scope and copy the parent
            # into its arena. This mirrors what a cross-arena consumer
            # (persistence cap, /rpc bridge, etc.) does — it pulls
            # the entity into its own arena before walking it.
            async with FxtrScope(store_provenance="strip") as dst:
                await dst.copy_from_scope(parent_id, outer)

                # Ask the destination arena for the parent's flat
                # data. The arena's ``load_flat`` returns the
                # entity's bytes + dep list; every dep id MUST be
                # bare (per the arena's wire-form invariant) so
                # downstream consumers can encode them without
                # tripping the bare-only assertion.
                flat = await dst._main_arena.load_flat(dst.clone_authorization(), parent_id)
                layered_deps = [
                    dep_id for dep_id, _ in flat.deps if dep_id._provenance_parent is not None
                ]
                assert not layered_deps, (
                    "arena.load_flat returned layered FxtrIDs in deps — "
                    "downstream arena consumers (cap-IR wire bridge, "
                    "persistence cap) will fail to encode them. The "
                    "scope's bare-mode store_cbor should strip "
                    f"provenance before handing children to the arena. "
                    f"layered deps: {layered_deps!r}"
                )

                # And each non-builtin dep should be loadable
                # directly from this arena (since copy_from_scope
                # brought the parent's transitive data along). This
                # is what a downstream consumer does after walking
                # deps. ``.``-tagged (anonymous) and ``!``-tagged
                # (builtin) ids are contentless and can't be
                # ``load``-ed; skip them.
                for dep_id, _ in flat.deps:
                    if dep_id.get_tag() in (".", "!"):
                        continue
                    await dst._main_arena.load(dst.clone_authorization(), dep_id)


# =============================================================================
# Public create_c_node / create_p_node scope wrappers
# =============================================================================


class TestCreateProvenanceNodes:
    """The public ``scope.create_c_node`` / ``scope.create_p_node`` wrappers."""

    @pytest.mark.anyio
    async def test_create_c_node_returns_c_tagged_id(self):
        async with FxtrScope() as scope:
            ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
            context_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
                "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
                "value": RawWeakFxtrID(inner_id=GROUND),
            }
            c_id = scope.create_c_node(context_data)
            assert c_id.get_tag() == "c"

    @pytest.mark.anyio
    async def test_create_p_node_returns_p_tagged_id(self):
        async with FxtrScope() as scope:
            pathroot_type = scope.track_builtin(FxtrID.builtin("pathroot_type"))
            pathroot_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
                "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
                "value": RawWeakFxtrID(inner_id=GROUND),
            }
            p_id = scope.create_p_node(pathroot_data)
            assert p_id.get_tag() == "p"

    @pytest.mark.anyio
    async def test_create_p_node_raises_when_unauthorized(self):
        scope = FxtrScope()
        pathroot_data: FxtrCbor = {"value": RawWeakFxtrID(inner_id=GROUND)}
        with pytest.raises(Exception):
            scope.create_p_node(pathroot_data)
