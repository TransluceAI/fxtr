# Part 2: SQLiteNamespaceBackend Integration

Implements indexing support in the SQLite store: schema, register/unregister, indexed pinning,
indexed unpinning, GC, and query execution.

## Overview

Part 1 built self-contained helpers: SQL expression parsing, indexer path parsing, indexer
serialization, unpack logic, query generation, and the IndexComputer. Part 2 wires these into
the SQLite backend so that `SQLiteNamespaceBackend` implements `IndexablePinningBackend`.

**New file:** `sqlite_store/sqlite_index_ops.py` — all index-related SQL strings and sync
database operations (parallel to `sqlite_ops.py` for the core store).

**Modified files:**
- `sqlite_store/sqlite_ops.py` — add `ns_index_registry` to schema, modify orphan check and GC
- `sqlite_store/sqlite_namespace.py` — `SQLiteNamespaceBackend` gains indexing methods

**Design constraint:** All transaction management lives in sqlite_ops / sqlite_index_ops.
`SQLiteNamespaceBackend` dispatches to the write/read pool but never opens transactions itself.

## Schema

### ns_index_registry (static table)

Added to `SQL_CREATE_TABLES` in `sqlite_ops.py`:

```sql
CREATE TABLE IF NOT EXISTS ns_index_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace_id INTEGER NOT NULL REFERENCES namespace(id),
    indexer_entity_id INTEGER NOT NULL REFERENCES entity(id),
    key_prefix VARCHAR NOT NULL DEFAULT '',
    state VARCHAR NOT NULL CHECK(state IN ('BUILDING', 'INDEXED', 'GARBAGE')),
    building_session_id INTEGER REFERENCES session(id),
    UNIQUE(namespace_id, indexer_entity_id, key_prefix)
);
```

### Dynamic per-indexer tables

Created on demand by `_ensure_index_tables_exist()`. Table names use the indexer entity's
**internal** SQLite ID as a suffix.

```sql
-- Index data: one row per (entity, key_tuple) produced by the indexer
CREATE TABLE IF NOT EXISTS NS_INDEX__{IDX_ID} (
    namespace_id       INTEGER NOT NULL,
    entity_internal_id INTEGER NOT NULL,
    IXKEY_0            {sqlite_type},
    ...
    IXKEY_{N-1}        {sqlite_type},
    VALUE              {sqlite_type},
    PRIMARY KEY (namespace_id, entity_internal_id, IXKEY_0, ..., IXKEY_{N-1})
);

-- Tracks which entities have been indexed (needed for entities producing zero rows)
CREATE TABLE IF NOT EXISTS NS_INDEX_PROCESSED__{IDX_ID} (
    namespace_id       INTEGER NOT NULL,
    entity_internal_id INTEGER NOT NULL,
    PRIMARY KEY (namespace_id, entity_internal_id)
);
```

Column types come from `COLUMN_TYPE_TO_SQLITE` in `sql_expr.py`.

Tables are shared across namespaces (the `namespace_id` column discriminates).

## New module: sqlite_store/sqlite_index_ops.py

All functions take `sqlite3.Connection` as first parameter and manage their own transactions
(same pattern as `sqlite_ops.py`).

### Public entry points (write pool)

| # | Function | Returns | Description |
|---|----------|---------|-------------|
| 1 | `register_indexer_start(conn, ns_id, indexer_entity_iid, key_prefix, session_id, key_types, value_type)` | `(reg_id, indexer_iid)` | Ensure index tables exist → INSERT BUILDING registration |
| 2 | `write_index_entries_batch(conn, ns_id, indexer_iid, entries: dict[int, list], num_key_cols)` | None | Write index rows + processed markers for multiple entities |
| 3 | `try_finish_registration(conn, reg_id, ns_id, indexer_iid, key_prefix)` | `bool` | Check all pins processed → if yes, flip BUILDING→INDEXED, return True; else return False |
| 4 | `set_registration_garbage(conn, ns_id, indexer_entity_iid, key_prefix)` | None | UPDATE state='GARBAGE'. Raises KeyError if not found. |
| 5 | `pin_with_index_data(conn, ns_id, entity_public_id, key, overwrite, index_entries, num_key_cols, expected_reg_ids)` | `(entity_iid, garbage_created)` | CAS: verify regs → pin → write index data → clean old index data if overwrite. Raises `IndexMismatchError` or `KeyError`. |
| 6 | `unpin_with_index_cleanup(conn, ns_id, name)` | `garbage_created: bool` | Get entity from pin → delete index entries → remove pin → orphan check/rebind. Raises `KeyError`. |
| 7 | `gc_cleanup_one_garbage_registration(conn)` | `(work_done, garbage_created)` | Find one GARBAGE reg → delete index/processed rows → delete reg → drop tables if unused → orphan check indexer entity |

