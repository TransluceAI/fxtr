# Stage 2: Visualizing fxtr2 Data in Thingwatcher

## Overview

This plan describes how to integrate fxtr2 with thingwatcher, allowing users to visualize and inspect fxtr2 entities stored in namespaces through thingwatcher's UI.

**Key goals:**
1. Mount an fxtr2 namespace (or `PersistentPinningBackend`) as a thingwatcher data source
2. Navigate the entity graph by following fxtr links
3. Register custom renderers for specific fxtr entity types
4. See the current pins in a namespace as a "root" entry point

## Design Decisions

Based on discussion:
- **Code location**: `fxtr2/ui/` module with thingwatcher as optional dependency
- **Watcher management**: Always attach to an existing watcher (defaults to the active watcher from context)
- **Pins listing**: Namespace is a tracked snapshot object showing current pins (updates on tick)
- **Metadata fields**: Keep `$fxtr.ctx` and `$fxtr.type` visible in the JSON data (transformed to readable form)
- **Backlinks**: Use fxtr2's `ObservableStore.backrefs()` to populate thingwatcher backlinks
- **Renderer fallback**: Auto-generate a generic renderer for fxtr entity types without custom renderers
- **Thing keys**: String-based keys only: `["fxtr_entity", "<id_string>"]`

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Thingwatcher UI                             │
│  (Graph view, Details panel, Timeline, Custom renderers)            │
└─────────────────────────────┬───────────────────────────────────────┘
                              │ GraphQL queries
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Thingwatcher Watcher                        │
│  - Manages ticks, things, links                                     │
│  - Has registered: ImmutableStore, Snapshotter, AnchorCapturer,     │
│                    RendererBuilder                                  │
└──────┬──────────────────────┬────────────────────────┬──────────────┘
       │                      │                        │
       ▼                      ▼                        ▼
┌──────────────┐   ┌───────────────────┐   ┌────────────────────────┐
│ FxtrImm.     │   │ FxtrNamespace     │   │ fxtr_renderer_builder  │
│ Store        │   │ Snapshotter       │   │                        │
│              │   │                   │   │ - Custom renderers per │
│ - get_thing_ │   │ - Produces thing  │   │   fxtr type            │
│   state()    │   │   showing pins    │   │ - Fallback renderer    │
│ - get_back   │   │ - Links to each   │   │   for link markers     │
│   links()    │   │   pinned entity   │   └────────────────────────┘
└──────┬───────┘   └─────────┬─────────┘
       │                     │
       └──────────┬──────────┘
                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    fxtr2 PersistentPinningBackend                   │
│  (SQLiteFxtrNamespace, GitNamespaceBackend, etc.)                   │
│  - list_pins() -> dict[str, FxtrID]                                 │
│  - ObservableStore: try_lookup(), backrefs()                        │
└─────────────────────────────────────────────────────────────────────┘

fxtr_namespace_view() context manager:
  - On enter: registers ImmutableStore, Snapshotter, AnchorCapturer,
              RendererBuilder, and adds namespace as root
  - On exit:  unregisters everything
```

## Thing Key Schema

All fxtr entities in thingwatcher use the key format:
```python
["fxtr_entity", entity_id.id_string()]
# Example: ["fxtr_entity", "dH4sIAAAAA..."]
```

Namespace things use:
```python
["fxtr_namespace", namespace_name]
# Example: ["fxtr_namespace", "myns"]
```

## CBOR to JSON Transformation

When transforming fxtr entity data for thingwatcher:

### ID Reference Transformation

Each fxtr ID in the CBOR data becomes:
- A JSON object with a special marker key
- A thingwatcher link to the referenced entity

| CBOR Type | JSON Marker | Link Type |
|-----------|-------------|-----------|
| `RawWeakFxtrID(id)` | `{"$!fxtr.link.weak": ["fxtr_entity", id.id_string()]}` | `"weak"` |
| `RawStrongFxtrID(id)` | `{"$!fxtr.link.strong": ["fxtr_entity", id.id_string()]}` | `"strong"` |
| `RawFxtrEntityMarkerID(id)` | `{"$!fxtr.link.marker": ["fxtr_entity", id.id_string()]}` | `"marker"` |

### Link Keys

The link key for each reference is the path (as a list) to that reference in the entity data. For example:
```python
# Entity data (CBOR):
{
    "$fxtr.ctx": RawFxtrEntityMarkerID(...),
    "$fxtr.type": RawWeakFxtrID(type_id),
    "parent": RawStrongFxtrID(parent_id),
    "children": [RawWeakFxtrID(child1), RawWeakFxtrID(child2)]
}

