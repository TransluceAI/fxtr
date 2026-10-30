# Plan: `_build_tmp_arena_copying_from_external_arena`

## Goal

Copy an entity and all its transitive dependencies from an external arena into this namespace, returning a temp arena containing them. This combines:
1. Ensuring entities exist in entity tables
2. Adding entities to namespace
3. Adding entities to temp arena

All in a single three-step worklist, protected by entity reservations.

## Key Invariants

1. If an entity is in a namespace, all its deps are also in that namespace.
2. If an entity is in the entity table, all its deps are also in the entity table.
3. Entity reservations protect entities from cleanup between "saved to entity table" and "added to namespace".

## Structure: Three-Step Worklist

The worklist contains three types of work items:
- **"discover"**: Check if entity needs work. If in namespace, add to arena. If in entity tables but not namespace, add reservation + enqueue track. If not in entity tables, fetch from source + enqueue discover(deps) + enqueue save.
- **"save"**: All deps now in entity tables. Save entity to entity tables, add reservation, enqueue track.
- **"track"**: Add entity to namespace tables and temp arena, release reservation.

The stack ordering ensures: deps discovered before save(self), deps saved before save(self), deps tracked before track(self).

## Discover Step

```
if entity_id in discovered:
    continue
discovered.add(entity_id)

# Case 1: Already in namespace
if is_in_namespace(entity_id):
    add_to_tmp_arena(entity_id)
    continue

# Case 2: In entity tables but not namespace
internal_id = db_lookup_entity(entity_id)
if internal_id is not None:
    add_entity_reservation(internal_id)
    worklist.append(("track", entity_id, internal_id))
    continue

# Case 3: Not in entity tables - fetch from source, enqueue deps + save
cbor_bytes, deps = _fetch_data_and_deps_from_external(entity_id, source)

# Enqueue: save(self) first (popped after deps), then discover(deps)
worklist.append(("save", entity_id, cbor_bytes, deps))
for dep_id, _ in deps:
    worklist.append(("discover", dep_id))
```

## Save Step

```
# All deps are now in entity tables (due to stack ordering)
# Look up their internal IDs and save this entity

with transaction:
    # Re-check: concurrent process might have saved it
    existing_id = db_lookup_entity(entity_id)
    if existing_id is not None:
        internal_id = existing_id
    else:
        internal_id = db_insert_entity(entity_id, cbor_bytes, deps)

    add_entity_reservation(internal_id)

worklist.append(("track", entity_id, internal_id))
```

## Track Step

```
with transaction:
    await namespace._add_entity_to_namespace_if_needed(conn, internal_id)
    add_to_tmp_arena(internal_id)  # inline INSERT INTO arena_membership
    release_entity_reservation(internal_id)
```

## Helper: `_add_entity_to_namespace_if_needed`

Namespace-level helper that adds an entity to namespace tables if not already present:

```python
async def _add_entity_to_namespace_if_needed(
    self,
    conn,  # Open transaction
    internal_id: int,
) -> None:
    """Add entity to namespace tables if not already present.

    - Checks namespace_entity_ownership
    - If not present, inserts into namespace_entity_ownership
    - Copies deps from entity_dep into namespace_entity_backref
    """
    # Check if already in namespace
    result = await conn.execute(
        select(namespace_entity_ownership.c.entity_id)
        .where(namespace_entity_ownership.c.namespace_id == self._internal_id)
        .where(namespace_entity_ownership.c.entity_id == internal_id)
    )
    if result.fetchone() is not None:
        return  # Already in namespace

    # Add to ownership
    await conn.execute(
        namespace_entity_ownership.insert().values(
            namespace_id=self._internal_id,
            entity_id=internal_id
        )
    )

    # Copy deps to backrefs
    result = await conn.execute(
        select(entity_dep.c.required_id)
        .where(entity_dep.c.requirer_id == internal_id)
    )
    for row in result:
        await conn.execute(
            namespace_entity_backref.insert().values(
                namespace_id=self._internal_id,
                requirer_id=internal_id,
                required_id=row.required_id
            )
        )
```

