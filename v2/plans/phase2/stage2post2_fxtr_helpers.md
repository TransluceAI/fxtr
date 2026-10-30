# Stage 2 Post-2: fxtr Tracking Helpers

## Overview

Add convenience APIs that let snapshotters easily embed links to fxtr entities without requiring users to set up explicit namespace views. The watcher manages an internal fxtr namespace behind the scenes.

**Key API:**
```python
# Module-level (uses active watcher)
from thingwatcher.fxtr import track_id
thing_key = track_id(fxtr_id)  # Returns ["fxtr_entity", id_string]

# Or on the watcher directly
thing_key = watcher.track_fxtr_id(fxtr_id)
```

**Use case:** In a snapshotter, you can link to fxtr entities without any setup:
```python
class MySnapshotter(StatelessSnapshotter):
    def snapshot(self, obj) -> ThingState:
        # Link to an fxtr entity directly
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
```

## Design Decisions

Based on discussion:
- **Error on no support**: If `fxtr_support=False` and `track_id()` is called, raise an error
- **Attach existing DB**: Don't delete existing internal DB - just attach if present (usually empty anyway)
- **Transitive copy**: Copy entity and all its dependencies via fxtr's copy mechanism
- **Pin with id_string**: Pin each copied entity using its `id_string` as the pin name (with `overwrite=True`)
- **Async copy, immediate return**: `track_id()` returns ThingKey immediately; copy happens in background
- **Default scope**: Use fxtr's `ACTIVE_SCOPE` context variable when `from_scope=None`
- **No GC**: Entities accumulate until watcher shutdown

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User Code (Snapshotter)                      │
│  thing_key = track_id(fxtr_id)                                      │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    thingwatcher.fxtr.track_id()                      │
│  1. Get active watcher from context                                  │
│  2. Call watcher.track_fxtr_id(fxtr_id, from_scope)                 │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Watcher.track_fxtr_id()                           │
│  1. Check fxtr_support enabled (raise if not)                       │
│  2. Compute thing_key = fxtr_key_to_thing_key(fxtr_id)              │
│  3. Queue async copy job: (fxtr_id, from_scope)                     │
│  4. Return thing_key immediately                                     │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼ (async, in background)
┌─────────────────────────────────────────────────────────────────────┐
│                    _FxtrCopyWorker (background task)                 │
│  For each queued (fxtr_id, from_scope):                             │
│  1. Copy entity from from_scope to internal namespace scope         │
│  2. Pin entity in internal namespace (or just copy, no pin needed?) │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│              Internal SQLiteFxtrNamespace                            │
│  - Created on watcher startup if fxtr_support=True                  │
│  - DB path: {watcher_db_path}-fxtr.db                               │
│  - Isolated scope (doesn't inherit from active scope)               │
│  - Registered as ImmutableStore for entity lookups                  │
└─────────────────────────────────────────────────────────────────────┘
```

## API Design

### Watcher Configuration

```python
class Watcher:
    def __init__(
        self,
        db_path: str | Path | None = None,
        port: int | None = None,
        fxtr_support: bool = False,  # NEW
    ):
        ...
```

When `fxtr_support=True`:
1. Compute internal fxtr DB path: `{db_path}-fxtr.db` (or temp file if no db_path)
2. Delete existing internal fxtr DB if present (fresh start)
3. Create `SQLiteFxtrNamespace` with isolated scope
4. Register it as an `ImmutableStore` (name: `"_fxtr_internal"`)
5. Start background copy worker task

### track_id Function

```python
# In thingwatcher/fxtr/track_helpers.py
from __future__ import annotations

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.fxtr_scope import ACTIVE_SCOPE, FxtrScope

from thingwatcher.api import NoWatcherConnectedError
from thingwatcher.context import get_current_watcher


def track_id(
    fxtr_id: FxtrID,
    from_scope: FxtrScope | None = None,
) -> list:
    """Track an fxtr entity for visualization in thingwatcher.

    This queues the entity for async copy into an internal namespace,
    making it available for graph navigation. The returned thing_key
    can be used in ThingLink.to_thing_key.

    Args:
        fxtr_id: The fxtr entity ID to track
        from_scope: Scope to copy from. If None, uses ACTIVE_SCOPE.

    Returns:
        The thingwatcher thing_key: ["fxtr_entity", id_string]

    Raises:
        NoWatcherConnectedError: If no watcher is active
        FxtrSupportDisabledError: If active watcher has fxtr_support=False
    """
    watcher = get_current_watcher()
    if watcher is None:
        raise NoWatcherConnectedError("track_id requires an active watcher")

    return watcher.track_fxtr_id(fxtr_id, from_scope)
```

### Watcher.track_fxtr_id Method

```python
# In thingwatcher/watcher.py (add imports at top of file)
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.fxtr_scope import ACTIVE_SCOPE, FxtrScope

from thingwatcher.fxtr.cbor_transform import fxtr_key_to_thing_key

class Watcher:
    def track_fxtr_id(
        self,
        fxtr_id: FxtrID,
        from_scope: FxtrScope | None = None,
    ) -> list:
        """Track an fxtr entity for visualization.

        Queues async copy to internal namespace and returns thing_key.
        """
        if not self._fxtr_support:
            raise FxtrSupportDisabledError(
                "track_fxtr_id requires fxtr_support=True on the Watcher"
            )

        # Get scope
        if from_scope is None:
            from_scope = ACTIVE_SCOPE.get()

        # Queue copy job (deduplicate)
        if fxtr_id not in self._fxtr_tracked_ids:
            self._fxtr_tracked_ids.add(fxtr_id)
            self._fxtr_copy_send.send_nowait((fxtr_id, from_scope))

        # Return thing key immediately
        return fxtr_key_to_thing_key(fxtr_id)
```

## Internal Namespace Management

### Lifecycle

1. **Startup** (in `watching_and_serving()` or similar):
   ```python
   if self._fxtr_support:
       # Compute path
       if self._db_path:
           fxtr_db_path = str(self._db_path) + "-fxtr.db"
       else:
           fxtr_db_path = tempfile.mktemp(suffix="-fxtr.db")

       # Create/attach namespace (don't delete existing - just attach)
       self._fxtr_namespace = SQLiteFxtrNamespace(
           f"sqlite+aiosqlite:///{fxtr_db_path}",
           "_thingwatcher_internal",
           isolate_scope=True,  # Don't inherit from active scope
       )
       await self._fxtr_namespace.__aenter__()

       # Register as immutable store
       self.register_immutable_store(
           "_fxtr_internal",
           FxtrImmutableStore(self._fxtr_namespace._get_backend()),
       )

       # Start copy worker
       self._fxtr_copy_send, self._fxtr_copy_recv = anyio.create_memory_object_stream()
       tg.start_soon(self._fxtr_copy_worker)
   ```

2. **Copy Worker** (background task):
   ```python
   async def _fxtr_copy_worker(self):
       async with self._fxtr_copy_recv:
           async for fxtr_id, from_scope in self._fxtr_copy_recv:
               try:
                   # Copy entity transitively into internal namespace's scope
                   await from_scope.copy_to(
                       self._fxtr_namespace.scope,
                       fxtr_id,
                   )
                   # Pin using id_string as pin name
                   await self._fxtr_namespace.pin(
                       fxtr_id.id_string(),
                       fxtr_id,
                       overwrite=True,
                   )
               except Exception:
                   # Log error with traceback but don't crash - entity just won't be available
                   logger.warning(
                       f"Failed to copy fxtr entity {fxtr_id}",
                       exc_info=True,
                   )
   ```

3. **Shutdown**:
   ```python
   if self._fxtr_support:
       self._fxtr_copy_send.close()  # Stop accepting new jobs
       # Wait for pending copies? Or just cancel?
       await self._fxtr_namespace.__aexit__(None, None, None)
       self.unregister_immutable_store("_fxtr_internal")
   ```

## New Exception

```python
# In thingwatcher/api.py
class FxtrSupportDisabledError(Exception):
    """Raised when fxtr features are used but fxtr_support=False."""
    pass
```

## Module Exports

Update `thingwatcher/fxtr/__init__.py`:
```python
from thingwatcher.fxtr.track_helpers import track_id

__all__ = [
    # ... existing exports ...
    "track_id",
]
```

Update `thingwatcher/__init__.py`:
```python
from thingwatcher.fxtr import track_id

__all__ = [
    # ... existing exports ...
    "track_id",
]
```

## Implementation Steps

### Phase 1: Core Infrastructure

- [x] 1. Add `FxtrSupportDisabledError` exception to `api.py`
- [x] 2. Add `fxtr_support` parameter to `Watcher.__init__()`
- [x] 3. Add internal state fields to Watcher:
  - [x] `_fxtr_support: bool`
  - [x] `_fxtr_namespace: SQLiteFxtrNamespace | None`
  - [x] `_fxtr_copy_send: MemoryObjectSendStream | None`
  - [x] `_fxtr_copy_recv: MemoryObjectReceiveStream | None`
  - [x] `_fxtr_tracked_ids: set[FxtrID]` (for deduplication)
  - Note: `_fxtr_db_path` was not needed - the URL is computed on the fly
- [x] 4. Implement namespace lifecycle in `watching_and_serving()`:
  - [x] Create/attach namespace on startup
  - [x] Register as immutable store
  - [x] Clean up on exit

### Phase 2: Copy Worker

- [x] 5. Implement `_fxtr_copy_worker()` background task
- [x] 6. Handle graceful shutdown (close queue, wait or cancel pending)

### Phase 3: Public API

- [x] 7. Implement `Watcher.track_fxtr_id()` method
- [x] 8. Implement `thingwatcher.fxtr.track_id()` module-level function
- [x] 9. Add exports to `thingwatcher/fxtr/__init__.py` and `thingwatcher/__init__.py`

### Phase 4: Tests

- [x] 10. Test `fxtr_support=False` raises error on `track_fxtr_id()`
- [x] 11. Test `track_id()` returns correct thing key
- [x] 12. Test entity becomes available via immutable store after copy
- [ ] 13. Test transitive copy (entity with children) - not explicitly tested, but covered by pin()
- [ ] 14. Test pinning uses id_string as pin name - not explicitly tested, but implementation uses id_string
- [x] 15. Test default scope from ACTIVE_SCOPE
- [x] 16. Test deduplication (same ID tracked twice only copies once)

### Phase 5: Documentation

- [x] 16. Update `thingwatcher/fxtr/LOCAL_CODE_OVERVIEW.md`
- [x] 17. Update `spikes/thingwatcher/CODE_OVERVIEW.md`

## Example Usage

```python
from dataclasses import dataclass
from thingwatcher import Watcher, ThingState, ThingLink, StatelessSnapshotter
from thingwatcher.fxtr import track_id
from fxtr2.data.entity_core import FxtrID

@dataclass
class MyAnalysis:
    name: str
    result_entity_id: FxtrID  # Points to fxtr data

class MySnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: MyAnalysis) -> ThingState:
        return ThingState(
            key=["analysis", obj.name],
            thing_type="my_app/analysis",
            thing_data={"name": obj.name},
            links=[
                ThingLink(
                    link_type="result",
                    link_key=["result"],
                    to_thing_key=track_id(obj.result_entity_id),
                )
            ],
        )

