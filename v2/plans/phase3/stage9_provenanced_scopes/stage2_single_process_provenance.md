# Stage 9 — Stage 2: Single-process provenance flow

After Stage 1 the system can describe a scope's c-node and inject it
into stored entities, but FxtrID-level provenance still doesn't flow
through any scope op. This stage makes provenance flow end-to-end
within a single process: stores produce IDs with `_provenance_parent`,
loads return cbor with provenance-parented IDs, and lift / copy /
track / abandon / is_tracked all maintain the (data, prov) pair as a
unit. Memory arena internals are unchanged.

## Pieces

### 1. `store_cbor` provenance branch

**File: `fxtr2/data/entity_ref_scope.py`**

Modify `FxtrScope.store_cbor`:

```
- If cbor["$fxtr.ctx"].inner_id._provenance_parent is None:
    fall through to existing behavior unchanged.

- Else (provenance mode):
    1. Collect every FxtrID inside cbor (marker, weak, strong slots).
    2. For each one, compute its "provenance form":
        - if id._provenance_parent is not None: use id._provenance_parent
          (must be a provenance-node tag).
        - else: ensure a u-node exists for id, by calling
          self._main_arena.create_u_node(self._main_lease.clone(), id).
          The u-node's id is the result.
       The data ID itself must already be tracked (by the existing
       `arena_util.visit_ids(cbor, lambda fid: self.lift(fid.inner_id))`
       guard — repurposed; see below).
    3. Project cbor to its provenance form via
       arena_util.visit_and_project_ids (NOT the canonical-CBOR-order
       variant; we're just substituting IDs, not encoding paths).
       This is safe since this operation does not depend on
       serialization order.
    4. Hand the projected cbor to self._main_arena.store(...). This
       returns an n-node ID.
    5. Sync-read self._main_arena.unwrap_provenance(self._main_lease.clone(),
       n_id). The arena populates this automatically on `store(...)`,
       so the result is the data-form d-node ID.
    6. Return FxtrID(d_id._raw_data_id,
                     _provenance_parent=n_id).
```

Note that step 1's "ensure tracked" guard already exists today as the
`visit_ids(cbor, lambda fid: self.lift(fid.inner_id))` line at the
top of `store_cbor`. We need to keep this — but, importantly, it
operates on the **data-level** IDs from the user-supplied cbor. After
the projection in step 3, the projected CBOR has provenance-level IDs
that all need to be tracked too. The new u-nodes from step 2 are
already tracked under our token via `create_u_node`. The pre-existing
provenance parents (when an ID came in already provenanced) need to
be lifted as well — same `self.lift(...)` mechanism, but on the
provenance ID rather than the data ID.

Cleanest split: do all of step 1 + step 2 in one pass that walks the
cbor once:

```
provenance_ids: dict[FxtrID, FxtrID] = {}  # data_id -> prov_id
def project(typed_id):
    fid = typed_id.inner_id
    self.lift(fid)  # ensures data ID is tracked
    if fid._provenance_parent is not None:
        prov = fid._provenance_parent
        self.lift(prov)
    else:
        prov = self._main_arena.create_u_node(self._main_lease.clone(), fid)
    provenance_ids[fid] = prov
    return prov  # the new ID for the projected cbor
```

Then `arena_util.visit_and_project_ids(cbor, project)` builds the
provenance-form cbor and `_main_arena.store(...)` does the rest.

### 2. `load` / `load_cbor` provenance branch

**File: `fxtr2/data/entity_ref_scope.py`**

Modify `FxtrScope.load_cbor` (and the underlying `load` path that
fetches cbor from `_main_arena.load`):

```
- If node_id._provenance_parent is None:
    fall through to existing behavior.

- Else (provenance mode):
    1. Load the provenance form from the arena:
       prov_cbor = await self._main_arena.load(self._main_lease.clone(),
                                               node_id._provenance_parent)
    2. Walk prov_cbor and rewrap each child FxtrID:
        for each typed_id in prov_cbor (marker / weak / strong):
            child_prov = typed_id.inner_id
            child_data = self._main_arena.unwrap_provenance(
                self._main_lease.clone(), child_prov
            )
            assert child_data is not None  # everything loaded should
                                            # have a known unwrap
            replace with FxtrID(child_data._raw_data_id,
                                _provenance_parent=child_prov).
       Use arena_util.visit_and_project_ids (sync; no async unwrap).
    3. Return the rewrapped cbor.
```

This logic should live in a small helper `_load_cbor_with_provenance`
so it can be reused by both `load_cbor` and the `load` path that
fetches the raw cbor inside `FxtrScope.load`.

### 3. `load` (typed entity load)

