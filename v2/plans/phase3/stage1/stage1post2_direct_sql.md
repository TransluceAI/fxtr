# Stage 1 Post 2: Direct SQL Queries on Index Views

## Goal

Expose a lower-level `sql()` method on `FxtrQueryableNamespace` that lets users run
arbitrary analytical SQL queries against their indexed data. Users define named "views"
(each composed of one or more indexer constraints joined on shared variables), and then
write free-form SQL referencing those views as tables.

This builds on the namespace indexing system from stage 1. The index tables already exist
in the SQLite store; this feature exposes them to the user via read-only ad-hoc queries.

## User-Facing API

```python
ns: FxtrQueryableNamespace

rows = await ns.sql(
    """
    SELECT turns.m, turns.turn_source, AVG(turn_metric_score)
    FROM turns
    JOIN metric_info ON metric_info.pin_name = ('metrics/' || turns.m)
    GROUP BY turns.m, turns.turn_source
    """,
    views=dict(
        turns=QueryView(
            "turns/",
            turn_metric_score=TypedPathValueIndexer.query(
                "!.turns[$t: int].metrics[$m: str].score: float"
            ),
            turn_source=TypedPathValueIndexer.query(
                "!.turns[$t: int].source: str"
            ),
        ),
        metric_info=QueryView(
            "metrics/",
            metric_desc=TypedPathValueIndexer.query("!.description: str"),
        ),
    ),
    dialect="sqlite",
)
# rows is list[sqlite3.Row]
```

## Concepts

### QueryView

A `QueryView` groups a prefix with one or more named indexer constraints that together
define the columns of a virtual table. Each QueryView produces a CTE in the generated SQL.

**Columns of a view:**

| Column type | Source | Examples |
|-------------|--------|----------|
| Binding variables | Union of all `key_variables` across constraints | `t`, `m` |
| Value columns | One per constraint, named by the kwarg | `turn_metric_score`, `turn_source` |
| `pin_name` | `namespace_pin.name` (always present) | `"turns/run_001"` |
| `entity_id` | `entity.public_id` BLOB (always present, LEFT JOIN) | raw FxtrID bytes |

**Validation (at QueryView construction time):**

- No duplicate names across the three column types (binding vars, value columns, reserved)
- Reserved names (`pin_name`, `entity_id`) cannot be used as value column names
- Binding variable names (from the `$var` names in each IndexerConstraint) cannot collide
  with value column names or reserved names
- At least one constraint is required

### Query Execution Model

Each `sql()` call:

1. Creates a fresh `sqlite3.connect(':memory:')` connection in a standalone thread
   (not from the internal pool)
2. ATTACHes the namespace's SQLite store read-only
   (`ATTACH DATABASE 'file:...?mode=ro' AS _fxtr_internal_store`)
3. Generates CTEs for each view and prepends them to the user's query
4. Executes the full query, fetches all rows
5. Closes the connection and returns `list[sqlite3.Row]`

This is safe because:
- The connection is read-only (ATTACH with `mode=ro`)
- The connection is ephemeral (new per call, closed after)
- No interference with the internal connection pool or transactions

## SQL Generation

### Per-View CTE Structure

For a view named `turns` with constraints `turn_metric_score` (vars: `t`, `m`) and
`turn_source` (vars: `t`), prefix `"turns/"`:

```sql
turns AS (
  SELECT
    ix0._VAR_t AS t,
    ix0._VAR_m AS m,
    ix0.value AS turn_metric_score,
    ix1.value AS turn_source,
    p.name AS pin_name,
    e.public_id AS entity_id
  FROM _fxtr_internal_store.namespace_pin p
  JOIN (SELECT * FROM (
    SELECT namespace_id, entity_internal_id,
           IXKEY_0 AS _VAR_t, IXKEY_1 AS _VAR_m, VALUE AS value
    FROM _fxtr_internal_store.NS_INDEX__42
  ) WHERE TRUE) ix0
    ON ix0.entity_internal_id = p.entity_id AND ix0.namespace_id = ?
  JOIN (SELECT * FROM (
    SELECT namespace_id, entity_internal_id,
           IXKEY_0 AS _VAR_t, VALUE AS value
    FROM _fxtr_internal_store.NS_INDEX__43
  ) WHERE TRUE) ix1
    ON ix1.entity_internal_id = p.entity_id AND ix1.namespace_id = ?
    AND ix1._VAR_t = ix0._VAR_t
  LEFT JOIN _fxtr_internal_store.entity e ON e.id = p.entity_id
  WHERE p.namespace_id = ? AND p.name LIKE ? || '%'
)
```

