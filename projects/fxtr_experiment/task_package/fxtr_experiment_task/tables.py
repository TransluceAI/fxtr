"""The dataframe runtime: polars as the table type, and every conversion.

The whole dataframe design — the representations, the column model, and the
rationale for every rule — is ``notes/dataframes.md``. This module is the
runtime half of it: the public table type is :class:`polars.DataFrame`
(there is no wrapper), and this module owns the mapping between the four
forms a frame takes in flight:

- **``pl.DataFrame``** — the API type. What task code and user code hand to
  / receive from the view-layer frame ops (`views.py`'s ``DataFrameView``).
  A frame carries no fxtr identity; its schema/dtypes are ordinary polars
  metadata.
- **Arrow IPC stream bytes** — the transport form carried by the wire
  (``runner_api.FrameWireType``). Never identity-bearing (arrow IPC output
  is not stable across library versions; fingerprints must not be
  codec-derived).
- **Canonical rows** (``model.FrameRow`` — one tuple of scalars in schema
  column order) — the handoff to the identity path: :func:`frame_to_rows`
  produces them, and `frame_encoding.build_frame_entity` turns them into
  the canonical CBOR frame entity whose id is the fingerprint.
- **The declared ``FrameSchema``** — the storage contract (`model.py`). Only
  the *host* knows it (token → grid config / declared ``FrameOutput``), so
  every canonicalization and every typed rebuild happens host-side after
  config resolution.

This is the one module that imports polars eagerly; the package core
(`views.py`, `task_api.py`) keeps polars ``TYPE_CHECKING``-only so importing
the core stays cheap.

Everything here is **task-side**: it is the runtime a sandbox guest needs,
so it imports nothing but `model.py` and `errors.py`. Minting identity is
deliberately *not* here — the funnel that turns wire bytes into a canonical
entity is `frame_encoding.canonical_frame_from_ipc`, which composes
:func:`validate_and_align` + :func:`frame_to_rows` from the host side.

**The dtype mapping is exact and one-to-one** (``str``↔String, ``int``↔Int64,
``float``↔Float64, ``bool``↔Boolean, ``bytes``↔Binary): there are no implicit
casts near identity, because an upcast (e.g. Int32→Int64) changes the encoded
bytes and therefore the frame's identity. Loosening this later is
compat-safe; tightening is not. The one sanctioned cast is a polars ``Null``
column (an all-null column has no dtype to preserve, so casting it to the
declared kind is unique and lossless — see :func:`validate_and_align`).

**Validation lives at the write boundary, not at construction.** A frame that
users build (or that arrives over the wire) is checked against the grid's
*declared* schema by :func:`validate_and_align` before it becomes identity:
column names must match as a set (order is aligned by name), dtypes must
match exactly, and no null may sit in a declared-non-nullable column. A
frame's own nullability is not even expressible in polars, so it is never
compared — nullability is a constraint on data, checked against the schema,
not a property carried by the frame.
"""

from __future__ import annotations

from typing import Sequence, cast

import polars as pl
from polars.datatypes import DataTypeClass

from fxtr_experiment_task.errors import EncodingError, ViewAxisError
from fxtr_experiment_task.model import (
    AxisKind,
    ColumnKind,
    CoordValue,
    FrameRow,
    FrameSchema,
    validate_frame_schema,
)

KIND_TO_DTYPE: dict[ColumnKind, DataTypeClass] = {
    "str": pl.String,
    "int": pl.Int64,
    "float": pl.Float64,
    "bool": pl.Boolean,
    "bytes": pl.Binary,
}
"""The exact, one-to-one column-kind → polars dtype mapping (module docstring)."""

DTYPE_TO_KIND: dict[DataTypeClass, ColumnKind] = {v: k for k, v in KIND_TO_DTYPE.items()}
"""Reverse of :data:`KIND_TO_DTYPE` (exact dtypes only)."""


def polars_schema(schema: FrameSchema) -> pl.Schema:
    """The declared schema as an ordered polars schema (canonical dtypes).

    Nullability is dropped (polars cannot express NOT NULL); it lives only in
    the ``FrameSchema`` and is enforced at the write boundary
    (:func:`validate_and_align`).

    Args:
        schema: The declared frame schema.

    Returns:
        A ``pl.Schema`` mapping each column name to its canonical dtype, in
        schema column order.
    """
    return pl.Schema({col.name: KIND_TO_DTYPE[col.kind] for col in schema})