# Resulting links (all link_keys are lists):
ThingLink(link_type="marker", link_key=["$fxtr.ctx"], to_thing_key=["fxtr_entity", "..."])
ThingLink(link_type="weak", link_key=["$fxtr.type"], to_thing_key=["fxtr_entity", "..."])
ThingLink(link_type="strong", link_key=["parent"], to_thing_key=["fxtr_entity", "..."])
ThingLink(link_type="weak", link_key=["children", 0], to_thing_key=["fxtr_entity", "..."])
ThingLink(link_type="weak", link_key=["children", 1], to_thing_key=["fxtr_entity", "..."])
```

### Thing Type Derivation

The `thing_type` for fxtr entities is derived from the entity's node type and `$fxtr.type` field:
- For d-node entities (ground data nodes) that have a `$fxtr.type` field: `"fxtr.transluce.org/fxtr_entity/d/<type_id_string>"`
- For other entities (no `$fxtr.type`, or non-d-node entities): `"fxtr.transluce.org/fxtr_entity_fallback"`

This allows registering renderers per fxtr type.

## Module Design

### `fxtr2/ui/__init__.py`

Re-exports public API.

### `fxtr2/ui/namespace_view.py`

Main module for mounting a namespace in thingwatcher.

```python
@asynccontextmanager
async def fxtr_namespace_view(
    namespace: PersistentPinningBackend | FxtrNamespace,
    name: str,
    watcher: Watcher | None = None,
):
    """Async context manager that mounts an fxtr namespace in thingwatcher.

    While active, registers:
    - An ImmutableStore for accessing fxtr entities
    - A snapshotter/anchor capturer for the namespace object
    - A root entry point for the namespace
    - A renderer builder for fxtr entity types

    On exit, unregisters everything.

    Args:
        namespace: The fxtr namespace to visualize. Can be either:
            - A PersistentPinningBackend (must also implement ObservableStore)
            - A FxtrNamespace (wraps PersistentPinningBackend with a scope)
            If FxtrNamespace is provided, uses its underlying backend.
        name: Display name for this namespace in thingwatcher
        watcher: The watcher to attach to. If None, uses the active watcher from context.

    Yields:
        The namespace object (pass-through for convenience)

    Example:
        async with watcher.watching_and_serving():
            async with fxtr_namespace_view(ns, "my_namespace"):
                watcher.tick("info", "Initial state")
                # ... use the watcher normally ...
    """
    # Extract backend if FxtrNamespace provided
    backend = namespace._backend if isinstance(namespace, FxtrNamespace) else namespace
    ...
```

### `fxtr2/ui/immutable_store.py`

Implements thingwatcher's `ImmutableStore` protocol for fxtr entities.

```python
class FxtrImmutableStore:
    """ImmutableStore that provides lazy access to fxtr entities."""

    def __init__(self, backend: ObservableStore):
        ...

    async def get_thing_state(self, key: Any) -> ThingState | None:
        """Look up an fxtr entity by thingwatcher key."""
        # 1. Parse key: ["fxtr_entity", id_string] -> FxtrID
        # 2. Call backend.try_lookup(fxtr_id)
        # 3. If found, load entity data and transform to ThingState
        # 4. Return ThingState with transformed JSON + links
        ...

    async def get_backlinks(self, key: Any) -> list[BacklinkInfo]:
        """Get backlinks using fxtr's backref system."""
        # 1. Parse key to FxtrID
        # 2. Call backend.backrefs(fxtr_id)
        # 3. Convert EntityBackref/OtherBackref to BacklinkInfo
        ...
```

### `fxtr2/ui/cbor_transform.py`

Utilities for transforming fxtr entities to thingwatcher ThingStates.

```python
def fxtr_entity_to_thing_state(
    entity_id: FxtrID,
    cbor_data: FxtrCbor,
) -> ThingState:
    """Transform an fxtr entity to a thingwatcher ThingState.

    This is the main transformation function. It:
    1. Derives the thing_type from the entity's node type and $fxtr.type
    2. Recursively transforms CBOR data to JSON, converting fxtr IDs to marker objects
    3. Extracts ThingLinks for each fxtr ID reference found
    4. Returns a complete ThingState ready for thingwatcher

    Args:
        entity_id: The FxtrID of the entity
        cbor_data: The loaded CBOR data for the entity

    Returns:
        A ThingState with key, thing_type, thing_data (JSON), and links
    """
    ...

