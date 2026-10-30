"""Tests for path-based renderer registration and reload detection."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from thingwatcher.registry import (
    RendererSourceSpec,
    clear_global_registries,
    get_all_renderer_mappings,
    get_renderer_mapping,
    get_renderer_module,
    register_renderer,
)


@pytest.fixture(autouse=True)
def clean_registries():
    """Clean registries before and after each test."""
    clear_global_registries()
    yield
    clear_global_registries()


class TestRendererSourceSpec:
    """Tests for RendererSourceSpec dataclass."""

    def test_path_spec(self):
        """Can create a path-based spec."""
        spec = RendererSourceSpec(path=Path("/tmp/test.tsx"))
        assert spec.path == Path("/tmp/test.tsx")
        assert spec.content is None

    def test_content_spec(self):
        """Can create a content-based spec."""
        spec = RendererSourceSpec(content="export default {}")
        assert spec.path is None
        assert spec.content == "export default {}"

    def test_requires_one_source(self):
        """Must have either path or content."""
        with pytest.raises(ValueError, match="must have either path or content"):
            RendererSourceSpec()

    def test_rejects_both_sources(self):
        """Cannot have both path and content."""
        with pytest.raises(ValueError, match="cannot have both path and content"):
            RendererSourceSpec(path=Path("/tmp/test.tsx"), content="export default {}")


class TestPathBasedRegistration:
    """Tests for registering renderers from file paths."""

    def test_register_with_path(self, tmp_path: Path):
        """Can register a renderer using a Path."""
        tsx_file = tmp_path / "Widget.tsx"
        tsx_file.write_text('export default { dialect: "0.1", renderers: {} }')

        module_id = register_renderer("Widget", tsx_file)

        assert module_id is not None
        mapping = get_renderer_mapping("Widget")
        assert mapping is not None
        assert mapping.module_id == module_id
        assert mapping.renderer_key == "Widget"

        # Verify module content
        module = get_renderer_module(module_id)
        assert module is not None
        assert module.main == 'export default { dialect: "0.1", renderers: {} }'

    def test_register_with_path_imports(self, tmp_path: Path):
        """Can register a renderer with path-based imports."""
        tsx_file = tmp_path / "Widget.tsx"
        tsx_file.write_text('import styles from "./styles.module.css";')

        css_file = tmp_path / "styles.module.css"
        css_file.write_text(".widget { color: red; }")

        module_id = register_renderer(
            "Widget",
            tsx_file,
            imports={"./styles.module.css": css_file},
        )

        module = get_renderer_module(module_id)
        assert module is not None
        assert module.main == 'import styles from "./styles.module.css";'
        assert module.imports == {"./styles.module.css": ".widget { color: red; }"}

    def test_register_mixed_path_and_string_imports(self, tmp_path: Path):
        """Can mix path-based and string-based imports."""
        tsx_file = tmp_path / "Widget.tsx"
        tsx_file.write_text("// main tsx")

        css_file = tmp_path / "styles.css"
        css_file.write_text(".from-file { }")

        module_id = register_renderer(
            "Widget",
            tsx_file,
            imports={
                "./styles.css": css_file,  # Path
                "./utils.tsx": "export const helper = () => {};",  # String
            },
        )

        module = get_renderer_module(module_id)
        assert module is not None
        assert module.imports["./styles.css"] == ".from-file { }"
        assert module.imports["./utils.tsx"] == "export const helper = () => {};"

    def test_multiple_types_share_registration(self, tmp_path: Path):
        """Multiple thing types from one register call share the same registration."""
        tsx_file = tmp_path / "bundle.tsx"
        tsx_file.write_text("// bundle")

        register_renderer(["TypeA", "TypeB", "TypeC"], tsx_file)

        mapping_a = get_renderer_mapping("TypeA")
        mapping_b = get_renderer_mapping("TypeB")
        mapping_c = get_renderer_mapping("TypeC")

        # All should have the same module ID
        assert mapping_a is not None
        assert mapping_b is not None
        assert mapping_c is not None
        assert mapping_a.module_id == mapping_b.module_id == mapping_c.module_id


class TestFileChangeDetection:
    """Tests for detecting file changes via mtime."""

    def test_file_change_updates_module_id(self, tmp_path: Path):
        """Changing a file on disk produces a new module ID."""
        tsx_file = tmp_path / "Widget.tsx"
        tsx_file.write_text("// version 1")

        register_renderer("Widget", tsx_file)
        mapping1 = get_renderer_mapping("Widget")
        assert mapping1 is not None
        module_id1 = mapping1.module_id

        # Wait a tiny bit to ensure mtime changes
        time.sleep(0.01)

        # Modify the file
        tsx_file.write_text("// version 2")

        # Get mapping again - should detect change
        mapping2 = get_renderer_mapping("Widget")
        assert mapping2 is not None
        module_id2 = mapping2.module_id

        assert module_id1 != module_id2

        # Verify new content
        module = get_renderer_module(module_id2)
        assert module is not None
        assert module.main == "// version 2"

    def test_import_file_change_updates_module_id(self, tmp_path: Path):
        """Changing an import file on disk produces a new module ID."""
        tsx_file = tmp_path / "Widget.tsx"
        tsx_file.write_text("// main")

        css_file = tmp_path / "styles.css"
        css_file.write_text(".v1 { }")

        register_renderer("Widget", tsx_file, imports={"./styles.css": css_file})
        mapping1 = get_renderer_mapping("Widget")
        assert mapping1 is not None
        module_id1 = mapping1.module_id

        time.sleep(0.01)

        # Modify the CSS file
        css_file.write_text(".v2 { }")

        mapping2 = get_renderer_mapping("Widget")
        assert mapping2 is not None
        module_id2 = mapping2.module_id

        assert module_id1 != module_id2

        module = get_renderer_module(module_id2)
        assert module is not None
        assert module.imports["./styles.css"] == ".v2 { }"

    def test_no_change_returns_cached_module_id(self, tmp_path: Path):
        """When files haven't changed, returns the cached module ID without re-reading."""
        tsx_file = tmp_path / "Widget.tsx"
        tsx_file.write_text("// content")

        register_renderer("Widget", tsx_file)
        mapping1 = get_renderer_mapping("Widget")
        mapping2 = get_renderer_mapping("Widget")

        assert mapping1 is not None
        assert mapping2 is not None
        assert mapping1.module_id == mapping2.module_id

    def test_shared_registration_cache(self, tmp_path: Path):
        """Multiple thing types sharing a registration share the mtime cache."""
        tsx_file = tmp_path / "bundle.tsx"
        tsx_file.write_text("// v1")

        register_renderer(["TypeA", "TypeB"], tsx_file)

        mapping_a1 = get_renderer_mapping("TypeA")
        mapping_b1 = get_renderer_mapping("TypeB")
        assert mapping_a1 is not None
        assert mapping_b1 is not None
        assert mapping_a1.module_id == mapping_b1.module_id

        time.sleep(0.01)
        tsx_file.write_text("// v2")

        # Query TypeA first - this updates the shared cache
        mapping_a2 = get_renderer_mapping("TypeA")
        assert mapping_a2 is not None
        assert mapping_a2.module_id != mapping_a1.module_id

        # Query TypeB - should get same new module_id (cache already updated)
        mapping_b2 = get_renderer_mapping("TypeB")
        assert mapping_b2 is not None
        assert mapping_b2.module_id == mapping_a2.module_id


