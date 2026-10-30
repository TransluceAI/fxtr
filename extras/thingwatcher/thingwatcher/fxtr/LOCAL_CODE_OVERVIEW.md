# thingwatcher/fxtr/ Code Overview

This module provides integration between fxtr2 and thingwatcher, allowing visualization of fxtr entities in thingwatcher's debugging UI.

## Overview

The fxtr module transforms fxtr entities into thingwatcher's data model and provides lazy access to them via thingwatcher's ImmutableStore protocol. This enables exploring fxtr entity graphs through thingwatcher's visual interface.

## Quick Start

### Option 1: Using fxtr_namespace_view (explicit namespace mounting)

```python
import anyio
from thingwatcher import Watcher
from thingwatcher.fxtr import fxtr_namespace_view
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace

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

### Option 2: Using track_id (automatic entity tracking)

For simpler use cases where you just want to link to fxtr entities from your snapshotters:

```python
from thingwatcher import Watcher, ThingState, ThingLink, StatelessSnapshotter
from thingwatcher.fxtr import track_id
from fxtr2.data.entity_core import FxtrID

class MySnapshotter(StatelessSnapshotter):
    def snapshot(self, obj) -> ThingState:
        return ThingState(
            key=["my_thing", obj.id],
            thing_type="my_type",
            thing_data={"name": obj.name},
            links=[
                ThingLink(
                    link_type="fxtr_ref",
                    link_key=["data"],
                    to_thing_key=track_id(obj.fxtr_data_id),  # Easy!
                )
            ],
        )

async def main():
    # Enable fxtr_support for automatic entity tracking
    watcher = Watcher(db_path="/tmp/demo.db", port=8765, fxtr_support=True)

    async with watcher.watching_and_serving():
        # track_id() works automatically - no namespace setup needed
        watcher.tick("info", "Initial state")
