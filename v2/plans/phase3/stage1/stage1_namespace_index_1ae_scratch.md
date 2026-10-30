# 1ae Scratch: SQL Expression Parsing & Query Generation

## sqlglot behavior (confirmed from experiment)

The experiment at `sqlite_cond_parsing/` confirmed:
- sqlglot parses `$foo` as `exp.Column` nodes (not `exp.Parameter`)
- The `$` is part of the column name string (`col.this.this` returns `"$foo"`)
- AST walking finds all Column nodes; validation checks names against an allowed set
- `transform()` on the AST can rewrite Column nodes to new names
- Statements, subqueries, and disallowed functions are detected and rejected

## Adaptation plan

From the experiment, we keep:
- Parse with `sqlglot.parse(sql_str, dialect="sqlite")`, check exactly 1 result
- Reject statement types, subqueries
- Function allowlist (adapt `safe_functions.py`)
- Column extraction via `find_all(exp.Column)`

We change:
- `value` is a special allowed column (not `$`-prefixed), only for IndexerConstraint conditions
- `parse_condition` returns a plain rewritten string (no wrapper object needed)
- Constraint conditions go inside subqueries where `_VAR_` and `value` names are directly
  in scope — no table aliases in condition strings
- Binding constraints go in an outermost wrapper where projected `_VAR_` names are unambiguous

## Column type mapping

| IndexerColumnType | SQLite type |
|-------------------|-------------|
| INT               | INTEGER     |
| STR               | TEXT        |
| BOOL              | INTEGER     |
| BYTES             | BLOB        |
| FLOAT             | REAL        |
| FXTR_ID           | BLOB        |

## Subquery renaming approach (revised)

Key insight: instead of having `ParsedCondition.render()` do `$t → ix0.IXKEY_0` column
remapping, we do two simpler things:
1. **Parse time**: rewrite `$t → _VAR_t` (one-time, decoupled from column order)
2. **Query gen time**: wrap each index table in a subquery that aliases
   `IXKEY_0 AS _VAR_t, IXKEY_1 AS _VAR_m, VALUE AS value`

Then condition strings just need table alias qualification, not full column remapping.

Two render modes for ParsedCondition:
- `render_with_table(alias)` — single table prefix, for constraint conditions
- `render_with_var_tables(var_to_table)` — per-variable table prefix, for binding constraints

## Generated SQL structure (revised)

Example with two constraints, shared `$t`:
- C0: table `NS_INDEX__42`, keys=("t","m"), cond=`$t > 3 AND value = 'foo'`
- C1: table `NS_INDEX__55`, keys=("t","n"), cond=`value IS NOT NULL`
- Binding: `$t + $n < 10`

```sql
SELECT
    p.entity_id,
    p.name AS pin_key,
    ix0._VAR_t,
    ix0._VAR_m,
    ix1._VAR_n
FROM namespace_pin p
JOIN (
    SELECT namespace_id, entity_internal_id,
           IXKEY_0 AS _VAR_t, IXKEY_1 AS _VAR_m, VALUE AS value
    FROM NS_INDEX__42
) ix0
    ON ix0.entity_internal_id = p.entity_id
    AND ix0.namespace_id = ?
JOIN (
    SELECT namespace_id, entity_internal_id,
           IXKEY_0 AS _VAR_t, IXKEY_1 AS _VAR_n, VALUE AS value
    FROM NS_INDEX__55
) ix1
    ON ix1.entity_internal_id = p.entity_id
    AND ix1.namespace_id = ?
    AND ix1._VAR_t = ix0._VAR_t             -- unify $t
WHERE p.namespace_id = ?
    AND p.name LIKE ? || '%'                  -- key prefix (LIKE optimization)
    AND (ix0._VAR_t > 3 AND ix0.value = 'foo')  -- C0 cond (render_with_table("ix0"))
    AND (ix1.value IS NOT NULL)                   -- C1 cond (render_with_table("ix1"))
    AND (ix0._VAR_t + ix1._VAR_n < 10)           -- binding (render_with_var_tables)
```

Rendering:
- C0 condition `_VAR_t > 3 AND value = 'foo'` → `render_with_table("ix0")` →
  `ix0._VAR_t > 3 AND ix0.value = 'foo'`
- Binding `_VAR_t + _VAR_n < 10` → `render_with_var_tables({"_VAR_t": "ix0", "_VAR_n": "ix1"})` →
  `ix0._VAR_t + ix1._VAR_n < 10`

Canonical source for shared variables in binding/SELECT: first constraint that defines it.

## Key prefix filtering

SQLite's LIKE optimization automatically converts `LIKE 'prefix%'` into an efficient range
scan (see https://www.sqlite.org/optoverview.html#the_like_optimization), so we just use
`p.name LIKE ? || '%'` like the existing `list_pins` code. Empty prefix → omit the filter.

## Duplicate binding variables

If a user calls `bind(("x", "x"), ...)`, the subquery would alias two columns to `_VAR_x`,
which is ambiguous. Reject duplicate variable names in `bind()` validation.

## Output columns

Fixed layout:
1. `p.entity_id` (internal ID for caller to resolve)
2. `p.name` (pin key)
3. One column per unique binding variable, in a deterministic order

The caller needs to know the variable order to build the bindings dict.
Return it alongside the SQL.
