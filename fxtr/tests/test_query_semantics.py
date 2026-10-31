"""What query results mean where Python, DuckDB, and the wire could each give a different answer."""

from __future__ import annotations

import gc
import math
import threading
import time
from collections.abc import Mapping
from typing import Annotated, Any, cast

import anyio
import pytest
from reference_backends import Executor, JobDidNotSucceed, postgres_backend

import fxtr
from fxtr.core import codec
from fxtr.duckdb_engine.compiler import compile_array_query
from fxtr.duckdb_engine.executor import DuckDBExecutionLimits, DuckDBResourceLimitError
from fxtr.entity_defns.array import (
    Array,
    ArrayError,
    ArrayType,
    ArrayTypeError,
    rename_type,
    sel_type,
)
from fxtr.entity_defns.query import (
    Arithmetic,
    QueryError,
    asc,
    count,
    desc,
    dim,
    literal,
    value,
    when,
)
from fxtr.entity_defns.query_plan import PlannedQuery
from fxtr.entity_defns.schema import optional, struct_of
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.runner.query_codec import decode_query, encode_query

pytestmark = pytest.mark.anyio


# ---- names that differ only by case ----


async def test_a_dimension_and_a_field_differing_only_by_case_stay_distinct() -> None:
    rows = Array.from_items(
        [((1,), {"I": 100}), ((2,), {"I": 200})], ("[i: int]", struct_of(I=int))
    )
    assert (await rows.select(value().field("I")).collect()).raw_rows == (((1,), 100), ((2,), 200))
    assert (await rows.filter(dim("i") == 2).collect()).raw_rows == (((2,), {"I": 200}),)
    assert (await rows.filter(value().field("I") == 100).collect()).raw_rows == (
        ((1,), {"I": 100}),
    )


def test_names_duckdb_cannot_tell_apart_are_rejected_when_the_query_is_built() -> None:
    fields = Array.from_items(
        [((1,), {"Score": 10, "score": 20})], ("[i: int]", struct_of(Score=int, score=int))
    )
    with pytest.raises(QueryError, match="'Score' and 'score' differ only by case"):
        fields.select({"out": value().field("score")})
    nested = Array.from_items(
        [((1,), {"meta": {"Rank": 1, "rank": 2}})],
        ("[i: int]", struct_of(meta=struct_of(Rank=int, rank=int))),
    )
    with pytest.raises(QueryError, match="differ only by case"):
        nested.filter(dim("i") == 1)
    dims = Array.from_items([((1, 2), 3)], ("[Run: int, run: int]", int))
    with pytest.raises(QueryError, match="Dimensions 'Run' and 'run' differ only by case"):
        dims.filter(dim("run") == 2)
    plain = Array.from_items([((1,), {"a": 1})], ("[i: int]", struct_of(a=int)))
    with pytest.raises(QueryError, match="differ only by case"):
        plain.select({"Total": value().field("a"), "total": value().field("a")})
    # A single expression can build such a struct too: a literal, or a conditional's branches.
    with pytest.raises(QueryError, match="differ only by case"):
        plain.select(literal({"a": 1, "A": 2}))
    with pytest.raises(QueryError, match="differ only by case"):
        plain.select(when(dim("i") > 0).then(literal({"x": 1, "X": 2})).otherwise(None))


# ---- arithmetic ----


def _records(array: Array[Any]) -> list[Mapping[str, Any]]:
    return [cast("Mapping[str, Any]", row) for row in array.raw_values()]


INT_PAIRS = [
    (-7, 2),
    (7, -2),
    (-7, -2),
    (7, 2),
    (6, 3),
    (-6, 3),
    (0, 5),
    (2**62 + 1, 1),
    (-(2**62) - 1, 3),
]
FLOAT_PAIRS = [
    (-7.5, 2.0),
    (7.5, -2.0),
    (7.5, 2.0),
    (-7.5, -2.0),
    (6.0, 3.0),
    (1.0, 0.1),
    (-0.0, 2.0),
]


def _truncated_division(a: int, b: int) -> tuple[int, int]:
    """``a // b`` and ``a % b`` as SQL answers them: toward zero, remainder signed like ``a``."""
    quotient = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
    return quotient, a - quotient * b


