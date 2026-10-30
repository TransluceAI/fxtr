"""Tests for the ``store_provenance`` modes and auto-fallback path (Stage 9 / Stage 5).

The new modes:

- ``"strip"``: always strip provenance on store; cannot combine with a
  ``provenance_context``; doesn't inherit a parent's provenance marker.
- ``"if_marked"``: only take the prov path when the cbor's ``$fxtr.ctx``
  marker carries a provenance parent (old default behavior).
- ``"auto"`` (new default): take the prov path when the marker is
  provenanced *or* any non-marker child reference is. When the marker
  is bare but children carry provenance, substitute the marker with
  the scope's own c-node (if it has one) or a lazily-minted fallback
  c-node built from ``FallbackContextMarker(scope_name=self._name)``.

Plus: scopes now infer a ``name`` from the caller's ``co_name`` when
``name=`` isn't supplied, and the fallback c-node dedups by that name.
"""

from __future__ import annotations

import pytest

# Import system_entities eagerly so its ``!fallback_ctx`` / ``!basic_type``
# builtin type registrations land in GLOBAL_SCOPE *before* the conftest
# ``global_scope`` fixture takes its leak-detection snapshot. The lazy-mint
# path in ``FxtrScope._get_or_mint_fallback_or_own_marker`` triggers this
# import the first time auto-fallback fires; without the eager import it
# would happen mid-test and the conftest would flag the builtins as leaked.
import fxtr2.data.system_entities  # noqa: F401
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope

assert fxtr2.data.system_entities is not None  # silence "unused import" linter

GROUND = FxtrID.from_tag_and_payload(".", b"")


# =============================================================================
# Helpers
# =============================================================================


def _make_c_node(scope: FxtrScope, ctx_key: str = "test_ctx") -> FxtrID:
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": ctx_key,
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), data)


def _store_simple(scope: FxtrScope, *, name: str = "test", value: int = 42) -> FxtrID:
    """Store a tiny d-node using ``scope.get_context_marker_id()`` as the marker."""
    builtin_type = scope.track_builtin(FxtrID.builtin("test_entity"))
    cbor: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
        "$fxtr.type": RawWeakFxtrID(inner_id=builtin_type),
        "name": name,
        "value": value,
    }
    return scope.store_cbor(cbor)


def _store_with_layered_child(
    scope: FxtrScope, child_id: FxtrID, *, marker: FxtrID | None = None
) -> FxtrID:
    """Store a tiny entity in ``scope`` that references ``child_id`` weakly."""
    parent_type = scope.track_builtin(FxtrID.builtin("test_parent_entity"))
    cbor: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(
            inner_id=marker if marker is not None else scope.get_context_marker_id()
        ),
        "$fxtr.type": RawWeakFxtrID(inner_id=parent_type),
        "child": RawWeakFxtrID(inner_id=child_id),
    }
    return scope.store_cbor(cbor)


# =============================================================================
# Auto mode (default)
# =============================================================================


