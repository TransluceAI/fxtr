# Stage 9 — Stage 1: Context plumbing

This stage lays groundwork for provenance flow without yet enabling it.
The goal is that after this stage, `FxtrScope` can carry a
provenance-aware context entity, `DataclassEntity` captures its
`fxtr_ctx` from the destination scope at store time (not the active
scope at construction time), and there are helper functions for
crossing between the data-level and provenance-level views of a node.

The actual store / load / lift / is_tracked / abandon paths are
unchanged in this stage; provenance parents on FxtrIDs continue to be
ignored downstream. That's covered in Stage 2.

## Pieces

### 1. `DataclassEntity.fxtr_ctx` side fix

**File: `fxtr2/conversion/converters/mashumaro_entities.py`**

Change `DataclassEntity.fxtr_ctx`:

- Type becomes `RawFxtrEntityMarkerID | None`.
- Default becomes `None` (drop the `default_factory=get_active_context_marker`).
- The `Config.aliases` mapping stays the same.

In `_fxtr_store_impl`:

- After `to_dict()`, check whether `cbor["$fxtr.ctx"]` is present /
  non-None. If absent or None, replace it with the marker from the
  destination scope: `RawFxtrEntityMarkerID(scope.get_context_marker_id())`.
  (The scope here is `get_active_scope()` because `_fxtr_store_impl`
  runs inside `Entity.store`'s `as_active_scope()`.)
- If it _is_ present, leave it alone — preserves the marker on a
  load → store round-trip.

In `DataclassEntityLoadHandler.from_lowlevel_entity_data`:

- Validate that the cbor has a non-None `$fxtr.ctx` and reject
  otherwise. (The codec path already produces a `RawFxtrEntityMarkerID`
  from the cbor when present, so this is mostly a guard against a
  malformed payload.)

There's a subtle interaction with mashumaro's encoder: with the field
defaulting to `None`, `to_dict()` will emit `$fxtr.ctx: None` rather
than omitting the key. The `_fxtr_store_impl` post-processing handles
this (we overwrite the None). The decoder will see the same `$fxtr.ctx`
field as before in the round-trip case.

### 2. Per-scope context entity override

**File: `fxtr2/data/entity_ref_scope.py`**

Add two new keyword-only parameters to `FxtrScope.__init__` and
`FxtrScope._with_derived_lifetime`:

- `provenance_context: FxtrID | None = None` — a c-node identifying
  what this scope represents. Required to be a `c`-node.
- `provenance_context_from_scope: FxtrScope | None = None` — the scope
  in which `provenance_context` is already tracked. If `None`, we
  attempt to lift `provenance_context` from a parent scope (same
  behavior as `FxtrScope.lift`).

In `_init_arena_and_cache` (or a new helper called from there), after
the scope's main arena exists:

1. If `provenance_context is not None`:
   - Validate `provenance_context.get_tag() == "c"`.
   - Pull the c-node into our main arena via **`track_from_scope`**
     (sync transfer), not lazy attach. Source scope is whichever of
     `provenance_context_from_scope` was supplied, or, when None, the
     closest parent in which the c-node is tracked (parent-walk same
     as `_lift_unchecked`'s walk, but committing to a sync transfer
     rather than installing a sentinel bridge). The transfer must
     succeed — i.e. the c-node must already be a straightforward
     in-memory entity reachable from the source scope's arena. This
     is deliberately stricter than `lift`: if `provenance_context_from_scope`
     is some short-lived scope, the bridge would silently dangle, so
     we copy the data into our own arena up front rather than holding
     a reference back to the source.
   - Typical caller pattern: build the c-node in a sibling scope with
     the same parent set as the target scope (so its data is in
     memory), then construct the target with `provenance_context=...,
     provenance_context_from_scope=sibling_scope`.
   - Sync-call `_main_arena.unwrap_provenance(self._main_lease.clone(),
     provenance_context)` and assert it returns `FxtrID(".")`. If not,
     raise `ValueError`.
   - Set `self._context_marker = FxtrID.from_tag_and_payload(".", b"",
     provenance_parent=provenance_context)`.
2. If `provenance_context is None`:
   - Inherit from the closest parent scope's marker. Concretely: if
     `self._parents` is non-empty, copy `self._parents[0]._context_marker`
     (sufficient because parent markers are immutable for the scope's
     lifetime). If there are no parents (root scope), use plain
     `FxtrID(".")` with no parent.
   - In the inherited case, the c-node referenced by the parent's
     marker is already tracked in the parent (and reachable from this
     scope via the ancestor cap-bridge), so no extra arena work is
     needed; the cap-bridge / lift machinery will pick it up
     transparently when needed during `store_cbor` / `load`.
   - **Open detail**: do we still want to eagerly `lift` (or
     `track_from_scope`) the parent's c-node into our arena at
     construction, so that `get_context_marker_id()` returns an ID we
     can immediately use in `store_cbor` without a parent walk?
     Probably yes — keeps the provenance hot path simple. Plan: do
     the lift at construction; this is one extra arena op per scope
     but cheap. (For consistency with the explicit-source path,
     `track_from_scope` here too — the parent is alive for the
     child's lifetime by construction, so the transfer always
     succeeds.)

Update `GLOBAL_SCOPE`'s construction to never inherit a provenance
parent (it's already root, so the inherit path's "no parents" branch
handles it).

`get_context_marker_id` continues to return the (possibly
provenance-parented) FxtrID. `get_active_context_marker` similarly
returns `RawFxtrEntityMarkerID(get_active_context_id())`, so
DataclassEntity's marker injection automatically picks up the
provenance parent on the active scope.

### 3. Helper functions for layer crossing

**File: `fxtr2/data/entity_ref_scope.py`** (on `FxtrScope`)

Add two sync methods, both consuming the scope's main lease internally:

```python
def extract_provenance_parent(self, data_with_prov: FxtrID) -> FxtrID:
    # Validate alive (read-only).
    # Validate data_with_prov._provenance_parent is not None.
    # Validate both data_with_prov (data ID) and its parent are tracked.
    # Return the bare provenance-level FxtrID with no _provenance_parent.
    ...

def embed_as_provenance_parent(self, prov_node: FxtrID) -> FxtrID:
    # Validate alive (read-only).
    # Validate prov_node._provenance_parent is None (caller hands us
    # bare provenance node, not an already-embedded ID).
    # Validate prov_node is a provenance node (tag in {n, c, p, u}).
    # Validate prov_node is tracked in self._main_arena.
    # Sync-call _main_arena.unwrap_provenance(prov_node) to get data_id.
    # If unwraps_to is None, raise (provenance node with unknown unwrap).
    # Validate the unwrapped data_id is tracked in self._main_arena
    # (should always be true since the arena's unwraps_to chain
    # propagates inheritance, but assert it explicitly so callers
    # discover any failure to track up front).
    # Return FxtrID(data_id, _provenance_parent=prov_node).
    ...
```

These don't mutate the arena — they're pure layer-crossing operations
on already-tracked IDs. They can be called on an alive scope at any
time. Frozen scopes can use them (they're read-only).

### 4. Tests

In `tests/test_entity_ref_scope/`:

- New file `test_provenance_context.py` covering the per-scope
  context-entity behavior:
  - Default scope has `.` marker with no provenance parent.
  - Constructing a scope with a c-node sets the marker correctly.
  - The c-node must be a `c` ID (other tags rejected with `ValueError`).
  - The c-node must unwrap to `.` (others rejected).
  - `provenance_context_from_scope` kwarg routes the lift correctly.
  - Default lift-from-parent kwarg works.
  - Child scopes inherit their parent's marker.
  - GLOBAL_SCOPE has no provenance parent.

- Extend `test_caching.py` (or new test file) to cover
  `extract_provenance_parent` / `embed_as_provenance_parent`:
  - Round-trip: embed then extract returns the same provenance node.
  - Extract requires `_provenance_parent` to be set.
  - Embed requires a provenance-node tag.
  - Embed requires the prov_node to be tracked in the scope.

In `tests/test_conversion/test_mashumaro_entities.py`:

- `fxtr_ctx` is None by default on construction.
- Storing a freshly-constructed entity captures the marker from the
  destination scope, not the active scope at construction time.
- Loading an entity surfaces the original `fxtr_ctx`; storing it again
  preserves it.
- (Deferred to Stage 2) Verifying that storing a freshly-constructed
  entity into a scope whose marker has a provenance parent picks up
  that provenance parent in the stored cbor's `$fxtr.ctx`. The cbor
  produced by `_fxtr_store_impl` is consumed immediately by
  `store_cbor` and isn't surfaced anywhere observable in stage 1, so
  this assertion has nothing clean to read. Once stage 2 lands, the
  provenance parent shows up in the returned FxtrID's
  `_provenance_parent`, which is the natural place to assert it.

## Implementation steps

- [x] Update `DataclassEntity.fxtr_ctx` field declaration and the
      `_fxtr_store_impl` injection logic.
- [x] Update `DataclassEntityLoadHandler` to require `$fxtr.ctx`.
- [x] Add `provenance_context` / `provenance_context_from_scope` to
      `FxtrScope.__init__` and `_with_derived_lifetime`.
- [x] Implement the marker-construction logic in
      `_init_arena_and_cache` (or split it into
      `_init_context_marker`). Implemented as
      `_apply_context_marker_override`, called after
      `_populate_ancestor_bridges` in both constructors so that
      `track_from_memory_arena` can dest-adopt inheritance entries
      from the source scope.
- [x] Implement `extract_provenance_parent` /
      `embed_as_provenance_parent`.
- [x] Tests for per-scope context.
- [x] Tests for helper methods.
- [x] Tests for `DataclassEntity` `fxtr_ctx` capture-at-store-time.
- [x] Update CODE_OVERVIEW.md.

## Things to watch / open questions

- `_with_derived_lifetime` callers (mainly the service-call dispatch
  code in `services/service_cap_common.py`) should keep working
  unchanged: passing no `provenance_context` leaves them on the
  inherit-from-parent path. We may want a follow-up to thread the
  caller's c-node through the call-scope creation so service calls
  inherit the caller's provenance context — but that's stage 3
  territory.
- The eager-lift of a parent's c-node into a child scope happens
  unconditionally at construction. If the parent is the global scope
  (no parent c-node), this is a no-op. Otherwise it adds one
  `lazily_attach_from_arena`-equivalent op per scope construction.
  Worth measuring once stage 2 lands.
- Whether `extract_provenance_parent` should also abandon the original
  data ID's "view" from the scope (since it's no longer being
  manipulated as data). Probably no: leaving it tracked is harmless
  and keeps the helper a pure layer-crossing op. The user can call
  `abandon` separately if they want.
