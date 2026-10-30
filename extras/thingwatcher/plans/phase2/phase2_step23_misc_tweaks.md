# Phase 2 Step 23: TypeRegistry Refactor + Dynamic Renderer Builders

## Overview

Refactor the registry system to use a `TypeRegistry` class that encapsulates all type-related configuration. This enables watcher-local overrides for all registry types (not just snapshotters/anchor capturers), and provides a clean place to add dynamic renderer builder support.

## Goals

1. **TypeRegistry class**: Consolidate scattered global state into a reusable class
2. **Local/global pattern**: Watcher-local registry checked before global registry
3. **Dynamic renderer builders**: Register functions that generate renderers on-demand for unrecognized types

## Design

### TypeRegistry Class

```python
@dataclass
class RendererBuilderResult:
    """Result from a renderer builder function."""
    tsx_source: str
    imports: dict[str, str] = field(default_factory=dict)

# Type alias
RendererBuilder = Callable[[str], RendererBuilderResult | None]

class TypeRegistry:
    """Registry for snapshotters, capturers, rendering configs, and renderers."""

    def __init__(self):
        self._snapshotters: dict[str, Snapshotter] = {}
        self._anchor_capturers: dict[str, AnchorCapturer] = {}
        self._rendering_configs: dict[str, RenderingConfig] = {}
        self._renderer_modules: dict[str, RendererModule] = {}
        self._thing_type_registrations: dict[str, tuple[RendererRegistration, str]] = {}
        self._renderer_builders: list[RendererBuilder] = []  # Stack

    # Registration methods
    def register_snapshotter(self, typename, snapshotter): ...
    def register_anchor_capturer(self, typename, capturer): ...
    def configure_rendering(self, thing_type, color, link_colors): ...
    def register_renderer(self, thing_types, tsx_source, imports): ...
    def register_renderer_builder(self, builder): ...
    def unregister_renderer_builder(self, builder): ...

    # Lookup methods (return None if not found - caller chains to next registry)
    def get_snapshotter(self, typename): ...
    def get_anchor_capturer(self, typename): ...
    def get_rendering_config(self, thing_type): ...
    def get_renderer_mapping(self, thing_type): ...  # Checks static, then builders
    def get_renderer_module(self, module_id): ...

    # Bulk methods
    def get_all_rendering_configs(self): ...
    def get_all_renderer_mappings(self): ...
    def get_all_renderer_modules(self): ...
    def clear(self): ...
```

### Lookup Order

For all lookups:
1. Check watcher's local registry
2. If not found, check global registry

For renderer builders specifically:
1. Check static registrations (local, then global)
2. If not found, try builders (local builders first in reverse order, then global builders in reverse order)
3. First builder to return non-None wins

### Dynamic Renderer Builders

Builder functions take a `thing_type` string and return either:
- `RendererBuilderResult(tsx_source, imports)` if they can handle this type
- `None` if they can't handle this type

Builders are called on every lookup (no caching of builder→type mapping), but the generated content gets a content-addressed module_id, so unchanged content won't recompile.

```python
def my_builder(thing_type: str) -> RendererBuilderResult | None:
    if thing_type.startswith("Dynamic"):
        return RendererBuilderResult(
            tsx_source=f'export default {{ dialect: "0.1", renderers: {{ "{thing_type}": ... }} }}'
        )
    return None

tw.register_renderer_builder(my_builder)
```

## Implementation Steps

- [x] 1. Create `TypeRegistry` class in registry.py with all state and methods
- [x] 2. Add `RendererBuilderResult` dataclass and `RendererBuilder` type alias
- [x] 3. Implement builder registration/unregistration and lookup in TypeRegistry
- [x] 4. Create global `_global_registry` instance and update module-level functions to delegate
- [x] 5. Update Watcher to use `_local_registry: TypeRegistry` instead of separate dicts
- [x] 6. Update Watcher lookup methods to check local then global
- [x] 7. Add `register_renderer_builder` and `unregister_renderer_builder` to api.py
- [x] 8. Add demo: DynamicDateThing class + builder in demo_dynamic_rendering
- [x] 9. Add tests for dynamic renderer builders
- [x] 10. Update CODE_OVERVIEW.md

## Demo: DynamicDateThing

In `examples/demo_dynamic_rendering/demo.py`:

```python
class DynamicDateThing:
    """A thing whose renderer is built dynamically with the current date."""
    def __init__(self, name: str):
        self.name = name

class DynamicDateThingSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: DynamicDateThing) -> ThingState:
        return ThingState(
            key=("dynamic_date", obj.name),
            thing_type="DynamicDateThing",
            thing_data={"name": obj.name},
            links=[],
        )

def dynamic_date_builder(thing_type: str) -> RendererBuilderResult | None:
    if thing_type == "DynamicDateThing":
        from datetime import datetime
        now = datetime.now().isoformat()
        return RendererBuilderResult(
            tsx_source=f'''
import type {{ FullRendererProps, ThingWatcherRenderConfig }} from 'thingwatcher-bindings';
import {{ usePayload }} from 'thingwatcher-bindings';

function DynamicDateFull({{ handle }}: FullRendererProps) {{
    const payload = usePayload(handle);
    return (
        <div style={{{{ padding: '12px' }}}}>
            <div style={{{{ fontWeight: 'bold' }}}}>{{payload?.name}}</div>
            <div style={{{{ color: '#888', marginTop: '8px' }}}}>
                Renderer built at: {now}
            </div>
            <div style={{{{ color: '#666', fontSize: '12px', marginTop: '4px' }}}}>
                (Refresh to see this timestamp change)
            </div>
        </div>
    );
}}

export default {{
    dialect: "0.1",
    renderers: {{
        "DynamicDateThing": {{ full: DynamicDateFull }}
    }}
}} as ThingWatcherRenderConfig;
'''
        )
    return None

# In setup_demo():
register_renderer_builder(dynamic_date_builder)
```

When the user clicks the refresh button in the Timeline, the builder runs again with a new timestamp, generating new TSX content with a new module_id, which gets recompiled.

## Files to Modify

- `thingwatcher/registry.py` - TypeRegistry class, refactor globals
- `thingwatcher/api.py` - Add register_renderer_builder, unregister_renderer_builder
- `thingwatcher/__init__.py` - Export new functions
- `thingwatcher/watcher.py` - Use TypeRegistry for local overrides
- `examples/demo_dynamic_rendering/demo.py` - Add DynamicDateThing demo
- `tests/` - Add tests for builder functionality
- `CODE_OVERVIEW.md` - Document new features