**File: `fxtr2/data/entity_ref_scope.py`**

`FxtrScope.load` already calls `self._main_arena.load(self._main_lease.clone(), ref.entity_id)`
to fetch the cbor before invoking the deserialization handler. Update
this so that if `ref.entity_id._provenance_parent is not None`, we use
the provenanced helper from §2. The deserialization handler itself
stays unaware of provenance — its inputs are already-rewrapped cbor
where every child FxtrID carries its provenance parent, so any
sub-loads triggered by inline entity handling will recursively
preserve provenance.

The `$fxtr.type` field on the cbor is already a child ID; in the
provenance branch it'll come back wrapped. The handler-support
machinery in `handler_supports_entity_type` is keyed on the bare
type_id; we should pass `type_field.inner_id` (which is now provenanced)
and let the registry use FxtrID equality (which ignores provenance) so
existing handlers keep matching. Worth a comment explaining this.

### 4. Cache key change

**File: `fxtr2/data/entity_ref_scope.py`**

Today:

```
_ref_loaded_value_cache: defaultdict[
    FxtrID, dict[EntityTypeAnnotation[Any], RefLoadCacheEntry]
]
```

Change to:

```
_ref_loaded_value_cache: defaultdict[
    tuple[FxtrID, FxtrID | None],
    dict[EntityTypeAnnotation[Any], RefLoadCacheEntry]
]
```

Helper to build the key:

```
def _cache_key(node_id: FxtrID) -> tuple[FxtrID, FxtrID | None]:
    return (node_id, node_id._provenance_parent)
```

(`FxtrID` equality ignores `_provenance_parent`, but we want both axes
for the cache key. Wrapping the parent in the tuple is enough — it's
hashable and equal under standard FxtrID rules.)

Update every read/write site:

- `_get_cache_entry_for_ref` (uses `ref.entity_id`)
- `_get_cache_entries_for_id` (uses bare id)
- `lift` cache adoption (adopts entries for `node_id` from a parent
  scope — must adopt only entries with the matching parent)
- `copy_from_scope` / `track_from_scope` cache adoption (same)
- `abandon` cache invalidation (clear the entry for the matching key)
- `clear_cache` (no change)
- `store` (no cache write — left as-is)

For `lift` / `copy_from_scope` / `track_from_scope` — when we adopt
cache entries, we should adopt them keyed exactly on `(data_id,
prov_id)` to preserve the trace; we don't want to mix data-only
entries and provenanced entries.

`_get_cache_entries_for_id` (the data-id-keyed walk) needs an audit:
in the current code it's used by `lift` / `copy_from_scope` /
`track_from_scope` cache adoption (where we now want a paired-key
variant) and by `abandon` (where we now invalidate by paired key).
Replace it with a paired-key version
(`_get_cache_entries_for_paired_key(data_id, prov_id)`) and check
whether anything still wants a "walk all provenance variants of this
data ID" form. If nothing does, drop the data-id-only walker.
**`EntityRef` equality / hashing stays unchanged** — refs continue to
key by data ID alone, mirroring `FxtrID`. The cache key carries the
provenance parent in a parallel tuple slot.

The `_refs_loaded_since_abandon` set holds `EntityRef` objects, which
hash by `(entity_id, target_type)` with `entity_id` ignoring
provenance. We have two options:

**Picked: (a)** — switch to a custom set keyed on `(entity_id, prov,
target_type)` tuples derived from the ref. Cleaner than wrapper
gymnastics and keeps `EntityRef` itself unchanged.

- (a) Switch to a custom set keyed on `(entity_id, prov, target_type)`
  tuples derived from the ref.
- (b) Add a `__hash__` override on a private wrapper `_RefCacheKey`
  that we use in the set.

Lean toward (a) for clarity. Touch points: `_load_for_side_effects`,
`get_cached_value`, the existing `add` calls in `load`, `lift`,
`copy_from_scope`, etc.

### 5. `is_tracked`

**File: `fxtr2/data/entity_ref_scope.py`**

`FxtrScope.is_tracked` and `_is_tracked_internal`:

```
node_id = node.entity_id if isinstance(node, EntityRef) else node
data_tracked = self._main_arena.is_tracked(lease, node_id)
if node_id._provenance_parent is None:
    return data_tracked
prov_tracked = self._main_arena.is_tracked(<another lease>, node_id._provenance_parent)
return data_tracked and prov_tracked
```

The lease is consumed by each call. Every `Lease` is cloneable, so
`_is_tracked_internal` just calls `lease.clone()` for the second
`is_tracked` call when a provenance parent is present. No factory or
secondary parameter needed — the existing single-lease signature
stays.

### 6. `abandon`

**File: `fxtr2/data/entity_ref_scope.py`**

