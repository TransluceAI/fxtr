"""Tests for per-scope context entity (Stage 9 / Stage 1).

Covers ``FxtrScope.provenance_context``, ``provenance_context_from_scope``,
and the helper methods ``extract_provenance_parent`` /
``embed_as_provenance_parent``. Storage / load / lift behavior under
provenance still uses the bare data ID (Stage 2 lands the actual flow).
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
    """Build a c-node in ``scope``'s main arena that unwraps to ``"."``.

    Uses the scope's ``_main_arena`` directly because c-node creation is a
    low-level arena op that scopes don't expose as a public method.
    """
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    context_data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": ctx_key,
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), context_data)


def _make_c_node_unwrapping_to(
    scope: FxtrScope, target: FxtrID, ctx_key: str = "non_dot_ctx"
) -> FxtrID:
    """Build a c-node whose ``value`` field is ``target`` (instead of ``.``).

    Used to test the "must unwrap to '.'" validation.
    """
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    context_data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=target),
        "ctx_key": ctx_key,
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), context_data)


# =============================================================================
# Default behavior (no provenance_context)
# =============================================================================


class TestDefaultMarker:
    """Without ``provenance_context``, scopes get a bare ``"."`` marker."""

    def test_global_scope_marker_is_bare_dot(self):
        marker = GLOBAL_SCOPE.get_context_marker_id()
        assert marker == GROUND
        assert marker._provenance_parent is None

    @pytest.mark.anyio
    async def test_default_scope_has_bare_dot_marker(self):
        async with FxtrScope() as scope:
            marker = scope.get_context_marker_id()
            assert marker == GROUND
            assert marker._provenance_parent is None

    @pytest.mark.anyio
    async def test_isolated_scope_has_bare_dot_marker(self):
        async with FxtrScope(isolated=True) as scope:
            marker = scope.get_context_marker_id()
            assert marker == GROUND
            assert marker._provenance_parent is None


# =============================================================================
# Explicit provenance_context
# =============================================================================


class TestProvenanceContext:
    """Constructing a scope with a c-node sets its marker correctly."""

    @pytest.mark.anyio
    async def test_explicit_c_node_sets_marker(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                marker = scope.get_context_marker_id()
                # Data-level part is "."
                assert marker == GROUND
                # Provenance parent is the supplied c-node
                assert marker._provenance_parent == c_node

    @pytest.mark.anyio
    async def test_c_node_is_tracked_in_new_scope(self):
        """The c-node is sync-copied into the new scope's arena, not a lazy attach."""
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                assert scope.is_tracked(c_node)

    @pytest.mark.anyio
    async def test_non_c_tag_rejected(self):
        async with FxtrScope() as builder:
            # Using "." (a ground node) is not a c-node.
            with pytest.raises(ValueError, match="c-node"):
                async with FxtrScope(
                    provenance_context=GROUND,
                    provenance_context_from_scope=builder,
                ):
                    pass

    @pytest.mark.anyio
    async def test_c_node_with_provenance_parent_rejected(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            wrapped = FxtrID(
                raw_data_id=c_node._raw_data_id,
                provenance_parent=FxtrID.from_tag_and_payload("u", c_node._raw_data_id),
            )
            with pytest.raises(ValueError, match="bare c-node"):
                async with FxtrScope(
                    provenance_context=wrapped,
                    provenance_context_from_scope=builder,
                ):
                    pass

    @pytest.mark.anyio
    async def test_c_node_must_unwrap_to_dot(self):
        async with FxtrScope() as builder:
            # Build a "." -> something nontrivial: use a builtin ID
            # (which is not "."), then make a c-node that unwraps to it.
            other = builder.track_builtin(FxtrID.builtin("not_dot"))
            bad_c = _make_c_node_unwrapping_to(builder, other)
            with pytest.raises(ValueError, match="unwrap"):
                async with FxtrScope(
                    provenance_context=bad_c,
                    provenance_context_from_scope=builder,
                ):
                    pass

    @pytest.mark.anyio
    async def test_c_node_not_tracked_in_supplied_source_rejected(self):
        async with FxtrScope() as builder_a:
            c_node = _make_c_node(builder_a)
            # builder_b doesn't track c_node.
            async with FxtrScope() as builder_b:
                with pytest.raises(ValueError, match="not tracked"):
                    async with FxtrScope(
                        provenance_context=c_node,
                        provenance_context_from_scope=builder_b,
                    ):
                        pass

    @pytest.mark.anyio
    async def test_provenance_context_from_scope_without_context_rejected(self):
        async with FxtrScope() as builder:
            with pytest.raises(ValueError, match="without a provenance_context"):
                async with FxtrScope(provenance_context_from_scope=builder):
                    pass


# =============================================================================
# Implicit lift-from-parent (no explicit source scope)
# =============================================================================


class TestImplicitLift:
    """If ``provenance_context_from_scope`` is None, walk parents."""

    @pytest.mark.anyio
    async def test_lift_from_immediate_parent(self):
        async with FxtrScope() as parent:
            c_node = _make_c_node(parent)
            # parent is automatically included via the active-scope path
            # because the inner scope is non-isolated.
            async with FxtrScope(provenance_context=c_node) as scope:
                marker = scope.get_context_marker_id()
                assert marker._provenance_parent == c_node
                assert scope.is_tracked(c_node)

    @pytest.mark.anyio
    async def test_lift_from_explicit_include(self):
        """``include=`` puts a scope in our parent set even if isolated=True."""
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                isolated=True, include=[builder, GLOBAL_SCOPE], provenance_context=c_node
            ) as scope:
                marker = scope.get_context_marker_id()
                assert marker._provenance_parent == c_node

    @pytest.mark.anyio
    async def test_no_parent_tracks_c_node_rejected(self):
        async with FxtrScope() as builder_a:
            c_node = _make_c_node(builder_a)
            # Make an isolated scope that doesn't have builder_a as parent.
            with pytest.raises(ValueError, match="not tracked in any parent"):
                async with FxtrScope(
                    isolated=True,
                    include=[GLOBAL_SCOPE],
                    provenance_context=c_node,
                ):
                    pass