### Public entry points (read pool)

| # | Function | Returns | Description |
|---|----------|---------|-------------|
| 8 | `get_indexed_registrations_for_prefix(conn, ns_id, key)` | `list[(reg_id, indexer_entity_iid, indexer_iid, key_prefix)]` | INDEXED registrations whose prefix matches `key` |
| 9 | `get_unprocessed_pins(conn, ns_id, indexer_iid, key_prefix, limit)` | `list[(pin_name, entity_iid)]` | Pins matching prefix NOT in NS_INDEX_PROCESSED__ |
| 10 | `get_non_garbage_registrations(conn, ns_id)` | `list[(indexer_entity_iid, key_prefix, state)]` | All non-GARBAGE registrations for list_indexers |
| 11 | `resolve_indexer_for_query(conn, ns_id, indexer_public_id)` | `(indexer_iid, key_types, value_type)` | Look up entity → verify INDEXED → return schema info |
| 12 | `execute_index_query(conn, sql, params)` | `list[Row]` | Execute generated SQL query |

### Called from within other transactions (not standalone)

| # | Function | Description |
|---|----------|-------------|
| 13 | `mark_building_registrations_garbage_for_session(conn, session_id)` | UPDATE BUILDING→GARBAGE for given session. Called from sqlite_ops session death path. |

### Shared private helpers

| Helper | Description |
|--------|-------------|
| `_ensure_index_tables_exist(conn, indexer_iid, key_types, value_type)` | CREATE TABLE IF NOT EXISTS for NS_INDEX__ and NS_INDEX_PROCESSED__ |
| `_verify_indexed_registrations(conn, ns_id, key, expected_reg_ids)` | Raises `IndexMismatchError` if current INDEXED regs ≠ expected |
| `_insert_or_update_pin(conn, ns_id, entity_iid, key, overwrite)` | Pin creation/update logic, returns `old_entity_iid \| None` |
| `_write_index_rows(conn, ns_id, indexer_iid, entity_iid, rows, num_key_cols)` | INSERT into NS_INDEX__ |
| `_write_processed_marker(conn, ns_id, indexer_iid, entity_iid)` | INSERT into NS_INDEX_PROCESSED__ |
| `_delete_index_entries_for_entity(conn, ns_id, entity_iid)` | DELETE from all NS_INDEX__ and NS_INDEX_PROCESSED__ (queries ns_index_registry for table list) |
| `_drop_index_tables_if_unused(conn, indexer_iid)` | DROP TABLE if no registrations reference this indexer |

### IndexMismatchError

```python
class IndexMismatchError(Exception):
    """Raised when INDEXED registrations changed during pin CAS."""
    current_registrations: list[tuple[int, int, int, str]]
    # (reg_id, indexer_entity_iid, indexer_iid, key_prefix)
```

Raised by `pin_with_index_data` when the set of INDEXED registrations for the key doesn't
match `expected_reg_ids`. The transaction rolls back automatically (exception exits the
`with transaction(...)` context manager). The backend catches this and retries with the
updated registration set.

## Modifications to sqlite_ops.py

### Schema change

Add `ns_index_registry` CREATE TABLE to `SQL_CREATE_TABLES`.

### _is_entity_orphaned_in_namespace

Add a fourth condition: entity is NOT orphaned if it is referenced as `indexer_entity_id` in
any non-GARBAGE registration for this namespace.

```sql
OR EXISTS(
    SELECT 1 FROM ns_index_registry
    WHERE indexer_entity_id = ? AND namespace_id = ? AND state != 'GARBAGE'
)
```

### GC changes

**In session death handling** (where `mark_session_garbage` is called):
After marking a session as garbage, call
`sqlite_index_ops.mark_building_registrations_garbage_for_session(conn, session_id)`.

**In `run_one_gc_step`**: Add a new GC phase for index cleanup. After existing arena/session
cleanup phases:

```python
# New phase: clean up GARBAGE index registrations
work_done, garbage_created = sqlite_index_ops.gc_cleanup_one_garbage_registration(conn)
if work_done:
    return (True, garbage_created)
```