class TestAutoMode:
    @pytest.mark.anyio
    async def test_default_mode_is_auto(self):
        async with FxtrScope() as scope:
            assert scope._store_provenance == "auto"

    @pytest.mark.anyio
    async def test_bare_scope_bare_children_yields_bare_id(self):
        """Auto mode with no prov-bearing children behaves like the bare path."""
        async with FxtrScope() as scope:
            stored = _store_simple(scope)
            assert stored._provenance_parent is None

    @pytest.mark.anyio
    async def test_provenanced_scope_yields_layered_id(self):
        """Auto mode with the scope's own provenance_context: prov path, no fallback."""
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                stored = _store_simple(scope)
                assert stored._provenance_parent is not None
                # The scope's own c-node is the layered marker, and the
                # fallback marker should not have been touched.
                assert scope._fallback_marker is None

    @pytest.mark.anyio
    async def test_bare_scope_layered_child_uses_fallback_marker(self):
        """Auto mode with a layered child and a bare scope mints a fallback c-node."""
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE]) as outer:
            assert outer._store_provenance == "auto"
            assert outer._fallback_marker is None

            async with FxtrScope() as builder:
                c_node = _make_c_node(builder)
                async with FxtrScope(
                    provenance_context=c_node,
                    provenance_context_from_scope=builder,
                ) as prov_scope:
                    layered_child = _store_simple(prov_scope, name="child", value=1)
                    assert layered_child._provenance_parent is not None
                    await outer.copy_from_scope(layered_child, prov_scope)

            parent_id = _store_with_layered_child(outer, layered_child)

            # Provenance preserved.
            assert parent_id._provenance_parent is not None
            # Fallback marker was minted.
            assert outer._fallback_marker is not None
            assert outer._fallback_marker._provenance_parent is not None
            assert outer._fallback_marker._provenance_parent.get_tag() == "c"
            # The parent's prov parent (an n-node) unwraps to a d-node
            # whose marker is our fallback c-node.
            fallback_c_node = outer._fallback_marker._provenance_parent
            n_id = parent_id._provenance_parent
            assert n_id is not None
            n_cbor = await outer._main_arena.load(outer.clone_authorization(), n_id)
            assert isinstance(n_cbor, dict)
            marker_field = n_cbor["$fxtr.ctx"]
            assert isinstance(marker_field, RawFxtrEntityMarkerID)
            # The marker's inner_id is the projected c-node (bare prov
            # form), which should equal our fallback c-node.
            assert marker_field.inner_id == fallback_c_node

    @pytest.mark.anyio
    async def test_fallback_marker_is_cached_across_calls(self):
        """Repeated auto-overrides reuse the same fallback marker / c-node."""
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE]) as outer:
            async with FxtrScope() as builder:
                c_node = _make_c_node(builder)
                async with FxtrScope(
                    provenance_context=c_node,
                    provenance_context_from_scope=builder,
                ) as prov_scope:
                    child_a = _store_simple(prov_scope, name="a", value=1)
                    child_b = _store_simple(prov_scope, name="b", value=2)
                    await outer.copy_from_scope(child_a, prov_scope)
                    await outer.copy_from_scope(child_b, prov_scope)

            _store_with_layered_child(outer, child_a)
            first_marker = outer._fallback_marker
            assert first_marker is not None

            _store_with_layered_child(outer, child_b)
            second_marker = outer._fallback_marker
            assert second_marker is first_marker

    @pytest.mark.anyio
    async def test_provenanced_scope_with_layered_child_uses_scope_marker(self):
        """A scope with its own provenance_context shouldn't trigger fallback minting.

        If the scope has a layered marker and the cbor's $fxtr.ctx already
        carries that marker (the normal Entity.store path), we go straight
        to the prov path with marker_has_prov=True — no scan, no fallback.
        """
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as scope:
                child = _store_simple(scope, name="child", value=1)
                parent = _store_with_layered_child(scope, child)
                assert parent._provenance_parent is not None
                # Fallback was never used.
                assert scope._fallback_marker is None


# =============================================================================
# Fallback c-node dedup by scope name
# =============================================================================


class TestFallbackDedup:
    @pytest.mark.anyio
    async def test_same_name_same_fallback_c_node(self):
        """Two scopes with the same name produce the same fallback c-node ID."""
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="shared_name") as scope_a:
            async with FxtrScope(
                isolated=True, include=[GLOBAL_SCOPE], name="shared_name"
            ) as scope_b:
                async with FxtrScope() as builder:
                    c_node = _make_c_node(builder)
                    async with FxtrScope(
                        provenance_context=c_node,
                        provenance_context_from_scope=builder,
                    ) as prov_scope:
                        layered_child = _store_simple(prov_scope, name="x")
                        await scope_a.copy_from_scope(layered_child, prov_scope)
                        await scope_b.copy_from_scope(layered_child, prov_scope)

                _store_with_layered_child(scope_a, layered_child)
                marker_a = scope_a._fallback_marker
                assert marker_a is not None

                _store_with_layered_child(scope_b, layered_child)
                marker_b = scope_b._fallback_marker
                assert marker_b is not None

                # Identical c-node ID (data-id comparison; FxtrID equality is
                # provenance-blind, which is what we want here).
                assert marker_a == marker_b
                assert marker_a._provenance_parent == marker_b._provenance_parent

    @pytest.mark.anyio
    async def test_different_names_different_fallback_c_nodes(self):
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="name_a") as scope_a:
            async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="name_b") as scope_b:
                async with FxtrScope() as builder:
                    c_node = _make_c_node(builder)
                    async with FxtrScope(
                        provenance_context=c_node,
                        provenance_context_from_scope=builder,
                    ) as prov_scope:
                        layered_child = _store_simple(prov_scope)
                        await scope_a.copy_from_scope(layered_child, prov_scope)
                        await scope_b.copy_from_scope(layered_child, prov_scope)

                _store_with_layered_child(scope_a, layered_child)
                _store_with_layered_child(scope_b, layered_child)
                fallback_a = scope_a._fallback_marker
                fallback_b = scope_b._fallback_marker

                assert fallback_a is not None and fallback_b is not None
                assert fallback_a._provenance_parent != fallback_b._provenance_parent