async def test_integer_floor_division_and_modulo_truncate_toward_zero() -> None:
    """SQL truncates and gives the remainder the dividend's sign, where Python floors and takes
    the divisor's; the operators an array query builds answer as the same SQL would."""
    rows = Array.from_items(
        [((i,), {"x": x, "y": y}) for i, (x, y) in enumerate(INT_PAIRS)],
        ("[i: int]", struct_of(x=int, y=int)),
    )
    x, y = value().field("x"), value().field("y")
    result = await rows.select({"floor": x // y, "mod": x % y}).collect()
    assert [(row["floor"], row["mod"]) for row in _records(result)] == [
        _truncated_division(a, b) for a, b in INT_PAIRS
    ]


async def test_float_floor_division_divides_and_modulo_takes_the_dividend_sign() -> None:
    """``//`` on floats is division: SQL reserves the integral form for integers, so ``7.5 // 2.0``
    is 3.75. ``%`` is the C remainder, signed like the dividend."""
    rows = Array.from_items(
        [((i,), {"x": x, "y": y}) for i, (x, y) in enumerate(FLOAT_PAIRS)],
        ("[i: int]", struct_of(x=float, y=float)),
    )
    x, y = value().field("x"), value().field("y")
    result = await rows.select({"true": x / y, "floor": x // y, "mod": x % y}).collect()
    # Compared by repr, which tells -0.0 from 0.0 where == does not.
    assert [repr((row["true"], row["floor"], row["mod"])) for row in _records(result)] == [
        repr((a / b, a / b, math.fmod(a, b))) for a, b in FLOAT_PAIRS
    ]


@pytest.mark.parametrize(
    ("operator", "reason"),
    [
        ("truediv", "NaN or an infinite"),
        ("floordiv", "contains nulls"),
        ("mod", "contains nulls"),
    ],
)
async def test_dividing_an_integer_by_zero_gives_a_value_no_array_can_hold(
    operator: str, reason: str
) -> None:
    """A zero divisor is SQL's to answer: ``/`` gives infinity and the integral operators give
    null. Neither belongs in an array of non-optional ints, so the query fails saying which."""
    rows = Array.from_items(
        [((1,), {"x": 4, "y": 2}), ((2,), {"x": 1, "y": 0})],
        ("[i: int]", struct_of(x=int, y=int)),
    )
    x, y = value().field("x"), value().field("y")
    expression = {"truediv": x / y, "floordiv": x // y, "mod": x % y}[operator]
    with pytest.raises(ArrayError, match=reason):
        await rows.select(expression).collect()


async def test_an_optional_column_keeps_the_null_an_integral_division_by_zero_gives() -> None:
    rows = Array.from_items(
        [((1,), {"x": 4, "y": 2}), ((2,), {"x": 1, "y": 0})],
        ("[i: int]", struct_of(x=optional(int), y=optional(int))),
    )
    x, y = value().field("x"), value().field("y")

    assert [row for _, row in (await rows.select(x // y).collect()).rows] == [2, None]


# ---- limits ----


async def test_a_limit_with_ties_works_whatever_the_fields_are_called() -> None:
    rows = Array.from_items(
        [((1,), {"__fxtr_rank": 5}), ((2,), {"__fxtr_rank": 5}), ((3,), {"__fxtr_rank": 9})],
        ("[i: int]", struct_of(**{"__fxtr_rank": int})),
    )
    result = await rows.limit(1, order_by=[asc(value().field("__fxtr_rank"))], ties="all").collect()
    assert list(result.keys()) == [(1,), (2,)]


def test_all_ties_needs_an_ordering_to_define_a_tie() -> None:
    rows = Array.from_items([((1,), 5), ((2,), 5)], ("[i: int]", int))
    with pytest.raises(QueryError, match="ties='all' needs an order_by"):
        rows.limit(1, ties="all")


async def test_order_by_accepts_one_key_or_several_and_bare_expressions() -> None:
    rows = Array.from_items(
        [((1,), {"a": 3, "b": 1}), ((2,), {"a": 1, "b": 2}), ((3,), {"a": 1, "b": 0})],
        ("[i: int]", struct_of(a=int, b=int)),
    )
    a, b = value().field("a"), value().field("b")
    assert list((await rows.limit(1, order_by=a).collect()).keys()) == [(2,)]
    assert list((await rows.limit(1, order_by=desc(a)).collect()).keys()) == [(1,)]
    assert list((await rows.limit(1, order_by=[a, b]).collect()).keys()) == [(3,)]
    with pytest.raises(QueryError, match="order_by takes query expressions"):
        rows.limit(1, order_by=["a"])  # type: ignore[list-item]


# ---- the order of group_by ----

ROWS_TYPE = ArrayType.of(("[run: int]", struct_of(harness=str, model=str)))
GROUPED_TYPE = ArrayType.of(("[harness: str, model: str]", struct_of(n=int)))


def grouped_rows() -> Array[Any]:
    return Array.from_records(
        [
            {"run": 1, "harness": "h1", "model": "m1"},
            {"run": 2, "harness": "h1", "model": "m2"},
            {"run": 3, "harness": "h0", "model": "m9"},
        ],
        ROWS_TYPE,
    )


def grouped(rows: Any) -> Any:
    return rows.aggregate(
        {"n": count()},
        group_by={"harness": value().field("harness"), "model": value().field("model")},
    )


def test_group_by_order_survives_the_wire() -> None:
    planned = grouped(PlannedQuery.over("rows", ROWS_TYPE))
    assert planned.type == GROUPED_TYPE
    decoded = decode_query(encode_query(planned.plan))
    assert decoded == planned.plan
    assert [name for name, _ in decoded.group_by] == ["harness", "model"]  # type: ignore[union-attr]


# ---- how a step's inputs arrive ----

COUNT_INPUT = ArrayType.of(("[i: int]", struct_of(x=int)))
received: dict[str, object] = {}


@step(name="tests.semantics.count_rows", inputs={"rows": COUNT_INPUT}, output=int)
async def count_rows(context: StepContext, rows: Annotated[Array[Any], "[i: int]"]) -> int:
    received["rows"] = rows
    return len(rows)


@step(name="tests.semantics.count_unannotated_rows", inputs={"rows": COUNT_INPUT}, output=int)
async def count_unannotated_rows(context: StepContext, rows) -> int:  # type: ignore[no-untyped-def]
    received["rows"] = rows
    return len(rows)


@step(name="tests.semantics.count_any_rows", inputs={"rows": COUNT_INPUT}, output=int)
async def count_any_rows(context: StepContext, rows: Any) -> int:
    received["rows"] = rows
    return len(list(rows.values()))


@pytest.mark.parametrize(
    ("counting_step", "expected"),
    [
        (count_rows, Array),
        # A body that says nothing was written for arrays, and keeps getting one.
        (count_unannotated_rows, Array),
        (count_any_rows, Array),
    ],
)
async def test_an_input_arrives_the_way_its_parameter_is_annotated(
    counting_step: Any, expected: type, execute: Executor
) -> None:
    @workflow(name=f"{counting_step.name}.report", inputs={"rows": COUNT_INPUT}, output=int)
    async def report(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[int]:
        return context.run_step("count", counting_step, {"rows": rows})

    rows = Array.from_records([{"i": i, "x": i} for i in range(4)], COUNT_INPUT)
    record = await execute(report, counting_step, inputs={"rows": rows})
    assert record.result.item() == 4
    assert isinstance(received["rows"], expected)


# ---- sel and rename, which Array already has, on lazy queries ----

SCORES_TYPE = ArrayType.of(("[harness: str, run: int]", struct_of(run=int, score=float)))


def scores() -> Array[Any]:
    return Array.from_items(
        [
            (("inspect", 1), {"run": 101, "score": 0.5}),
            (("inspect", 2), {"run": 102, "score": 0.75}),
            (("swe-agent", 1), {"run": 201, "score": 0.25}),
        ],
        SCORES_TYPE,
    )


@pytest.mark.parametrize("group", [literal("all"), literal(1)])
async def test_a_literal_can_be_the_group_key(group: Any) -> None:
    grouped = await scores().aggregate({"runs": count()}, group_by={"g": group}).collect()
    assert list(grouped.raw_values()) == [{"runs": 3}]


@pytest.mark.parametrize(
    "key",
    [
        {"harness": "inspect"},
        # Dropping the dim ``run`` leaves the value field ``run`` as the only column of that name.
        {"run": 1},
        {"harness": "swe-agent", "run": 1},
        {"harness": "no such harness"},
        {},
    ],
)
async def test_sel_keeps_the_matching_rows_and_drops_their_dims(key: dict[str, Any]) -> None:
    rows = scores()
    names = rows.dim_names
    kept = [
        (tuple(k for name, k in zip(names, keys, strict=True) if name not in key), row_value)
        for keys, row_value in rows.raw_rows
        if all(keys[names.index(name)] == wanted for name, wanted in key.items())
    ]
    expected = Array.from_items(kept, sel_type(rows.type, key))
    assert await rows.sel(key).collect() == expected


async def test_sel_to_a_scalar_with_no_matching_row_is_an_error() -> None:
    with pytest.raises(ArrayError):
        await scores().sel({"harness": "inspect", "run": 9}).collect()


@pytest.mark.parametrize(
    "key,error",
    [
        ({"model": "gpt"}, ArrayTypeError),
        ({"run": "1"}, ArrayTypeError),
        ({"harness": 1}, ArrayTypeError),
        # Not what a dim key can be, though 1.0 == 1 and True == 1 in Python: the wire form of a
        # key does not accept them, and every path that builds a sel has to agree.
        ({"run": True}, QueryError),
        ({"run": 1.0}, QueryError),
        ({"run": None}, QueryError),
        ({"run": 2**70}, QueryError),
    ],
)
def test_lazy_sel_checks_its_key_when_the_query_is_built(
    key: dict[str, Any], error: type[Exception]
) -> None:
    with pytest.raises(error):
        scores().query().sel(key)


async def test_rename_relabels_the_dims_and_later_steps_see_the_new_name() -> None:
    names = {"run": "attempt", "harness": "tool"}
    renamed = scores().rename(names)

    expected = Array.from_items(scores().raw_rows, rename_type(scores().type, names))
    assert await renamed.collect() == expected
    kept = await renamed.filter(dim("attempt") == 2).collect()
    assert list(kept.keys()) == [("inspect", 2)]


def test_lazy_rename_rejects_names_duckdb_cannot_tell_apart() -> None:
    with pytest.raises(ArrayTypeError):
        scores().query().rename({"model": "m"})
    with pytest.raises(ArrayTypeError):
        scores().query().rename({"run": "harness"})
    with pytest.raises(QueryError, match="differ only by case|ignoring case"):
        scores().query().rename({"run": "Harness"})


def test_sel_and_rename_survive_the_wire() -> None:
    planned = (
        PlannedQuery.over("rows", SCORES_TYPE)
        .sel({"harness": "inspect"})
        .rename({"run": "attempt"})
    )

    assert decode_query(encode_query(planned.plan)) == planned.plan


@pytest.mark.parametrize(
    "node",
    [
        {"op": "sel", "key": {"run": True}},
        {"op": "sel", "key": {"run": 1.0}},
        {"op": "sel", "key": {"run": None}},
        {"op": "sel", "key": [["run", 1]]},
        {"op": "rename", "names": {"run": 1}},
        {"op": "rename", "names": ["run"]},
        {"op": "rename", "names": {"run": "attempt"}, "extra": 1},
    ],
)
def test_malformed_sel_and_rename_wire_nodes_are_rejected(node: dict[str, Any]) -> None:
    data = {"input": {"op": "source", "index": 0}} | node

    with pytest.raises(QueryError):
        decode_query(codec.encode(cast("Any", data)))


@pytest.mark.parametrize("kind", [{}, []])
def test_a_wire_type_with_a_malformed_dim_kind_is_refused(kind: object) -> None:
    wire = {"dims": [["i", kind]], "schema": "int"}
    source = {"op": "source", "index": 0}
    for data in (
        {"op": "retype", "input": source, "type": wire},
        {
            "op": "sql",
            "inputs": {"t": source},
            "text": "SELECT 1",
            "parameters": {},
            "output_type": wire,
        },
    ):
        with pytest.raises(QueryError):
            decode_query(codec.encode(cast("Any", data)))


def test_a_plan_is_refused_unless_its_encoding_is_canonical() -> None:
    """A query node's identity is the hash of its plan's bytes, so a plan written another way
    than :func:`encode_query` writes it would be another node for the same query."""
    planned = PlannedQuery.sql(
        "SELECT dims, value FROM t WHERE value.score > $minimum",
        tables={"t": PlannedQuery.over(0, SCORES_TYPE)},
        output_type=SCORES_TYPE,
        parameters={"minimum": 0.5},
    )
    canonical = codec.decode(encode_query(planned.plan))
    assert decode_query(codec.encode(canonical)) == planned.plan
    renamed = dict(cast("dict[str, Any]", canonical))
    renamed["parameters"] = {"MINIMUM": 0.5}  # decodes to the same plan, under a folded name
    with pytest.raises(QueryError, match="not canonical"):
        decode_query(codec.encode(cast("Any", renamed)))


# ---- what a workflow's handle does ----

handle_reports: dict[str, object] = {}


@step(name="tests.semantics.count_inspect", inputs={"rows": SCORES_TYPE}, output=int)
async def count_inspect(
    context: StepContext, rows: Annotated[Array[Any], "[harness: str, run: int]"]
) -> int:
    return len(await rows.sel({"harness": "inspect"}).collect())


@workflow(name="tests.semantics.handle_in_a_workflow", inputs={"rows": SCORES_TYPE}, output=int)
async def handle_in_a_workflow(
    context: WorkflowContext, rows: ArrayHandle[Any]
) -> ArrayHandle[int]:
    # Each builds and type-checks a query; none is collected, so none becomes a node or runs.
    for name, build in {
        "filter": lambda: rows.query().filter(dim("run") == 1),
        "select": lambda: rows.query().select(value().field("score")),
        "limit": lambda: rows.query().limit(1),
        "aggregate": lambda: rows.query().aggregate({"n": count()}),
        "join": lambda: rows.query().join(rows, on=[(dim("run"), dim("run"))]),
        "fxtr.sql": lambda: fxtr.sql(
            "SELECT dims, value FROM rows",
            tables={"rows": rows},
            output_type=SCORES_TYPE,
        ),
    }.items():
        handle_reports[f"workflow {name}"] = build().type
    sliced = rows.sel({"harness": "inspect"}).rename({"run": "attempt"}).field("score")
    handle_reports["workflow slice type"] = sliced.type
    # A collected query feeds a step without the workflow ever holding the rows.
    best = rows.query().filter(value().field("score") >= 0.5).collect()
    handle_reports["workflow queried"] = await best.observe()
    # Both calls are ones the overloads of fxtr.sql reject, so they are made untyped.
    untyped_sql = cast("Any", fxtr.sql)
    misuses: dict[str, dict[str, Any]] = {
        "mixed": {"tables": {"held": rows, "local": scores()}},
        "limits": {"tables": {"held": rows}, "limits": DuckDBExecutionLimits(max_result_rows=1)},
    }
    for name, arguments in misuses.items():
        try:
            untyped_sql("SELECT 1", **arguments)
        except QueryError as error:
            handle_reports[name] = error
    try:
        rows.join(cast("Any", scores()), on=[(dim("run"), dim("run"))])
    except QueryError as error:
        handle_reports["join with an array"] = error
    return context.run_step("count", count_inspect, {"rows": best})


async def test_a_workflow_body_queries_its_handles_as_dag_nodes(execute: Executor) -> None:
    handle_reports.clear()

    record = await execute(handle_in_a_workflow, count_inspect, inputs={"rows": scores()})

    assert record.result.item() == 2
    scored = scores()
    for operation in ("filter", "limit", "fxtr.sql"):
        assert handle_reports[f"workflow {operation}"] == scored.type
    assert cast("ArrayType", handle_reports["workflow select"]).dims == scored.dims
    assert cast("ArrayType", handle_reports["workflow aggregate"]).dims == ()
    # Joined on run, so the right's own harness dim survives under a prefixed name.
    joined = cast("ArrayType", handle_reports["workflow join"])
    assert [dimension.name for dimension in joined.dims] == ["harness", "run", "right_harness"]
    # The query ran on the host, and the step counted only the rows it kept.
    kept = Array.from_items(
        [(key, row) for key, row in scored.items() if row["score"] >= 0.5], scored.type
    )
    assert handle_reports["workflow queried"] == kept
    assert handle_reports["workflow slice type"] == ArrayType.of(("[attempt: int]", float))
    assert "all concrete arrays and local queries, or all handles" in str(handle_reports["mixed"])
    assert "a query over handles runs as a workflow node" in str(handle_reports["limits"])
    # Refused as the query is built, naming the array by its type and not by its rows.
    joined_with_array = str(handle_reports["join with an array"])
    assert "join partners must be handles or handle queries" in joined_with_array
    assert "a value of type Array" in joined_with_array


# ---- Python's and/or, which cannot be overloaded, never silently drop half a predicate ----

A, B = value().field("a"), value().field("b")


@pytest.mark.parametrize(
    "build",
    [
        lambda: (A == 1) and (B == 2),
        lambda: (A == 1) or (B == 2),
        lambda: (A != 1) and (B != 2),
        lambda: A == B == 2,
        lambda: not (A == 1),
        lambda: (A > 1) and (B == 2),
        lambda: 1 if A == 1 else 2,
    ],
)
def test_a_comparison_has_no_truth_value(build: Any) -> None:
    with pytest.raises(TypeError, match="combine predicates with '&', '\\|', and '~'"):
        build()


def test_membership_in_a_python_list_names_is_in() -> None:
    with pytest.raises(TypeError, match="test membership with is_in\\(\\), not 'in'"):
        _ = A in [1, 2]


def test_things_that_hold_expressions_still_compare_and_hash_by_structure() -> None:
    assert desc(A) == desc(value().field("a"))
    assert desc(A) != asc(A)
    assert A.sum() == value().field("a").sum()
    assert {desc(A), desc(value().field("a"))} == {desc(A)}
    rows = PlannedQuery.over("rows", ArrayType.of(("[i: int]", struct_of(a=int, b=int))))
    assert rows.select({"s": A + B}).plan == rows.select({"s": A + B}).plan
    assert rows.select({"s": A + B}).plan != rows.select({"s": A - B}).plan
    assert rows.aggregate({"n": count()}, group_by={"a": A}).plan == (
        rows.aggregate({"n": count()}, group_by={"a": A}).plan
    )


# ---- casts give Python's value, or raise ----

CASTS_TYPE = ArrayType.of(("[i: int]", struct_of(f=float, s=str, b=bytes, ok=bool)))


def casts() -> Array[Any]:
    return Array.from_items(
        [
            ((1,), {"f": 2.7, "s": "é", "b": "é".encode(), "ok": True}),
            ((2,), {"f": -2.5, "s": "\\x41", "b": b"plain", "ok": False}),
            ((3,), {"f": 3.5, "s": " 12 ", "b": b"", "ok": True}),
        ],
        CASTS_TYPE,
    )


async def test_casts_convert_as_sql_does_except_between_text_and_bytes() -> None:
    """``cast`` is SQL's conversion: a float rounds to the nearest integer and a half to the even
    one, numeric text parses, and a bool writes as ``'true'``. Text and bytes are the exception,
    converting by UTF-8 rather than by the escape form SQL's cast between them writes."""
    f, s, b, ok = (value().field(name) for name in ("f", "s", "b", "ok"))
    result = (
        await casts()
        .select(
            {
                "rounded": f.cast(int),
                "utf8": s.cast(bytes),
                "text": b.cast(str),
                "word": ok.cast(str),
            }
        )
        .collect()
    )

    assert _records(result) == [
        {
            "rounded": round(row["f"]),
            "utf8": row["s"].encode(),
            "text": row["b"].decode(),
            "word": "true" if row["ok"] else "false",
        }
        for row in _records(casts())
    ]


async def test_numeric_text_casts_to_an_integer_and_other_text_does_not() -> None:
    assert await casts().filter(dim("i") == 3).select(value().field("s").cast(int)).collect() == (
        Array.from_items([((3,), 12)], ("[i: int]", int))
    )
    assert await casts().filter(dim("i") == 3).select(literal("1.7").cast(int)).collect() == (
        Array.from_items([((3,), 2)], ("[i: int]", int))
    )
    for text in ("twelve", ""):
        with pytest.raises(QueryError, match="Could not convert string"):
            await casts().select(literal(text).cast(int)).collect()


async def test_bytes_that_are_not_utf8_do_not_cast_to_text() -> None:
    broken = Array.from_items([((1,), b"\xff\x00")], ("[i: int]", bytes))

    with pytest.raises(QueryError, match="UTF8"):
        await broken.select(value().cast(str)).collect()


# ---- literals ----


async def test_int_literals_are_64_bit() -> None:
    one_row = Array.from_items([((1,), 0)], ("[i: int]", int))

    result = await one_row.select(
        {
            "product": literal(60_000) * 60_000,
            "branch": when(value() == 0).then(100_000).otherwise(0) * 100_000,
            "filled": literal(None, optional(int)).fill_null(2_000_000_000) + 2_000_000_000,
        }
    ).collect()

    assert _records(result) == [
        {"product": 3_600_000_000, "branch": 10_000_000_000, "filled": 4_000_000_000}
    ]


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf"), 2**63, -(2**63) - 1])
def test_a_literal_no_array_could_hold_is_refused_when_the_query_is_built(number: Any) -> None:
    with pytest.raises(QueryError, match="query literal"):
        _ = value() < number
    with pytest.raises(QueryError, match="query literal"):
        value().is_in([1, number])


def test_a_limit_count_fits_64_bits() -> None:
    rows = Array.from_items([((1,), 0)], ("[i: int]", int))

    assert rows.limit(2**63 - 1).type == rows.type
    with pytest.raises(QueryError, match="non-negative 64-bit int"):
        rows.limit(2**63)


async def test_a_result_that_is_not_a_finite_number_says_the_query_produced_it() -> None:
    rows = Array.from_items([((1,), 1e300)], ("[i: int]", float))

    for expression in (value() / 1e-10, value() * 1e300, (-value()) ** 0.5):
        with pytest.raises(
            ArrayError, match="query's rows are not a valid array.*NaN or an infinite"
        ):
            await rows.select(expression).collect()


# ---- types a query over pairs can be declared as ----

PAIRS_TYPE = ArrayType.of(("[a: int, b: int]", struct_of(i=int)))
DECLARED_TYPES = {
    "the query's own type": PAIRS_TYPE,
    "dims in another order": ArrayType.of(("[b: int, a: int]", struct_of(i=int))),
    "a nullable field": ArrayType.of(("[a: int, b: int]", struct_of(i=optional(int)))),
    "a nullable value": ArrayType.of(("[a: int, b: int]", optional(struct_of(i=int)))),
}


def pairs() -> Array[Any]:
    return Array.from_items(
        [((1, 10), {"i": 5}), ((1, 20), {"i": -5}), ((2, 10), {"i": 7})], PAIRS_TYPE
    )


def test_retype_survives_the_wire_and_refuses_a_type_the_query_does_not_fit() -> None:
    rows = PlannedQuery.over("rows", PAIRS_TYPE)
    planned = rows.retype(DECLARED_TYPES["dims in another order"])

    assert planned.type == DECLARED_TYPES["dims in another order"]
    assert decode_query(encode_query(planned.plan)) == planned.plan
    with pytest.raises(ArrayTypeError, match="Cannot retype"):
        rows.retype(ArrayType.of(("[a: int, b: int]", struct_of(i=str))))
    with pytest.raises(QueryError):
        decode_query(
            codec.encode(
                cast("Any", {"op": "retype", "input": {"op": "source", "index": 0}, "type": 5})
            )
        )


# ---- small misuses say what to write ----

RUNS_TYPE = ArrayType.of(("[run: int]", struct_of(model=str, score=float)))
SCORE, MODEL = value().field("score"), value().field("model")


def runs() -> Array[Any]:
    return Array.from_items(
        [((1,), {"model": "a", "score": 0.9}), ((2,), {"model": "b", "score": 0.4})], RUNS_TYPE
    )


def untyped_runs() -> Any:
    """``runs()`` for calls the type checker would reject, which is what these tests make."""
    return runs()


@pytest.mark.parametrize(
    "build,message",
    [
        (lambda: untyped_runs().aggregate(count()), "aggregate\\(\\) takes a mapping"),
        (lambda: untyped_runs().aggregate({"m": SCORE}), "Aggregation 'm' must be count\\(\\) or"),
        (lambda: untyped_runs().aggregate({"n": count}), "Aggregation 'n' must be count\\(\\) or"),
        (lambda: untyped_runs().aggregate({"n": count()}, group_by=MODEL), "group_by a mapping"),
        (
            lambda: untyped_runs().aggregate({"n": count()}, group_by=["model"]),
            "group_by a mapping",
        ),
        (
            lambda: untyped_runs().aggregate({"n": count()}, group_by={"model": "model"}),
            "Group 'model' must be a query expression.*value\\(\\).field\\('model'\\)",
        ),
        (
            lambda: untyped_runs().select({"s": "score"}),
            "Selected field 's' must be a query expression.*value\\(\\).field\\('score'\\)",
        ),
        (
            lambda: untyped_runs().select({"one": 1}),
            "Selected field 'one'.*wrap a constant in literal",
        ),
        (lambda: untyped_runs().select(when(SCORE > 0.5).then(1)), "needs .otherwise"),
        (lambda: untyped_runs().select({"x": when(SCORE > 0.5).then(1)}), "needs .otherwise"),
        (lambda: untyped_runs().join(runs(), on=dim("run")), "Join keys are pairs"),
        (lambda: untyped_runs().join(runs(), on=["run"]), "Each join key is"),
        (
            lambda: untyped_runs().filter("score > 0.5"),
            "Expected a query predicate, got the text 'score > 0.5'",
        ),
        (
            lambda: untyped_runs().filter(runs()),
            "Expected a query predicate, got a value of type Array",
        ),
        (
            lambda: cast("Any", fxtr.sql)("SELECT 1", tables={"runs": [runs()]}),
            "all concrete arrays",
        ),
        (lambda: untyped_runs().filter(SCORE is None), "Test for null with is_null\\(\\)"),
        (
            lambda: untyped_runs().select({"mean": SCORE.mean()}),
            "Selected field 'mean': an aggregation .* result field of aggregate\\(\\)",
        ),
        (
            lambda: untyped_runs().filter(SCORE > cast("Any", SCORE.mean())),
            "an aggregation .* result field of aggregate\\(\\)",
        ),
        (lambda: untyped_runs().sel(cast("Any", "run")), "sel\\(\\) takes a mapping"),
        (lambda: untyped_runs().rename(cast("Any", "run")), "rename\\(\\) takes a mapping"),
        (
            lambda: untyped_runs().limit(1, order_by=cast("Any", "score")),
            "got 'score'; write value\\(\\).field\\('score'\\)",
        ),
    ],
)
def test_a_small_misuse_is_a_query_error_that_says_what_to_write(build: Any, message: str) -> None:
    with pytest.raises(QueryError, match=message) as failure:
        build()

    assert "raw_rows" not in str(failure.value)


async def test_one_join_key_needs_no_list() -> None:
    pair = (dim("run"), dim("run"))

    assert runs().join(runs(), on=pair).type == runs().join(runs(), on=[pair]).type


async def test_a_bool_expression_is_a_predicate() -> None:
    flagged = runs().select({"good": SCORE > 0.5, "score": SCORE})

    kept = await flagged.filter(value().field("good")).collect()

    assert list(kept.keys()) == [(1,)]
    picked = await flagged.select(
        when(value().field("good")).then("keep").otherwise("drop")
    ).collect()
    assert list(picked.raw_values()) == ["keep", "drop"]
    with pytest.raises(
        ArrayTypeError, match="A condition must be a bool expression, got one of type float"
    ):
        runs().filter(SCORE)


@pytest.mark.parametrize("declared", list(DECLARED_TYPES))
async def test_a_local_query_retypes_to_what_array_retype_gives(declared: str) -> None:
    target = DECLARED_TYPES[declared]
    kept = pairs().filter(value().field("i") > 0)

    retyped = await kept.retype(target).collect()

    assert retyped == (await kept.collect()).retype(target)
    assert retyped.type == target
    assert (await kept.expect_type(PAIRS_TYPE).collect()).type == PAIRS_TYPE
    with pytest.raises(ArrayTypeError):
        kept.expect_type(("[a: int, b: int]", struct_of(i=str)))
    with pytest.raises(ArrayTypeError, match="Cannot retype"):
        kept.retype(("[a: int]", struct_of(i=int)))


# ---- an earlier filter guards a later cast, however many rows there are ----


async def test_a_filter_protects_a_later_cast_on_a_large_array() -> None:
    # DuckDB reorders the conditions of a merged filter while it runs, which shows from about
    # 100,000 rows, unless it can see that one of them may raise. The arithmetic cases answer
    # null for the rows the guard removes, and are here for the count they give.
    count_of_rows = 120_000
    rows = Array.from_items(
        [
            ((k,), {"x": k, "y": k % 5, "s": str(k) if k % 5 else "n/a"})
            for k in range(count_of_rows)
        ],
        ("[i: int]", struct_of(x=int, y=int, s=str)),
    )
    x, y, s = (value().field(name) for name in ("x", "y", "s"))
    guarded = rows.filter(y != 0)

    for risky, python in [
        (x // y > 10, lambda k: k // (k % 5) > 10),
        (x % y == 1, lambda k: k % (k % 5) == 1),
        (x / y > 10, lambda k: k / (k % 5) > 10),
        (s.cast(int) > 10, lambda k: k > 10),
    ]:
        kept = (await guarded.filter(risky).aggregate({"n": count()}).field("n").collect()).item()
        assert kept == sum(1 for k in range(count_of_rows) if k % 5 and python(k))


async def test_an_untyped_null_casts_to_a_null_of_the_target_type() -> None:
    rows = Array.from_items([((1,), 5), ((2,), 6)], ("[i: int]", int))

    result = await rows.select(literal(None).cast(int)).collect()

    assert result.type == ArrayType.of(("[i: int]", optional(int)))
    assert list(result.raw_values()) == [None, None]
    assert list((await rows.filter(literal(None).cast(int) == 1).collect()).keys()) == []


# ---- bool expressions combine as predicates; expressions are not dict keys ----


async def test_a_bool_expression_combines_on_either_side_and_negates() -> None:
    flagged = runs().select({"good": SCORE > 0.5, "score": SCORE})
    good, score = value().field("good"), value().field("score")

    for predicate, expected in [
        (good & (score > 0.1), [(1,)]),
        ((score > 0.1) & good, [(1,)]),
        (good | (score > 0.1), [(1,), (2,)]),
        (~good, [(2,)]),
    ]:
        assert list((await flagged.filter(predicate).collect()).keys()) == expected


def test_an_expression_cannot_be_a_dict_key_because_it_could_not_be_found_again() -> None:
    with pytest.raises(TypeError, match="unhashable"):
        _ = {SCORE: 1}
    with pytest.raises(TypeError, match="unhashable"):
        _ = {SCORE + 1}


def test_plans_that_differ_only_in_the_type_or_sign_of_a_number_are_different_plans() -> None:
    rows = PlannedQuery.over("rows", RUNS_TYPE)

    assert rows.filter(SCORE > 0.0).plan != rows.filter(SCORE > -0.0).plan
    assert rows.select(literal(1) + SCORE).plan != rows.select(literal(1.0) + SCORE).plan
    lowest, also_lowest = SCORE.quantile(0), SCORE.quantile(0.0)
    assert rows.aggregate({"q": lowest}).plan == rows.aggregate({"q": also_lowest}).plan


def test_a_name_holding_nul_cannot_be_queried() -> None:
    with pytest.raises(QueryError, match="holds a NUL character"):
        scores().query().rename({"run": "run\x00id"})


# ---- fxtr.sql keeps what its tables were given ----


async def test_sql_runs_under_the_limits_a_table_was_given() -> None:
    limited = runs().query(limits=DuckDBExecutionLimits(max_result_rows=1))
    text = "SELECT dims, value FROM t"

    with pytest.raises(DuckDBResourceLimitError, match="exceeded 1 rows"):
        await fxtr.sql(text, tables={"t": limited}).collect()
    with pytest.raises(QueryError, match="exceeded 1 rows"):
        await fxtr.sql(
            "SELECT t.dims, t.value FROM t JOIN u USING (dims)",
            tables={"t": limited, "u": runs()},
            output_type=RUNS_TYPE,
        ).collect()
    generous = DuckDBExecutionLimits(max_result_rows=10)
    assert await fxtr.sql(text, tables={"t": limited}, limits=generous).collect() == runs()
    other = runs().query(limits=generous)
    with pytest.raises(QueryError, match="roots with different limits"):
        fxtr.sql(text, tables={"t": limited, "u": other}, output_type=RUNS_TYPE)


def test_sql_queries_of_one_array_share_its_connection() -> None:
    array = runs()

    queries = [fxtr.sql("SELECT dims, value FROM t", tables={"t": array}) for _ in range(3)]

    assert all(query._executor is array.query()._executor for query in queries)


# ---- `&` binds tighter than `==`: the mistake is reported, never answered with False ----


@pytest.mark.parametrize(
    "build",
    [
        lambda: when(SCORE > 0).then(value().field("ok") & SCORE == 1).otherwise(False),
        lambda: untyped_runs().filter(value().field("ok") | SCORE == 1),
        lambda: (SCORE > 1) == value().field("ok"),
        lambda: (SCORE > 1) != 1,
    ],
)
def test_a_predicate_compared_with_something_else_names_the_parentheses(build: Any) -> None:
    with pytest.raises(QueryError, match=r"write `a & \(b == 1\)`"):
        build()


def test_an_unparenthesized_comparison_inside_and_names_the_parentheses() -> None:
    with pytest.raises(QueryError, match="parenthesize it"):
        runs().filter(SCORE == 1 & SCORE == 2)  # type: ignore[operator]


# ---- limits follow a query into a join or SQL, and do not depend on anything else alive ----


async def test_limits_a_root_was_given_apply_on_either_side_of_a_join() -> None:
    limited = runs().query(limits=DuckDBExecutionLimits(max_result_rows=1))
    on = (dim("run"), dim("run"))

    for joined in (limited.join(runs(), on=on), runs().join(limited, on=on)):
        with pytest.raises(DuckDBResourceLimitError, match="exceeded 1 rows"):
            await joined.collect()


async def test_equal_limits_are_not_different_limits() -> None:
    one_row = DuckDBExecutionLimits(max_result_rows=1)
    shared = anyio.CapacityLimiter(1)
    text = "SELECT t.dims, t.value FROM t JOIN u USING (dims)"

    same_limits = {"t": runs().query(limits=one_row), "u": runs().query(limits=one_row)}
    with pytest.raises(DuckDBResourceLimitError, match="exceeded 1 rows"):
        await fxtr.sql(text, tables=same_limits, output_type=RUNS_TYPE).collect()
    same_limiter = {
        "t": runs().query(query_limiter=shared),
        "u": runs().query(query_limiter=shared),
    }
    assert await fxtr.sql(text, tables=same_limiter, output_type=RUNS_TYPE).collect() == runs()


async def test_a_query_without_limits_stays_one_after_its_array_is_collected_as_garbage() -> None:
    array = runs()
    query = array.filter(SCORE > 0)
    await query.collect()
    derived = query.filter(SCORE > 0.5)
    del array, query
    gc.collect()
    limited = runs().query(limits=DuckDBExecutionLimits(max_result_rows=100))

    result = await fxtr.sql(
        "SELECT t.dims, t.value FROM t JOIN u USING (dims)",
        tables={"t": derived, "u": limited},
        output_type=RUNS_TYPE,
    ).collect()

    assert list(result.keys()) == [(1,)]


def test_building_sql_queries_while_other_threads_query_new_arrays() -> None:
    target = runs()
    failures: list[BaseException] = []
    stop = threading.Event()

    def churn() -> None:
        while not stop.is_set():
            runs().query()

    def build() -> None:
        try:
            for _ in range(2000):
                fxtr.sql("SELECT dims, value FROM t", tables={"t": target})
        except BaseException as error:  # noqa: BLE001 - reported by the assertion below
            failures.append(error)

    churners = [threading.Thread(target=churn) for _ in range(2)]
    for thread in churners:
        thread.start()
    build()
    stop.set()
    for thread in churners:
        thread.join()

    assert failures == []


SHARED_ACROSS_LOOPS = Array.from_items([((k,), k) for k in range(50_000)], ("[i: int]", int))


def test_threads_with_their_own_event_loops_can_first_query_one_array_together() -> None:
    counts: list[object] = []

    async def count_rows() -> None:
        counts.append(
            (
                await SHARED_ACROSS_LOOPS.filter(value() >= 0).aggregate({"n": count()}).collect()
            ).item()
        )

    threads = [threading.Thread(target=anyio.run, args=(count_rows,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert counts == [{"n": 50_000}] * 4


# ---- an int against a float is compared through a double, as SQL compares them ----


async def test_an_int_compares_with_a_float_through_a_double() -> None:
    """SQL converts the integer to a double, so integers a double cannot tell apart compare
    equal above 2**53."""
    pairs = [(2**53, float(2**53)), (2**53 + 1, float(2**53)), (7, 7.5)]
    rows = Array.from_items(
        [((k,), {"i": i, "f": f}) for k, (i, f) in enumerate(pairs)],
        ("[k: int]", struct_of(i=int, f=float)),
    )
    i, f = value().field("i"), value().field("f")

    async def kept(predicate: Any) -> set[Any]:
        return {key[0] for key in (await rows.filter(predicate).collect()).keys()}

    assert await kept(i == f) == {0, 1}
    assert await kept(i < f) == {2}
    assert await kept(i > f) == set()
    assert await kept(f > i) == {2}
    assert await kept(i.is_in([float(2**53)])) == {0, 1}
    assert await kept(i.between(float(2**53), float(2**53))) == {0, 1}


async def test_a_null_operand_keeps_an_int_float_comparison_null() -> None:
    rows = Array.from_items(
        [((1,), {"i": None, "f": 0.5}), ((2,), {"i": 1, "f": None}), ((3,), {"i": 1, "f": 0.5})],
        ("[k: int]", struct_of(i=optional(int), f=optional(float))),
    )
    i, f = value().field("i"), value().field("f")

    assert list((await rows.filter(i == f).collect()).keys()) == []
    assert list((await rows.filter(~(i == f)).collect()).keys()) == [(3,)]
    assert list((await rows.filter((i == f).is_null()).collect()).keys()) == [(1,), (2,)]


# ---- a query runs once, and its SQL is bounded ----


async def test_a_query_with_a_bound_parameter_runs_once() -> None:
    rows = Array.from_items([((1,), 5)], ("[i: int]", int))
    # Slow enough that running it a second or third time, to learn its columns, shows as time.
    slow = (
        "SELECT dims, (SELECT count(*) FROM range(40000000) r(i) WHERE i % 7 = {m})::BIGINT"
        " AS value FROM t"
    )

    started = time.perf_counter()
    written_in = await fxtr.sql(slow.format(m="3"), tables={"t": rows}).collect()
    without_parameter = time.perf_counter() - started
    started = time.perf_counter()
    bound = await fxtr.sql(slow.format(m="$m"), tables={"t": rows}, parameters={"m": 3}).collect()
    with_parameter = time.perf_counter() - started

    assert bound == written_in
    assert with_parameter < 2 * without_parameter + 0.1


async def test_nested_casts_and_betweens_write_each_operand_once() -> None:
    texts = Array.from_items([((1,), "7")], ("[i: int]", str))
    casts = value()
    for _ in range(40):
        casts = casts.cast(int).cast(str)
    numbers = Array.from_items([((1,), 1)], ("[i: int]", int))
    betweens = value()
    for _ in range(40):
        betweens = betweens.between(0, 1).cast(int)

    for rows, expression in ((texts, casts), (numbers, betweens)):
        query = rows.select(expression)
        compiled = compile_array_query(query._query.plan, (rows.type,), ("source",))
        # Forty levels, each writing its operand twice, would be 2**40 copies of it.
        assert len(compiled.sql) < 200_000
        assert await query.collect() == rows


async def test_an_is_in_mixing_ints_and_floats_writes_its_operand_once() -> None:
    rows = Array.from_items([((1,), 3), ((2,), 4)], ("[i: int]", int))
    nested = value()
    for _ in range(20):
        nested = nested.cast(str).cast(int)
    options = [float(option) + 0.5 for option in range(2_000)] + [3]
    query = rows.filter(nested.is_in(options))

    compiled = compile_array_query(query._query.plan, (rows.type,), ("source",))
    assert len(compiled.sql) < 200_000
    assert list((await query.collect()).keys()) == [(1,)]


async def test_is_in_compares_numbers_of_either_kind_in_one_in_list() -> None:
    rows = Array.from_items(
        [((1,), {"n": 2, "f": 2.0}), ((2,), {"n": 3, "f": 2.5}), ((3,), {"n": None, "f": None})],
        ("[i: int]", struct_of(n=optional(int), f=optional(float))),
    )
    n, f = value().field("n"), value().field("f")

    async def kept(predicate: Any) -> list[Any]:
        return [key[0] for key in (await rows.filter(predicate).collect()).keys()]

    assert await kept(n.is_in([2.0, 2.5, 1e30])) == [1]
    assert await kept(f.is_in([2, 2**53 + 1])) == [1]
    assert await kept(n.is_in([2.5])) == []
    # No int is 2.5, and a null operand still gives null, not false.
    assert await kept(~n.is_in([2.5])) == [1, 2]
    many = compile_array_query(
        rows.filter(n.is_in([float(k) for k in range(20_000)]))._query.plan, (rows.type,), ("s",)
    )
    assert " IN (" in many.sql and "list_transform" not in many.sql


def test_a_string_literal_that_is_not_unicode_text_is_refused_when_the_query_is_built() -> None:
    with pytest.raises(QueryError, match="valid Unicode text"):
        literal("\ud800")
    with pytest.raises(QueryError, match="valid Unicode text"):
        _ = value() == "caf\udce9"


# ---- an operator is one of the allowed strings, not something that prints like one ----


def test_an_object_that_prints_like_an_operator_is_not_one() -> None:
    class PrintsAsAdd:
        def __repr__(self) -> str:
            return "'add'"

    with pytest.raises(QueryError, match="Unknown arithmetic operator"):
        Arithmetic(SCORE, cast("Any", PrintsAsAdd()), SCORE)
    assert Arithmetic(SCORE, "add", SCORE).operator == "add"


# ---- a workflow's queries run in the Postgres runner, not on a remote worker ----

RUNS_TYPE = ArrayType.of(("[run: int]", struct_of(model=str, score=float)))
COUNTED_TYPE = ArrayType.of(("[model: str]", struct_of(n=int)))


@workflow(name="tests.semantics.counted_by_model", inputs={"rows": RUNS_TYPE}, output=COUNTED_TYPE)
async def counted_by_model(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[Any]:
    kept = rows.query().filter(value().field("score") >= 0.5)
    return kept.aggregate({"n": count()}, group_by={"model": value().field("model")}).collect()


def model_runs() -> Array[Any]:
    return Array.from_records(
        [
            {"run": 1, "model": "a", "score": 0.9},
            {"run": 2, "model": "a", "score": 0.1},
            {"run": 3, "model": "b", "score": 0.7},
        ],
        RUNS_TYPE,
    )


async def test_a_workflow_queries_its_handles_without_running_a_step(execute: Executor) -> None:
    record = await execute(counted_by_model, inputs={"rows": model_runs()})

    assert record.result == Array.from_items([(("a",), {"n": 1}), (("b",), {"n": 1})], COUNTED_TYPE)
    assert record.steps == {}


async def test_a_workflow_query_on_a_remote_worker_fails_the_job_by_name() -> None:
    async with postgres_backend(remote=True) as execute_remotely:
        with pytest.raises(JobDidNotSucceed, match="QueryNotSupportedError"):
            await execute_remotely(counted_by_model, inputs={"rows": model_runs()})


def test_a_step_parameter_annotated_as_a_handle_is_refused_when_the_step_is_defined() -> None:
    with pytest.raises(TypeError, match="a step receives concrete arrays"):

        @step()
        async def takes_a_handle(
            context: StepContext, rows: Annotated[ArrayHandle[int], "[i: int]"]
        ) -> int:
            return 0
