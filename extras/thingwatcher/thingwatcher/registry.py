"""Type registries for snapshotters, rendering configuration, and custom renderers.

These registries exist separately from the Watcher so they can be configured
before a watcher is created. Individual watchers can override these with
local registrations via their own TypeRegistry instance.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cbor2

logger = logging.getLogger("thingwatcher")

if TYPE_CHECKING:
    from .api import AnchorCapturer, StatefulOwningSnapshotter, StatelessSnapshotter


# =============================================================================
# Data Types
# =============================================================================


@dataclass
class RenderingConfig:
    """Configuration for how a thing type should be rendered in the UI."""

    color: str
    link_colors: dict[str, str]


@dataclass
class RendererModule:
    """A renderer module containing the main TSX source and optional imports.

    The main TSX can import from the modules in the imports dict using
    relative paths (e.g., `import styles from './styles.module.css'`).

    A single module can export renderers for multiple thing types via
    a ThingWatcherRenderConfig default export.
    """

    main: str  # Main TSX source code
    imports: dict[str, str]  # Module path -> source code (e.g., './utils.tsx': '...')


@dataclass
class RendererMapping:
    """Maps a thing type to a specific renderer within a module."""

    module_id: str  # Content-addressed ID of the RendererModule
    renderer_key: str  # Key within the module's renderers map


@dataclass
class RendererSourceSpec:
    """Specification for a renderer source - either a path or inline string."""

    path: Path | None = None  # If set, read from disk
    content: str | None = None  # If set, use this content directly

    def __post_init__(self) -> None:
        if self.path is None and self.content is None:
            raise ValueError("RendererSourceSpec must have either path or content")
        if self.path is not None and self.content is not None:
            raise ValueError("RendererSourceSpec cannot have both path and content")


@dataclass
class RendererRegistration:
    """A registration of a renderer bundle with its sources.

    Multiple thing_types can share the same registration (and thus the same
    mtime cache) when registered together via register_renderer().
    """

    typenames: set[str]
    main_spec: RendererSourceSpec
    import_specs: dict[str, RendererSourceSpec]  # import_path -> spec

    # Cached state (invalidated when files change)
    _cached_module_id: str | None = field(default=None, repr=False)
    _cached_mtimes: dict[Path, float] | None = field(default=None, repr=False)


@dataclass
class RendererBuilderResult:
    """Result from a renderer builder function."""

    tsx_source: str
    imports: dict[str, str] = field(default_factory=dict)


# Type alias for renderer builder functions
RendererBuilder = Callable[[str], RendererBuilderResult | None]


# =============================================================================
# Helper Functions
# =============================================================================


def resolve_typename(typename: str | type[Any]) -> str:
    """Convert a type or string to a canonical typename string."""
    if isinstance(typename, type):
        return f"{typename.__module__}.{typename.__qualname__}"
    return typename


def _compute_module_id(main: str, imports: dict[str, str]) -> str:
    """Compute content-addressed module ID using CBOR canonical form."""
    data = cbor2.dumps({"main": main, "imports": imports}, canonical=True)
    digest = hashlib.sha256(data).digest()
    # Base32 encode, lowercase, no padding, truncate to 16 chars
    return base64.b32encode(digest).decode().lower().rstrip("=")[:16]


def _read_spec(spec: RendererSourceSpec) -> str:
    """Read content from a RendererSourceSpec."""
    if spec.content is not None:
        return spec.content
    assert spec.path is not None
    return spec.path.read_text()


def _collect_mtimes(registration: RendererRegistration) -> dict[Path, float]:
    """Collect current mtimes for all path-based specs in a registration."""
    mtimes: dict[Path, float] = {}
    if registration.main_spec.path is not None:
        mtimes[registration.main_spec.path] = registration.main_spec.path.stat().st_mtime
    for spec in registration.import_specs.values():
        if spec.path is not None:
            mtimes[spec.path] = spec.path.stat().st_mtime
    return mtimes


# =============================================================================
# TypeRegistry Class
# =============================================================================


class TypeRegistry:
    """Registry for snapshotters, capturers, rendering configs, and renderers.

    A TypeRegistry holds all type-related configuration. There is one global
    instance, and each Watcher can have its own local instance. Lookups check
    local first, then global.
    """

    def __init__(self) -> None:
        # Snapshotters and capturers
        self._snapshotters: dict[str, StatelessSnapshotter | StatefulOwningSnapshotter] = {}
        self._anchor_capturers: dict[str, AnchorCapturer] = {}

        # Rendering configuration
        self._rendering_configs: dict[str, RenderingConfig] = {}

        # Renderer modules (content-addressed storage)
        self._renderer_modules: dict[str, RendererModule] = {}

        # Static renderer registrations: thing_type -> (registration, renderer_key)
        self._thing_type_registrations: dict[str, tuple[RendererRegistration, str]] = {}

        # Dynamic renderer builders (stack - most recent last, tried in reverse order)
        self._renderer_builders: list[RendererBuilder] = []

    # -------------------------------------------------------------------------
    # Snapshotters
    # -------------------------------------------------------------------------

    def register_snapshotter(
        self,
        typename: str | type[Any],
        snapshotter: StatelessSnapshotter | StatefulOwningSnapshotter,
    ) -> None:
        """Register a snapshotter for a given typename."""
        key = resolve_typename(typename)
        self._snapshotters[key] = snapshotter

    def get_snapshotter(
        self, typename: str
    ) -> StatelessSnapshotter | StatefulOwningSnapshotter | None:
        """Get the registered snapshotter for a typename, or None."""
        return self._snapshotters.get(typename)

    # -------------------------------------------------------------------------
    # Anchor Capturers
    # -------------------------------------------------------------------------

    def register_anchor_capturer(
        self,
        typename: str | type[Any],
        capturer: AnchorCapturer,
    ) -> None:
        """Register an anchor capturer for a given typename."""
        key = resolve_typename(typename)
        self._anchor_capturers[key] = capturer

    def get_anchor_capturer(self, typename: str) -> AnchorCapturer | None:
        """Get the registered anchor capturer for a typename, or None."""
        return self._anchor_capturers.get(typename)

    # -------------------------------------------------------------------------
    # Rendering Configuration
    # -------------------------------------------------------------------------

    def configure_rendering(
        self,
        thing_type: str,
        color: str,
        link_colors: Mapping[str, str] | None = None,
    ) -> None:
        """Configure how a thing type should be rendered."""
        self._rendering_configs[thing_type] = RenderingConfig(
            color=color,
            link_colors=dict(link_colors) if link_colors else {},
        )

    def get_rendering_config(self, thing_type: str) -> RenderingConfig | None:
        """Get the rendering config for a thing type, or None."""
        return self._rendering_configs.get(thing_type)

    def get_all_rendering_configs(self) -> dict[str, RenderingConfig]:
        """Get all rendering configs in this registry."""
        return dict(self._rendering_configs)

    # -------------------------------------------------------------------------
    # Renderer Modules
    # -------------------------------------------------------------------------

    def _get_current_module_id(self, registration: RendererRegistration) -> str:
        """Get the current module ID, re-reading files if they changed.

        Uses mtime-based change detection for efficiency.
        """
        # Check if any file mtimes have changed
        if registration._cached_module_id is not None and registration._cached_mtimes is not None:
            changed = False
            for path, cached_mtime in registration._cached_mtimes.items():
                try:
                    current_mtime = path.stat().st_mtime
                    if current_mtime != cached_mtime:
                        changed = True
                        break
                except OSError:
                    # File no longer exists or inaccessible - treat as changed
                    changed = True
                    break

            if not changed:
                return registration._cached_module_id

        # Read current content from specs
        main_content = _read_spec(registration.main_spec)
        imports_content = {k: _read_spec(v) for k, v in registration.import_specs.items()}

        # Compute module ID
        module_id = _compute_module_id(main_content, imports_content)

        # Store module in _renderer_modules (content-addressed storage)
        self._renderer_modules[module_id] = RendererModule(
            main=main_content, imports=imports_content
        )

        # Log changes
        if (
            registration._cached_module_id is not None
            and registration._cached_module_id != module_id
        ):
            logger.info(
                f"Renderer module changed for {registration.typenames}: "
                f"{registration._cached_module_id} -> {module_id}"
            )

        # Update cache
        registration._cached_module_id = module_id
        registration._cached_mtimes = _collect_mtimes(registration)

        return module_id

    def register_renderer(
        self,
        thing_types: str | Mapping[str, str] | Sequence[str],
        tsx_source: str | Path,
        imports: Mapping[str, str | Path] | None = None,
    ) -> str:
        """Register a renderer module for one or more thing types.

        The renderer module is TSX source code that will be compiled in the browser.
        It must have a default export of type ThingWatcherRenderConfig:

            export default {
                dialect: "0.1",
                renderers: {
                    "TypeName": { full: FullRenderer, ref: RefRenderer, ... },
                }
            } as ThingWatcherRenderConfig;

        The main TSX can import additional modules using relative paths. These modules
        must be provided in the imports dict. Import paths must include extensions
        (e.g., './utils.tsx' not './utils') to match Deno conventions.

        When paths are provided, files are read lazily on demand and changes are
        detected via mtime. Use the frontend refresh button to pick up changes.

        Args:
            thing_types: One of:
                - Single string: one thing type (renderer_key = thing_type)
                - Sequence of strings: multiple thing types (renderer_key = thing_type for each)
                - Mapping from thing_type to renderer_key (for when they differ)
            tsx_source: The TSX source code for the renderer module, or a Path to read from.
            imports: Optional dict mapping relative import paths to source code or Paths.
                Example: {'./utils.tsx': 'export const helper = ...', './styles.module.css': Path('...')}

        Returns:
            The content-addressed module ID (for debugging/inspection).
        """
        # Create source specs
        if isinstance(tsx_source, Path):
            main_spec = RendererSourceSpec(path=tsx_source)
        else:
            main_spec = RendererSourceSpec(content=tsx_source)

        import_specs: dict[str, RendererSourceSpec] = {}
        if imports:
            for import_path, source in imports.items():
                if isinstance(source, Path):
                    import_specs[import_path] = RendererSourceSpec(path=source)
                else:
                    import_specs[import_path] = RendererSourceSpec(content=source)

        # Normalize thing_types to a mapping
        if isinstance(thing_types, str):
            # Single string: identity mapping
            type_to_key: dict[str, str] = {thing_types: thing_types}
        elif isinstance(thing_types, Mapping):
            # Explicit mapping
            type_to_key = dict(thing_types)
        else:
            # Sequence: identity mapping for each
            type_to_key = {t: t for t in thing_types}

        # Create registration
        registration = RendererRegistration(
            typenames=set(type_to_key.keys()), main_spec=main_spec, import_specs=import_specs
        )

        # Register each thing_type -> (registration, renderer_key)
        # All thing_types share the same registration object (and thus the same mtime cache)
        for thing_type, renderer_key in type_to_key.items():
            self._thing_type_registrations[thing_type] = (registration, renderer_key)

        # Compute and return initial module ID (this reads files and caches)
        return self._get_current_module_id(registration)

    def register_renderer_builder(self, builder: RendererBuilder) -> None:
        """Register a dynamic renderer builder function.

        Builder functions are called for thing types that don't have a static
        renderer registration. They are tried in most-recent-first order (stack).

        The builder function takes a thing_type string and returns either:
        - RendererBuilderResult(tsx_source, imports) if it can handle this type
        - None if it can't handle this type

        Builders are called on every lookup (not cached), but the generated content
        gets a content-addressed module_id, so unchanged content won't recompile.

        Args:
            builder: A callable that takes thing_type and returns RendererBuilderResult or None.
        """
        self._renderer_builders.append(builder)

    def unregister_renderer_builder(self, builder: RendererBuilder) -> None:
        """Unregister a previously registered renderer builder.

        Args:
            builder: The builder function to unregister.

        Raises:
            ValueError: If the builder is not registered.
        """
        try:
            self._renderer_builders.remove(builder)
        except ValueError:
            raise ValueError("Builder is not registered") from None

    def _try_builders(self, thing_type: str) -> RendererMapping | None:
        """Try renderer builders in most-recent-first order.

        Returns a RendererMapping if a builder handles this type, or None.
        """
        # Try builders in reverse order (most recent first)
        for builder in reversed(self._renderer_builders):
            try:
                result = builder(thing_type)
                if result is not None:
                    # Builder handled this type - compute module ID and store
                    module_id = _compute_module_id(result.tsx_source, result.imports)
                    self._renderer_modules[module_id] = RendererModule(
                        main=result.tsx_source, imports=result.imports
                    )
                    return RendererMapping(module_id=module_id, renderer_key=thing_type)
            except Exception:
                # Builder raised an exception - log and try next
                logger.exception(f"Renderer builder raised exception for type {thing_type}")
                continue
        return None

    def get_renderer_mapping(self, thing_type: str) -> RendererMapping | None:
        """Get the current renderer mapping for a thing type, or None.

        Checks static registrations first, then tries builders.
        This checks for file changes (via mtime) for path-based registrations.
        """
        # Check static registrations first
        entry = self._thing_type_registrations.get(thing_type)
        if entry:
            registration, renderer_key = entry
            module_id = self._get_current_module_id(registration)
            return RendererMapping(module_id=module_id, renderer_key=renderer_key)

        # Try builders
        return self._try_builders(thing_type)

    def get_renderer_module(self, module_id: str) -> RendererModule | None:
        """Get a renderer module by its content-addressed ID, or None."""
        return self._renderer_modules.get(module_id)

    def get_all_renderer_mappings(self) -> dict[str, RendererMapping]:
        """Get all thing type -> renderer mappings from static registrations.

        Note: This does NOT include dynamically-built renderers, since those
        are generated on-demand per thing_type lookup.

        This checks for file changes (via mtime) and returns up-to-date module IDs.
        """
        result: dict[str, RendererMapping] = {}
        for thing_type, (registration, renderer_key) in self._thing_type_registrations.items():
            module_id = self._get_current_module_id(registration)
            result[thing_type] = RendererMapping(module_id=module_id, renderer_key=renderer_key)
        return result

    def get_all_renderer_modules(self) -> dict[str, RendererModule]:
        """Get all registered renderer modules."""
        return dict(self._renderer_modules)

    def clear(self) -> None:
        """Clear all registrations in this registry."""
        self._snapshotters.clear()
        self._anchor_capturers.clear()
        self._rendering_configs.clear()
        self._renderer_modules.clear()
        self._thing_type_registrations.clear()
        self._renderer_builders.clear()


# =============================================================================
# Global Registry Instance
# =============================================================================

_global_registry = TypeRegistry()


# =============================================================================
# Module-Level Functions (delegate to global registry)
# =============================================================================


def register_snapshotter(
    typename: str | type[Any],
    snapshotter: StatelessSnapshotter | StatefulOwningSnapshotter,
) -> None:
    """Register a snapshotter globally for a given typename."""
    _global_registry.register_snapshotter(typename, snapshotter)


def get_snapshotter(typename: str) -> StatelessSnapshotter | StatefulOwningSnapshotter | None:
    """Get the globally registered snapshotter for a typename, or None."""
    return _global_registry.get_snapshotter(typename)


def register_anchor_capturer(
    typename: str | type[Any],
    capturer: AnchorCapturer,
) -> None:
    """Register an anchor capturer globally for a given typename."""
    _global_registry.register_anchor_capturer(typename, capturer)


def get_anchor_capturer(typename: str) -> AnchorCapturer | None:
    """Get the globally registered anchor capturer for a typename, or None."""
    return _global_registry.get_anchor_capturer(typename)


def configure_rendering(
    thing_type: str,
    color: str,
    link_colors: Mapping[str, str] | None = None,
) -> None:
    """Configure how a thing type should be rendered."""
    _global_registry.configure_rendering(thing_type, color, link_colors)


def get_rendering_config(thing_type: str) -> RenderingConfig | None:
    """Get the rendering config for a thing type, or None."""
    return _global_registry.get_rendering_config(thing_type)


def get_all_rendering_configs() -> dict[str, RenderingConfig]:
    """Get all rendering configs."""
    return _global_registry.get_all_rendering_configs()


def register_renderer(
    thing_types: str | Mapping[str, str] | Sequence[str],
    tsx_source: str | Path,
    imports: Mapping[str, str | Path] | None = None,
) -> str:
    """Register a renderer module for one or more thing types.

    See TypeRegistry.register_renderer for full documentation.
    """
    return _global_registry.register_renderer(thing_types, tsx_source, imports)


def register_renderer_builder(builder: RendererBuilder) -> None:
    """Register a dynamic renderer builder function globally.

    See TypeRegistry.register_renderer_builder for full documentation.
    """
    _global_registry.register_renderer_builder(builder)


def unregister_renderer_builder(builder: RendererBuilder) -> None:
    """Unregister a previously registered renderer builder.

    See TypeRegistry.unregister_renderer_builder for full documentation.
    """
    _global_registry.unregister_renderer_builder(builder)


def get_renderer_mapping(thing_type: str) -> RendererMapping | None:
    """Get the current renderer mapping for a thing type, or None.

    This checks for file changes (via mtime) and returns an up-to-date module ID.
    Also tries renderer builders if no static registration exists.
    """
    return _global_registry.get_renderer_mapping(thing_type)


def get_renderer_module(module_id: str) -> RendererModule | None:
    """Get a renderer module by its content-addressed ID, or None."""
    return _global_registry.get_renderer_module(module_id)


def get_all_renderer_mappings() -> dict[str, RendererMapping]:
    """Get all thing type -> renderer mappings.

    This checks for file changes (via mtime) and returns up-to-date module IDs.
    """
    return _global_registry.get_all_renderer_mappings()


def get_all_renderer_modules() -> dict[str, RendererModule]:
    """Get all registered renderer modules."""
    return _global_registry.get_all_renderer_modules()


def get_global_registry() -> TypeRegistry:
    """Get the global TypeRegistry instance."""
    return _global_registry


def clear_global_registries() -> None:
    """Clear all global registries. Useful for testing."""
    _global_registry.clear()