# =============================================================================
# Inheritance from parent's marker
# =============================================================================


class TestInheritedMarker:
    """Child scopes (no explicit provenance_context) inherit the parent's marker."""

    @pytest.mark.anyio
    async def test_child_inherits_parent_marker(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ):
                # The outer scope is now active and has the c-node marker.
                # Create a non-isolated child with no explicit context.
                async with FxtrScope() as child:
                    child_marker = child.get_context_marker_id()
                    assert child_marker == GROUND
                    assert child_marker._provenance_parent == c_node
                    # And the child's arena tracks the c-node too (eager copy).
                    assert child.is_tracked(c_node)

    @pytest.mark.anyio
    async def test_isolated_child_with_include_inherits(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as outer:
                async with FxtrScope(isolated=True, include=[outer, GLOBAL_SCOPE]) as child:
                    assert child.get_context_marker_id()._provenance_parent == c_node

    @pytest.mark.anyio
    async def test_isolated_child_no_include_no_inherit(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ):
                # Fully isolated, no parents — gets bare ".".
                async with FxtrScope(isolated=True) as iso:
                    assert iso.get_context_marker_id() == GROUND
                    assert iso.get_context_marker_id()._provenance_parent is None


# =============================================================================
# Helper methods: extract_provenance_parent / embed_as_provenance_parent
# =============================================================================


class TestLayerCrossingHelpers:
    """``extract_provenance_parent`` and ``embed_as_provenance_parent``."""

    @pytest.mark.anyio
    async def test_extract_then_embed_roundtrips(self):
        async with FxtrScope() as scope:
            c_node = _make_c_node(scope)
            # Construct a layered FxtrID by hand: data="." with prov=c-node.
            layered = FxtrID(raw_data_id=GROUND._raw_data_id, provenance_parent=c_node)
            # extract returns the bare provenance node.
            extracted = scope.extract_provenance_parent(layered)
            assert extracted == c_node
            assert extracted._provenance_parent is None
            # embed re-attaches it as a provenance parent.
            re_embedded = scope.embed_as_provenance_parent(extracted)
            assert re_embedded == GROUND  # data ID compares as the ground node
            assert re_embedded._provenance_parent == c_node

    @pytest.mark.anyio
    async def test_extract_requires_provenance_parent(self):
        async with FxtrScope() as scope:
            with pytest.raises(ValueError, match="no provenance parent"):
                scope.extract_provenance_parent(GROUND)

    @pytest.mark.anyio
    async def test_extract_requires_data_tracked(self):
        async with FxtrScope() as scope:
            c_node = _make_c_node(scope)
            # Synthesize a layered ID whose data side isn't tracked here.
            fake_data = FxtrID.from_tag_and_payload("d", b"x" * 32)
            layered = FxtrID(raw_data_id=fake_data._raw_data_id, provenance_parent=c_node)
            with pytest.raises(ValueError, match="not tracked"):
                scope.extract_provenance_parent(layered)

    @pytest.mark.anyio
    async def test_extract_as_u_if_missing_wraps_bare_data(self):
        """With the flag, a bare id (no prov parent) is wrapped to
        ``u(id)`` instead of raising — same as ``create_u_node``."""
        async with FxtrScope() as scope:
            projected = scope.extract_provenance_parent(GROUND, extract_as_u_if_missing=True)
            assert projected.get_tag() == "u"
            assert projected._provenance_parent is None
            assert projected == scope.create_u_node(GROUND)

    @pytest.mark.anyio
    async def test_extract_as_u_if_missing_layered_is_noop(self):
        """The flag only affects the bare branch; a layered input still
        returns its bare prov half."""
        async with FxtrScope() as scope:
            c_node = _make_c_node(scope)
            layered = FxtrID(raw_data_id=GROUND._raw_data_id, provenance_parent=c_node)
            assert scope.extract_provenance_parent(layered, extract_as_u_if_missing=True) == c_node

    @pytest.mark.anyio
    async def test_extract_as_u_if_missing_wraps_bare_prov_tag(self):
        """Branch is on the layering, never the tag: a bare prov-tag
        node (here a c-node) is wrapped to ``u(node)`` just like bare
        data."""
        async with FxtrScope() as scope:
            c_node = _make_c_node(scope)
            assert c_node.get_tag() == "c"
            assert c_node._provenance_parent is None
            projected = scope.extract_provenance_parent(c_node, extract_as_u_if_missing=True)
            assert projected.get_tag() == "u"
            assert projected == scope.create_u_node(c_node)

    @pytest.mark.anyio
    async def test_embed_requires_bare_input(self):
        async with FxtrScope() as scope:
            c_node = _make_c_node(scope)
            # Embed something that already has a provenance parent.
            layered = FxtrID(
                raw_data_id=c_node._raw_data_id,
                provenance_parent=FxtrID.from_tag_and_payload("u", c_node._raw_data_id),
            )
            with pytest.raises(ValueError, match="already has a provenance parent"):
                scope.embed_as_provenance_parent(layered)

    @pytest.mark.anyio
    async def test_embed_requires_provenance_node_tag(self):
        async with FxtrScope() as scope:
            # "." is not a provenance node.
            with pytest.raises(ValueError, match="provenance-node tag"):
                scope.embed_as_provenance_parent(GROUND)

    @pytest.mark.anyio
    async def test_embed_requires_node_tracked(self):
        async with FxtrScope() as scope:
            # Construct a c-node ID that isn't actually tracked.
            fake_c = FxtrID.from_tag_and_payload("c", b"x" * 32)
            with pytest.raises(ValueError, match="not tracked"):
                scope.embed_as_provenance_parent(fake_c)

    @pytest.mark.anyio
    async def test_embed_unwraps_via_arena(self):
        """``embed_as_provenance_parent`` reads the unwrap target from the arena."""
        async with FxtrScope() as scope:
            c_node = _make_c_node(scope)
            embedded = scope.embed_as_provenance_parent(c_node)
            # The c-node we built unwraps to "." (its value field), so the
            # embedded ID should have data="." and prov=c-node.
            assert embedded == GROUND
            assert embedded._provenance_parent == c_node


# =============================================================================
# Helper method: create_c_node
# =============================================================================


class TestCreateCNode:
    """``FxtrScope.create_c_node``: thin wrapper over
    ``MemoryArena.create_c_node`` for minting c-nodes from d-node cbor.
    """

    @pytest.mark.anyio
    async def test_create_c_node_matches_arena_call(self):
        async with FxtrScope() as scope:
            ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
            context_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
                "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
                "value": RawWeakFxtrID(inner_id=GROUND),
                "ctx_key": "via_scope",
            }
            c_node = scope.create_c_node(context_data)
            assert c_node.get_tag() == "c"
            assert c_node._provenance_parent is None
            # The c-node unwraps to GROUND (the value field).
            assert (
                scope._main_arena.unwrap_provenance(scope.clone_authorization(), c_node) == GROUND
            )

    @pytest.mark.anyio
    async def test_create_c_node_usable_as_provenance_context(self):
        """The c-node minted via the new helper is immediately usable
        as another scope's ``provenance_context``.
        """
        async with FxtrScope() as builder:
            ctx_type = builder.track_builtin(FxtrID.builtin("ctx"))
            context_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
                "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
                "value": RawWeakFxtrID(inner_id=GROUND),
                "ctx_key": "downstream_test",
            }
            c_node = builder.create_c_node(context_data)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                marker = scope.get_context_marker_id()
                assert marker._provenance_parent == c_node