# =============================================================================
# Strip mode
# =============================================================================


class TestStripMode:
    @pytest.mark.anyio
    async def test_strip_strips_layered_children(self):
        async with FxtrScope(
            isolated=True, include=[GLOBAL_SCOPE], store_provenance="strip"
        ) as strip_scope:
            assert strip_scope._store_provenance == "strip"

            async with FxtrScope() as builder:
                c_node = _make_c_node(builder)
                async with FxtrScope(
                    provenance_context=c_node,
                    provenance_context_from_scope=builder,
                ) as prov_scope:
                    layered_child = _store_simple(prov_scope)
                    await strip_scope.copy_from_scope(layered_child, prov_scope)

            parent_id = _store_with_layered_child(strip_scope, layered_child)
            # Strip mode never produces a layered result.
            assert parent_id._provenance_parent is None
            # And fallback was never minted.
            assert strip_scope._fallback_marker is None

    @pytest.mark.anyio
    async def test_strip_plus_provenance_context_raises(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            with pytest.raises(ValueError, match="strip"):
                FxtrScope(
                    isolated=True,
                    include=[builder],
                    store_provenance="strip",
                    provenance_context=c_node,
                    provenance_context_from_scope=builder,
                )

    @pytest.mark.anyio
    async def test_strip_does_not_inherit_parent_marker(self):
        """Strip mode keeps the bare "." marker even when the parent is provenanced."""
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as outer:
                # outer's marker is layered.
                assert outer.get_context_marker_id()._provenance_parent is not None
                async with FxtrScope(store_provenance="strip") as inner:
                    # inner is a non-isolated child of outer, but strip mode
                    # keeps the bare marker.
                    assert inner.get_context_marker_id() == GROUND
                    assert inner.get_context_marker_id()._provenance_parent is None


# =============================================================================
# if_marked mode
# =============================================================================


class TestIfMarkedMode:
    @pytest.mark.anyio
    async def test_if_marked_bare_scope_bare_result(self):
        async with FxtrScope(store_provenance="if_marked") as scope:
            stored = _store_simple(scope)
            assert stored._provenance_parent is None

    @pytest.mark.anyio
    async def test_if_marked_prov_scope_layered_result(self):
        async with FxtrScope() as builder:
            c_node = _make_c_node(builder)
            async with FxtrScope(
                provenance_context=c_node,
                provenance_context_from_scope=builder,
                store_provenance="if_marked",
            ) as scope:
                stored = _store_simple(scope)
                assert stored._provenance_parent is not None

    @pytest.mark.anyio
    async def test_if_marked_strips_layered_children(self):
        """if_marked + bare marker + prov-bearing child still strips (old default)."""
        async with FxtrScope(
            isolated=True, include=[GLOBAL_SCOPE], store_provenance="if_marked"
        ) as scope:
            async with FxtrScope() as builder:
                c_node = _make_c_node(builder)
                async with FxtrScope(
                    provenance_context=c_node,
                    provenance_context_from_scope=builder,
                ) as prov_scope:
                    layered_child = _store_simple(prov_scope)
                    await scope.copy_from_scope(layered_child, prov_scope)

            parent_id = _store_with_layered_child(scope, layered_child)
            assert parent_id._provenance_parent is None
            assert scope._fallback_marker is None


# =============================================================================
# Name inference
# =============================================================================


class TestNameInference:
    @pytest.mark.anyio
    async def test_name_inferred_from_caller(self):
        async with FxtrScope() as scope:
            assert scope._name == "test_name_inferred_from_caller"

    @pytest.mark.anyio
    async def test_explicit_name_wins(self):
        async with FxtrScope(name="explicit") as scope:
            assert scope._name == "explicit"

    @pytest.mark.anyio
    async def test_empty_string_name_is_kept(self):
        """An explicit empty string is not the same as None — no inference."""
        async with FxtrScope(name="") as scope:
            assert scope._name == ""