`gc_cleanup_one_garbage_registration` performs one step:
1. Find a GARBAGE registration
2. Delete all NS_INDEX__ and NS_INDEX_PROCESSED__ rows for that (namespace, indexer)
3. Delete the registration entry
4. Call `_drop_index_tables_if_unused(conn, indexer_iid)`
5. Check if indexer entity is now orphaned; if so, rebind to garbage
6. Return `(work_done=True, garbage_created)`

One registration per call to keep transactions short.

## Backend method flows

### register_indexer

```python
async def register_indexer(self, indexer_id, source, key_prefix=""):
    # 1. Copy indexer into namespace (reuse existing copy flow)
    temp_arena_id = await self._build_tmp_arena_copying_from_external_arena(
        indexer_id, source.copy_to_new_arena([indexer_id]))
    await self._session._dispose_arena_in_db(temp_arena_id)

    # 2. Get indexer schema via IndexComputer
    indexer_iid = await pool.read(lookup_entity_internal_id, indexer_id._raw_data_id)
    arena = await SQLiteNamespaceArena._create_for_entities(
        session, namespace, [indexer_iid])
    key_types, value_type = await IndexComputer().get_indexer_types(indexer_id, arena)
    arena.close()

    # 3. Create registration + tables
    reg_id, indexer_iid = await pool.write(
        register_indexer_start, ns_id, indexer_iid, key_prefix,
        session._internal_sid, key_types, value_type)

    # 4. Build loop
    BATCH = 100
    while True:
        unprocessed = await pool.read(
            get_unprocessed_pins, ns_id, indexer_iid, key_prefix, limit=BATCH)

        if not unprocessed:
            done = await pool.write(
                try_finish_registration, reg_id, ns_id, indexer_iid, key_prefix)
            if done:
                break
            else:
                continue  # new pins appeared, loop again

        # Create arena for batch entities + indexer
        entity_iids = [e_iid for _, e_iid in unprocessed]
        arena = await SQLiteNamespaceArena._create_for_entities(
            session, namespace, entity_iids + [indexer_iid])
        entity_fxtr_ids = [arena._internal_id_to_fxtr[e_iid]
                           for _, e_iid in unprocessed]

        results = await IndexComputer().compute_indexer_entries(
            entity_fxtr_ids, [indexer_id], arena)

        # Translate and write
        batch_entries: dict[int, list] = {}
        for (_, entity_fxtr_id), entries in results.items():
            batch_entries[arena._fxtr_id_to_internal[entity_fxtr_id]] = entries
        await pool.write(
            write_index_entries_batch, ns_id, indexer_iid,
            batch_entries, len(key_types))
        arena.close()
```

### unregister_indexer

```python
async def unregister_indexer(self, indexer_id, key_prefix=""):
    indexer_iid = await pool.read(lookup_entity_internal_id, indexer_id._raw_data_id)
    await pool.write(set_registration_garbage, ns_id, indexer_iid, key_prefix)
    if self._session._garbage_evt is not None:
        self._session._garbage_evt.set()
```

### list_indexers

```python
async def list_indexers(self):
    rows = await pool.read(get_non_garbage_registrations, ns_id)
    # rows: list of (indexer_entity_iid, key_prefix, state)
    entity_iids = list(set(r[0] for r in rows))
    if not entity_iids:
        return []
    arena = await SQLiteNamespaceArena._create_for_entities(
        session, namespace, entity_iids)
    return [(arena._internal_id_to_fxtr[r[0]], arena, r[1]) for r in rows]
```

### pin_entity_from_arena (replaces old flow)

`pin_with_index_data` replaces `create_or_update_pin` as the entry point. When there are no
INDEXED registrations, the CAS check is a single cheap SELECT confirming zero registrations,
then the function does a plain pin. No separate code path needed.

