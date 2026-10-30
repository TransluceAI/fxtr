# Part 2 Scratch Notes

## Key things to get right

### Orphan check needs to know about index registrations
The current `_is_entity_orphaned_in_namespace` checks backrefs, pins, and arena membership.
We need to add: "is this entity an indexer in a non-GARBAGE registration?" Otherwise GC
will treat indexer entities as orphaned and garbage-collect them.

### Dynamic table names
`NS_INDEX__{indexer_internal_id}` and `NS_INDEX_PROCESSED__{indexer_internal_id}` are named
by the entity's *internal* SQLite ID (not by FxtrID). This is simpler and avoids long table
names from hex hashes.

### Table sharing across namespaces
The same indexer entity can be registered in multiple namespaces. The NS_INDEX__ table is
per-indexer (shared across namespaces), with `namespace_id` column to discriminate. This means:
- Table creation is idempotent (IF NOT EXISTS)
- Table drops need to check that no other namespace uses the indexer

### Compare-and-swap for pin_entity_from_arena
The `transaction()` context manager in sqlite_ops auto-commits on exit and rollbacks on
exception. For the CAS pattern, the cleanest approach is:
1. Open IMMEDIATE transaction
2. Verify INDEXED registrations match expected set
3. If mismatch: raise a custom exception that the caller catches
4. If match: insert pin + index data, commit

The caller catches the mismatch exception, computes missing index entries, and retries.

### ns_index_registry table placement
Should be added to SQL_CREATE_TABLES list. All statements use IF NOT EXISTS so they're
idempotent for existing databases. No migration needed since it's a new table.

### How register_indexer gets entity data for indexing
register_indexer needs to:
1. Copy the indexer entity into the namespace (reuse _build_tmp_arena_copying_from_external_arena)
2. Create a SQLiteNamespaceArena for the indexer entity (for IndexComputer.get_indexer_types)
3. In the build loop:
   - Get unprocessed (pin_key, entity_internal_id) pairs
   - Create a SQLiteNamespaceArena tracking those entities + the indexer
   - Call IndexComputer.compute_indexer_entries(entities, [indexer], arena)
   - Write results to index tables

### How pin_entity_from_arena gets index data
After building the tmp arena (which copies the entity into the store), we need to:
1. Query INDEXED registrations for this namespace matching the key prefix
2. For each registration: need the indexer FxtrID and an arena containing both the indexer
   and the new entity
3. Since the new entity is now in the namespace (via temp arena), we can create a
   SQLiteNamespaceArena containing both
4. Call IndexComputer.compute_indexer_entries([entity], indexers, arena)
5. Pass results into the pin transaction

### Unpin cleanup
When unpinning, we need to delete from all index tables. We don't need to distinguish
INDEXED/BUILDING/GARBAGE - just delete from all tables for this (namespace, entity).
This is safe because:
- For INDEXED: entity is being unpinned, index data should go
- For BUILDING: the build loop will handle any inconsistency
- For GARBAGE: data was going to be deleted anyway

### GC additions
1. In `_is_entity_orphaned_in_namespace`: add check against ns_index_registry
2. In session cleanup (when marking session garbage): set any BUILDING registrations with
   that session's building_session_id to GARBAGE
3. In `run_one_gc_step`: add a new phase that handles GARBAGE index registrations:
   - Delete NS_INDEX__ and NS_INDEX_PROCESSED__ rows for the namespace
   - Delete the registration entry
   - If no other registrations use the same indexer: drop the tables
   - If the indexer entity is now orphaned: rebind to garbage

### iter_matches result mapping
The generated query returns (entity_internal_id, pin_key, var_0, var_1, ...).
We need FxtrID, not internal_id. Options:
1. Modify the generated query to also SELECT entity.public_id via JOIN
2. Do a batch lookup after query execution
3. Let the arena handle the mapping

Option 2 seems cleanest - collect all entity_internal_ids from results, create one shared
arena via _create_for_entities, then map internal_id -> FxtrID from the arena's cache.

### Where to put new SQL functions
sqlite_ops.py is already ~900 lines. Options:
- Add to sqlite_ops.py (keeps everything in one place)
- Create new sqlite_index_ops.py (cleaner separation)

Going with new sqlite_index_ops.py since:
- The index SQL is self-contained
- It has its own set of constants (dynamic table names, registry states)
- Keeps sqlite_ops.py focused on the core entity/pin/session/arena operations

---

## Operation → Transaction → Function mapping

Design constraint: all transaction management lives in sqlite_ops / sqlite_index_ops.
SQLiteNamespaceBackend dispatches to the write/read pool but never opens transactions itself.

### pin_entity_from_arena (replaces old pin flow entirely)