Key structural choices:
- **INNER JOIN on `namespace_pin`** to enforce prefix filtering (same as existing query)
- **Variable unification** via equijoins on shared `_VAR_*` columns (same as existing)
- **LEFT JOIN on `entity`** to get `public_id` — admits SQLite's "Omit OUTER JOIN
  Optimization" so the join is elided if the user doesn't reference `entity_id`
- **Per-constraint conditions** applied inside each subquery's WHERE (from `IndexerConstraint.condition`)
- **Table names qualified** with ATTACH alias `_fxtr_internal_store.`
- **Value columns renamed** from `VALUE`/`value` to the user's kwarg name in the outer SELECT

### Full Query Assembly

```sql
WITH
  <view1_name> AS (...),
  <view2_name> AS (...)
<user_query>
```

The user's query is substituted verbatim after the WITH clause. We parse it with sqlglot
as a basic well-formedness check, but don't rewrite or escape it — it runs in a read-only
isolated connection anyway.

## Implementation Plan

### Part 1: QueryView type and validation

**File: `store_indexing/indexing_types.py`**

Add `QueryView` dataclass:

```python
@dataclasses.dataclass(frozen=True)
class QueryView:
    prefix: str
    columns: dict[str, IndexerConstraint]  # name -> constraint

    def __init__(self, prefix: str = "", /, **columns: IndexerConstraint): ...
```

Construction validates:
- At least one column
- No reserved names (`pin_name`, `entity_id`) used as column names
- Collect all binding variable names from all constraints' `key_variables`
- No collision between binding variable names and column names
- No collision between binding variable names and reserved names

- [x] Add `QueryView` class to `indexing_types.py`
- [x] Add tests for `QueryView` validation (name collisions, reserved names, empty columns)

### Part 2: CTE generation

**File: `store_indexing/query_gen.py`**

Add a new function `generate_view_cte()` alongside the existing `generate_iter_matches_query()`.
These are closely related: both join index tables on shared variables via `namespace_pin`.
Add cross-references in docstrings so readers understand the relationship.

New types:

```python
@dataclasses.dataclass(frozen=True)
class ResolvedViewColumn:
    """A single column in a view, resolved to its SQL table info."""
    column_name: str          # user-chosen name (the kwarg key)
    table_name: str           # e.g., "NS_INDEX__42" (unqualified)
    key_variables: tuple[str, ...]
    key_column_types: tuple[IndexerColumnType, ...]
    value_column_type: IndexerColumnType
    condition: str            # rewritten SQL from parse_condition

@dataclasses.dataclass(frozen=True)
class ResolvedView:
    """A QueryView resolved to SQL table info."""
    view_name: str
    prefix: str
    columns: list[ResolvedViewColumn]

@dataclasses.dataclass(frozen=True)
class GeneratedDirectQuery:
    """Result of direct SQL query generation."""
    sql: str                  # WITH ... <user_query>
    parameters: list[int | str]
```

Function signature:

```python
def generate_direct_sql_query(
    views: list[ResolvedView],
    user_query: str,
    namespace_internal_id: int,
    db_alias: str = "_fxtr_internal_store",
) -> GeneratedDirectQuery:
```

The function:
1. For each view, generates a CTE following the structure described above
2. Joins constraints within each view using the same variable-unification logic
   as `generate_iter_matches_query` (shared vars → equijoin)
3. Renames `_VAR_*` to bare names and `value` to column names in the outer SELECT
4. Adds LEFT JOIN to `{db_alias}.entity` for `entity_id`
5. Concatenates: `WITH <cte1>, <cte2>, ... <user_query>`

- [x] Add `ResolvedViewColumn`, `ResolvedView`, `GeneratedDirectQuery` types
- [x] Implement `generate_view_cte()` (generates one CTE for one view)
- [x] Implement `generate_direct_sql_query()` (assembles full WITH + user query)
- [x] Add unit tests for CTE generation (single view, multiple views, variable
      unification across constraints, prefix filtering, LEFT JOIN structure)

### Part 3: Backend execution method

**File: `store_indexing/indexing_types.py`**

Add `ParsedViewColumn` and `ParsedView` intermediate types (handoff between user-facing
layer and backend), and a single abstract method on `IndexablePinningBackend`:

```python
async def run_direct_sql(
    self,
    query: str,
    views: list[ParsedView],
) -> list[sqlite3.Row]: ...
```

This replaces three earlier methods (`resolve_indexer_table_info`,
`_get_namespace_internal_id`, `execute_direct_sql`) with a single method that
encapsulates table resolution, query generation, and execution.

**File: `sqlite_store/sqlite_namespace.py`**