```python
async def pin_entity_from_arena(self, key, entity_id, source, *, overwrite=False):
    temp_arena_id = await self._build_tmp_arena_copying_from_external_arena(
        entity_id, source.copy_to_new_arena([entity_id]))
    try:
        indexed_regs = await pool.read(
            get_indexed_registrations_for_prefix, ns_id, key)
        # list of (reg_id, indexer_entity_iid, indexer_iid, key_prefix)

        computed_entries: dict[int, list] = {}  # indexer_iid -> rows
        num_key_cols: dict[int, int] = {}
        expected_reg_ids: frozenset[int] = frozenset()

        while True:
            # Compute entries for any registrations we haven't handled yet
            new_regs = [r for r in indexed_regs
                        if r[0] not in expected_reg_ids]
            if new_regs:
                new_indexer_iids = [r[2] for r in new_regs]
                # entity is already in namespace from temp arena copy
                entity_iid = ...  # look up from entity table
                all_iids = [entity_iid] + new_indexer_iids
                arena = await SQLiteNamespaceArena._create_for_entities(
                    session, namespace, all_iids)
                entity_fxtr_id = arena._internal_id_to_fxtr[entity_iid]
                indexer_fxtr_ids = [arena._internal_id_to_fxtr[iid]
                                   for iid in new_indexer_iids]

                results = await IndexComputer().compute_indexer_entries(
                    [entity_fxtr_id], indexer_fxtr_ids, arena)

                for (idx_fid, _), entries in results.items():
                    idx_iid = arena._fxtr_id_to_internal[idx_fid]
                    computed_entries[idx_iid] = entries
                    # get num_key_cols from indexer types (cached)
                    ...
                expected_reg_ids |= frozenset(r[0] for r in new_regs)
                arena.close()

            try:
                entity_iid, garbage = await pool.write(
                    pin_with_index_data, ns_id, entity_id._raw_data_id,
                    key, overwrite, computed_entries, num_key_cols,
                    expected_reg_ids)
                break
            except IndexMismatchError as e:
                indexed_regs = e.current_registrations
                continue

        if garbage and self._session._garbage_evt is not None:
            self._session._garbage_evt.set()
    finally:
        await self._session._dispose_arena_in_db(temp_arena_id)
```

### unpin_entity

```python
async def unpin_entity(self, key):
    garbage = await pool.write(unpin_with_index_cleanup, ns_id, key)
    if garbage and self._session._garbage_evt is not None:
        self._session._garbage_evt.set()
```

`unpin_with_index_cleanup` queries `ns_index_registry` internally to find all indexer tables
for the namespace. Everything is atomic in one transaction — the backend doesn't need to
pre-query registrations.

### iter_matches

```python
async def iter_matches(self, first, /, *rest, key_prefix="", where=None):
    all_constraints = [first, *rest]

    # 1. Resolve each constraint to table/column info
    resolved = []
    for c in all_constraints:
        info = await pool.read(
            resolve_indexer_for_query, ns_id, c.indexer_id._raw_data_id)
        # info: (indexer_iid, key_types, value_type)
        rewritten = parse_condition(
            c.condition, frozenset(c.key_variables), allow_value=True)
        resolved.append(ResolvedConstraint(
            table_name=f"NS_INDEX__{info[0]}",
            key_variables=c.key_variables,
            key_column_types=tuple(info[1]),
            value_column_type=info[2],
            condition=rewritten))

    # 2. Build query
    binding_sql = None
    if where:
        all_vars = set()
        for c in all_constraints:
            all_vars.update(c.key_variables)
        binding_sql = parse_condition(
            where.condition, frozenset(all_vars), allow_value=False)
    query = generate_iter_matches_query(
        resolved, binding_sql, ns_internal_id, key_prefix)

    # 3. Execute
    rows = await pool.read(execute_index_query, query.sql, query.parameters)

    # 4. Map results
    entity_iids = list(set(row[0] for row in rows))
    if not entity_iids:
        return
    arena = await SQLiteNamespaceArena._create_for_entities(
        session, namespace, entity_iids)
    for row in rows:
        entity_iid, pin_key, *var_values = row
        fxtr_id = arena._internal_id_to_fxtr[entity_iid]
        bindings = dict(zip(query.binding_variable_names, var_values))
        yield (pin_key, fxtr_id, bindings, arena)
```

## Testing Strategy

Tests go in `tests/test_store_indexing/test_backend_integration.py`.

### Fixtures

- `tmp_db_path`: Temporary SQLite database path (already exists in conftest)
- `session_and_namespace`: Sets up a `SQLiteFxtrSession` + `SQLiteNamespaceBackend`
- `sample_entities`: A few entities with known CBOR structure for indexing
- `sample_indexer`: A `TypedPathValueIndexer` that indexes the sample entities

### Test cases

**register_indexer:**
- Register on empty namespace → state INDEXED, no index rows
- Register on namespace with existing pins → all get indexed
- Register same indexer+prefix twice → error (or idempotent? TBD)
- Register with key prefix → only matching pins get indexed

**pin_entity_from_arena (with indexing):**
- Pin with no indexers → works as before (regression)
- Pin with one INDEXED indexer → index rows created
- Pin with multiple INDEXED indexers → all index rows created
- Pin with overwrite → old index rows replaced, new ones written