**Backend loop:**
```python
async def pin_entity_from_arena(self, key, entity_id, source, *, overwrite=False):
    temp_arena_id = await self._build_tmp_arena_copying_from_external_arena(entity_id, source.copy_to_new_arena([entity_id]))
    try:
        indexed_regs = await pool.read(get_indexed_registrations_for_prefix, ns_id, key)
        # indexed_regs: list of (reg_id, indexer_entity_iid, indexer_iid, key_prefix)

        computed_entries: dict[int, list[...]] = {}   # indexer_iid -> rows
        num_key_cols: dict[int, int] = {}
        expected_reg_ids: frozenset[int] = frozenset()

        while True:
            new_regs = [r for r in indexed_regs if r.reg_id not in expected_reg_ids]
            if new_regs:
                # Create arena with entity + indexer entities
                all_iids = [entity_iid] + [r.indexer_iid for r in new_regs]
                arena = await SQLiteNamespaceArena._create_for_entities(...)
                results = await IndexComputer().compute_indexer_entries(
                    [entity_fxtr_id], [indexer_fxtr_ids...], arena)
                # Merge into computed_entries (keyed by indexer_iid)
                ...
                expected_reg_ids |= frozenset(r.reg_id for r in new_regs)
                arena.close()

            try:
                entity_iid, garbage = await pool.write(
                    pin_with_index_data, ns_id, entity_public_id, key, overwrite,
                    computed_entries, num_key_cols, expected_reg_ids)
                break
            except IndexMismatchError as e:
                indexed_regs = e.current_registrations
                continue

        if garbage: session._garbage_evt.set()
    finally:
        await session._dispose_arena_in_db(temp_arena_id)
```

**`pin_with_index_data` replaces `create_or_update_pin`** as the entry point for pinning.
When expected_reg_ids is empty and there are no INDEXED registrations, it degrades to plain
pin creation. The CAS check (verify registrations match) always runs, even in the empty case
— this is cheap (one SELECT) and guarantees correctness.

**sqlite_index_ops functions:**

| Function | Kind | Description |
|----------|------|-------------|
| `get_indexed_registrations_for_prefix(conn, ns_id, key)` | read | Returns INDEXED registrations whose key_prefix is a prefix of `key` |
| `pin_with_index_data(conn, ns_id, entity_public_id, key, overwrite, index_entries, num_key_cols, expected_reg_ids)` | write | CAS: verify regs → create/update pin → write index data → clean old entity's index data if overwrite. Raises `IndexMismatchError` or `KeyError`. Returns `(entity_iid, garbage_created)` |