```

## Key Concepts

### Thing Keys

All fxtr entities use the key format `["fxtr_entity", "<id_string>"]` in thingwatcher:
- `["fxtr_entity", "dxyz..."]` - A d-node entity
- `["fxtr_entity", "!mytype"]` - A builtin entity
- `["fxtr_entity", "."]` - The ground node

Namespaces use the key format `["fxtr_namespace", "<name>"]`.

### Thing Types

The `thing_type` for entities is derived from the entity's node tag and `$fxtr.type` field:
- For d-nodes with `$fxtr.type`: `"fxtr.transluce.org/fxtr_entity/d/<type_id_string>"`
- For all others: `"fxtr.transluce.org/fxtr_entity_fallback"`

### CBOR to JSON Transformation

When transforming entity data for thingwatcher:
- FxtrID references become JSON marker objects and ThingLinks
- `RawWeakFxtrID` → `{"$!fxtr.link.weak": ["fxtr_entity", ...]}` + weak link
- `RawStrongFxtrID` → `{"$!fxtr.link.strong": ["fxtr_entity", ...]}` + strong link
- `RawFxtrEntityMarkerID` → `{"$!fxtr.link.marker": ["fxtr_entity", ...]}` + marker link
- Bytes are encoded as `{"$!bytes": "<base64>"}` for JSON compatibility

Link keys are the path from root to the reference (e.g., `["nested", "data", 0]`).

### Async Snapshots with AsyncThingUpdate

The namespace snapshotter uses thingwatcher's `AsyncThingUpdate` to handle the async `list_pins()` call:
- **Immediate update**: Uses cached pins (marked as stale) for instant feedback
- **Async updater**: Fetches fresh pins in background, result appears in streaming tick

This ensures the UI remains responsive while waiting for database operations.

## Module Details

### cbor_transform.py

Core transformation utilities:

- `fxtr_key_to_thing_key(entity_id: FxtrID) -> list`: Convert FxtrID to thingwatcher key
- `thing_key_to_fxtr_id(thing_key: Any) -> FxtrID | None`: Parse thingwatcher key back to FxtrID
- `fxtr_entity_to_thing_state(entity_id, cbor_data) -> ThingState`: Main transformation function

### immutable_store.py

ImmutableStore implementation for lazy entity access:

- `FxtrImmutableStore(backend: ObservableStore)`: Store that looks up entities by thingwatcher key
  - `get_thing_state(key) -> ThingState | None`: Look up entity and transform to ThingState
  - `get_backlinks(key) -> list[BacklinkInfo]`: Get backlinks via ObservableStore.backrefs()

### snapshotter.py

Snapshotter and anchor capturer for namespace objects:

- `FxtrNamespaceSnapshotter(backend, name)`: Tracks namespace pins using AsyncThingUpdate
  - `stateless_snapshot(obj) -> AsyncThingUpdate`: Returns cached state immediately + async updater
  - `snapshot_owned(obj) -> AsyncThingUpdate`: Delegates to stateless_snapshot
- `FxtrNamespaceAnchorCapturer(name)`: Returns ThingRef for namespace when used as anchor

### namespace_view.py

Main entry point for mounting namespaces:

- `fxtr_namespace_view(namespace, name, watcher)`: Async context manager that:
  - Registers ImmutableStore for entity lookup
  - Registers snapshotter/anchor capturer for namespace
  - Adds namespace as a root
  - Registers renderer builder for fxtr types
  - Cleans up everything on exit

Accepts either `PersistentPinningBackend` or `FxtrNamespace` as input.

### renderers.py

Renderer registration and dynamic renderer builder:

- `register_fxtr_renderer(fxtr_type_id, tsx_source, imports)`: Register custom renderer for fxtr type
- `unregister_fxtr_renderer(fxtr_type_id)`: Remove custom renderer
- `fxtr_renderer_builder(thing_type) -> RendererBuilderResult | None`: Dynamic renderer generation

The fallback renderer makes `$!fxtr.link.*` markers render as clickable node references.

### track_helpers.py

Convenience functions for tracking fxtr entities without explicit namespace setup:

- `track_id(fxtr_id, from_scope=None) -> list`: Track an fxtr entity for visualization
  - Returns the thingwatcher thing_key immediately: `["fxtr_entity", id_string]`
  - Queues the entity for async copy to an internal namespace
  - Uses `ACTIVE_SCOPE` if `from_scope` is None
  - Requires `fxtr_support=True` on the Watcher

This enables snapshotters to easily create links to fxtr entities:
```python
links=[ThingLink(link_type="ref", link_key=["data"], to_thing_key=track_id(fxtr_id))]
```

## File Structure

```
fxtr/
├── __init__.py              # Public API re-exports
├── cbor_transform.py        # CBOR to ThingState transformation
├── immutable_store.py       # ImmutableStore implementation
├── snapshotter.py           # Namespace snapshotter with AsyncThingUpdate
├── namespace_view.py        # Main context manager
├── renderers.py             # Renderer registration and builder
├── track_helpers.py         # track_id() for automatic entity tracking
├── components/              # TSX renderer components
│   ├── deno.json            # Deno config for type checking
│   ├── FxtrEntityFallback.tsx  # Fallback renderer for entities
│   └── FxtrNamespace.tsx    # Renderer for namespace objects
└── LOCAL_CODE_OVERVIEW.md   # This file
```

### components/

TSX renderer components for thingwatcher. These are loaded by `renderers.py` at runtime.

**Type checking:** Run `deno check *.tsx` in the components directory to type check.

Components use the `thingwatcher-bindings` module (imported from thingwatcher's d.ts file) for:
- Hooks: `usePayload`, `useLinks`, etc.
- Components: `InlineNodeRef`, `ChildNodeView`, etc.
- Types: `ThingWatcherRenderConfig`, `FullRendererProps`, etc.

## Tests

Tests are in `tests/test_fxtr/` (59 tests total):
- `test_cbor_transform.py` - Key conversion and CBOR transformation (18 tests)
- `test_immutable_store.py` - ImmutableStore lookup and backlinks (10 tests)
- `test_namespace_view.py` - Snapshotter and namespace view (9 tests)
- `test_renderers.py` - Renderer registration and builder (12 tests)
- `test_track_helpers.py` - track_id() and fxtr_support (10 tests)

Run tests with:
```bash
uv run pytest tests/test_fxtr/ -v
```

## Demo

A working demo is available at `examples/demo_fxtr/demo.py`. Run it with:

```bash
uv run python examples/demo_fxtr/demo.py
```

Then open http://localhost:8765 to explore a sample entity graph with:
- People, tasks, and projects with cross-references
- Clickable links between entities
- Backlinks showing what references each entity

## Related Documentation

- `../../v2/plans/phase2/stage2_fxtr_in_thingwatcher.md` - Original implementation plan
- `../../v2/plans/phase2/stage2post1_refactor_under_tw.md` - Refactor plan (this move)
- `../CODE_OVERVIEW.md` - Main thingwatcher documentation