# =============================================================================
# Helper method: create_u_node
# =============================================================================


class TestCreateUNode:
    """``FxtrScope.create_u_node``: produce a bare ``u``-tagged
    provenance node wrapping a tracked node. Pair with
    :meth:`embed_as_provenance_parent` to materialize the canonical
    "data with trivial provenance" layered form.
    """

    @pytest.mark.anyio
    async def test_create_u_node_for_ground(self):
        async with FxtrScope() as scope:
            u = scope.create_u_node(GROUND)
            assert u.get_tag() == "u"
            assert u._provenance_parent is None
            # The u-node unwraps to GROUND, so embedding gives us the
            # layered (GROUND, u(GROUND)) form.
            layered = scope.embed_as_provenance_parent(u)
            assert layered == GROUND
            assert layered._provenance_parent == u

    @pytest.mark.anyio
    async def test_create_u_node_idempotent_per_data(self):
        """Repeated calls for the same wrapped_id return the same FxtrID
        (the u-node hash is derived from the wrapped data)."""
        async with FxtrScope() as scope:
            u1 = scope.create_u_node(GROUND)
            u2 = scope.create_u_node(GROUND)
            assert u1 == u2

    @pytest.mark.anyio
    async def test_create_u_node_rejects_layered_input(self):
        async with FxtrScope() as scope:
            c_node = _make_c_node(scope)
            layered = FxtrID(raw_data_id=GROUND._raw_data_id, provenance_parent=c_node)
            with pytest.raises(ValueError, match="already has a provenance parent"):
                scope.create_u_node(layered)

    @pytest.mark.anyio
    async def test_create_u_node_rejects_untracked_input(self):
        async with FxtrScope() as scope:
            untracked = FxtrID.from_tag_and_payload("d", b"x" * 32)
            with pytest.raises(ValueError, match="not tracked"):
                scope.create_u_node(untracked)