def fxtr_key_to_thing_key(entity_id: FxtrID) -> list:
    """Convert FxtrID to thingwatcher key: ["fxtr_entity", id_string]"""
    return ["fxtr_entity", entity_id.id_string()]

def thing_key_to_fxtr_id(thing_key: Any) -> FxtrID | None:
    """Parse thingwatcher key back to FxtrID, or None if invalid."""
    ...
```

### `fxtr2/ui/snapshotter.py`

Snapshotter and anchor capturer for namespace objects.

**Async Solution: Using `AsyncThingUpdate`**

Thingwatcher's `AsyncThingUpdate` allows snapshotters to return async work:
- `immediate_update`: Sync result shown at current tick (cached pins or placeholder)
- `async_updater`: Async callable that fetches fresh data; result appears in streaming tick

This lets us call the async `list_pins()` without blocking.

```python
class FxtrNamespaceSnapshotter(StatefulOwningSnapshotter):
    """Snapshotter that captures the current state of an fxtr namespace.

    Uses AsyncThingUpdate to handle async list_pins() call.
    """

    def __init__(self, backend: PersistentPinningBackend, name: str):
        self._backend = backend
        self._name = name
        self._cached_pins: dict[str, FxtrID] = {}  # Cache for immediate_update

    def stateless_snapshot(self, obj: Any) -> AsyncThingUpdate[StatelessResult]:
        """Return async update that fetches current pins."""
        # Immediate: use cached pins (marked as stale)
        immediate = self._make_thing_state(self._cached_pins, stale=True)

        async def fetch_pins() -> ThingState:
            pins = await self._backend.list_pins()
            self._cached_pins = pins  # Update cache
            return self._make_thing_state(pins, stale=False)

        return AsyncThingUpdate(immediate_update=immediate, async_updater=fetch_pins)

    def snapshot_owned(self, obj: Any) -> AsyncThingUpdate[StatefulResult]:
        """Delegates to stateless_snapshot."""
        return self.stateless_snapshot(obj)

    def _make_thing_state(self, pins: dict[str, FxtrID], *, stale: bool) -> ThingState:
        """Build ThingState from pins dict."""
        links = [
            ThingLink(
                link_type="pin",
                link_key=["pins", pin_name],
                to_thing_key=fxtr_key_to_thing_key(entity_id),
            )
            for pin_name, entity_id in pins.items()
        ]
        return ThingState(
            key=["fxtr_namespace", self._name],
            thing_type="fxtr.transluce.org/fxtr_namespace",
            thing_data={"name": self._name, "pin_count": len(pins), "stale": stale},
            links=links,
        )


class FxtrNamespaceAnchorCapturer(AnchorCapturer):
    """Anchor capturer for fxtr namespace objects."""

    def __init__(self, name: str):
        self._name = name

    def handle(self, anchor: Any) -> ThingRef:
        return ThingRef(key=["fxtr_namespace", self._name])
```

### `fxtr2/ui/renderers.py`

Renderer registration and builder for fxtr types.

**Background: Thingwatcher Renderer Builders**

Thingwatcher supports dynamic renderer generation via "renderer builders". A `RendererBuilder` is a callable with signature:

```python
def builder(thing_type: str) -> RendererBuilderResult | None:
    """Generate a renderer for a thing type, or return None to pass."""
    ...

@dataclass
class RendererBuilderResult:
    tsx_source: str | Path
    imports: dict[str, str | Path] | None = None
```

Builders are called when thingwatcher needs to render a thing type that doesn't have a statically registered renderer. Multiple builders can be registered; they're tried in most-recent-first order until one returns a result.

**Our Design:**

```python
# Global registry: fxtr type ID (as string) -> (tsx_source, imports)
_fxtr_renderers: dict[str, tuple[str | Path, dict[str, str | Path] | None]] = {}

def register_fxtr_renderer(
    fxtr_type_id: FxtrID | str,
    tsx_source: str | Path,
    imports: dict[str, str | Path] | None = None,
):
    """Register a custom thingwatcher renderer for an fxtr entity type.

    The renderer will be used for things with thing_type
    "fxtr.transluce.org/fxtr_entity/d/<type_id_string>".

    Args:
        fxtr_type_id: The FxtrID of the type (or its id_string)
        tsx_source: TSX source code or path to file
        imports: Optional additional imports for the renderer
    """
    ...

