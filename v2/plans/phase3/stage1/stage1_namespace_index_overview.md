# Stage 1: Namespace Indexing - Overview

## Goal

Add indexing to namespaces so that users can efficiently query pinned entities by their field
values. Currently namespaces hold `str -> Entity` mappings (pins). Indexing adds the reverse
direction: given constraints on entity fields, find matching (key, entity) pairs.

Queries use datalog-style variable unification of path traversals, e.g.:
"where `turns[$t].metrics[$m].name == 'foo'` and `turns[$t].metrics[$m].score >= 3`, return
`turns[$t].content`"

## Concepts

### Indexers

An **indexer** is an immutable fxtr entity that defines how to extract (key_tuple, value) pairs
from an entity. Conceptually an indexer defines a function:

```
f(entity, $var1, $var2, ...) -> value
```

For example, `TypedPathValueIndexer` with path `turns[*].metrics[*].score` defines:

```
f(entity, $t, $m) = entity["turns"][$t]["metrics"][$m]["score"]
```

Calling `unpack_entity` on a concrete entity enumerates all valid bindings:

```
((0, "accuracy"), 0.95)
((0, "f1"),       0.87)
((1, "accuracy"), 0.91)
...
```

Two built-in indexer types:

- **TypedPathValueIndexer**: Extracts a primitive value at a path. Path may include dict key
  lookups, list index variables, FxtrID dereferences, and type checks.
- **TypedPathExistsIndexer**: Like the above but doesn't require a primitive at the end.
  Always produces value `True` for every valid path. Useful for type/presence checks.

Both are `DataclassEntity` subclasses and are stored as regular fxtr entities in the store.
The backend only supports these two well-known types (`WellKnownBuiltinIndexer`).

### Index Registration

Indexers are attached to a namespace at a specific **key prefix**:

- Prefix `"turns/"` → only entities pinned under keys starting with `"turns/"` are indexed
- Empty prefix `""` → all entities in the namespace are indexed

Each `(indexer, prefix)` pair is a registration. Registrations have states:

| State    | Meaning |
|----------|---------|
| BUILDING | Index is being constructed. Not yet queryable. Tracks the building session. |
| INDEXED  | Index is complete and queryable. |
| GARBAGE  | Unregistered. Awaiting cleanup. |

### Queries

Users construct `IndexerConstraint` objects:

```python
IndexerConstraint(
    indexer=my_indexer,            # the Indexer entity
    key_variables=("t", "m"),     # bind key columns to named variables
    condition="value > 3",        # SQLite expression filtering on value + vars
)
```

Multiple constraints are joined on shared variable names (unification). An optional
`BindingConstraint` adds a global condition on the joined result.

Convenience: `TypedPathValueIndexer.query(types, "turns[$t: int].metrics[$m: str].score: float", "value > 3")` parses the expression and creates an `IndexerConstraint` in one step.

### SQL Expression Safety

User-provided condition strings are parsed by `sqlglot`:
- `$varname` → `_VAR_varname`
- Column references validated against in-scope variables
- Only `value` and declared binding variables are allowed

## Data Model

### Index Registration Table

```sql
CREATE TABLE ns_index_registry (
    namespace_id     INTEGER NOT NULL REFERENCES namespace(id),
    indexer_entity_id INTEGER NOT NULL REFERENCES entity(id),
    key_prefix       VARCHAR NOT NULL,
    state            VARCHAR NOT NULL,  -- 'BUILDING', 'INDEXED', 'GARBAGE'
    building_session_id INTEGER REFERENCES session(id),
    UNIQUE(namespace_id, indexer_entity_id, key_prefix)
);
```

### Per-Indexer Index Table

One table per indexer (across all namespaces that use it):

```sql
CREATE TABLE NS_INDEX__{INDEXER_INTERNAL_ID} (
    namespace_id       INTEGER NOT NULL,
    entity_internal_id INTEGER NOT NULL,
    IXKEY_0            <sqlite_type>,
    IXKEY_1            <sqlite_type>,
    ...
    VALUE              <sqlite_type>,
    PRIMARY KEY (namespace_id, entity_internal_id, IXKEY_0, IXKEY_1, ...)
);
CREATE INDEX ix_NS_INDEX__{ID}_value ON NS_INDEX__{ID}(VALUE);
```

Column types are determined by the indexer's `index_key_types()` and `index_value_type()`.

### Per-Indexer Processed Table

Tracks which entities have been indexed (needed because an entity might produce zero rows):

```sql
CREATE TABLE NS_INDEX_PROCESSED__{INDEXER_INTERNAL_ID} (
    namespace_id       INTEGER NOT NULL,
    entity_internal_id INTEGER NOT NULL,
    PRIMARY KEY (namespace_id, entity_internal_id)
);
```