`FxtrScope.abandon(node)`:

```
node_id = node.entity_id if isinstance(node, EntityRef) else node
if not self.is_tracked(node_id):
    raise ValueError(...)
self._main_arena.abandon(self._main_lease.clone(), node_id)
if node_id._provenance_parent is not None:
    self._main_arena.abandon(self._main_lease.clone(),
                             node_id._provenance_parent)
self._ref_loaded_value_cache.pop(_cache_key(node_id), None)
self._refs_loaded_since_abandon.clear()
```

The two arena `abandon` calls are independent; either can succeed
without the other. The `is_tracked(node_id)` precondition above
already checks both data and prov are tracked, so both arena calls
will succeed.

### 7. `lift`

**File: `fxtr2/data/entity_ref_scope.py`**

`FxtrScope.lift(node)`: when the input has a provenance parent, lift
the **provenance parent only** — the arena's `unwraps_to`
inheritance propagation will pull the data ID along automatically
(and the data ID is reachable from the prov ID by sync unwrap, so
once prov is tracked, data is too). Concretely, in `_lift_unchecked`:

- If the input is bare (FxtrID, no prov): existing behavior.
- If the input has a provenance parent:
    - Walk parents looking for the prov ID (not the data ID).
    - Install a sentinel lift for the prov ID from the matching
      parent. The arena auto-attaches the data ID via the
      inheritance walk over `unwraps_to`.
    - Adopt cache entries keyed on `(data_id, prov_id)` from that
      parent.

For the EntityRef variant, the `requested_ref` cache lookup uses the
ref's data ID; we already adapted the cache key in §4 so this should
fall out naturally.

### 8. `copy_from_scope` / `track_from_scope`

**File: `fxtr2/data/entity_ref_scope.py`**

Both methods today call `self._main_arena.copy_from_arena` /
`track_from_memory_arena` with the data ID. In provenance mode we
copy / track **just the provenance parent** — copying a provenance
node into the destination arena always also copies the data it
unwraps to (the arena's DAG walk follows `unwraps_to` as a hard
dependency). So:

```
node_id = ...
target_id = node_id._provenance_parent or node_id
await self._main_arena.copy_from_arena(
    self._main_lease.clone(), source_scope._main_arena,
    source_scope._main_lease.clone(), target_id)
```

(Same shape for `track_from_scope`.) Cache adoption: same as `lift`
— adopt the entry under `(data_id, prov_id)` from the source scope.

The optimization here matters: it halves the DAG-walk work in the
common case, and avoids double-tracking shared substructure when the
data ID and provenance ID share a lot of children.

### 9. CapIRArena — update local provenance reads

**File: `fxtr2/data/cap_ir_arena.py`**

This stage doesn't add wire support yet, but `CapIRArena` is also a
`ReadableArena` used by some single-process tests / paths through the
local Cap-IR plumbing. Its `unwrap_provenance` (sync) reads from the
precomp cache populated by `load_flat` replies. As long as the
provenanced `load`/`load_cbor` path goes through `_main_arena.load`
on a `MemoryArena` (which is what FxtrScope uses), this stage can
leave `CapIRArena` alone. Add a TODO referencing stage 3.

### 10. Tests

In `tests/test_entity_ref_scope/`:

- New file `test_provenance_flow.py`:
  - Storing into a scope with a c-node-marker yields a FxtrID with a
    `_provenance_parent` whose tag is `n` and whose unwrap is the
    data ID.
  - Loading a provenance-parented FxtrID returns a cbor whose child
    FxtrIDs all carry their own provenance parents.
  - Round-trip: store an entity with mixed already-provenanced and
    bare child IDs. The bare IDs should come back wrapped in u-nodes;
    the already-provenanced IDs should come back with their original
    provenance parents.
  - `is_tracked` returns False if either of (data, prov) is missing
    (use a manually-constructed FxtrID with a fake provenance parent
    that isn't tracked).
  - `abandon` removes both data and prov from the scope.
  - `lift` from a parent works for provenanced IDs.
  - `copy_from_scope` / `track_from_scope` round-trip provenanced IDs.

- Extend `test_caching.py`:
  - The same data ID with two different provenance parents has
    independent cache entries.
  - Abandoning one does not invalidate the other.

- New test in `test_conversion/test_mashumaro_entities.py`:
  - End-to-end test: define an entity, store it into a scope with a
    c-node marker, load it via EntityRef, observe that
    `loaded._provenance_parent` is set on the EntityRef's FxtrID,
    and re-storing produces an equivalent provenance trace (round
    trip through the entity Python representation).

## Implementation steps

- [x] Refactor `store_cbor` to add the provenance branch (with
      consolidated visit pass). Implemented as a single
      ``visit_and_project_ids`` walk that lifts each ID into our arena
      and projects to its bare provenance form (existing prov parent or
      a freshly-created u-node).
- [x] Add `_load_cbor_with_provenance` helper and route `load_cbor`
      through it. Implemented as ``_load_arena_cbor`` (chosen the
      shorter name); used by both ``load`` and the side-effect replay.
- [x] Update `load` (typed) to use the same helper for the cbor fetch.
- [x] Change `_ref_loaded_value_cache` key shape and update all read /
      write sites. Helpers: ``_cache_key_for_id``, ``_ref_load_key``,
      ``_get_cache_entries_for_key``.
- [x] Update `_refs_loaded_since_abandon` to be provenance-aware.
      Switched to ``set[tuple[FxtrID, FxtrID | None, EntityTypeAnnotation[Any]]]``.
- [x] Update `is_tracked` / `_is_tracked_internal`. The internal helper
      uses ``lease.clone()`` for the second arena call (no factory
      needed since every Lease is cloneable).
- [x] Update `abandon` to release both halves of a layered ID.
- [x] Update `_lift_unchecked` to lift the prov ID; ``_install_sentinel_lift``
      attaches *both* halves to the destination's liveset (the arena's
      ``unwraps_to`` propagation gives access through inheritance, but
      ``is_tracked`` checks the liveset, so we add both explicitly).
