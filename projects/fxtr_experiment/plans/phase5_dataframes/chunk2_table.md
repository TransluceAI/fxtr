# Chunk 2 — the frame runtime

**Status: complete.**

Design source of truth: `notes/dataframes.md` (the frame-value +
representations + write/read sections). Adds the polars dependency and
the runtime conversion layer.

**Landed under the 2026-07-17 amendment** (`discussion_log.md`, top):
the `Table` wrapper ABC is **deleted** — the frame value is a raw
`polars.DataFrame`. That reshaped this chunk from "build the concrete
`Table` class + factories + interop methods" into "make polars the
frame type and own every conversion around it." The compat guarantee
that made the deletion safe (`pl.DataFrame` confined to the view layer;
the wire carries arrow bytes; identity is the polars-free canonical
CBOR entity) is in the amendment.

## What landed

- **polars** added as the sole runtime table dep (`polars>=1.42.1`,
  the tested version; loosenable later). **duckdb** + **pyarrow** added
  to the **dev group** (`duckdb>=1.5.4`, `pyarrow>=25.0.0`) — neither is
  a runtime dep (see the notes/log): the framework's frame handling and
  native polars querying are pyarrow-free; both are opt-ins a task
  author installs only to use the duckdb query path.
- **`tables.py`** (new; the sole eager-polars module — `views.py` /
  `task_api.py` keep polars `TYPE_CHECKING`-only so importing the core
  stays cheap):
    - `KIND_TO_DTYPE` / `DTYPE_TO_KIND` — the exact one-to-one mapping
      (`str`↔String, `int`↔Int64, `float`↔Float64, `bool`↔Boolean,
      `bytes`↔Binary); `polars_schema(schema)` builds the ordered
      polars schema (nullability dropped — polars can't express it).
    - `make_frame(schema, rows)` — a typed constructor, empty-safe
      (dtypes pinned from the schema so an empty frame still round-trips
      through IPC). Runs no identity-level value validation; that is the
      write boundary's job. Replaces the planned `make_table` /
      `Table.from_polars`.
    - `frame_to_ipc` / `ipc_to_frame` — IPC **stream** bytes ⇆ frame
      (the transport form; never identity-bearing).
    - `frame_to_rows(df)` — extract an aligned frame's rows as canonical
      row tuples (the accepted write-path Python loop; feeds
      `frame_encoding.build_frame_entity`).
    - `validate_and_align(df, schema)` — the write-boundary check
      (host-side): column names match as a *set* (aligned to schema
      order), dtypes match exactly (no implicit casts — Int32 etc.
      rejected; the one exception is a polars `Null` column, cast to the
      declared kind), and null legality via native `null_count` (no row
      loop).
- **`views.py`**: `Table` ABC and its `TableValue`/`abc` imports
  removed; the doctrine (relational data = `pl.DataFrame`, multiset at
  the storage boundary, nullability is `FrameSchema`-only) moved to a
  module comment + the `DataFrameView` method docstrings, which now
  take/return `pl.DataFrame` (`TYPE_CHECKING`-only `import polars as pl`,
  string annotations). `gather`'s docstring notes that only the `on`
  key columns are constrained — payload columns ride through as any
  polars dtype.
- **`task_api.py`**: the task return type is
  `Mapping[str, FxtrID | pl.DataFrame] | FxtrID | pl.DataFrame`
  (`TYPE_CHECKING`-only polars).
- **Doc/comment references** to the old `Table` runtime type updated to
  `polars.DataFrame` in `runner_api.py`, `task_service.py`,
  `frame_encoding.py` (the *wire*-form descriptions, still
  `FrameWireType = FxtrID` until chunk 3, are unchanged). `test_smoke.py`
  no longer asserts `Table` is abstract; it checks the `tables.py`
  surface instead.

## Decisions confirmed at chunk start

- polars floor `>=1.42.1`; module `tables.py` (no class — raw polars).
- A frame does **not** cache IPC bytes (recompute per `frame_to_ipc`).
- duckdb + pyarrow ship as dev deps with an always-run smoke test;
  neither is a runtime dep (the dependency-split decision below).
- `validate_and_align` takes the declared `FrameSchema` explicitly and
  reorders by name (Q6); `Null`-dtype columns are cast to the declared
  kind rather than rejected (Q7) — works for any declared target kind;
  the schema-less inference path that would have made Q7 awkward is gone
  with the wrapper.
- `read_table` returns ordinary polars-nullable frames with canonical
  dtypes; non-nullability is a `FrameSchema`-only constraint.

## Tests (`tests/test_tables.py`)

- Round trips are exact: rows → frame → IPC → rows preserves values
  bit-for-bit (NaN, `-0.0`, int64 extremes, bytes, nulls, empty frames,
  unicode).
- Dtype mapping is one-to-one over the five kinds;
  `validate_and_align` rejects non-canonical dtypes (Int32/Float32/
  Int8/UInt64) with a useful message.
- Validation: name-set mismatch (missing/extra), `Null`-cast, null in a
  non-nullable column (incl. an all-`Null` column), reorder-by-name.
- Cross-check: `frame_to_rows` → `build_frame_entity` is order-
  independent (permuted driving order → identical canonical entity),
  confirming the runtime and identity layers agree.
- Interop: `__arrow_c_stream__` consumable by a fresh polars consumer
  and by duckdb, both via a stream-only object (the genuinely
  pyarrow-free path).
- `uv run pyright` clean.

## Notes/log

- **Only duckdb needs pyarrow; both stay opt-in** (verified 2026-07-17
  with pyarrow uninstalled). The whole framework path (`make_frame`,
  IPC↔frame, `frame_to_rows`, `validate_and_align`) and all native
  polars querying (`group_by`/`filter`/`join`/`write_parquet`/
  `__arrow_c_stream__`) work without pyarrow — polars' IPC is native
  Rust arrow. Only `df.to_arrow()` and `duckdb.sql("from df")` (which
  calls `to_arrow`) need it. Every ergonomic duckdb path routes through
  `to_arrow`; the only pyarrow-free route is an ugly capsule-only
  wrapper (duckdb detects the polars frame and takes its `to_arrow`
  path). So duckdb + pyarrow are dev/opt-in, not runtime deps, and the
  smoke test drives the natural `duckdb.sql("from df")` path (+ a
  `to_arrow()` round-trip) with pyarrow present in dev.
- **polars coerces at construction** (e.g. `bool` into an Int64 column →
  1/0), so by the time `frame_to_rows` extracts, values already match
  the frame dtype and `encode_row`'s exact-type check passes. This is
  intentional under the amendment — polars is the ergonomic coercion
  layer; the authoritative guard is `validate_and_align`'s exact-*dtype*
  check plus `build_frame_entity`'s value checks.
- The IDE's pyright flags `import polars` unresolved because it targets
  the repo-root venv; the project's own `uv run pyright` (the gated one)
  is clean.