def make_frame(schema: FrameSchema, rows: Sequence[FrameRow]) -> pl.DataFrame:
    """Build a canonically-typed ``pl.DataFrame`` from rows (empty-safe).

    A thin typed constructor: dtypes are pinned from ``schema`` (so an empty
    frame still carries the right dtypes, and the frame round-trips through
    arrow IPC unchanged). This does **not** run identity-level value
    validation — polars applies its own construction coercion (its role as
    the ergonomic layer), and the authoritative check is at the write
    boundary (:func:`validate_and_align` → `frame_encoding.build_frame_entity`).
    Used for user-facing frame construction and for host-side projection
    rebuilds from canonical rows (which are already valid).

    Args:
        schema: The frame's declared schema (column-name law validated here).
        rows: The rows, each a tuple of scalars in schema column order.

    Returns:
        A frame with columns in schema order and canonical dtypes.

    Raises:
        EncodingError: On an invalid schema (column-name law).
    """
    validate_frame_schema(schema)
    ps = polars_schema(schema)
    if not rows:
        return pl.DataFrame(schema=ps)
    return pl.DataFrame(list(rows), schema=ps, orient="row")


def frame_to_ipc(df: pl.DataFrame) -> bytes:
    """Serialize a frame to arrow IPC **stream** bytes (the transport form).

    The IPC *stream* format (not the file format) — the wire and projection
    carry streams. Never identity-bearing.
    """
    return df.write_ipc_stream(None).getvalue()


def ipc_to_frame(data: bytes) -> pl.DataFrame:
    """Parse arrow IPC stream bytes back into a frame (no validation).

    The inverse of :func:`frame_to_ipc`. Validation against a declared schema
    is separate (:func:`validate_and_align`) and applies only on the write
    boundary; frames the framework itself produced (read/gather results) are
    trusted and parsed straight through.
    """
    return pl.read_ipc_stream(data)


def frame_to_rows(df: pl.DataFrame) -> list[FrameRow]:
    """Extract a validated+aligned frame's rows as canonical row tuples.

    The accepted write-path Python loop (a native encoder is a deferred
    follow-up). The caller must have run :func:`validate_and_align` first, so
    the frame is in schema order with canonical dtypes; ``polars`` then yields
    exact Python scalars (``int``/``float``/``str``/``bool``/``bytes``/``None``)
    that `frame_encoding.encode_row` accepts.

    Args:
        df: A frame already aligned to a declared schema.

    Returns:
        One tuple per row, in the frame's current order (the canonical sort
        is applied later, when the rows become a frame entity).
    """
    # polars .rows() returns list[tuple]; every dtype here maps to an exact
    # Python scalar (verified against the dtype mapping), so the values are
    # already the TableValue union.
    return cast("list[FrameRow]", df.rows())


def validate_and_align(df: pl.DataFrame, schema: FrameSchema) -> pl.DataFrame:
    """Validate a frame against a declared schema and align it to schema order.

    The write-boundary check (host-side): column names must match ``schema``
    as a *set* (order is irrelevant and is aligned here by name), each
    column's dtype must match its declared kind **exactly** — no implicit
    casts, since an upcast would change identity — and no null may appear in a
    declared-non-nullable column. The one exception is a polars ``Null``
    column (all-null, no dtype to preserve): it is cast to the declared kind,
    after which the null-legality check applies as normal.

    Args:
        df: The frame to validate (as built by a task / arrived over the wire).
        schema: The grid's declared schema — the authority for names, dtypes,
            and nullability.

    Returns:
        The frame reordered to schema column order, with any ``Null`` column
        cast to its declared dtype. Ready for :func:`frame_to_rows`.

    Raises:
        EncodingError: On a column-name set mismatch, an exact-dtype mismatch,
            or a null in a non-nullable column.
    """
    validate_frame_schema(schema)
    want = [col.name for col in schema]
    have = df.columns
    if set(have) != set(want):
        missing = [n for n in want if n not in have]
        extra = [n for n in have if n not in want]
        raise EncodingError(
            f"frame columns {sorted(have)} do not match schema columns {want} "
            f"(missing {missing}, extra {extra})"
        )

    aligned = df.select(want)
    casts: list[pl.Expr] = []
    for col in schema:
        want_dtype = KIND_TO_DTYPE[col.kind]
        got = aligned.schema[col.name]
        if got == pl.Null:
            # All-null column: no dtype to preserve, so the cast to the
            # declared kind is unique and lossless. Null legality is still
            # checked below (a non-nullable column rejects the nulls).
            casts.append(pl.col(col.name).cast(want_dtype))
        elif got != want_dtype:
            raise EncodingError(
                f"column {col.name!r}: frame dtype {got} does not match "
                f"declared kind {col.kind!r} ({want_dtype}); cast explicitly "
                f"(no implicit casts near identity)"
            )
    if casts:
        aligned = aligned.with_columns(casts)

    # Null legality, checked natively (no row loop): one 1-row null-count frame.
    null_counts = aligned.null_count().row(0, named=True)
    bad = [col.name for col in schema if not col.nullable and null_counts[col.name] > 0]
    if bad:
        raise EncodingError(f"nulls in non-nullable column(s) {bad}")
    return aligned