class TestGetAllRendererMappings:
    """Tests for get_all_renderer_mappings with path-based registrations."""

    def test_includes_path_based_registrations(self, tmp_path: Path):
        """get_all_renderer_mappings includes path-based registrations."""
        tsx_file = tmp_path / "Widget.tsx"
        tsx_file.write_text("// widget")

        register_renderer("Widget", tsx_file)

        all_mappings = get_all_renderer_mappings()
        assert "Widget" in all_mappings
        assert all_mappings["Widget"].renderer_key == "Widget"

    def test_updates_on_file_change(self, tmp_path: Path):
        """get_all_renderer_mappings returns updated module IDs when files change."""
        tsx_file = tmp_path / "Widget.tsx"
        tsx_file.write_text("// v1")

        register_renderer("Widget", tsx_file)
        mappings1 = get_all_renderer_mappings()
        module_id1 = mappings1["Widget"].module_id

        time.sleep(0.01)
        tsx_file.write_text("// v2")

        mappings2 = get_all_renderer_mappings()
        module_id2 = mappings2["Widget"].module_id

        assert module_id1 != module_id2


class TestStringBasedRegistrationStillWorks:
    """Ensure string-based registration still works as before."""

    def test_string_based_registration(self):
        """Can still register renderers with string sources."""
        module_id = register_renderer("Widget", "// tsx source")

        mapping = get_renderer_mapping("Widget")
        assert mapping is not None
        assert mapping.module_id == module_id

        module = get_renderer_module(module_id)
        assert module is not None
        assert module.main == "// tsx source"

    def test_string_based_with_imports(self):
        """Can still use string-based imports."""
        module_id = register_renderer(
            "Widget",
            "// main",
            imports={"./utils.tsx": "// utils"},
        )

        module = get_renderer_module(module_id)
        assert module is not None
        assert module.main == "// main"
        assert module.imports == {"./utils.tsx": "// utils"}