This helper is also used in pt2 (load).

## Helper: `_fetch_data_and_deps_from_external`

Same as before - handles all node-type-specific logic for fetching from source arena:
- `cbor_bytes`: Serialized CBOR data (for d/n nodes), or None (for derived nodes)
- `deps`: List of `(FxtrID, DepKind)` tuples

By node type:
- **d nodes**: `source.load()`, serialize, extract child refs
- **n nodes**: Same + `source.unwrap_provenance()` for UNWRAPS_TO
- **c nodes**: `source.c_node_to_context_data()` + `source.unwrap_provenance()`
- **p nodes**: `source.p_node_to_pathroot()` + `source.unwrap_provenance()` + optional `source.step_back_p_ctx()`
- **u nodes**: `source.unwrap_provenance()`
- **. and ! nodes**: `(None, [])`

## Helper: `db_load_deps`

Load deps from entity_dep table, returning list of `(FxtrID, DepKind)`:

```sql
SELECT e.public_id, ed.kind
FROM entity_dep ed
JOIN entity e ON ed.required_id = e.id
WHERE ed.requirer_id = ?
```

Reconstruct FxtrID from public_id bytes for each row.

## Full Algorithm

```
_build_tmp_arena_copying_from_external_arena(root_id, source):
    temp_arena_id = create_temp_arena()

    discovered = set()
    worklist = [("discover", root_id)]

    while worklist:
        item = worklist.pop()

        if item[0] == "discover":
            entity_id = item[1]

            if entity_id in discovered:
                continue
            discovered.add(entity_id)

            # Case 1: Already in namespace
            if is_in_namespace(entity_id):
                add_to_tmp_arena(entity_id, temp_arena_id)
                continue

            # Case 2: In entity tables but not namespace
            internal_id = db_lookup_entity(entity_id)
            if internal_id is not None:
                add_entity_reservation(internal_id)
                worklist.append(("track", entity_id, internal_id))
                continue

            # Case 3: Not in entity tables
            cbor_bytes, deps = _fetch_data_and_deps_from_external(entity_id, source)
            worklist.append(("save", entity_id, cbor_bytes, deps))
            for dep_id, _ in deps:
                worklist.append(("discover", dep_id))

        elif item[0] == "save":
            entity_id, cbor_bytes, deps = item[1], item[2], item[3]

            with transaction:
                existing_id = db_lookup_entity(entity_id)
                if existing_id is not None:
                    internal_id = existing_id
                else:
                    internal_id = db_insert_entity(entity_id, cbor_bytes, deps)
                add_entity_reservation(internal_id)

            worklist.append(("track", entity_id, internal_id))

        elif item[0] == "track":
            entity_id, internal_id = item[1], item[2]

            with transaction:
                await namespace._add_entity_to_namespace_if_needed(conn, internal_id)
                add_to_tmp_arena(internal_id, temp_arena_id)
                release_entity_reservation(internal_id)

    return temp_arena_id
```

## Why This Works

1. **Stack ordering**: `save(self)` pushed before `discover(deps)`, so deps are discovered and saved before we save self. Similarly for track.

2. **Three steps solve the dep-internal-id problem**: We can't save until deps exist in entity tables (need their internal IDs for entity_dep). The save step only fires after all deps have been discovered and saved.

3. **Reservations**: Protect entities between save (add to entity tables) and track (add to namespace). No cleanup can race with us.

4. **Atomic save and track**: Both steps re-check state in a transaction, handling concurrent operations gracefully.

5. **Single loop**: No separate passes - entity table population and namespace tracking happen together, driven by the same worklist.

6. **Efficiency**: If entity is in entity tables but not namespace (Case 2), we skip straight to track (no source arena, no save). If already in namespace (Case 1), we just add to arena.