Implement `run_direct_sql` on `SQLiteNamespaceBackend`:
1. Resolve each view's `ParsedViewColumn` indexer IDs to table names via `resolve_indexer_for_query`
2. Build `ResolvedView`/`ResolvedViewColumn` (internal to query_gen)
3. Call `generate_direct_sql_query()` to produce the full SQL
4. Execute in a fresh `:memory:` connection with the store ATTACHed read-only

**File: `git_store/git_namespace.py`**

Implement on `GitNamespaceBackend` by syncing indexes then delegating to
`self._worktree_ns.run_direct_sql()`.

- [x] Add `ParsedViewColumn`, `ParsedView` types to `indexing_types.py`
- [x] Add `run_direct_sql` abstract method to `IndexablePinningBackend`
- [x] Implement on `SQLiteNamespaceBackend` (resolve + generate + execute)
- [x] Implement on `GitNamespaceBackend` (sync + delegate)
- [x] Test via integration tests

### Part 4: User-facing `sql()` method

**File: `fxtr_context_api.py`**

Add `sql()` method to `FxtrQueryableNamespace`:

```python
async def sql(
    self,
    query: str,
    *,
    views: dict[str, QueryView],
    dialect: str = "sqlite",
) -> list[sqlite3.Row]:
```

The method:
1. Validates `dialect == "sqlite"` (error otherwise, future extensibility)
2. Parses `query` with sqlglot as a basic well-formedness check
3. For each view, for each constraint:
   a. Resolve the indexer entity to a FxtrID (same as `iter_matches` does)
   b. Rewrite condition via `parse_condition()`
   c. Build `ParsedViewColumn`
4. Build list of `ParsedView`s
5. Call `self._get_backend().run_direct_sql(query, parsed_views)`

The user-facing layer handles entity resolution (scope-dependent) and condition
rewriting (security-sensitive). Table resolution, query generation, and execution
are encapsulated in the backend's `run_direct_sql()`.

- [x] Implement `sql()` on `FxtrQueryableNamespace`
- [x] Add integration tests:
  - Basic single-view query
  - Multi-view query with JOIN
  - Aggregation (GROUP BY, AVG, COUNT)
  - Prefix filtering across views
  - `entity_id` column usage
  - Error: unregistered indexer
  - Error: unsupported dialect

### Part 5: Lightweight sqlglot validation (optional)

Add a helper in `sql_expr.py` that parses a user query string with sqlglot to check
basic well-formedness (is it a valid SELECT statement?). This is purely for better error
messages — the query would fail in SQLite anyway. We should NOT try to rewrite or validate
column references since the user's query can reference CTEs, use subqueries, etc.

- [x] Add `validate_user_query(query: str)` helper to `sql_expr.py`
- [x] Call it from `FxtrQueryableNamespace.sql()` before generating the full query

### Part 6: Documentation

- [x] Update `CODE_OVERVIEW.md`
- [x] Update `store_indexing/LOCAL_CODE_OVERVIEW.md`

## Design Decisions

1. **New function, not refactored**: `generate_view_cte` is a new function alongside
   `generate_iter_matches_query` rather than a refactor of it. They share structural
   patterns (variable unification, subquery joining) but differ enough (table qualification,
   value renaming, entity LEFT JOIN, CTE wrapping) that sharing code would be awkward.
   Cross-references in docstrings connect them.

2. **entity_id is a BLOB**: Exposed as raw FxtrID bytes (`entity.public_id`), not a
   human-readable string. The bytes↔string conversion is only implemented in Python,
   not SQL. This matches how IDs are stored internally.

3. **LEFT JOIN for entity_id**: The `entity` table join is a LEFT JOIN specifically to
   admit SQLite's "Omit OUTER JOIN Optimization" — if the user's query doesn't reference
   `entity_id`, the join is elided entirely by the query planner.

4. **INNER JOIN for namespace_pin**: We always INNER JOIN with `namespace_pin` because we
   need it for prefix filtering and to get `pin_name`. This is the same as the existing
   `generate_iter_matches_query`.

5. **Standalone connection per call**: Each `sql()` call gets its own `:memory:` connection
   with the store ATTACHed read-only. No interference with the internal pool, no risk of
   blocking writes, complete isolation.

6. **No result type conversion**: Returns raw `sqlite3.Row` objects. No FxtrID resolution,
   no arena management, no scope interaction. The user gets the same power as if they'd
   opened a `sqlite3` connection themselves.

7. **Conditions applied inside CTEs**: Per-constraint conditions from `IndexerConstraint`
   (e.g., the `where` parameter of `TypedPathValueIndexer.query()`) are applied inside
   each constraint's subquery within the CTE. This means pre-filtering happens before the
   user's query runs.

8. **QueryView validates at construction**: Name collision checks happen eagerly when
   `QueryView` is constructed, giving clear errors before any database work begins.
