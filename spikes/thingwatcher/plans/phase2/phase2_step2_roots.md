# Phase 2 Step 2: Roots

## Overview

Add the concept of "roots" to thingwatcher: named entry points that provide consistent starting points for exploring the state graph. Roots are programmatically defined anchors (like "currentUser", "mainTask") that point to specific tracked things.

Key characteristics:
- **Named**: Each root has a unique string name
- **Historical**: Root bindings are tracked per-tick with valid_from/valid_until
- **Entry points**: Selecting a root in the UI highlights the resolved thing in the Graph and shows it in DetailsPanel

## Design Decisions

### 1. Rename PauseCapturer to AnchorCapturer

Both pauses and roots need to convert anchor objects to `ThingState | ThingRef`. Rather than duplicate the capturer concept, rename `PauseCapturer` to `AnchorCapturer` and use it for both.

Since there are no external users of this library yet, we do a clean rename with no backwards-compat aliases.

### 2. Root Resolution

Roots use the same resolution logic as pauses:
- `AnchorCapturer.handle(anchor)` returns `ThingState | ThingRef`
- If `ThingRef` points to a key that doesn't exist, creates a broken thing
- Root then binds its name to the resolved thing's key

### 3. Database Schema

New `roots` table with temporal tracking:

```
roots
├── id (INTEGER PRIMARY KEY)
├── root_name (TEXT NOT NULL)
├── thing_key (BLOB NOT NULL)  -- CBOR canonical key
├── valid_from (INTEGER FK ticks.tick)
├── valid_until (INTEGER FK ticks.tick, nullable)
```

Indexes: `root_name`, `(valid_from, valid_until)`

### 4. Python API

Module-level (errors if no watcher):
```python
add_root(name: str, anchor: Any, typename: str | type | None = None, replace: bool = False) -> None
remove_root(name: str) -> None
```

Watcher methods:
```python
watcher.add_root(name, anchor, typename, replace=False)
watcher.remove_root(name)
```

If `add_root` is called with a name that already exists and `replace=False`, raise `ValueError`.

### 5. GraphQL API

```graphql
type Root {
  name: String!
  thing: Thing!
}

# Extend existing types
extend type Tick {
  roots: [Root!]!
}

extend type Query {
  root(name: String!, atTick: Int): Root
}
```

## Implementation Steps

### Step 2.1: Rename PauseCapturer to AnchorCapturer

- [x] In `api.py`: Rename `PauseCapturer` protocol to `AnchorCapturer`
- [x] In `api.py`: Rename `register_pause_capturer` to `register_anchor_capturer`
- [x] In `registry.py`: Rename `_pause_capturers` to `_anchor_capturers`
- [x] In `registry.py`: Rename `register_pause_capturer` to `register_anchor_capturer`
- [x] In `registry.py`: Rename `get_pause_capturer` to `get_anchor_capturer`
- [x] In `watcher.py`: Rename `_local_pause_capturers` to `_local_anchor_capturers`
- [x] In `watcher.py`: Rename `register_pause_capturer` to `register_anchor_capturer`
- [x] In `watcher.py`: Rename `get_pause_capturer` to `get_anchor_capturer`
- [x] In `watcher.py`: Update all internal references
- [x] In `__init__.py`: Update exports
- [x] Update all tests that use `PauseCapturer` or `register_pause_capturer`
- [x] Update all examples that use these names
- [x] Verify all existing pause tests still pass

### Step 2.2: Add roots table and DB methods

- [x] In `db.py`: Add `roots` table definition with schema above
- [x] Add `write_root()` method: insert new root binding
- [x] Add `invalidate_root()` method: set valid_until
- [x] Add `get_roots_at_tick()` method: get all roots valid at tick
- [x] Add `get_root_at_tick()` method: get specific root by name at tick
- [x] Add `get_root_history()` method: get all bindings for a root name over time

### Step 2.3: Add watcher root tracking

In `watcher.py`:

- [x] Add `_roots: dict[str, tuple[Any, str]]` for name -> (anchor, typename)
- [x] Add `CurrentRootInfo` dataclass with `root_db_id`, `thing_key_bytes`
- [x] Add `_current_roots: dict[str, CurrentRootInfo]` for change detection
- [x] Implement `add_root(name, anchor, typename, replace=False)`:
  - Resolve typename (same logic as track/pause)
  - Verify anchor capturer exists
  - If name exists and `replace=False`, raise `ValueError`
  - Store in `_roots` (replaces if `replace=True`)