---

## Revised Structure: Four-Step Worklist with TrackDeps

The original plan has a bug: Case 2 in Discover (entity in entity tables but not namespace) directly enqueues Track, but Track's `_add_entity_to_namespace_if_needed` requires all deps to already be in the namespace (to insert backrefs). If the entity's deps are also not in namespace, this fails.

The fix is to introduce a `TrackDeps` step that recursively ensures deps are tracked before tracking self.

### Key Insight

Move the "is in namespace?" check from Discover to TrackDeps. This:
1. Simplifies Discover to only two cases (in entity tables vs not)
2. Centralizes namespace membership logic in TrackDeps
3. TrackDeps doesn't need to check if individual deps are in namespace—it reserves ALL deps and enqueues TrackDeps for ALL of them; each dep's TrackDeps handles its own "already in namespace" check

### Revised Work Items

**Discover(entity_id)**:
- Requires: entity_id exists in the source arena we're copying from
- Ensures: entity_id will be saved to entity tables and added to namespace (via enqueued work)
- How:
  - If already in `discovered` set: skip (deduplication)
  - If in entity tables: reserve it, enqueue `TrackDeps(internal_id)`
  - If not in entity tables: fetch data from source, enqueue `Save(...)` then `Discover(deps)`

**TrackDeps(internal_id)**:
- Requires: internal_id is in entity tables, and is reserved
- Ensures: internal_id and all its transitive deps are added to namespace
- How:
  - If already in namespace: add to temp arena, release reservation, done
  - If not in namespace: load deps from entity_dep, reserve ALL deps, enqueue `TrackDeps(dep)` for ALL deps, then enqueue `Track(internal_id)`

**Save(entity_id, cbor, deps)**:
- Requires: entity_id serializes to given cbor/deps, all deps have been added to namespace (guaranteed by stack ordering)
- Ensures: entity_id is saved to entity tables and added to namespace (via enqueued Track)
- How: save to entity tables (re-check for concurrent insert), reserve it, enqueue `Track(internal_id)`

**Track(internal_id)**:
- Requires: internal_id is in entity tables, all deps are in namespace, internal_id is reserved
- Ensures: internal_id is added to namespace and temp arena, reservation released
- How: call `_add_entity_to_namespace_if_needed`, insert into arena_membership, release reservation

### Stack Ordering

With a LIFO stack:

**Discover** pushes `Save(self)` then `Discover(deps)`:
- Pop order: `Discover(deps)` all run first, then `Save(self)`
- This ensures all deps are discovered/saved/tracked before we save self

**TrackDeps** pushes `Track(self)` then `TrackDeps(deps)`:
- Pop order: `TrackDeps(deps)` all run first, then `Track(self)`
- This ensures all deps are in namespace before we track self

### Example Trace: Diamond Dependency

Copy A where A→B, A→C, B→X, C→X. All in entity tables, none in namespace.

```
1. Discover(A) → reserve(A), enqueue TrackDeps(A)
   Stack: [TrackDeps(A)]

2. TrackDeps(A) → deps=[B,C], reserve(B), reserve(C), enqueue Track(A), TrackDeps(B), TrackDeps(C)
   Stack: [Track(A), TrackDeps(B), TrackDeps(C)]

3. Pop TrackDeps(C) → deps=[X], reserve(X), enqueue Track(C), TrackDeps(X)
   Stack: [Track(A), TrackDeps(B), Track(C), TrackDeps(X)]

4. Pop TrackDeps(X) → deps=[], enqueue Track(X)
   Stack: [Track(A), TrackDeps(B), Track(C), Track(X)]

5. Pop Track(X) → add X to namespace, release reservation
6. Pop Track(C) → add C to namespace (X is there), release reservation

7. Pop TrackDeps(B) → deps=[X], X IS in namespace now!
   → reserve(X) again, enqueue Track(B), TrackDeps(X)
   Stack: [Track(A), Track(B), TrackDeps(X)]

8. Pop TrackDeps(X) → X IS in namespace → add to arena, release reservation, done
   Stack: [Track(A), Track(B)]

9. Pop Track(B) → add B to namespace (X is there), release reservation
10. Pop Track(A) → add A to namespace (B,C are there), release reservation
```

