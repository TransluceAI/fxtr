# Phase 2 Step 2 Scratch - Roots Feature

## Summary of Requirements (from interview)

1. **Use case**: Named entry points - programmatic anchors like 'currentUser', 'mainTask' that provide consistent starting points for exploration
2. **Selection behavior**: Selecting a root highlights it in Graph and shows it in DetailsPanel
3. **Historicity**: Per-tick historical - roots are snapshotted each tick, can see what roots existed at tick N
4. **API style**: Module-level + watcher methods, module-level errors if no watcher
5. **Capturer**: Generalize PauseCapturer to AnchorCapturer, used by both pauses and roots
6. **Resolution**: Like pause - returns ThingState or ThingRef. If ref points to non-existent thing, produces a broken thing
7. **DB storage**: New row per change with valid_from/valid_until like thing_states

## Key Design Decisions

### 1. AnchorCapturer Generalization

Current `PauseCapturer` becomes `AnchorCapturer`. Both pauses and roots use this:
- Protocol signature unchanged: `handle(anchor: Any) -> ThingState | ThingRef`
- Registry functions renamed: `register_pause_capturer` -> `register_anchor_capturer`
- Old name kept as alias for backwards compatibility

### 2. Root State in Watcher

New watcher state:
```python
# Roots: name -> (anchor_obj, typename)
# Unlike pauses, roots persist until removed
self._roots: dict[str, tuple[Any, str]] = {}
```

### 3. Roots Table Schema

```sql
CREATE TABLE roots (
  id INTEGER PRIMARY KEY,
  root_name TEXT NOT NULL,        -- The user-given name
  thing_key BLOB NOT NULL,        -- CBOR canonical key of the resolved thing
  valid_from INTEGER NOT NULL,    -- Tick when this binding started
  valid_until INTEGER,            -- NULL = still valid
  FOREIGN KEY (valid_from) REFERENCES ticks(tick),
  FOREIGN KEY (valid_until) REFERENCES ticks(tick)
);
CREATE INDEX idx_roots_name ON roots(root_name);
CREATE INDEX idx_roots_valid ON roots(valid_from, valid_until);
```

### 4. In-Memory Root Tracking for Change Detection

Similar to `_current_things`:
```python
@dataclass
class CurrentRootInfo:
    root_db_id: int           # ID of current DB row
    thing_key_bytes: bytes    # Current resolved key

# Maps root_name -> CurrentRootInfo
self._current_roots: dict[str, CurrentRootInfo] = {}
```

### 5. Snapshot Flow Changes

During `_take_snapshot`:
1. Snapshot tracked objects (existing)
2. Capture pause anchors (existing)
3. **NEW**: Capture root anchors using same AnchorCapturer
4. Unify by key (existing)
5. **NEW**: Build root_results: list of (root_name, resolved_key_bytes)

During `_process_snapshot`:
1. Write tick record (existing)
2. Process thing states/deltas (existing)
3. **NEW**: Process root bindings (diff against `_current_roots`)
4. Invalidate disappeared things (existing)
5. **NEW**: Invalidate disappeared roots (roots that were removed via `remove_root`)

### 6. API Design

Module-level functions (in api.py):
```python
def add_root(name: str, anchor: Any, typename: str | type | None = None) -> None:
    """Add a named root. Errors if no watcher active."""
    watcher = get_current_watcher()
    if watcher is None:
        raise NoWatcherConnectedError("add_root() requires an active watcher")
    watcher.add_root(name, anchor, typename)

def remove_root(name: str) -> None:
    """Remove a named root. Errors if no watcher active."""
    watcher = get_current_watcher()
    if watcher is None:
        raise NoWatcherConnectedError("remove_root() requires an active watcher")
    watcher.remove_root(name)
```

Watcher methods:
```python
def add_root(self, name: str, anchor: Any, typename: str | type | None = None) -> None:
    """Add or update a named root."""

def remove_root(self, name: str) -> None:
    """Remove a named root."""
```

### 7. GraphQL Schema Extensions

```graphql
type Root {
  name: String!
  thing: Thing!  # The resolved thing at this tick
}

extend type Tick {
  roots: [Root!]!  # All roots valid at this tick
}

extend type Query {
  root(name: String!, atTick: Int): Root  # Get a specific root
}
```

### 8. Frontend RootsPanel Component

New component showing:
- List of root names
- Each root shows: name, resolved thing's shortId/type
- Click to select the resolved thing in Graph/DetailsPanel
- No CRUD operations in UI (roots are set programmatically)

## Implementation Layers

1. **Backend - Rename PauseCapturer -> AnchorCapturer** (with backwards compat alias)
2. **Backend - Add roots table and DB methods**
3. **Backend - Add watcher root tracking (`_roots`, `_current_roots`)**
4. **Backend - Modify snapshot/write flow to include roots**
5. **Backend - Add GraphQL schema extensions and resolvers**
6. **Frontend - Create RootsPanel component**
7. **Frontend - Wire up root selection to existing selection context**

## Edge Cases

- Root pointing to untracked thing -> broken thing (like pause)
- Root name collision (add_root with existing name) -> update the anchor, old binding invalidated at next tick
- Root removed mid-session -> invalidated at next tick, no longer appears in subsequent ticks
- Multiple roots pointing to same thing -> allowed, both roots resolve to same shortId
