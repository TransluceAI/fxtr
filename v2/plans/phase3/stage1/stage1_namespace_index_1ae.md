# Stage 1ae: SQL Expression Parsing & Query Generation

## Overview

Implements parts 1a (SQL expression parsing/validation) and 1e (query generation) of the
namespace indexing feature. 1a parses user condition strings into validated, rewritten SQL
strings. 1e assembles those into full SQL queries.

Reference: prior art at `sqlite_cond_parsing/cond_parser.py` (external experiment).

## New files

- `fxtr2/data/store_indexing/sql_expr.py` — expression parsing, validation, safe function list
- `fxtr2/data/store_indexing/query_gen.py` — query generation from resolved constraints
- `fxtr2/data/store_indexing/__init__.py` — package init
- `tests/test_store_indexing/__init__.py`
- `tests/test_store_indexing/test_sql_expr.py`
- `tests/test_store_indexing/test_query_gen.py`
- `fxtr2/data/store_indexing/LOCAL_CODE_OVERVIEW.md` — module docs

## New dependency

`sqlglot` — add via `uv add sqlglot`.

---

## Part 1a: SQL Expression Parsing

### Module: `sql_expr.py`

#### Core function

```python
def parse_condition(
    condition: str,
    allowed_variables: frozenset[str],
    *,
    allow_value: bool = True,
) -> str:
    """Parse, validate, and rewrite a SQL condition expression.

    Steps:
    1. Parse with sqlglot (SQLite dialect)
    2. Reject multiple statements
    3. Reject statement types (SELECT, INSERT, ...) and subqueries in AST
    4. Validate function calls against safe function allowlist
    5. Collect all column references; validate:
       - $-prefixed columns: strip $, check name is in allowed_variables
       - bare `value`: only allowed if allow_value=True
       - no other bare column names
    6. Rewrite AST: $foo → _VAR_foo (Column nodes)
    7. Render back to SQL string and return

    Args:
        condition: User-provided SQL condition, e.g. "$t > 3 AND value = 'foo'"
        allowed_variables: Variable names without $ prefix, e.g. {"t", "m"}
        allow_value: If True, bare `value` column is allowed. Set to False for
            BindingConstraint conditions which have no value column.

    Returns:
        Rewritten SQL string, e.g. "_VAR_t > 3 AND value = 'foo'"

    Raises:
        ValueError: On any validation failure (descriptive message).
    """
```

Returns a plain string. No wrapper object needed — the condition is self-contained once
rewritten. Constraint conditions use `_VAR_` names and `value`; binding conditions use
only `_VAR_` names. Both are directly embeddable into the generated SQL.

#### Safe function allowlist

Adapt `safe_functions.py` from the experiment. Combine typed class names and anonymous function
names into `SAFE_FUNCTIONS: frozenset[str]`. Connectors (AND/OR/NOT) are always allowed.

#### Column type mapping

```python
COLUMN_TYPE_TO_SQLITE: dict[IndexerColumnType, str] = {
    IndexerColumnType.INT: "INTEGER",
    IndexerColumnType.STR: "TEXT",
    IndexerColumnType.BOOL: "INTEGER",
    IndexerColumnType.BYTES: "BLOB",
    IndexerColumnType.FLOAT: "REAL",
    IndexerColumnType.FXTR_ID: "BLOB",
}
```

Used by query_gen and later by table DDL (part 2a).

#### Validation in `Indexer.bind()`

Add duplicate variable name check and call `parse_condition` for eager validation:

```python
def bind(self, key_variables, where):
    if len(key_variables) != len(set(key_variables)):
        raise ValueError("Duplicate variable names in key_variables")
    # Validate the condition eagerly
    parse_condition(where, frozenset(key_variables), allow_value=True)
    ...
```

### Tests: `test_sql_expr.py`

Adapt tests from the experiment, plus:

- **Basic valid expressions**: `"$t > 3"`, `"$t > 3 AND value = 'foo'"`,
  `"$t BETWEEN 1 AND 10"`, `"$t IN (1, 2, 3)"`, `"$t IS NULL"`, `"TRUE"`
- **Arithmetic**: `"$a + $b > $c"`, `"ABS($t) > 3"`
- **Rewriting**: `"$t > 3"` → `"_VAR_t > 3"`, `"value = 'foo'"` → `"value = 'foo'"` (unchanged)
- **Variable validation**: reject `"$unknown > 3"` when not in allowed set
- **value keyword**: allowed by default, rejected when `allow_value=False`
- **No other bare columns**: reject `"some_column > 3"`
- **Statement injection**: reject `"SELECT * FROM t"`, `"1; DROP TABLE t"`, multi-statements
- **Subquery injection**: reject `"$t > (SELECT 1)"`, `"EXISTS (SELECT 1)"`
- **Function safety**: reject `load_extension`, allow `ABS`, `LENGTH`, `COALESCE`
- **Duplicate variable rejection** in `bind()`

---

## Part 1e: SQL Query Generation

### Module: `query_gen.py`

#### Design: condition placement

Per-constraint conditions are pushed **inside** their index table subqueries. This means:

- Constraint condition strings (with `_VAR_` names and `value`) are used directly in a
  WHERE clause where those column aliases are the only names in scope — no table aliases needed.
- The binding constraint is applied in an **outermost wrapper** query where the projected
  column names (`_VAR_t`, `_VAR_m`, etc.) are unambiguous — again no table aliases in the
  condition string.
- Table aliases (`ix0`, `ix1`, ...) are only used internally by the query generator for
  JOINs, unification, and SELECT projection.

#### Generated SQL structure

Two constraints with shared `$t`, plus a binding constraint:

```sql
-- Outermost: binding constraint uses projected _VAR_ names (no table aliases)
SELECT entity_id, pin_key, _VAR_t, _VAR_m, _VAR_n
FROM (
    SELECT
        p.entity_id,
        p.name AS pin_key,
        ix0._VAR_t,
        ix0._VAR_m,
        ix1._VAR_n
    FROM namespace_pin p
    -- C0: condition pushed inside subquery, uses _VAR_ and value directly
    JOIN (SELECT * FROM (
        SELECT namespace_id, entity_internal_id,
               IXKEY_0 AS _VAR_t, IXKEY_1 AS _VAR_m, VALUE AS value
        FROM NS_INDEX__42
    ) WHERE _VAR_t > 3 AND value = 'foo') ix0
        ON ix0.entity_internal_id = p.entity_id AND ix0.namespace_id = ?
    -- C1: same pattern
    JOIN (SELECT * FROM (
        SELECT namespace_id, entity_internal_id,
               IXKEY_0 AS _VAR_t, IXKEY_1 AS _VAR_n, VALUE AS value
        FROM NS_INDEX__55
    ) WHERE value IS NOT NULL) ix1
        ON ix1.entity_internal_id = p.entity_id AND ix1.namespace_id = ?
        AND ix1._VAR_t = ix0._VAR_t                  -- variable unification
    WHERE p.namespace_id = ?
        AND p.name LIKE ? || '%'                      -- key prefix (LIKE optimization)
) WHERE _VAR_t + _VAR_n < 10                          -- binding constraint
```

When there is no binding constraint, the outermost wrapper is omitted — the middle query
becomes the top-level query directly.

#### Input types

```python
@dataclass(frozen=True)
class ResolvedConstraint:
    """An IndexerConstraint resolved to its SQL table info."""
    table_name: str                 # e.g., "NS_INDEX__42"
    key_variables: tuple[str, ...]  # e.g., ("t", "m") — without $ prefix
    key_column_types: tuple[IndexerColumnType, ...]
    value_column_type: IndexerColumnType
    condition: str                  # rewritten SQL from parse_condition

@dataclass(frozen=True)
class GeneratedQuery:
    """Result of query generation."""
    sql: str
    parameters: list[int | str]
    binding_variable_names: tuple[str, ...]  # ordered unique variable names in SELECT
    # Result row layout: (entity_internal_id, pin_key, var_0, var_1, ...)
```