# -- gather mechanics (the wire key table, the stitch, the local join) -----------
#
# Gather's wire dialect (`notes/dataframes.md` §Gather): the request key
# table and the response stream name key columns **positionally** — column
# "i" binds the selection's i-th open position (ascending grant order) — so
# no axis names cross the wire. The response carries those key columns plus
# the cell schema columns; the local join translates back to the caller's
# vocabulary.

_GATHER_IDX = "__fxtr_gather_idx__"  # the local join's row-index column


def _key_names(n: int) -> list[str]:
    return [str(i) for i in range(n)]


def extract_keys(
    driving: pl.DataFrame, key_cols: Sequence[str], kinds: Sequence[AxisKind]
) -> pl.DataFrame:
    """The request key table: validate + project + dedupe (client-side).

    Args:
        driving: The caller's driving frame.
        key_cols: Driving column names binding the open axes, in open-axis
            order.
        kinds: The open axes' coordinate kinds, same order.

    Returns:
        The deduped key table with positional column names (row order is
        deliberately meaningless — the host sorts).

    Raises:
        ViewAxisError: A missing column, a dtype not exactly matching the
            axis kind, or a null key value.
    """
    exprs = []
    for i, (col, kind) in enumerate(zip(key_cols, kinds)):
        if col not in driving.columns:
            raise ViewAxisError(f"driving table has no key column {col!r}")
        want = KIND_TO_DTYPE[kind]
        got = driving.schema[col]
        if got == pl.Null:
            # The sanctioned Null cast (as at the write boundary): an
            # all-null column has no dtype to preserve — typically an
            # *empty* literal frame; actual null values still fail below.
            pass
        elif got != want:
            raise ViewAxisError(
                f"key column {col!r} has dtype {got}, but the axis kind is"
                f" {kind!r} ({want}); cast explicitly"
            )
        if driving[col].null_count() > 0:
            raise ViewAxisError(f"null values in key column {col!r}")
        exprs.append(pl.col(col).cast(want).alias(str(i)))
    if not exprs:
        return pl.DataFrame()  # zero open axes: the degenerate single key
    return driving.select(exprs).unique()


def decode_key_table(data: bytes, kinds: Sequence[AxisKind]) -> list[tuple[CoordValue, ...]]:
    """Parse + authoritatively validate a wire key table (host-side).

    Args:
        data: The request key table (arrow IPC stream bytes).
        kinds: The selection's open axes' kinds, ascending position order.

    Returns:
        The distinct key tuples (host dedups — never rejects duplicates;
        order is meaningless). Zero open axes decode to the single empty
        key whatever the payload carries.

    Raises:
        ViewAxisError: Unparseable payload, a column-name/count mismatch,
            a dtype not matching its axis kind, or a null key value.
    """
    if not kinds:
        return [()]
    try:
        df = ipc_to_frame(data)
    except Exception as exc:
        raise ViewAxisError(f"unparseable gather key table: {exc}") from exc
    want_names = _key_names(len(kinds))
    if set(df.columns) != set(want_names):
        raise ViewAxisError(
            f"gather key table columns {sorted(df.columns)} != expected positional"
            f" names {want_names}"
        )
    df = df.select(want_names)
    casts: list[pl.Expr] = []
    for name, kind in zip(want_names, kinds):
        want = KIND_TO_DTYPE[kind]
        if df.schema[name] == pl.Null:
            casts.append(pl.col(name).cast(want))  # the sanctioned Null cast
        elif df.schema[name] != want:
            raise ViewAxisError(
                f"gather key column {name} has dtype {df.schema[name]}, axis kind"
                f" is {kind!r} ({want})"
            )
        if df[name].null_count() > 0:
            raise ViewAxisError(f"null values in gather key column {name}")
    if casts:
        df = df.with_columns(casts)
    return [tuple(row) for row in df.unique().rows()]


