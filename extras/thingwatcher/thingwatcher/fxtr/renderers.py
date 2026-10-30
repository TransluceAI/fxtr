"""Renderer registration and builder for fxtr entity types.

This module provides:
- A registry for custom renderers per fxtr type
- A renderer builder that generates renderers for fxtr entities
- A fallback renderer that makes fxtr link markers clickable
"""

from __future__ import annotations

from pathlib import Path

from fxtr2.data.entity_core import FxtrID
from thingwatcher.registry import RendererBuilderResult

# Global registry: fxtr type ID (as string) -> (tsx_source, imports)
_fxtr_renderers: dict[str, tuple[str | Path, dict[str, str | Path] | None]] = {}

# Prefix for fxtr entity thing types
FXTR_ENTITY_TYPE_PREFIX = "fxtr.transluce.org/fxtr_entity/d/"
FXTR_FALLBACK_TYPE = "fxtr.transluce.org/fxtr_entity_fallback"

# Path to component files
_COMPONENTS_DIR = Path(__file__).parent / "components"
_FALLBACK_RENDERER_PATH = _COMPONENTS_DIR / "FxtrEntityFallback.tsx"
_NAMESPACE_RENDERER_PATH = _COMPONENTS_DIR / "FxtrNamespace.tsx"


def _read_source(source: str | Path) -> str:
    """Read source from a Path or return string as-is."""
    if isinstance(source, Path):
        return source.read_text()
    return source


def _read_imports(imports: dict[str, str | Path] | None) -> dict[str, str] | None:
    """Read all import sources, converting Paths to strings."""
    if imports is None:
        return None
    return {key: _read_source(value) for key, value in imports.items()}


def register_fxtr_renderer(
    fxtr_type_id: FxtrID | str,
    tsx_source: str | Path,
    imports: dict[str, str | Path] | None = None,
) -> None:
    """Register a custom thingwatcher renderer for an fxtr entity type.

    The renderer will be used for things with thing_type
    "fxtr.transluce.org/fxtr_entity/d/<type_id_string>".

    Args:
        fxtr_type_id: The FxtrID of the type (or its id_string)
        tsx_source: TSX source code or path to file
        imports: Optional additional imports for the renderer
    """
    if isinstance(fxtr_type_id, FxtrID):
        type_str = fxtr_type_id.id_string()
    else:
        type_str = fxtr_type_id

    _fxtr_renderers[type_str] = (tsx_source, imports)


def unregister_fxtr_renderer(fxtr_type_id: FxtrID | str) -> None:
    """Unregister a custom renderer for an fxtr entity type.

    Args:
        fxtr_type_id: The FxtrID of the type (or its id_string)

    Raises:
        KeyError: If no renderer is registered for this type
    """
    if isinstance(fxtr_type_id, FxtrID):
        type_str = fxtr_type_id.id_string()
    else:
        type_str = fxtr_type_id

    del _fxtr_renderers[type_str]


def fxtr_renderer_builder(thing_type: str) -> RendererBuilderResult | None:
    """Renderer builder for fxtr entity types.

    This function is registered with thingwatcher via register_renderer_builder().
    It handles thing_types matching "fxtr.transluce.org/fxtr_entity/d/<type_id>".

    Behavior:
    1. If thing_type doesn't match our patterns, return None (let other builders handle it)
    2. For typed entities: check if a custom renderer is registered, return it if so
    3. For fallback type or unregistered types: return the fallback renderer

    The fallback renderer enhances the default JSON view by:
    - Highlighting $!fxtr.link.* markers as clickable links
    - Showing the entity type prominently
    - Formatting the data in a readable way

    Args:
        thing_type: The thing_type to build a renderer for

    Returns:
        RendererBuilderResult if we can handle this type, None otherwise
    """
    # Handle the fallback type
    if thing_type == FXTR_FALLBACK_TYPE:
        return RendererBuilderResult(tsx_source=_read_source(_FALLBACK_RENDERER_PATH))

    # Handle typed entities
    if thing_type.startswith(FXTR_ENTITY_TYPE_PREFIX):
        type_id = thing_type[len(FXTR_ENTITY_TYPE_PREFIX) :]

        # Check for custom renderer
        if type_id in _fxtr_renderers:
            tsx_source, imports = _fxtr_renderers[type_id]
            return RendererBuilderResult(
                tsx_source=_read_source(tsx_source),
                imports=_read_imports(imports),
            )

        # Use fallback for unregistered types
        # Generate a renderer that reuses the fallback but registers under this type
        tsx = f"""
import FallbackRenderer from "./fallback.tsx";

export default {{
  dialect: "0.1",
  renderers: {{
    "{thing_type}": FallbackRenderer.renderers["fxtr.transluce.org/fxtr_entity_fallback"],
  }},
}} as const;
"""
        return RendererBuilderResult(
            tsx_source=tsx,
            imports={"./fallback.tsx": _read_source(_FALLBACK_RENDERER_PATH)},
        )

    # Handle namespace type
    if thing_type == "fxtr.transluce.org/fxtr_namespace":
        return RendererBuilderResult(tsx_source=_read_source(_NAMESPACE_RENDERER_PATH))

    # Not our type
    return None