- [x] Update `copy_from_scope` / `track_from_scope` to flow only the
      prov parent + a sync ``unwrap_provenance`` afterward to add the
      data ID to the destination's liveset.
- [x] Fix incidental memory-arena dedup bug: when `_bare_store_impl`
      dedups an existing n-node, walk the ``unwraps_to`` chain and add
      the visiting token to every node's ``owned_tokens``. Without
      this, the post-store ``unwrap_provenance`` fails reachability.
- [x] Add tests in `test_provenance_flow.py` (15 tests).
- [x] Strengthen the two TODO'd Stage 1 tests in
      `test_mashumaro_entities.py` to cover the layered-marker capture
      and round-trip-across-distinct-traces cases.
- [x] Update CODE_OVERVIEW.md.

## Things to watch / open questions

- Performance: provenance mode roughly doubles the per-op work
  (separate data and prov arena calls). Worth measuring with a
  representative workload once stage 2 lands; we may want to fast-path
  the "no provenance parent anywhere in the cbor" case in `store_cbor`.
- `_refs_loaded_since_abandon` already gets cleared on `abandon`; we
  may be able to keep its existing `set[EntityRef]` shape if we
  sidestep the issue with a per-(data_id, prov_id) cleanup. Decide
  during implementation.
- The unwraps_to invariant: when we store an n-node, the arena
  populates `unwraps_to` automatically. When we sync-read
  `unwrap_provenance` immediately after, it's expected to return
  non-None. Worth a sanity assert; if it ever returns None it indicates
  a memory-arena bug.
- For p-node provenance parents (rather than n / c / u): the
  projection in `store_cbor` step 2 just uses the parent ID directly,
  so any provenance node tag works. p-nodes happen to be canonical in
  the path-following sense; that's the arena's concern, not the
  scope's.
- `extract_provenance_parent` / `embed_as_provenance_parent` (from
  Stage 1) may want extra validation in this stage now that they have
  a concrete use case — e.g. extract should validate the data ID is
  also tracked, since the user is about to manipulate the provenance
  node "as data" and we want a clean break.

## Follow-ups discovered during implementation

- **Memory-store provenance-dep invariant**: see
  ``stage2post1_fix_memory_store_provenance.md``. The dedup-of-existing-
  n-node fix patched in Stage 2 is a narrow special case of a broader
  invariant violation in the post-refactor memory store; needs a
  proper audit and follow-up patch.
- **`arena_util` sentinel-skip**: ``visit_ids`` /
  ``visit_and_project_ids`` now treat ``RawFxtrEntityMissingMarkerSentinel``
  as a no-op leaf so the bare-nested-DataclassEntity case can reach
  the encoder for the targeted ``DuplicateEntityMarkerError``.
  Alternative shape would be an explicit pre-check in ``store_cbor``
  / ``_fxtr_store_impl`` that surfaces a "found nested sentinel —
  probably bare-nested entity" error directly, avoiding the visitor
  type expansion. Cosmetic, can revisit.
- **`_install_sentinel_lift` attaches both halves explicitly** vs.
  the equivalent "attach prov, then ``unwrap_provenance`` to pull the
  data into the liveset" shape used in ``copy_from_scope`` /
  ``track_from_scope``. Both produce the same end state; comment in
  the helper notes the alternative.