Key observations:
- X gets reserved twice (step 3 and step 7) and released twice (step 5 and step 8)
- The reservation count mechanism handles this correctly (increments on reserve, decrements on release)
- When TrackDeps(X) runs the second time, it sees X is in namespace and short-circuits
- Between step 5 (X released) and step 7 (X reserved again), X is protected from GC because it's in the namespace (with C depending on it via backref)

### Revised Full Algorithm

```
_build_tmp_arena_copying_from_external_arena(root_id, source):
    temp_arena_id = create_temp_arena()
    discovered = set()  # FxtrIDs we've seen in Discover
    fxtr_to_internal = {}  # Cache of FxtrID -> internal_id

    worklist = [Discover(root_id)]

    while worklist:
        item = worklist.pop()

        if isinstance(item, Discover):
            entity_id = item.entity_id

            if entity_id in discovered:
                continue
            discovered.add(entity_id)

            # Check if in entity tables
            internal_id = db_lookup_entity(entity_id)
            if internal_id is not None:
                fxtr_to_internal[entity_id] = internal_id
                add_entity_reservation(internal_id)
                worklist.append(TrackDeps(internal_id))
            else:
                # Not in entity tables - fetch from source
                cbor_bytes, deps = _fetch_data_and_deps_from_external(entity_id, source)
                worklist.append(Save(entity_id, cbor_bytes, deps))
                for dep_id, _ in deps:
                    worklist.append(Discover(dep_id))

        elif isinstance(item, TrackDeps):
            internal_id = item.internal_id

            # Check if already in namespace
            if is_in_namespace(internal_id):
                add_to_temp_arena(internal_id)  # Use ON CONFLICT DO NOTHING
                release_entity_reservation(internal_id)
                continue

            # Load deps from entity_dep table
            deps = db_load_deps(internal_id)  # Returns list of internal_ids

            # Reserve all deps and enqueue TrackDeps for all
            for dep_internal_id in deps:
                add_entity_reservation(dep_internal_id)

            worklist.append(Track(internal_id))
            for dep_internal_id in deps:
                worklist.append(TrackDeps(dep_internal_id))

        elif isinstance(item, Save):
            entity_id = item.entity_id
            cbor_bytes = item.cbor_bytes
            deps = item.deps

            with transaction:
                # Re-check for concurrent insert
                existing_id = db_lookup_entity(entity_id)
                if existing_id is not None:
                    internal_id = existing_id
                else:
                    internal_id = db_insert_entity(entity_id, cbor_bytes, deps)

                fxtr_to_internal[entity_id] = internal_id
                add_entity_reservation(internal_id)

            worklist.append(Track(internal_id))

        elif isinstance(item, Track):
            internal_id = item.internal_id

            with transaction:
                _add_entity_to_namespace_if_needed(internal_id)
                add_to_temp_arena(internal_id)
                release_entity_reservation(internal_id)

    return temp_arena_id
```

### Why This Works

1. **TrackDeps handles the recursive namespace tracking**: Unlike the original plan where Track assumed deps were in namespace, now TrackDeps explicitly ensures deps are tracked first via stack ordering.

2. **No namespace check duplication**: The "is in namespace?" check only exists in TrackDeps, not in Discover.

3. **TrackDeps is idempotent-ish**: If the same entity goes through TrackDeps multiple times (via different dependency paths), the first one adds it to namespace, and subsequent ones see it's there and short-circuit. The reservation count ensures proper cleanup.

4. **Reservation count handles diamonds**: When X is a dep of both B and C, it may get reserved multiple times. Each reserve increments the count, each release decrements. The entity is protected as long as count > 0.
||||||| Base
# Plan: `_build_tmp_arena_copying_from_external_arena`