#### Query generation function

```python
def generate_iter_matches_query(
    constraints: list[ResolvedConstraint],
    binding_constraint: str | None,       # rewritten SQL from parse_condition, or None
    namespace_internal_id: int,
    key_prefix: str,
) -> GeneratedQuery:
```

**Algorithm:**

1. **Assign table aliases**: `ix0`, `ix1`, ... for each constraint.

2. **Build variable registry**: For each variable across all constraints, record which
   constraint(s) define it. Pick canonical source = first constraint that defines it.

3. **Build per-constraint subqueries**: For each constraint, generate:
   ```sql
   (SELECT * FROM (
       SELECT namespace_id, entity_internal_id,
              IXKEY_0 AS _VAR_t, IXKEY_1 AS _VAR_m, VALUE AS value
       FROM {table_name}
   ) WHERE {condition}) AS ixN
   ```
   The inner SELECT aliases IXKEY columns to `_VAR_` names. The outer SELECT applies the
   constraint condition, which references `_VAR_` names and `value` directly.

4. **Build JOINs**: First constraint joins to `namespace_pin` on entity_id + namespace_id.
   Subsequent constraints also join on entity_id + namespace_id, plus equijoin conditions
   for shared variables:
   ```sql
   AND ix1._VAR_t = ix0._VAR_t  -- shared variable unification
   ```

5. **Build middle WHERE**: `p.namespace_id = ?` and key prefix filter
   (`AND p.name LIKE ? || '%'`, omitted if prefix is empty).

6. **Build middle SELECT**: `p.entity_id, p.name AS pin_key`, plus one column per unique
   variable from its canonical source: `ix0._VAR_t, ix0._VAR_m, ix1._VAR_n`.

7. **Wrap with binding constraint** (if present): Wrap the entire query in
   `SELECT ... FROM (...) WHERE {binding_constraint}`. The binding condition uses `_VAR_`
   names directly since they're the projected column names.

8. **Collect parameters**: namespace_id once per JOIN's ON clause + once in middle WHERE.
   Key prefix once. Order matches SQL placeholder positions.

### Tests: `test_query_gen.py`

Structural tests verifying generated SQL and parameter order:

- **Single constraint, no prefix**: Basic structure, one JOIN, condition inside subquery
- **Single constraint, with prefix**: LIKE clause present in WHERE
- **Two constraints, shared variable**: Equijoin in ON clause, correct unification
- **Two constraints, no shared variables**: No equijoin, just entity_id + namespace_id join
- **Three constraints, multiple shared variables**: Correct multi-way unification
- **With binding constraint**: Outer wrapper query present with binding condition
- **Without binding constraint**: No outer wrapper
- **Empty prefix**: LIKE clause omitted
- **Parameter ordering**: Parameters list matches SQL placeholder positions
- **Variable output ordering**: `binding_variable_names` matches SELECT column order

For a few key cases, also run the generated SQL against an in-memory SQLite database with
hand-created index tables and test data, to verify correct results end-to-end.

---

## Implementation Steps

- [x] Add `sqlglot` dependency (`uv add sqlglot`)
- [x] Create `fxtr2/data/store_indexing/__init__.py`
- [x] Implement `sql_expr.py` (parse_condition, safe functions, column type map)
- [x] Add duplicate variable check + eager validation to `Indexer.bind()` in `indexing_types.py`
- [x] Write `test_sql_expr.py` and run tests
- [x] Implement `query_gen.py` (ResolvedConstraint, GeneratedQuery, generate_iter_matches_query)
- [x] Write `test_query_gen.py` and run tests
- [x] Create `fxtr2/data/store_indexing/LOCAL_CODE_OVERVIEW.md`
- [x] Update `CODE_OVERVIEW.md` (add store_indexing to file tree and module details)
