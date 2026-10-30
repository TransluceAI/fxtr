"""Tests for thingwatcher.fxtr.renderers module."""

from __future__ import annotations

import pytest

# Skip all tests if thingwatcher is not installed
pytest.importorskip("thingwatcher")

from fxtr2.data.entity_core import FxtrID
from thingwatcher.fxtr.renderers import (
    FXTR_ENTITY_TYPE_PREFIX,
    FXTR_FALLBACK_TYPE,
    fxtr_renderer_builder,
    register_fxtr_renderer,
    unregister_fxtr_renderer,
)
from thingwatcher.registry import RendererBuilderResult


class TestFxtrRendererRegistry:
    """Tests for renderer registration functions."""

    def test_register_renderer_with_fxtr_id(self):
        """Test registering a renderer with FxtrID."""
        type_id = FxtrID.builtin("test_type")
        tsx = "// MY_CUSTOM_RENDERER\nexport default {};"

        register_fxtr_renderer(type_id, tsx)

        try:
            # Check it was registered by querying the builder
            thing_type = f"{FXTR_ENTITY_TYPE_PREFIX}!test_type"
            result = fxtr_renderer_builder(thing_type)
            assert result is not None
            # The builder returns the custom renderer directly
            assert "MY_CUSTOM_RENDERER" in result.tsx_source
        finally:
            unregister_fxtr_renderer(type_id)

    def test_register_renderer_with_string_id(self):
        """Test registering a renderer with string id."""
        tsx = "export default {};"

        register_fxtr_renderer("!another_type", tsx)

        try:
            thing_type = f"{FXTR_ENTITY_TYPE_PREFIX}!another_type"
            result = fxtr_renderer_builder(thing_type)
            assert result is not None
        finally:
            unregister_fxtr_renderer("!another_type")

    def test_unregister_renderer(self):
        """Test unregistering a renderer."""
        type_id = FxtrID.builtin("removable")
        tsx = "export default {};"

        register_fxtr_renderer(type_id, tsx)
        unregister_fxtr_renderer(type_id)

        # Querying should now return the fallback
        thing_type = f"{FXTR_ENTITY_TYPE_PREFIX}!removable"
        result = fxtr_renderer_builder(thing_type)
        # Should get a fallback-based renderer, not None
        assert result is not None
        assert "fallback" in result.tsx_source.lower() or (
            result.imports and "./fallback.tsx" in result.imports
        )

    def test_unregister_nonexistent_raises(self):
        """Test that unregistering non-existent renderer raises KeyError."""
        with pytest.raises(KeyError):
            unregister_fxtr_renderer("!nonexistent_type")


class TestFxtrRendererBuilder:
    """Tests for fxtr_renderer_builder function."""

    def test_builder_returns_none_for_unrelated_types(self):
        """Test that builder returns None for non-fxtr types."""
        result = fxtr_renderer_builder("some.other.type")
        assert result is None

        result = fxtr_renderer_builder("fxtr.transluce.org/something_else")
        assert result is None

    def test_builder_handles_fallback_type(self):
        """Test that builder returns renderer for fallback type."""
        result = fxtr_renderer_builder(FXTR_FALLBACK_TYPE)

        assert result is not None
        assert isinstance(result, RendererBuilderResult)
        assert "fxtr_entity_fallback" in result.tsx_source

    def test_builder_handles_typed_entity(self):
        """Test that builder returns renderer for typed entities."""
        thing_type = f"{FXTR_ENTITY_TYPE_PREFIX}!some_type"
        result = fxtr_renderer_builder(thing_type)

        assert result is not None
        assert isinstance(result, RendererBuilderResult)
        # Should use fallback for unregistered types
        assert "./fallback.tsx" in (result.imports or {})

    def test_builder_handles_namespace_type(self):
        """Test that builder returns renderer for namespace type."""
        result = fxtr_renderer_builder("fxtr.transluce.org/fxtr_namespace")

        assert result is not None
        assert isinstance(result, RendererBuilderResult)
        assert "fxtr_namespace" in result.tsx_source

    def test_fallback_renderer_has_link_marker_support(self):
        """Test that fallback renderer handles link markers."""
        result = fxtr_renderer_builder(FXTR_FALLBACK_TYPE)

        assert result is not None
        # Should contain references to link marker types
        assert "$!fxtr.link.weak" in result.tsx_source
        assert "$!fxtr.link.strong" in result.tsx_source
        assert "$!fxtr.link.marker" in result.tsx_source

    def test_fallback_renderer_uses_inline_node_ref(self):
        """Test that fallback renderer uses InlineNodeRef for links."""
        result = fxtr_renderer_builder(FXTR_FALLBACK_TYPE)

        assert result is not None
        assert "InlineNodeRef" in result.tsx_source

    def test_custom_renderer_takes_precedence(self):
        """Test that custom renderer is used when registered."""
        type_id = FxtrID.builtin("custom_type")
        custom_tsx = "// CUSTOM RENDERER\nexport default {};"

        register_fxtr_renderer(type_id, custom_tsx)

        try:
            thing_type = f"{FXTR_ENTITY_TYPE_PREFIX}!custom_type"
            result = fxtr_renderer_builder(thing_type)

            assert result is not None
            # Should contain our custom TSX
            assert "CUSTOM RENDERER" in result.tsx_source
        finally:
            unregister_fxtr_renderer(type_id)

    def test_custom_renderer_with_imports(self):
        """Test that custom renderer imports are passed through."""
        type_id = FxtrID.builtin("styled_type")
        custom_tsx = "import styles from './styles.css';\nexport default {};"
        custom_imports = {"./styles.css": ".my-class { color: red; }"}

        register_fxtr_renderer(type_id, custom_tsx, custom_imports)

        try:
            thing_type = f"{FXTR_ENTITY_TYPE_PREFIX}!styled_type"
            result = fxtr_renderer_builder(thing_type)

            assert result is not None
            assert result.imports is not None
            assert "./styles.css" in result.imports
        finally:
            unregister_fxtr_renderer(type_id)