# =============================================================================
# C-node closures containing foreign-inherited (never-loaded) entities
# =============================================================================


class TestProvenanceClosureWithForeignInheritedDep:
    """Deriving a provenance-marked scope must work when the c-node's dep
    closure includes an entity the source scope only *inherits* from a
    foreign arena (data never loaded locally).

    This is the scope-level face of the ``track_from_memory_arena``
    adoption bug: ``_track_provenance_context_from`` does a sync walk of
    the c-node closure into the new scope. When the walk reaches the
    foreign-inherited entity, the old strategy (2) required the *foreign
    arena's own token* to be among the new scope's ancestor bridges (a
    bridge-topology coincidence that doesn't hold in general), and the
    fallback strategy (3) demanded in-memory data → ``RuntimeError: ...
    no in-memory data``. The structurally-justified path: the parent
    scope holds a claim on the entity and the child keeps the parent
    alive, so the entity is adoptable as a same-store inheritance entry
    targeting the parent — no data, no I/O.

    In the workstation guest incident this fired on the marker-inherit
    branch of every per-call dispatch-scope derivation (the mount scope's
    provenance c-node references the connect-call bundle entity, which is
    inherited from a wire-input arena and never loaded guest-side). Both
    the explicit ``provenance_context=`` branch and the marker-inherit
    branch funnel into the same ``_track_provenance_context_from`` walk;
    this test uses the explicit branch for directness.
    """

    @pytest.mark.anyio
    async def test_derive_scope_with_foreign_inherited_closure_dep(self):
        from fxtr2.data.memory_arena import MemoryStore
        from fxtr2.leases.lease_bridges import IndependentLocalBridge
        from fxtr2.leases.local_lease import UnconditionalLocalLifetime

        # Foreign side: a separate store holding the bundle's bytes.
        foreign_store = MemoryStore()
        foreign_ull = UnconditionalLocalLifetime("foreign-prov-test")
        foreign_store.register_token(foreign_ull.token, name="foreign-prov-test")
        foreign_lease = foreign_ull.root_lease.take()
        foreign_arena = foreign_store.new_arena(foreign_lease.clone())
        try:
            foreign_arena.track_builtin(foreign_lease.clone(), GROUND)
            bundle_id = foreign_arena.store(
                foreign_lease.clone(),
                {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
                    "payload": "config bundle; bytes never loaded into the scope store",
                },
            )

            async with FxtrScope(name="holder") as holder:
                # holder inherits the bundle lazily from the foreign arena
                # (low-level attach: scopes don't expose this directly).
                holder._main_arena.lazily_attach_from_arena(
                    holder.clone_authorization(),
                    bundle_id,
                    foreign_arena,
                    IndependentLocalBridge(lease=foreign_lease.clone()),
                )
                bundle_node = holder._global_store._entity_states[bundle_id]
                assert bundle_node.data is None

                # A c-node whose payload references the bundle (the
                # SandboxProvenanceContext shape from the incident).
                ctx_type = holder.track_builtin(FxtrID.builtin("ctx"))
                context_data: FxtrCbor = {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
                    "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
                    "value": RawWeakFxtrID(inner_id=GROUND),
                    "bundle": RawWeakFxtrID(inner_id=bundle_id),
                }
                c_node = holder._main_arena.create_c_node(
                    holder.clone_authorization(), context_data
                )

                # Deriving the provenance-marked child sync-walks the
                # c-node closure — including the never-loaded bundle —
                # from holder into the child.
                async with FxtrScope(provenance_context=c_node) as child:
                    marker = child.get_context_marker_id()
                    assert marker._provenance_parent == c_node

                    # The closure was adopted lazily: the bundle still has
                    # no local data. Walking the graph from the marker
                    # materializes it — c-node → payload → bundle, each hop
                    # loadable from the child (the last one async, through
                    # holder's foreign entry).
                    assert bundle_node.data is None
                    payload_id = child._main_arena.c_node_to_context_data(
                        child.clone_authorization(), c_node
                    )
                    await child._main_arena.load_flat(child.clone_authorization(), payload_id)
                    flat = await child._main_arena.load_flat(child.clone_authorization(), bundle_id)
                    assert flat.entity_id == bundle_id
                    assert flat.entity_bytes is not None
        finally:
            foreign_arena.close()
            foreign_lease.discard()
            foreign_store.close()