`NS_INDEX__` and `NS_INDEX_PROCESSED__` must always be updated in the same transaction.

## Key Operations

### register_indexer

Backend signature: `register_indexer(indexer_id: FxtrID, source: ReadableArena, key_prefix: str)`

1. Copy the indexer entity into the store (reusing existing copy flow)
2. Insert registration row: state=BUILDING, building_session_id=current
3. Create `NS_INDEX__` and `NS_INDEX_PROCESSED__` tables if they don't exist
4. **Build loop**:
   a. Fetch all pinned entities matching the prefix that aren't in `NS_INDEX_PROCESSED__`
   b. For each entity: run the indexer (`unpack_entity`), write results + processed marker
   c. Check if all pins are now processed
   d. If yes → flip state to INDEXED. If no → loop again (new pins appeared mid-build)
5. Return

### unregister_indexer

Backend signature: `unregister_indexer(indexer_id: FxtrID, key_prefix: str)`

The unregister operation itself is lightweight — it just marks the registration for cleanup:

1. Set the registration state to GARBAGE (single transaction)
2. Return immediately — index data is **not** deleted inline

The actual cleanup is deferred to GC (see "GC Changes" below). This keeps unregistration fast
and avoids holding a long transaction to delete potentially many index rows.

Note: while a registration is in GARBAGE state, the index data still exists in the
`NS_INDEX__` and `NS_INDEX_PROCESSED__` tables. This is harmless — GARBAGE registrations are
excluded from queries (`iter_matches` only considers INDEXED registrations), and the data will
be cleaned up eventually.

**What GC does for GARBAGE registrations:**

1. For each GARBAGE registration in a namespace:
   a. Find all `(namespace_id, entity_internal_id)` pairs in `NS_INDEX_PROCESSED__` for this
      indexer where the entity does NOT match any *live* (INDEXED or BUILDING) registration's
      key prefix in the same namespace for the same indexer
   b. Bulk delete those rows from `NS_INDEX__` and `NS_INDEX_PROCESSED__`
   c. Delete the GARBAGE registration entry from `ns_index_registry`
2. If no other registrations (in any namespace) reference the same indexer entity:
   a. The `NS_INDEX__` and `NS_INDEX_PROCESSED__` tables can be dropped entirely
   b. The indexer entity is no longer a namespace root and becomes eligible for entity GC

**Edge case — re-registration:** If the same indexer is re-registered at the same prefix before
GC runs, the new registration goes through the normal BUILDING flow. The existing index data
from the GARBAGE registration may still be present, but the build loop will skip entities
already in `NS_INDEX_PROCESSED__`, so it efficiently picks up where it left off. The GARBAGE
entry can then be cleaned up without deleting any rows (since they now belong to the new
registration).

### pin_entity_from_arena (modified)

Uses a **compare-and-swap** pattern to atomically pin + index:

1. Fetch all INDEXED registrations matching the key prefix
2. For each, run the indexer on the new entity (on the Python side)
3. In a single transaction:
   - Insert the pin
   - Insert all index rows into `NS_INDEX__` and `NS_INDEX_PROCESSED__`
   - Verify the set of INDEXED registrations hasn't changed since step 1
4. If verification fails: fetch the newly-appeared registrations, compute their index results,
   and retry from step 3
5. Commit when everything is consistent

### unpin_entity (modified)

In the same transaction as removing the pin:
- Remove all `NS_INDEX__` rows for this (namespace, entity) across all index tables
- Remove all `NS_INDEX_PROCESSED__` rows for this (namespace, entity)

### iter_matches

1. For each `IndexerConstraint`, look up the indexer's registration and table name
2. Validate and rewrite SQL condition expressions via sqlglot
3. Generate a SQL query:
   - Each constraint becomes a subquery on its `NS_INDEX__` table
   - Subqueries are JOINed on shared binding variables
   - The `BindingConstraint` (if any) becomes a global WHERE clause
   - Result includes entity internal ID, pin key, and all binding variable values
4. Execute query
5. Yield `(key, FxtrID, bindings, arena)` tuples

### GC Changes

- Indexer entities are treated as namespace roots (like pins) for ownership purposes: if an
  entity is the `indexer_entity_id` in any non-GARBAGE registration, it stays alive
- When a building session dies, its BUILDING registrations are set to GARBAGE
- GARBAGE cleanup: delete index rows for entities not matching any live prefix, then delete
  the GARBAGE registration entries

## Scope Management (IndexComputer)

Running an indexer requires careful scope management:

- An **isolated scope per indexer** holds the loaded indexer entity and anything it references
  (e.g., type IDs in `supported_types`)
- A **fresh isolated scope per entity** being indexed provides the context for `unpack_entity`
  to load child entities (for DerefMarker traversal)
- This is encapsulated in an **IndexComputer** helper that provides a clean API to the backend,
  hiding the scope lifecycle details

## API Layers

### Backend (IndexablePinningBackend → SQLiteNamespaceBackend)

`IndexablePinningBackend` extends `PersistentPinningBackend` and becomes the base class of
`SQLiteNamespaceBackend`. Backend methods take FxtrID + ReadableArena for indexers:

```python
class IndexablePinningBackend(PersistentPinningBackend):
    async def register_indexer(self, indexer_id: FxtrID, source: ReadableArena, key_prefix: str = "") -> None
    async def unregister_indexer(self, indexer_id: FxtrID, key_prefix: str = "") -> None
    async def list_indexers(self) -> tuple[list[tuple[FxtrID, str]], ReadableArena]
        # Returns (indexer_id, prefix) pairs plus an arena containing all the indexer entities.
        # Only INDEXED registrations are returned (BUILDING/GARBAGE are internal details).
        # Since there are typically few indexers, eagerly loading them into an arena is fine
        # (unlike list_pins which can have many entries).
    async def iter_matches(self, first, /, *rest, key_prefix="", where=None) -> AsyncGenerator[...]
```

### User-facing (FxtrQueryableNamespace)

Extends `FxtrNamespace`. Takes `Indexer` objects, stores them, passes FxtrID to backend:

```python
class FxtrQueryableNamespace(FxtrNamespace):
    async def register_indexer(self, indexer: Indexer, key_prefix: str = "") -> None
    async def unregister_indexer(self, indexer: Indexer, key_prefix: str = "") -> None
    async def list_indexers(
        self,
        into_scope: FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> list[tuple[EntityRef[WellKnownBuiltinIndexer], str]]
        # Returns (indexer_ref, prefix). Only INDEXED registrations are returned.
        # Indexer entities are attached to the namespace scope (or copied into the
        # target scope if copy_into_scope=True), following the same pattern as retrieve().
    async def iter_matches(self, first, /, *rest, ...) -> AsyncGenerator[...]
```

`SQLiteFxtrNamespace` uses `FxtrQueryableNamespace` instead of `FxtrNamespace`.

## Implementation Stages

### Part 1: Self-contained helpers

These are independent modules that can be built and tested in isolation:

| Part | Module | Description |
|------|--------|-------------|
| 1a | SQL expression parsing | sqlglot-based parsing, `$var` rewriting, column validation |
| 1b | Indexer path parsing | `parse()` and `query()` for TypedPathValue/ExistsIndexer |
| 1c | Indexer serialization | Custom codec for path element union type, roundtrip tests |
| 1d | Indexer unpack logic | `unpack_entity` implementations (path walking, deref, type checks) |
| 1e | SQL query generation | IndexerConstraints → joined SQL query with conditions |
| 1f | IndexComputer helper | Scope management for safely loading/running indexers |

### Part 2: SQLiteNamespaceBackend integration

| Part | Description |
|------|-------------|
| 2a | SQL table management: registration table schema, dynamic index table DDL |
| 2b | `register_indexer`: building loop with state machine |
| 2c | Modified `pin_entity_from_arena`: compare-and-swap indexing |
| 2d | Modified `unpin_entity` + GC changes |
| 2e | `iter_matches` implementation |

### Part 3: User-facing API

| Part | Description |
|------|-------------|
| 3a | `FxtrQueryableNamespace` class |
| 3b | `SQLiteFxtrNamespace` updates |
| 3c | Update `CODE_OVERVIEW.md` |

## Key Design Decisions

1. **Indexers are entities**: Content-addressed, immutable, stored in the fxtr store. The
   backend only supports well-known types to keep indexing logic implementation-owned.

2. **Compare-and-swap for consistency**: Both registration and pinning use optimistic
   concurrency - do work on the Python side, then atomically commit if invariants hold,
   else retry. Avoids long-held transactions.

3. **Per-indexer tables**: Dynamic DDL creates tables with proper column types and indices,
   enabling SQLite to optimize queries naturally.

4. **Backend takes FxtrID, user-facing takes Indexer**: Clean separation of concerns.
   Backend doesn't need to know about Indexer class hierarchy.

5. **Building session tracking**: If a session dies mid-build, GC can detect and clean up
   the orphaned BUILDING state rather than leaving the index stuck.

6. **Isolated scopes for indexing**: IndexComputer manages scope lifecycle to prevent
   entity leaks between indexing operations.