def fxtr_renderer_builder(thing_type: str) -> RendererBuilderResult | None:
    """Renderer builder for fxtr entity types.

    This function is registered with thingwatcher via register_renderer_builder().
    It handles thing_types matching "fxtr.transluce.org/fxtr_entity/d/<type_id>".

    Behavior:
    1. If thing_type doesn't match our prefix, return None (let other builders handle it)
    2. Extract the type_id from the thing_type
    3. If a custom renderer is registered for this type_id, return it
    4. Otherwise, return a fallback renderer that renders fxtr link markers nicely

    The fallback renderer enhances the default JSON view by:
    - Highlighting $!fxtr.link.* markers as clickable links
    - Showing the entity ID and type prominently
    - Formatting the $fxtr.ctx and $fxtr.type fields readably
    """
    ...
```

The fallback renderer provides fxtr-specific enhancements over thingwatcher's generic JSON view - primarily making the link markers (`$!fxtr.link.weak`, etc.) render as actual clickable node references instead of raw JSON objects.

## Implementation Steps

**Note:** Tests for ui/ functionality should go in `tests/ui/` rather than at the top level.

### Phase 1: Core Infrastructure

- [x] 1. Create `fxtr2/ui/` directory structure with `__init__.py`
- [x] 2. Add thingwatcher as optional dependency in `v2/pyproject.toml`
- [x] 3. Update `CODE_OVERVIEW.md` with high-level description of ui/ module
- [x] 4. Implement `fxtr2/ui/cbor_transform.py`:
  - [x] `fxtr_entity_to_thing_state()` - main transformation function
  - [x] `fxtr_key_to_thing_key()` and `thing_key_to_fxtr_id()` - key conversion
- [x] 5. Implement `fxtr2/ui/immutable_store.py`:
  - [x] `FxtrImmutableStore` class
  - [x] `get_thing_state()` with entity lookup and transformation
  - [x] `get_backlinks()` using ObservableStore.backrefs()
- [x] 6. Write tests in `tests/ui/` for CBOR transform and immutable store
- [x] 7. Create `fxtr2/ui/LOCAL_CODE_OVERVIEW.md` documenting phase 1 modules

### Phase 2: Namespace Tracking

- [x] 8. Implement `fxtr2/ui/snapshotter.py`:
  - [x] `FxtrNamespaceSnapshotter` using `AsyncThingUpdate` for async `list_pins()`
  - [x] `FxtrNamespaceAnchorCapturer`
- [x] 9. Implement `fxtr2/ui/namespace_view.py`:
  - [x] `fxtr_namespace_view()` async context manager
  - [x] Support both `PersistentPinningBackend` and `FxtrNamespace`
  - [x] Store/snapshotter/capturer/renderer registration
  - [x] Cleanup on exit
- [x] 10. Write tests in `tests/ui/` for namespace view
- [x] 11. Update `fxtr2/ui/LOCAL_CODE_OVERVIEW.md` with phase 2 modules

### Phase 3: Renderers

- [x] 12. Implement `fxtr2/ui/renderers.py`:
  - [x] `register_fxtr_renderer()` function
  - [x] `fxtr_renderer_builder()` function
  - [x] Fallback renderer with link marker handling
- [x] 13. Write tests in `tests/ui/` for renderer registration
- [x] 14. Update `fxtr2/ui/LOCAL_CODE_OVERVIEW.md` with phase 3 modules

### Phase 4: Integration & Polish

- [ ] 15. Create example/demo script showing usage
- [x] 16. Add docstrings and type annotations throughout
- [x] 17. Final review and update of `fxtr2/ui/LOCAL_CODE_OVERVIEW.md`

## Example Usage

```python
import anyio
from thingwatcher import Watcher
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.ui import fxtr_namespace_view

async def main():
    watcher = Watcher(db_path="/tmp/demo.db", port=8765)

    async with watcher.watching_and_serving():
        async with SQLiteFxtrNamespace("sqlite+aiosqlite:///data.db", "myns") as ns:
            # Create some entities and pin them
            # ...

            # Mount namespace in thingwatcher
            async with fxtr_namespace_view(ns, "my_namespace", watcher):
                # Initial tick to show pins
                watcher.tick("info", "Initial state")

                # Keep running until interrupted
                await anyio.sleep_forever()

anyio.run(main)
```

## Open Questions

### Future Work

- **Entity type display**: Currently we use the full `$fxtr.type` ID string. Could make this more user-friendly by looking up type names from a registry.

- **Provenance visualization**: Could add special handling to visualize provenance relationships (unwrap arrows, context hierarchies).

- **Multiple namespaces**: Current design mounts one namespace per view. Could extend to mount multiple namespaces in a single thingwatcher.

- **Live updates**: Currently requires explicit `tick()` calls. Could add file system watching for GitNamespaceBackend to auto-tick on changes.