class TestRendererBuilders:
    """Tests for dynamic renderer builder functions."""

    def test_builder_generates_renderer(self):
        """A builder function can generate a renderer for an unknown type."""
        from thingwatcher.registry import (
            RendererBuilderResult,
            register_renderer_builder,
            unregister_renderer_builder,
        )

        def my_builder(thing_type: str) -> RendererBuilderResult | None:
            if thing_type == "DynamicWidget":
                return RendererBuilderResult(tsx_source=f"// renderer for {thing_type}")
            return None

        register_renderer_builder(my_builder)
        try:
            mapping = get_renderer_mapping("DynamicWidget")
            assert mapping is not None
            assert mapping.renderer_key == "DynamicWidget"

            module = get_renderer_module(mapping.module_id)
            assert module is not None
            assert module.main == "// renderer for DynamicWidget"
            assert module.imports == {}
        finally:
            unregister_renderer_builder(my_builder)

    def test_builder_returns_none_for_unhandled_type(self):
        """Builder returning None means it doesn't handle this type."""
        from thingwatcher.registry import (
            RendererBuilderResult,
            register_renderer_builder,
            unregister_renderer_builder,
        )

        def my_builder(thing_type: str) -> RendererBuilderResult | None:
            if thing_type.startswith("Handled"):
                return RendererBuilderResult(tsx_source="// handled")
            return None

        register_renderer_builder(my_builder)
        try:
            # Unhandled type returns None
            mapping = get_renderer_mapping("UnhandledType")
            assert mapping is None

            # Handled type returns a mapping
            mapping = get_renderer_mapping("HandledType")
            assert mapping is not None
        finally:
            unregister_renderer_builder(my_builder)

    def test_static_registration_takes_precedence(self):
        """Static registrations are checked before builders."""
        from thingwatcher.registry import (
            RendererBuilderResult,
            register_renderer_builder,
            unregister_renderer_builder,
        )

        def my_builder(thing_type: str) -> RendererBuilderResult | None:
            return RendererBuilderResult(tsx_source="// from builder")

        # Register static first
        register_renderer("StaticWidget", "// from static")
        register_renderer_builder(my_builder)

        try:
            mapping = get_renderer_mapping("StaticWidget")
            assert mapping is not None

            module = get_renderer_module(mapping.module_id)
            assert module is not None
            assert module.main == "// from static"  # Static wins
        finally:
            unregister_renderer_builder(my_builder)

    def test_builders_tried_in_most_recent_first_order(self):
        """Multiple builders are tried in LIFO order (most recent first)."""
        from thingwatcher.registry import (
            RendererBuilderResult,
            register_renderer_builder,
            unregister_renderer_builder,
        )

        def builder_old(thing_type: str) -> RendererBuilderResult | None:
            return RendererBuilderResult(tsx_source="// old builder")

        def builder_new(thing_type: str) -> RendererBuilderResult | None:
            return RendererBuilderResult(tsx_source="// new builder")

        register_renderer_builder(builder_old)
        register_renderer_builder(builder_new)

        try:
            mapping = get_renderer_mapping("AnyType")
            assert mapping is not None

            module = get_renderer_module(mapping.module_id)
            assert module is not None
            assert module.main == "// new builder"  # Newer wins
        finally:
            unregister_renderer_builder(builder_new)
            unregister_renderer_builder(builder_old)

    def test_builder_with_imports(self):
        """Builder can return a result with imports."""
        from thingwatcher.registry import (
            RendererBuilderResult,
            register_renderer_builder,
            unregister_renderer_builder,
        )

        def my_builder(thing_type: str) -> RendererBuilderResult | None:
            return RendererBuilderResult(
                tsx_source="// main",
                imports={"./styles.css": ".widget { }"},
            )

        register_renderer_builder(my_builder)
        try:
            mapping = get_renderer_mapping("StyledWidget")
            assert mapping is not None

            module = get_renderer_module(mapping.module_id)
            assert module is not None
            assert module.main == "// main"
            assert module.imports == {"./styles.css": ".widget { }"}
        finally:
            unregister_renderer_builder(my_builder)

    def test_unregister_builder_not_registered(self):
        """Unregistering a builder that was never registered raises ValueError."""
        from thingwatcher.registry import RendererBuilderResult, unregister_renderer_builder

        def my_builder(thing_type: str) -> RendererBuilderResult | None:
            return None

        with pytest.raises(ValueError, match="not registered"):
            unregister_renderer_builder(my_builder)

    def test_builder_exception_is_caught(self):
        """Builders that raise exceptions are skipped, next builder is tried."""
        from thingwatcher.registry import (
            RendererBuilderResult,
            register_renderer_builder,
            unregister_renderer_builder,
        )

        def failing_builder(thing_type: str) -> RendererBuilderResult | None:
            raise RuntimeError("Builder failed!")

        def working_builder(thing_type: str) -> RendererBuilderResult | None:
            return RendererBuilderResult(tsx_source="// from working builder")

        register_renderer_builder(working_builder)
        register_renderer_builder(failing_builder)

        try:
            # failing_builder is tried first (most recent), fails, then working_builder succeeds
            mapping = get_renderer_mapping("AnyType")
            assert mapping is not None

            module = get_renderer_module(mapping.module_id)
            assert module is not None
            assert module.main == "// from working builder"
        finally:
            unregister_renderer_builder(failing_builder)
            unregister_renderer_builder(working_builder)

    def test_builder_runs_on_each_lookup(self):
        """Builders run on each lookup (no caching of builder results)."""
        from thingwatcher.registry import (
            RendererBuilderResult,
            register_renderer_builder,
            unregister_renderer_builder,
        )

        call_count = 0

        def counting_builder(thing_type: str) -> RendererBuilderResult | None:
            nonlocal call_count
            call_count += 1
            return RendererBuilderResult(tsx_source=f"// call {call_count}")

        register_renderer_builder(counting_builder)
        try:
            mapping1 = get_renderer_mapping("DynamicType")
            mapping2 = get_renderer_mapping("DynamicType")

            assert call_count == 2  # Builder was called twice

            # But if content is the same, module_id will be the same (content-addressed)
            # In this case, content differs so IDs differ
            assert mapping1 is not None
            assert mapping2 is not None
            assert mapping1.module_id != mapping2.module_id
        finally:
            unregister_renderer_builder(counting_builder)