**Private helpers (called inside pin_with_index_data's transaction):**

| Helper | Description |
|--------|-------------|
| `_verify_indexed_registrations(conn, ns_id, key, expected)` | Raises IndexMismatchError if current INDEXED regs ≠ expected |
| `_insert_or_update_pin(conn, ns_id, entity_iid, key, overwrite)` | Pin creation/update, returns old_entity_iid or None |
| `_write_index_rows(conn, ns_id, indexer_iid, entity_iid, rows, num_key_cols)` | INSERT into NS_INDEX__ |
| `_write_processed_marker(conn, ns_id, indexer_iid, entity_iid)` | INSERT into NS_INDEX_PROCESSED__ |
| `_delete_index_entries_for_entity(conn, ns_id, entity_iid)` | DELETE from all NS_INDEX__ and NS_INDEX_PROCESSED__ for this entity (queries ns_index_registry to find tables) |

Note: `_delete_index_entries_for_entity` is also used by unpin and GC.

---

### unpin_entity

**Backend:**
```python
async def unpin_entity(self, key):
    garbage = await pool.write(unpin_with_index_cleanup, ns_id, key)
    if garbage: session._garbage_evt.set()
```

**sqlite_index_ops functions:**

| Function | Kind | Description |
|----------|------|-------------|
| `unpin_with_index_cleanup(conn, ns_id, name)` | write | Get entity_iid from pin → delete index entries for entity → remove pin → check orphan → rebind if needed. Returns garbage_created. Raises KeyError if pin not found. |

The function queries ns_index_registry internally to find all indexer tables, so the backend
doesn't need to pre-query registrations. Everything is atomic in one transaction.

---

### register_indexer

**Backend loop:**
```python
async def register_indexer(self, indexer_id, source, key_prefix=""):
    # 1. Copy indexer entity into namespace
    temp_arena_id = await self._build_tmp_arena_copying_from_external_arena(indexer_id, source)
    await self._session._dispose_arena_in_db(temp_arena_id)

    # 2. Get indexer schema
    arena = await SQLiteNamespaceArena._create_for_entities(session, namespace, [indexer_iid])
    key_types, value_type = await IndexComputer().get_indexer_types(indexer_id, arena)
    arena.close()

    # 3. Create registration
    reg_id, indexer_iid = await pool.write(
        register_indexer_start, ns_id, indexer_entity_iid, key_prefix,
        session._internal_sid, key_types, value_type)

    # 4. Build loop
    BATCH = 100
    while True:
        unprocessed = await pool.read(
            get_unprocessed_pins, ns_id, indexer_iid, key_prefix, limit=BATCH)

        if not unprocessed:
            done = await pool.write(
                try_finish_registration, reg_id, ns_id, indexer_iid, key_prefix)
            if done: break
            else: continue

        # Create arena for batch + indexer
        entity_iids = [e_iid for _, e_iid in unprocessed]
        arena = await SQLiteNamespaceArena._create_for_entities(
            session, namespace, entity_iids + [indexer_iid])
        entity_fxtr_ids = [arena._internal_id_to_fxtr[e_iid] for _, e_iid in unprocessed]
        results = await IndexComputer().compute_indexer_entries(
            entity_fxtr_ids, [indexer_id], arena)

        # Translate results to internal IDs and write
        batch_entries = {}  # entity_iid -> rows
        for (_, entity_fxtr_id), entries in results.items():
            batch_entries[arena._fxtr_id_to_internal[entity_fxtr_id]] = entries
        await pool.write(
            write_index_entries_batch, ns_id, indexer_iid, batch_entries, len(key_types))
        arena.close()
```

**sqlite_index_ops functions:**

| Function | Kind | Description |
|----------|------|-------------|
| `register_indexer_start(conn, ns_id, indexer_entity_iid, key_prefix, session_id, key_types, value_type)` | write | Ensure index tables exist → INSERT BUILDING registration. Returns `(reg_id, indexer_iid)` |
| `get_unprocessed_pins(conn, ns_id, indexer_iid, key_prefix, limit)` | read | SELECT pins matching prefix that are NOT in NS_INDEX_PROCESSED__. Returns `list[(pin_name, entity_iid)]` |
| `write_index_entries_batch(conn, ns_id, indexer_iid, entries: dict[int, list], num_key_cols)` | write | For each entity: write index rows + processed marker |
| `try_finish_registration(conn, reg_id, ns_id, indexer_iid, key_prefix)` | write | Check all pins processed → if yes, flip to INDEXED, return True. If no, return False. |

**Private helpers:**

| Helper | Description |
|--------|-------------|
| `_ensure_index_tables_exist(conn, indexer_iid, key_types, value_type)` | CREATE TABLE IF NOT EXISTS for NS_INDEX__ and NS_INDEX_PROCESSED__ |
| `_write_index_rows` | (same as above, shared with pin) |
| `_write_processed_marker` | (same as above, shared with pin) |

---

### unregister_indexer

**Backend:**
```python
async def unregister_indexer(self, indexer_id, key_prefix=""):
    await pool.write(set_registration_garbage, ns_id, indexer_entity_iid, key_prefix)
    session._garbage_evt.set()
```

**sqlite_index_ops functions:**

| Function | Kind | Description |
|----------|------|-------------|
| `set_registration_garbage(conn, ns_id, indexer_entity_iid, key_prefix)` | write | UPDATE state='GARBAGE'. Raises KeyError if not found. |

---

### list_indexers

**Backend:**
```python
async def list_indexers(self):
    rows = await pool.read(get_non_garbage_registrations, ns_id)
    # rows: list of (indexer_entity_iid, key_prefix, state)
    entity_iids = list(set(r.indexer_entity_iid for r in rows))
    arena = await SQLiteNamespaceArena._create_for_entities(session, namespace, entity_iids)
    return [(arena._internal_id_to_fxtr[r.iid], arena, r.key_prefix) for r in rows]
```

**sqlite_index_ops functions:**

| Function | Kind | Description |
|----------|------|-------------|
| `get_non_garbage_registrations(conn, ns_id)` | read | SELECT from ns_index_registry WHERE state != 'GARBAGE'. Returns list of (indexer_entity_iid, key_prefix, state) |

---

### iter_matches

**Backend:**
```python
async def iter_matches(self, first, /, *rest, key_prefix="", where=None):
    all_constraints = [first, *rest]

    # 1. Resolve each constraint to table/column info
    resolved = []
    for c in all_constraints:
        info = await pool.read(
            resolve_indexer_for_query, ns_id, c.indexer_id._raw_data_id)
        # info: (indexer_iid, key_types, value_type) — verifies INDEXED state
        rewritten_cond = parse_condition(c.condition, frozenset(c.key_variables), allow_value=True)
        resolved.append(ResolvedConstraint(
            table_name=f"NS_INDEX__{info.indexer_iid}",
            key_variables=c.key_variables,
            key_column_types=info.key_types,
            value_column_type=info.value_type,
            condition=rewritten_cond))

    # 2. Build query
    binding_sql = None
    if where:
        all_vars = set()
        for c in all_constraints:
            all_vars.update(c.key_variables)
        binding_sql = parse_condition(where.condition, frozenset(all_vars), allow_value=False)
    query = generate_iter_matches_query(resolved, binding_sql, ns_internal_id, key_prefix)

    # 3. Execute
    rows = await pool.read(execute_index_query, query.sql, query.parameters)

    # 4. Map results
    entity_iids = list(set(row[0] for row in rows))
    if not entity_iids:
        return
    arena = await SQLiteNamespaceArena._create_for_entities(session, namespace, entity_iids)
    for row in rows:
        entity_iid = row[0]
        pin_key = row[1]
        var_values = row[2:]
        fxtr_id = arena._internal_id_to_fxtr[entity_iid]
        bindings = dict(zip(query.binding_variable_names, var_values))
        yield (pin_key, fxtr_id, bindings, arena)
```

**sqlite_index_ops functions:**

| Function | Kind | Description |
|----------|------|-------------|
| `resolve_indexer_for_query(conn, ns_id, indexer_public_id)` | read | Look up entity internal ID → verify INDEXED registration exists → return (indexer_iid, key_types, value_type). Raises if not INDEXED. |
| `execute_index_query(conn, sql, params)` | read | Execute generated SQL, return all rows. |

---

### GC changes

**Modified in sqlite_ops.py:**

In the path where `mark_session_garbage` is called:
```python
# After marking session garbage:
sqlite_index_ops.mark_building_registrations_garbage_for_session(conn, session_id)
```

In `run_one_gc_step`, add a new phase (after existing arena/session cleanup):
```python
# New phase: index cleanup
cleaned = sqlite_index_ops.gc_cleanup_one_garbage_registration(conn)
if cleaned:
    return (True, maybe_garbage_created)
```

**sqlite_index_ops functions:**

| Function | Kind | Description |
|----------|------|-------------|
| `mark_building_registrations_garbage_for_session(conn, session_id)` | (in-txn helper) | UPDATE BUILDING → GARBAGE for all registrations with given building_session_id. Called from within mark_session_garbage's transaction. |
| `gc_cleanup_one_garbage_registration(conn)` | write | Find one GARBAGE registration → delete index/processed rows for that namespace → delete registration → drop tables if unused → check indexer entity orphan → rebind if needed. Returns (work_done, garbage_created). |

**Modified in sqlite_ops.py:**

| Function | Change |
|----------|--------|
| `_is_entity_orphaned_in_namespace` | Add OR EXISTS check against ns_index_registry WHERE state != 'GARBAGE' |
| `run_one_gc_step` | Add call to `gc_cleanup_one_garbage_registration` |
| session death path | Add call to `mark_building_registrations_garbage_for_session` |

---

### Summary: all public sqlite_index_ops entry points

**Write pool:**
1. `register_indexer_start(conn, ns_id, indexer_entity_iid, key_prefix, session_id, key_types, value_type) -> (reg_id, indexer_iid)`
2. `write_index_entries_batch(conn, ns_id, indexer_iid, entries, num_key_cols)`
3. `try_finish_registration(conn, reg_id, ns_id, indexer_iid, key_prefix) -> bool`
4. `set_registration_garbage(conn, ns_id, indexer_entity_iid, key_prefix)`
5. `pin_with_index_data(conn, ns_id, entity_public_id, key, overwrite, index_entries, num_key_cols, expected_reg_ids) -> (entity_iid, garbage_created)`
6. `unpin_with_index_cleanup(conn, ns_id, name) -> bool`
7. `gc_cleanup_one_garbage_registration(conn) -> (bool, bool)`

**Read pool:**
8. `get_indexed_registrations_for_prefix(conn, ns_id, key) -> list[...]`
9. `get_unprocessed_pins(conn, ns_id, indexer_iid, key_prefix, limit) -> list[...]`
10. `get_non_garbage_registrations(conn, ns_id) -> list[...]`
11. `resolve_indexer_for_query(conn, ns_id, indexer_public_id) -> (...)`
12. `execute_index_query(conn, sql, params) -> list[Row]`

**Called from within other transactions (not standalone):**
13. `mark_building_registrations_garbage_for_session(conn, session_id)` (called from sqlite_ops session death)

**Shared private helpers:**
- `_ensure_index_tables_exist(conn, indexer_iid, key_types, value_type)`
- `_verify_indexed_registrations(conn, ns_id, key, expected_reg_ids)`
- `_insert_or_update_pin(conn, ns_id, entity_iid, key, overwrite) -> old_entity_iid | None`
- `_write_index_rows(conn, ns_id, indexer_iid, entity_iid, rows, num_key_cols)`
- `_write_processed_marker(conn, ns_id, indexer_iid, entity_iid)`
- `_delete_index_entries_for_entity(conn, ns_id, entity_iid)`
- `_drop_index_tables_if_unused(conn, indexer_iid)`
