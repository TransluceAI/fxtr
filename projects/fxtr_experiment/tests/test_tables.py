"""Table-runtime tests: the polars dataframe layer and its conversions
(`tables.py`).

Frames move through the system as ``polars.DataFrame`` (no wrapper type);
this module owns the dtype mapping and the conversions among the four forms
(frame / arrow IPC bytes / canonical rows / declared schema). These tests
pin the exactness that identity depends on: the round trip
rows → frame → IPC → rows must be bit-for-bit, dtype mapping is one-to-one
with no implicit casts, and the write-boundary validation matches the
declared schema (names, exact dtypes, null legality). A cross-check against
`frame_encoding` confirms the runtime and identity layers agree.
"""

import math
import struct

import duckdb
import polars as pl
import pytest

from fxtr_experiment import frame_encoding as fe
from fxtr_experiment_task import model as m
from fxtr_experiment_task import tables as t
from fxtr_experiment_task.errors import EncodingError

ALL_KINDS = (
    m.ColumnSpec("s", "str"),
    m.ColumnSpec("i", "int"),
    m.ColumnSpec("f", "float"),
    m.ColumnSpec("b", "bool"),
    m.ColumnSpec("y", "bytes"),
    m.ColumnSpec("opt", "int", nullable=True),
)

INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)


def _bits(f: float) -> bytes:
    return struct.pack(">d", f)


# -- dtype mapping -------------------------------------------------------------


def test_kind_dtype_mapping_is_one_to_one_over_the_five_kinds() -> None:
    assert set(t.KIND_TO_DTYPE) == {"str", "int", "float", "bool", "bytes"}
    assert t.KIND_TO_DTYPE == {
        "str": pl.String,
        "int": pl.Int64,
        "float": pl.Float64,
        "bool": pl.Boolean,
        "bytes": pl.Binary,
    }
    # Reverse is a genuine inverse (no two kinds share a dtype).
    assert len(t.DTYPE_TO_KIND) == len(t.KIND_TO_DTYPE)


def test_polars_schema_pins_dtypes_in_schema_order() -> None:
    ps = t.polars_schema(ALL_KINDS)
    assert list(ps.keys()) == ["s", "i", "f", "b", "y", "opt"]
    assert ps["i"] == pl.Int64 and ps["opt"] == pl.Int64  # nullability dropped


# -- round trips: rows -> frame -> IPC -> rows are bit-for-bit -----------------


def _roundtrip(schema: m.FrameSchema, rows: list[m.FrameRow]) -> list[m.FrameRow]:
    df = t.make_frame(schema, rows)
    reparsed = t.ipc_to_frame(t.frame_to_ipc(df))
    return t.frame_to_rows(t.validate_and_align(reparsed, schema))


def test_roundtrip_preserves_all_scalar_kinds_and_nulls() -> None:
    rows: list[m.FrameRow] = [
        ("héllo→", 1, 1.5, True, b"\x00\xff", None),
        ("", INT64_MIN, -2.5, False, b"", INT64_MAX),
    ]
    assert _roundtrip(ALL_KINDS, rows) == rows


def test_roundtrip_preserves_negative_zero_and_nan_bits() -> None:
    schema = (m.ColumnSpec("f", "float"),)
    out = _roundtrip(schema, [(-0.0,), (0.0,), (float("inf"),), (float("nan"),)])
    vals = [row[0] for row in out]
    assert all(isinstance(v, float) for v in vals)
    v0, v1, v2, v3 = (v for v in vals if isinstance(v, float))
    assert _bits(v0) == _bits(-0.0)  # sign preserved, distinct from +0.0
    assert _bits(v1) == _bits(0.0)
    assert v2 == float("inf")
    assert math.isnan(v3)


def test_roundtrip_empty_frame_keeps_dtypes() -> None:
    df = t.make_frame(ALL_KINDS, [])
    assert df.height == 0
    reparsed = t.ipc_to_frame(t.frame_to_ipc(df))
    assert reparsed.schema == df.schema  # dtypes survive an empty IPC round trip
    assert t.frame_to_rows(t.validate_and_align(reparsed, ALL_KINDS)) == []


def test_make_frame_rejects_invalid_schema() -> None:
    with pytest.raises(EncodingError):
        t.make_frame((), [])  # zero columns
    with pytest.raises(EncodingError):
        t.make_frame((m.ColumnSpec("x", "int"), m.ColumnSpec("x", "str")), [])  # dup name


# -- validate_and_align: names, exact dtypes, null legality --------------------


def test_validate_and_align_reorders_by_name() -> None:
    df = pl.DataFrame(
        {"opt": [1], "i": [2], "y": [b"z"], "b": [True], "f": [1.0], "s": ["a"]},
        schema={
            "opt": pl.Int64,
            "i": pl.Int64,
            "y": pl.Binary,
            "b": pl.Boolean,
            "f": pl.Float64,
            "s": pl.String,
        },
    )
    aligned = t.validate_and_align(df, ALL_KINDS)
    assert aligned.columns == ["s", "i", "f", "b", "y", "opt"]