## Goal

Copy an entity and all its transitive dependencies from an external arena into this namespace, returning a temp arena containing them. This combines:
1. Ensuring entities exist in entity tables
2. Adding entities to namespace
3. Adding entities to temp arena

All in a single three-step worklist, protected by entity reservations.

## Key Invariants

1. If an entity is in a namespace, all its deps are also in that namespace.
2. If an entity is in the entity table, all its deps are also in the entity table.
3. Entity reservations protect entities from cleanup between "saved to entity table" and "added to namespace".

## Structure: Three-Step Worklist

The worklist contains three types of work items:
- **"discover"**: Check if entity needs work. If in namespace, add to arena. If in entity tables but not namespace, add reservation + enqueue track. If not in entity tables, fetch from source + enqueue discover(deps) + enqueue save.
- **"save"**: All deps now in entity tables. Save entity to entity tables, add reservation, enqueue track.
- **"track"**: Add entity to namespace tables and temp arena, release reservation.

The stack ordering ensures: deps discovered before save(self), deps saved before save(self), deps tracked before track(self).

## Discover Step

```
if entity_id in discovered:
    continue
discovered.add(entity_id)

# Case 1: Already in namespace
if is_in_namespace(entity_id):
    add_to_tmp_arena(entity_id)
    continue

# Case 2: In entity tables but not namespace
internal_id = db_lookup_entity(entity_id)
if internal_id is not None:
    add_entity_reservation(internal_id)
    worklist.append(("track", entity_id, internal_id))
    continue

# Case 3: Not in entity tables - fetch from source, enqueue deps + save
cbor_bytes, deps = _fetch_data_and_deps_from_external(entity_id, source)

# Enqueue: save(self) first (popped after deps), then discover(deps)
worklist.append(("save", entity_id, cbor_bytes, deps))
for dep_id, _ in deps:
    worklist.append(("discover", dep_id))
```

## Save Step

```
# All deps are now in entity tables (due to stack ordering)
# Look up their internal IDs and save this entity

with transaction:
    # Re-check: concurrent process might have saved it
    existing_id = db_lookup_entity(entity_id)
    if existing_id is not None:
        internal_id = existing_id
    else:
        internal_id = db_insert_entity(entity_id, cbor_bytes, deps)

    add_entity_reservation(internal_id)

worklist.append(("track", entity_id, internal_id))
```

## Track Step

```
with transaction:
    await namespace._add_entity_to_namespace_if_needed(conn, internal_id)
    add_to_tmp_arena(internal_id)  # inline INSERT INTO arena_membership
    release_entity_reservation(internal_id)
```

## Helper: `_add_entity_to_namespace_if_needed`

Namespace-level helper that adds an entity to namespace tables if not already present:

```python
async def _add_entity_to_namespace_if_needed(
    self,
    conn,  # Open transaction
    internal_id: int,
) -> None:
    """Add entity to namespace tables if not already present.

    - Checks namespace_entity_ownership
    - If not present, inserts into namespace_entity_ownership
    - Copies deps from entity_dep into namespace_entity_backref
    """
    # Check if already in namespace
    result = await conn.execute(
        select(namespace_entity_ownership.c.entity_id)
        .where(namespace_entity_ownership.c.namespace_id == self._internal_id)
        .where(namespace_entity_ownership.c.entity_id == internal_id)
    )
    if result.fetchone() is not None:
        return  # Already in namespace

    # Add to ownership
    await conn.execute(
        namespace_entity_ownership.insert().values(
            namespace_id=self._internal_id,
            entity_id=internal_id
        )
    )

    # Copy deps to backrefs
    result = await conn.execute(
        select(entity_dep.c.required_id)
        .where(entity_dep.c.requirer_id == internal_id)
    )
    for row in result:
        await conn.execute(
            namespace_entity_backref.insert().values(
                namespace_id=self._internal_id,
                requirer_id=internal_id,
                required_id=row.required_id
            )
        )
```