def stitch_gather(
    kinds: Sequence[AxisKind],
    schema: FrameSchema,
    parts: "Sequence[tuple[tuple[CoordValue, ...], pl.DataFrame]]",
) -> pl.DataFrame:
    """The response stream: key columns + cell schema columns (host-side).

    Args:
        kinds: The open axes' kinds (the key columns' dtypes).
        schema: The cells' declared schema.
        parts: One ``(key tuple, cell frame)`` per distinct addressed
            cell, already in the serving order (sorted coords; each cell's
            frame in canonical row order).

    Returns:
        One frame: positional key columns then schema columns, all parts
        concatenated (empty-safe: zero parts yield the empty, correctly
        typed stream).
    """
    names = _key_names(len(kinds))
    key_dtypes = [KIND_TO_DTYPE[k] for k in kinds]
    stitched = [
        cell.select(
            [pl.lit(v, dtype=dt).alias(n) for n, dt, v in zip(names, key_dtypes, key)]
            + [pl.col(c.name) for c in schema]
        )
        for key, cell in parts
    ]
    if not stitched:
        empty_schema = pl.Schema(
            dict(zip(names, key_dtypes)) | {c.name: KIND_TO_DTYPE[c.kind] for c in schema}
        )
        return pl.DataFrame(schema=empty_schema)
    return pl.concat(stitched)


def gather_join(
    driving: pl.DataFrame,
    key_cols: Sequence[str],
    stream: pl.DataFrame,
    schema: FrameSchema,
    column_prefix: str | None,
) -> pl.DataFrame:
    """The local key-join: the response stream back onto the driving frame.

    Output order pins to driving-row order × stream (canonical) order via
    an explicit row index + stable sort — no reliance on join-engine
    ordering. Join arity is inner-join-shaped: a driving row whose cell
    holds a zero-row frame contributes zero output rows (empty cells never
    null-pad).

    Args:
        driving: The caller's driving frame (payload columns ride through).
        key_cols: Driving column names binding the open axes, in open-axis
            order (`extract_keys`'s vocabulary).
        stream: The host's stitched response.
        schema: The cells' declared schema (for the appended names).
        column_prefix: Prefix for the appended schema columns — the rename
            point for collisions.

    Returns:
        ``driving``'s columns, then the (prefixed) schema columns.

    Raises:
        ViewAxisError: A post-prefix appended name collides with a driving
            column (or with a key column's local name).
    """
    prefix = column_prefix or ""
    appended = {c.name: prefix + c.name for c in schema}
    for name in appended.values():
        if name in driving.columns:
            raise ViewAxisError(
                f"gathered column {name!r} collides with a driving column"
                " (disambiguate with column_prefix)"
            )
    renames = dict(zip(_key_names(len(key_cols)), key_cols)) | appended
    if len(set(renames.values())) != len(renames):
        raise ViewAxisError(
            f"gathered column names collide with the key columns: {sorted(renames.values())}"
        )
    # The sanctioned Null cast, join side: an all-null (typically empty)
    # driving key column takes the stream's dtype so the join keys match.
    null_casts = [
        pl.col(kc).cast(stream.schema[str(i)])
        for i, kc in enumerate(key_cols)
        if driving.schema[kc] == pl.Null
    ]
    if null_casts:
        driving = driving.with_columns(null_casts)
    stream = stream.rename(renames)
    if _GATHER_IDX in driving.columns:
        raise ViewAxisError(f"driving table may not carry the reserved column {_GATHER_IDX!r}")
    indexed = driving.with_row_index(_GATHER_IDX)
    if key_cols:
        joined = indexed.join(stream, on=list(key_cols), how="inner")
    else:
        joined = indexed.join(stream, how="cross")
    return joined.sort(_GATHER_IDX, maintain_order=True).drop(_GATHER_IDX)