**unpin_entity (with indexing):**
- Unpin removes index rows from all index tables
- Unpin + GC cleans up entity if only held by pin

**unregister_indexer:**
- Unregister sets state to GARBAGE
- After GC: index data cleaned up, registration deleted
- Indexer entity GC'd after all registrations removed

**iter_matches:**
- Single constraint, single match → correct (key, id, bindings, arena)
- Single constraint, multiple matches → returns all
- Multiple constraints with shared variable → unification
- With BindingConstraint → further filtering
- With key_prefix → only matching keys
- No matches → empty generator

**GC:**
- Building session dies → BUILDING registrations become GARBAGE
- GARBAGE registration cleanup removes index data
- Indexer entity cleanup after all registrations removed

**list_indexers:**
- Lists all non-GARBAGE registrations with loadable arena

## Implementation Steps

- [x] Add `ns_index_registry` to `SQL_CREATE_TABLES` in `sqlite_ops.py`
- [x] Modify `_is_entity_orphaned_in_namespace` to check index registry
- [x] Create `sqlite_store/sqlite_index_ops.py`:
  - [x] Constants and table name helpers
  - [x] Private helpers: `_ensure_index_tables_exist`, `_write_index_rows`, `_write_processed_marker`, `_delete_index_entries_for_entity`, `_drop_index_tables_if_unused`, `_verify_indexed_registrations`
  - [x] `register_indexer_start`
  - [x] `get_unprocessed_pins`
  - [x] `write_index_entries_batch`
  - [x] `try_finish_registration`
  - [x] `set_registration_garbage`
  - [x] `IndexMismatchError` (used by modified `create_or_update_pin` in sqlite_ops.py)
  - [x] `get_indexed_registrations_for_prefix`
  - [x] `get_indexed_registrations_for_namespace` (renamed from `get_non_garbage_registrations`)
  - [x] `resolve_indexer_for_query`
  - [x] `execute_index_query`
  - [x] `mark_building_registrations_garbage_for_session`
  - [x] `gc_cleanup_one_garbage_registration`
- [x] Modify `sqlite_ops.py`:
  - [x] Modify `create_or_update_pin` with index params (replaces `pin_with_index_data`)
  - [x] Modify `unpin_and_maybe_rebind` with index cleanup (replaces `unpin_with_index_cleanup`)
  - [x] Add `mark_building_registrations_garbage_for_session` call to session death path
  - [x] Add `gc_cleanup_one_garbage_registration` call to `run_one_gc_step`
  - [x] Add GARBAGE registrations to `check_and_claim_gc` garbage detection
- [x] Add v4→v5 migration for `ns_index_registry` table
- [x] Implement `SQLiteNamespaceBackend.register_indexer`
- [x] Implement `SQLiteNamespaceBackend.unregister_indexer`
- [x] Implement `SQLiteNamespaceBackend.list_indexers`
- [x] Modify `SQLiteNamespaceBackend.pin_entity_from_arena` (CAS loop)
- [x] Implement `SQLiteNamespaceBackend.iter_matches`
- [x] Write integration tests (20 tests)
- [x] Update `sqlite_store/LOCAL_CODE_OVERVIEW.md`
- [x] Update `store_indexing/LOCAL_CODE_OVERVIEW.md`
- [x] Update `CODE_OVERVIEW.md`

# Implementation results

## Deviations from the plan

1. pin_with_index_data / unpin_with_index_cleanup replaced by modifying existing functions
    — Per your feedback, create_or_update_pin and unpin_and_maybe_rebind were modified in-place
with optional index parameters rather than creating parallel functions
2. key_types and value_type stored in registry — The plan's schema didn't include these
columns, but resolve_indexer_for_query needs them for query generation without loading the
indexer entity, so they were added to ns_index_registry
3. get_non_garbage_registrations renamed — To get_indexed_registrations_for_namespace since
per your feedback, only INDEXED registrations are returned

## Things worth noting

- The GC cleanup for GARBAGE registrations handles the complex case where the same indexer
has both GARBAGE and live registrations at different prefixes — it only deletes index rows
for entities not covered by any live prefix
- The IndexerConstraintByID class was missing its @dataclasses.dataclass decorator — fixed
as a drive-by
- Pyright shows import resolution errors for store_indexing submodules when editing
sqlite_store files, but all tests pass fine — likely a Pyright config/environment issue
