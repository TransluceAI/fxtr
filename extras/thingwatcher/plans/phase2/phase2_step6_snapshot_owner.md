# Phase 2 Step 6: Snapshot Ownership System

## Overview

Replace the current `StateSnapshotter` Protocol with two ABCs that distinguish between stateless snapshotters (which may overlap) and stateful owning snapshotters (which exclusively own their output keys).

**Goals:**
- Make it an error if two different sources emit the same thing key with owning semantics
- Allow stateless sources (stateless snapshotters + anchor capturers) to safely overlap if they produce identical states
- Track bidirectional ownership maps: which tracked object owns which thing keys, and vice versa
- Track which keys have been produced by stateless sources (for streaming subtick conflict detection)
- Provide better error messages when ownership conflicts occur

## Design

### New ABC Hierarchy

```python
from abc import ABC, abstractmethod

class StatelessSnapshotter(ABC):
    """Snapshotter that returns ThingStates only.

    OK for multiple stateless snapshotters (or anchor capturers) to return
    the same key, as long as the states are identical.
    """
    @abstractmethod
    def snapshot(self, obj: Any) -> ThingState | list[ThingState]:
        ...

class StatefulOwningSnapshotter(ABC):
    """Snapshotter that exclusively owns its output keys.

    Can return ThingState or ThingDelta. It is an error if any key returned
    by this snapshotter is also returned by another snapshotter or anchor capturer.
    """
    @abstractmethod
    def snapshot_owned(self, obj: Any) -> ThingState | ThingDelta | list[ThingState | ThingDelta]:
        ...
```

The old `StateSnapshotter` Protocol is removed entirely (no backward compatibility needed).

### Ownership and Stateless Key Tracking

Add three maps to `Watcher`:

```python
# Maps tracked object id -> set of thing key bytes it currently owns
_owned_things: dict[int, set[bytes]]

# Maps thing key bytes -> owning tracked object id (if any)
_thing_owners: dict[bytes, int]

# Set of thing key bytes produced by stateless sources (snapshotters + anchor capturers)
# Used for fast conflict detection during streaming subticks
_stateless_keys: set[bytes]
```

These maps are updated during snapshot processing and cleaned up when:
1. A tracked object is garbage collected (in the weakref finalizer)
2. A thing key disappears from an owning snapshotter's output
3. A tracked object is explicitly untracked
4. A stateless key disappears from all stateless sources

### Validation Rules

During `_unify_snapshots`:

1. **Stateful-owned keys must be unique**: If a key comes from a `StatefulOwningSnapshotter`, it must not appear from any other source (other snapshotters, anchor capturers, roots)

2. **Stateless overlap is OK**: Multiple stateless sources can emit the same key if the `ThingState` values are identical (existing behavior)

3. **No mixing ThingState/ThingDelta for same key**: Existing rule, unchanged

4. **Anchor capturers are always stateless**: They can overlap with stateless snapshotters

For streaming subticks (future): When running only one owning snapshotter, we can check its output keys against `_thing_owners` (other owners) and `_stateless_keys` (stateless sources) without re-running other snapshotters.

### Error Handling

When an ownership conflict is detected:
- Create a `__broken__` thing with the conflicting key
- Include an error message identifying which tracked object(s) were involved
- Log the conflict with useful debugging info

## Implementation Steps

- [x] **1. Define new ABCs in `api.py`**
  - Remove `StateSnapshotter` Protocol entirely
  - Add `StatelessSnapshotter` ABC with `snapshot()` method
  - Add `StatefulOwningSnapshotter` ABC with `snapshot_owned()` method
  - Update `__all__` exports

- [x] **2. Add ownership/stateless maps to `Watcher`**
  - Add `_owned_things: dict[int, set[bytes]]`
  - Add `_thing_owners: dict[bytes, int]`
  - Add `_stateless_keys: set[bytes]`
  - Initialize in `__init__`

- [x] **3. Update weakref finalizer**
  - When a tracked object is garbage collected, also clean up its entries in `_owned_things` and `_thing_owners`

- [x] **4. Update `_take_snapshot` to track ownership info**
  - When running snapshotters, check `isinstance(snapshotter, StatefulOwningSnapshotter)` vs `StatelessSnapshotter`
  - For owning snapshotters, collect `(obj_id, list_of_key_bytes)` pairs
  - For stateless snapshotters, collect keys separately
  - Pass this info to `_unify_snapshots`

- [x] **5. Update `_unify_snapshots` signature and logic**
  - Accept ownership info and stateless keys parameters
  - Track which keys came from owning snapshotters and which obj_id owns them
  - Track which keys came from stateless sources
  - Detect conflicts: if an owned key appears from multiple sources, create broken thing
  - Return updated ownership info and stateless keys alongside existing returns

- [x] **6. Update ownership/stateless maps after successful snapshot**
  - After `_unify_snapshots`, update `_owned_things`, `_thing_owners`, and `_stateless_keys`
  - Remove keys that are no longer owned/present
  - Add newly owned/stateless keys

- [x] **7. Update registration functions**
  - Update type hints to accept `StatelessSnapshotter | StatefulOwningSnapshotter`
  - No need to store a flag - just use `isinstance` at snapshot time

- [x] **8. Update `untrack` method**
  - Clean up ownership maps when explicitly untracking

- [x] **9. Update existing snapshotters in codebase**
  - Find all existing snapshotters and convert them to appropriate ABC
  - Most will likely become `StatelessSnapshotter`

- [x] **10. Write tests**
  - Test stateless snapshotters can overlap with identical states
  - Test stateless snapshotters conflict if states differ (existing behavior)
  - Test owning snapshotter conflict with another owning snapshotter → broken thing
  - Test owning snapshotter conflict with stateless snapshotter → broken thing
  - Test owning snapshotter conflict with anchor capturer → broken thing
  - Test ownership maps are correctly updated
  - Test stateless_keys set is correctly updated
  - Test ownership maps are cleaned up on GC
  - Test ownership maps are cleaned up on untrack

- [x] **11. Update CODE_OVERVIEW.md**

## Future Use

The ownership maps will be used by the streaming subticks feature (next step in phase 2) to:
- Know which tracked object to snapshot for a streaming update
- Validate that only owned things are updated in streaming mode
- Provide useful error messages

They could also be used to:
- Show in the UI which Python object owns a given thing
- Provide better debugging when things unexpectedly disappear
