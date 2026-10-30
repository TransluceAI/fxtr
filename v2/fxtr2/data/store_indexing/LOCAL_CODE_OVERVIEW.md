# store_indexing Module

Namespace index construction, querying, and type definitions for efficient querying of pinned entities by field values.

## Files

### indexing_types.py
Core type definitions and abstract interfaces:

- **`IndexerColumnType`** enum: INT, STR, BOOL, BYTES, FLOAT, FXTR_ID
- **`IndexerColumnValue`** type alias: `int | str | bool | bytes | float | FxtrID`
- **`Indexer`** abstract base class (extends `Entity`): Defines the interface for extracting (key_tuple, value) pairs from entities. Methods: `index_key_types()`, `index_value_type()`, `unpack_entity(entity_id, scope)`, `bind()`. `unpack_entity` takes a `FxtrID` and loads frozen CBOR internally via `scope.load_frozen_cbor()`.
  - `bind()` eagerly validates conditions via `parse_condition` and rejects duplicate variable names
- **`IndexerConstraint`**: Frozen dataclass binding an Indexer to variable names and a condition string
- **`IndexerConstraintByID`**: Frozen dataclass referencing an indexer by `FxtrID` (used by the backend's `iter_matches`)
- **`BindingConstraint`**: Frozen dataclass for global conditions on binding variables (validated at query time, not at construction)
- **`ParsedViewColumn`**: A column parsed by the user-facing layer (column name, indexer FxtrID, key variables, rewritten condition) — not yet resolved to table info
- **`ParsedView`**: A view parsed by the user-facing layer (view name, prefix, list of `ParsedViewColumn`s) — passed to `IndexablePinningBackend.run_direct_sql()`
- **`QueryView`**: View definition for `sql()`. Groups a key prefix with named `IndexerConstraint` columns. Validates no name collisions between binding variables, column names, and reserved names (`pin_name`, `entity_id`).
- **`IndexablePinningBackend`**: Abstract interface extending `PersistentPinningBackend` with `register_indexer`, `unregister_indexer`, `list_indexers`, `iter_matches`, `run_direct_sql`. Implemented by `SQLiteNamespaceBackend` and `TsvNamespaceBackend`.
- **`NamespaceIndexMetadata`** (`DataclassEntity`): Entity type for export/import of indexer registrations. Contains `indexer_ids: tuple[ChildFxtrID, ...]` (strong links keeping indexer entities alive in archives) and parallel `key_prefixes: tuple[str, ...]`.

### indexers.py
Path element types, concrete indexer implementations, and unpack logic:

- **Path element hierarchy** (`PathElement` base, mashumaro discriminated union on `_k` field):
  - **`PathKey`** (`_k="k"`): Dict key lookup
  - **`PathIndex`** (`_k="i"`): Fixed list index lookup
  - **`VarMarker`** (`_k="v"`): Fan-out variable over dict keys or list indices
  - **`DerefMarker`** (`_k="d"`): Dereference a FxtrID reference
  - **`TypedDerefMarker`** (`_k="td"`): Dereference + type check (with its own `strict` flag for type filtering)
- **`TypedPathValueIndexer`** (`DataclassEntity`): Extracts primitive values at typed paths. Key fields:
  - `initial_lookup`: `DerefMarker` (no type filter) or `TypedDerefMarker` (type filter with strict/lax)
  - `strict`: Schema strictness (missing keys, bad indices, value type mismatches)
  - `path`: Tuple of `PathElement`s defining the extraction path
  - `var_types`, `value_type`: Column type metadata
  - Class methods: `parse(expression)` and `query(expression, where)` for string-based construction
- **`TypedPathExistsIndexer`** (`DataclassEntity`): Like above but always produces `True` for valid paths (no value type)
- **`WellKnownBuiltinIndexer`**: Type alias `TypedPathValueIndexer | TypedPathExistsIndexer`
- **Unpack helpers**: `_walk_path()` async recursive path walker, `_check_initial_lookup()`, `_check_value_type()` for value coercion (e.g., int→float)

**Two-axis strictness model:**
1. Indexer-level `strict`: Controls schema errors (missing keys, bad indices, var/value type mismatches)
2. TypedDerefMarker `strict`: Controls type filtering errors (entity type not in supported_types)

These are independent — an indexer can be strict about schema but lax about type filtering, or vice versa.

### path_parse.py
Hand-written character-by-character parser for path expressions:

- **`parse_path_expression(expr, named_vars, require_value_type)`**: Parses expressions like `"!(as '!my.type').turns[*: int].score: float"` into a `ParsedPathExpression`. Expressions always start with `!` (initial deref), optionally with `(as ...)` (strict type filter) or `(as? ...)` (lax type filter). Variables are `*` (unnamed, parse mode) or `$name` (named, query mode).
- **`ParsedPathExpression`**: Result dataclass with `initial_lookup`, `path`, `var_types`, `var_names`, `value_type`
- Uses Python's `tokenize` module to parse quoted FxtrID string literals in `!(as ...)` expressions

### sql_expr.py
SQL expression parsing, validation, and rewriting:

- **`parse_condition(condition, allowed_variables, allow_value=True)`**: Parses a user-provided SQL condition string using sqlglot, validates it (rejects statements, subqueries, disallowed functions, unknown columns), and rewrites `$var` references to `_VAR_var` column names. Returns rewritten SQL string. Raises `ValueError` on validation failure.
- **`SAFE_FUNCTIONS`**: Frozenset of ~90 safe SQLite function names (both sqlglot class names and anonymous function names)
- **`COLUMN_TYPE_TO_SQLITE`**: Maps `IndexerColumnType` to SQLite type strings (e.g. `INT → "INTEGER"`)
- **`validate_user_query(query)`**: Lightweight syntax check for user-provided SQL queries (used by `sql()`). Only verifies parseability, not semantic correctness.

### query_gen.py
SQL query generation from resolved constraints. Two generators:

- **`generate_iter_matches_query`**: For `iter_matches` — flat query returning `(entity_internal_id, pin_key, var_0, ...)`.
- **`generate_direct_sql_query`**: For `sql()` — `WITH <cte1>, <cte2>, ... <user_query>` where each CTE is a named view.

Types:
- **`ResolvedConstraint`**: Frozen dataclass holding a constraint's table name, variable bindings, column types, and rewritten condition (used by `generate_iter_matches_query`)
- **`GeneratedQuery`**: Result of iter_matches query generation — SQL string, parameter list, and binding variable name ordering
- **`ResolvedViewColumn`**: A single named column within a view, resolved to SQL table info (used by `generate_direct_sql_query`)
- **`ResolvedView`**: A QueryView resolved to SQL table info
- **`GeneratedDirectQuery`**: Result of direct SQL query generation — full SQL string and parameters

Functions:
- **`generate_iter_matches_query(constraints, binding_constraint, namespace_internal_id, key_prefix)`**: Generates a parameterized SQL query that joins index tables, unifies shared variables via equijoins, applies per-constraint conditions inside subqueries, and optionally wraps with a binding constraint. Returns `GeneratedQuery`.
- **`generate_direct_sql_query(views, user_query, namespace_internal_id, db_alias)`**: Generates a `WITH ... <user_query>` statement from resolved views. Each view becomes a CTE joining index tables with user-friendly column names, a LEFT JOIN on entity for `entity_id`, and table names qualified with the ATTACH db alias.
- **`_generate_view_cte(view, namespace_internal_id, db_alias)`**: Internal helper generating one CTE for one view.

### index_computer.py
Bridge between the store (FxtrIDs + SQL) and indexer logic (loaded entities + scopes):

- **`IndexComputer`**: Async context manager that caches loaded indexer objects across calls in a persistent isolated scope. Tied to `SQLiteFxtrSession` lifetime. Callers pass **`Lease`** handles for each arena; the computer consumes them (or clones as needed) and mounts external indexer/entity data into per-call child scopes via `MemoryArena.lazily_attach_from_arena`. Two methods:
  - `get_indexer_types(indexer_id, arena=None, arena_lease=None)`: Return column schema (key types + value type). Uses cached indexer if available, otherwise loads from arena (lease required when loading).
  - `compute_indexer_entries(entities, indexers, entity_arena, indexer_arena=None, entity_lease=..., indexer_lease=...)`: Run all indexers on all entities. Takes separate arenas for entities and new (uncached) indexers. Returns dict mapping `(indexer_id, entity_id)` to `(key_tuple, value)` pairs. Provided arenas are not modified.
- **Scope management**: The persistent scope holds cached indexer objects. `compute_indexer_entries` activates the persistent scope, creates a single child scope for the entire call, attaches entities from the entity arena, and runs all indexers. This avoids per-pin scope/indexer re-creation overhead.
- **Empty results**: If an indexer returns `[]` for an entity (lax mode, non-matching type), the key `(iid, eid)` is still present in the result dict with an empty list.

### __init__.py
Package init.

## Design Notes

- **Condition placement**: Per-constraint conditions are pushed inside their index table subqueries using a double-wrap pattern (`SELECT * FROM (SELECT ... aliases) WHERE condition`). This means condition strings use `_VAR_` names and `value` directly without table aliases. Binding constraints go in an outermost wrapper where projected `_VAR_` names are unambiguous.
- **Variable unification**: Shared variables across constraints are unified via equijoin conditions in the JOIN ON clause. The canonical source for each variable is the first constraint that defines it.
- **Safety**: `parse_condition` validates against a curated allowlist of SQLite functions and rejects statement injection, subqueries, and unauthorized column references.
- **PathElement serialization**: Uses mashumaro `DataClassDictMixin` with a `_k` field discriminator (separate from `DataclassEntity`'s `$fxtr.type` discriminator). The `initial_lookup` field on indexers is typed as `PathElement` (not `DerefMarker | TypedDerefMarker`) to leverage the discriminator for deserialization, with runtime validation in `__post_init__`.