async def main():
    watcher = Watcher(db_path="/tmp/demo.db", port=8765, fxtr_support=True)
    watcher.register_snapshotter("MyAnalysis", MySnapshotter())

    async with watcher.watching_and_serving():
        # Create some fxtr data in the active scope...
        analysis = MyAnalysis(name="test", result_entity_id=some_fxtr_id)
        watcher.track(analysis, "MyAnalysis")
        watcher.tick("info", "Analysis ready")
        # The fxtr entity is now navigable in the UI!
```

## Design Notes

### Deduplication

If the same `fxtr_id` is tracked multiple times, we deduplicate copy jobs using a set of already-queued/copied IDs (`_fxtr_tracked_ids`). This avoids redundant work. See `track_fxtr_id()` implementation above.

### Scope Lifetime Limitation

**Known limitation:** The `from_scope` is captured at `track_id()` call time. If the scope exits before the async copy completes, the copy will fail and the entity won't be available.

For now, document this limitation. Users must ensure the scope stays alive until the async copy can complete (typically until the next tick).

```python
# TODO(daniel): Replace with scope.open_sync() when implemented.
# The open_sync() function will allow creating a child scope eagerly and
# lifting the needed value into it synchronously, keeping the value alive
# until the copy finishes without requiring an async task.
```

## Open Questions

- **Copy failure handling**: If copy fails (e.g., entity not in source scope), the link will point to a missing entity. Should we track failed copies and show them differently in the UI? For now, just log a warning.