- [x] Implement `remove_root(name)`:
  - Remove from `_roots`
  - Mark for invalidation at next tick

### Step 2.4: Integrate roots into snapshot flow

In `watcher.py._take_snapshot()`:

- [x] After capturing pause anchors, capture root anchors using same AnchorCapturer
- [x] Build `root_results: list[tuple[str, ThingState | ThingRef]]` - (name, capturer_result)
- [x] In `_unify_snapshots()`: include root results in unification (same conflict rules)
- [x] Return `root_bindings: dict[str, bytes]` mapping root name -> resolved key_bytes
- [x] Add `root_bindings` to `SnapshotResult`

In `watcher.py._process_snapshot()`:

- [x] After processing thing states, call new `_process_roots()` helper
- [x] `_process_roots()`: diff `root_bindings` against `_current_roots`
  - New roots: write_root()
  - Changed roots (different key): invalidate old, write new
  - Unchanged roots: no-op
- [x] Handle removed roots: if name in `_current_roots` but not in `root_bindings`, invalidate
- [x] Update `_current_roots` tracking

### Step 2.5: Add module-level API functions

In `api.py`:

- [x] Add `add_root(name, anchor, typename, replace=False)` function
- [x] Add `remove_root(name)` function
- [x] Both raise `NoWatcherConnectedError` if no watcher active
- [x] Export in `__all__`

### Step 2.6: Add GraphQL schema and resolvers

In `schema.graphql`:

- [x] Add `Root` type
- [x] Add `roots: [Root!]!` field to `Tick` type
- [x] Add `root(name: String!, atTick: Int): Root` query

In `server.py`:

- [x] Add resolver for `Tick.roots`
- [x] Add resolver for `Root.thing` (similar to existing Link.toThing)
- [x] Add resolver for `Query.root`

### Step 2.7: Backend tests

- [x] Unit tests for DB root methods
- [x] Unit tests for watcher add_root/remove_root
- [x] Test add_root conflict error (without replace=True)
- [x] Test add_root with replace=True
- [x] Integration tests for root tracking across ticks
- [x] Tests for root change detection (same root, different target)
- [x] Tests for root removal
- [x] Tests for root pointing to untracked thing (broken thing)
- [x] GraphQL tests for roots queries

### Step 2.8: Update CODE_OVERVIEW.md (backend)

- [x] Document AnchorCapturer rename (replace PauseCapturer references)
- [x] Document roots concept in Key Concepts
- [x] Add roots table to DB schema documentation
- [x] Document new API functions (add_root, remove_root)
- [x] Update Public API section

---

**Note: Phase 2 Step 1 (dockview integration) must be completed before proceeding with the frontend steps below.**

---

### Step 2.9: Add RootsPanel frontend component

- [x] Create `components/RootsPanel/` directory with `index.ts`, `RootsPanel.tsx`, `RootsPanel.module.css`
- [x] Query roots via `tick.roots` in existing tick data query
- [x] Display list of roots: name, resolved thing's shortId, thingType
- [x] Click root to select the resolved thing (use existing selection context)
- [x] Style consistent with other panels

### Step 2.10: Integrate RootsPanel into dockview layout

- [x] Add RootsPanel as a non-closeable dockview panel
- [x] Position alongside Graph, DetailsPanel, and ThingsList panels
- [x] Update graphql queries if needed to fetch roots data

### Step 2.11: Update CODE_OVERVIEW.md (frontend)

- [x] Document RootsPanel component
- [x] Update frontend directory structure

## Edge Cases

1. **Root to untracked thing**: AnchorCapturer returns ThingRef with key that doesn't exist -> creates broken thing, root resolves to that broken thing's key

2. **Root name collision without replace**: `add_root("x", obj1)` then `add_root("x", obj2)` -> raises `ValueError`

3. **Root name collision with replace**: `add_root("x", obj1)` then `add_root("x", obj2, replace=True)` -> second call replaces the anchor, old binding invalidated at next tick, new binding created

4. **Multiple roots to same thing**: `add_root("a", obj)` and `add_root("b", obj)` where both resolve to same key -> allowed, both roots point to same thing

5. **Root removed then re-added**: Creates new binding with gap in history

6. **Root anchor garbage collected**: Unlike tracked objects, root anchors are held with strong references in `_roots`, so they won't be GC'd while mounted