This helper is also used in pt2 (load).

## Helper: `_fetch_data_and_deps_from_external`

Same as before - handles all node-type-specific logic for fetching from source arena:
- `cbor_bytes`: Serialized CBOR data (for d/n nodes), or None (for derived nodes)
- `deps`: List of `(FxtrID, DepKind)` tuples

By node type:
- **d nodes**: `source.load()`, serialize, extract child refs
- **n nodes**: Same + `source.unwrap_provenance()` for UNWRAPS_TO
- **c nodes**: `source.c_node_to_context_data()` + `source.unwrap_provenance()`
- **p nodes**: `source.p_node_to_pathroot()` + `source.unwrap_provenance()` + optional `source.step_back_p_ctx()`
- **u nodes**: `source.unwrap_provenance()`
- **. and ! nodes**: `(None, [])`

## Helper: `db_load_deps`

Load deps from entity_dep table, returning list of `(FxtrID, DepKind)`:

```sql
SELECT e.public_id, ed.kind
FROM entity_dep ed
JOIN entity e ON ed.required_id = e.id
WHERE ed.requirer_id = ?
```

Reconstruct FxtrID from public_id bytes for each row.

## Full Algorithm

```
_build_tmp_arena_copying_from_external_arena(root_id, source):
    temp_arena_id = create_temp_arena()

    discovered = set()
    worklist = [("discover", root_id)]

    while worklist:
        item = worklist.pop()

        if item[0] == "discover":
            entity_id = item[1]

            if entity_id in discovered:
                continue
            discovered.add(entity_id)

            # Case 1: Already in namespace
            if is_in_namespace(entity_id):
                add_to_tmp_arena(entity_id, temp_arena_id)
                continue

            # Case 2: In entity tables but not namespace
            internal_id = db_lookup_entity(entity_id)
            if internal_id is not None:
                add_entity_reservation(internal_id)
                worklist.append(("track", entity_id, internal_id))
                continue

            # Case 3: Not in entity tables
            cbor_bytes, deps = _fetch_data_and_deps_from_external(entity_id, source)
            worklist.append(("save", entity_id, cbor_bytes, deps))
            for dep_id, _ in deps:
                worklist.append(("discover", dep_id))

        elif item[0] == "save":
            entity_id, cbor_bytes, deps = item[1], item[2], item[3]

            with transaction:
                existing_id = db_lookup_entity(entity_id)
                if existing_id is not None:
                    internal_id = existing_id
                else:
                    internal_id = db_insert_entity(entity_id, cbor_bytes, deps)
                add_entity_reservation(internal_id)

            worklist.append(("track", entity_id, internal_id))

        elif item[0] == "track":
            entity_id, internal_id = item[1], item[2]

            with transaction:
                await namespace._add_entity_to_namespace_if_needed(conn, internal_id)
                add_to_tmp_arena(internal_id, temp_arena_id)
                release_entity_reservation(internal_id)

    return temp_arena_id
```

## Why This Works

1. **Stack ordering**: `save(self)` pushed before `discover(deps)`, so deps are discovered and saved before we save self. Similarly for track.

2. **Three steps solve the dep-internal-id problem**: We can't save until deps exist in entity tables (need their internal IDs for entity_dep). The save step only fires after all deps have been discovered and saved.

3. **Reservations**: Protect entities between save (add to entity tables) and track (add to namespace). No cleanup can race with us.

4. **Atomic save and track**: Both steps re-check state in a transaction, handling concurrent operations gracefully.

5. **Single loop**: No separate passes - entity table population and namespace tracking happen together, driven by the same worklist.

6. **Efficiency**: If entity is in entity tables but not namespace (Case 2), we skip straight to track (no source arena, no save). If already in namespace (Case 1), we just add to arena.