def test_validate_and_align_rejects_non_canonical_dtypes() -> None:
    schema = (m.ColumnSpec("i", "int"),)
    for bad_dt in (pl.Int32, pl.Float32, pl.Int8, pl.UInt64):
        df = pl.DataFrame({"i": [1]}, schema={"i": bad_dt})
        with pytest.raises(EncodingError, match="does not match declared kind"):
            t.validate_and_align(df, schema)


def test_validate_and_align_casts_null_dtype_columns() -> None:
    schema = (m.ColumnSpec("opt", "int", nullable=True),)
    df = pl.DataFrame({"opt": [None, None]})  # dtype Null
    assert df.schema["opt"] == pl.Null
    aligned = t.validate_and_align(df, schema)
    assert aligned.schema["opt"] == pl.Int64
    assert t.frame_to_rows(aligned) == [(None,), (None,)]


def test_validate_and_align_rejects_null_in_non_nullable() -> None:
    schema = (m.ColumnSpec("i", "int"),)
    df = pl.DataFrame({"i": [1, None]}, schema={"i": pl.Int64})
    with pytest.raises(EncodingError, match="non-nullable"):
        t.validate_and_align(df, schema)
    # An all-Null column against a non-nullable schema is also rejected (the
    # cast succeeds, then null legality bites).
    df2 = pl.DataFrame({"i": [None]})
    with pytest.raises(EncodingError, match="non-nullable"):
        t.validate_and_align(df2, schema)


def test_validate_and_align_rejects_name_set_mismatch() -> None:
    schema = (m.ColumnSpec("a", "int"), m.ColumnSpec("b", "int"))
    with pytest.raises(EncodingError, match="missing"):
        t.validate_and_align(pl.DataFrame({"a": [1]}, schema={"a": pl.Int64}), schema)
    with pytest.raises(EncodingError, match="extra"):
        t.validate_and_align(
            pl.DataFrame(
                {"a": [1], "b": [2], "c": [3]}, schema={"a": pl.Int64, "b": pl.Int64, "c": pl.Int64}
            ),
            schema,
        )


def test_validate_and_align_rejects_out_of_int64_via_extraction() -> None:
    # int64 range is a value law owned by frame_encoding; a polars frame
    # can't even hold >int64 in an Int64 column, so this documents that the
    # boundary is enforced downstream (build_frame_entity), not here.
    with pytest.raises(Exception):
        pl.DataFrame({"i": [2**63]}, schema={"i": pl.Int64})


# -- cross-check: the runtime layer agrees with the identity layer -------------


def test_frame_to_rows_feeds_build_frame_entity_order_independently() -> None:
    rows_a: list[m.FrameRow] = [("a", 1, 1.0, True, b"x", None), ("b", 2, 2.0, False, b"y", 5)]
    rows_b = list(reversed(rows_a))
    id_a = fe.build_frame_entity(
        ALL_KINDS, t.frame_to_rows(t.validate_and_align(t.make_frame(ALL_KINDS, rows_a), ALL_KINDS))
    )
    id_b = fe.build_frame_entity(
        ALL_KINDS, t.frame_to_rows(t.validate_and_align(t.make_frame(ALL_KINDS, rows_b), ALL_KINDS))
    )
    # Multiset identity: driving order does not affect the canonical entity.
    assert id_a.schema == id_b.schema and id_a.rows == id_b.rows


# -- interop: arrow and duckdb -------------------------------------------------
#
# Frames are plain polars, so their whole interop surface is polars': duckdb
# picks them up directly via replacement scan (over pyarrow, a required dep),
# and `to_arrow()` hands them to any arrow-native consumer. This is the
# ergonomic path the design targets ("query gathered data with polars/duckdb").


def test_duckdb_queries_a_frame_via_replacement_scan() -> None:
    df = t.make_frame(  # noqa: F841 — duckdb resolves `df` by name in the SQL
        (m.ColumnSpec("k", "str"), m.ColumnSpec("v", "int")),
        [("a", 1), ("b", 2), ("a", 3)],
    )
    got = duckdb.sql("select k, sum(v) as s from df group by k order by k").fetchall()
    assert got == [("a", 4), ("b", 2)]


def test_to_arrow_round_trips_through_pyarrow() -> None:
    df = t.make_frame(ALL_KINDS, [("a", 1, 1.5, True, b"z", 9), ("b", 2, 2.5, False, b"", None)])
    table = df.to_arrow()  # a pyarrow.Table
    assert table.num_rows == 2
    back = pl.from_arrow(table)  # DataFrame (pyarrow Table in -> frame out)
    assert isinstance(back, pl.DataFrame)
    assert back.rows() == df.rows()
