"""Typed queries over concrete struct arrays."""

from __future__ import annotations

from typing import Any, cast

import duckdb
import pytest
from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ArrayType, ArrayTypeError
from fxtr.entity_defns.query import (
    Aggregation,
    Arithmetic,
    Boolean,
    Cast,
    Column,
    Comparison,
    JoinKey,
    LiteralExpression,
    QueryError,
    UnaryArithmetic,
    as_join_keys,
    count,
    desc,
    dim,
    literal,
    value,
    when,
)
from fxtr.entity_defns.schema import optional, struct_of

ROWS_TYPE = (
    "[run: int]",
    struct_of(
        model=str,
        score=float,
        tokens=int,
        error=optional(str),
        meta=struct_of(rank=int),
    ),
)
MODEL_TYPE = ("[model: str]", struct_of(entity=EntityID, family=str))
RUN_TYPE = ("[run: int]", struct_of(entity=EntityID, score=float))


def rows_array() -> Array[Any]:
    return Array.from_records(
        [
            {
                "run": 1,
                "model": "a",
                "score": 0.9,
                "tokens": 10,
                "error": None,
                "meta": {"rank": 2},
            },
            {
                "run": 2,
                "model": "a",
                "score": 0.9,
                "tokens": 20,
                "error": "bad",
                "meta": {"rank": 1},
            },
            {
                "run": 3,
                "model": "b",
                "score": 0.7,
                "tokens": 30,
                "error": None,
                "meta": {"rank": 3},
            },
        ],
        ROWS_TYPE,
    )


def join_inputs() -> tuple[Array[Any], Array[Any]]:
    entity_id = EntityID(
        cid=codec.compute_cid(
            codec.encode(frozendict({"$type": "fxtr.builtin.testing.node", "name": "a"}))
        )
    )
    other_entity_id = EntityID(
        cid=codec.compute_cid(
            codec.encode(frozendict({"$type": "fxtr.builtin.testing.node", "name": "b"}))
        )
    )
    models = Array.from_records(
        [
            {"model": "a", "entity": entity_id, "family": "x"},
            {
                "model": "b",
                "entity": other_entity_id,
                "family": "y",
            },
        ],
        MODEL_TYPE,
    )
    runs = Array.from_records(
        [
            {"run": 1, "entity": entity_id, "score": 0.8},
            {"run": 2, "entity": entity_id, "score": 0.9},
        ],
        RUN_TYPE,
    )
    return models, runs


@pytest.mark.anyio
async def test_filter_boolean_comparisons_and_nulls() -> None:
    result = (
        await rows_array()
        .query()
        .filter((value().field("score") >= 0.8) & value().field("error").is_null())
        .collect()
    )
    assert list(result.keys()) == [(1,)]
    assert result.type == ArrayType.of(ROWS_TYPE)

    with pytest.raises(ArrayTypeError, match="Cannot compare"):
        rows_array().query().filter(value().field("score") == "high")
    with pytest.raises(ArrayTypeError, match="Ordering comparison"):
        rows_array().query().filter(literal(True) > literal(False))


@pytest.mark.anyio
async def test_strings_order_the_same_way_in_comparisons_sorting_and_min_max() -> None:
    rows = rows_array().query()
    later = await rows.filter(value().field("model") > "a").collect()
    assert list(later.keys()) == [(3,)]
    first = await rows.limit(1, order_by=[desc(value().field("model"))]).collect()
    assert list(first.keys()) == [(3,)]
    greatest = (await rows.aggregate({"model": value().field("model").max()}).collect()).item()
    assert greatest == {"model": "b"}


def test_query_expressions_have_structural_equality_for_dag_records() -> None:
    assert (value().field("score") >= 0.8) == (value().field("score") >= 0.8)
    assert ((value().field("score") >= 0.8) & value().field("error").is_null()) == (
        (value().field("score") >= 0.8) & value().field("error").is_null()
    )
    assert desc(value().field("score")) == desc(value().field("score"))
    assert value().field("score").mean() == value().field("score").mean()
    assert value().field("score").quantile(0.9) == value().field("score").quantile(0.9)
    assert value().field("score").median() == value().field("score").quantile(0.5)
    assert JoinKey(value().field("entity"), value().field("entity")) == JoinKey(
        value().field("entity"), value().field("entity")
    )


def test_query_nodes_reject_unknown_operations() -> None:
    predicate = value().field("score") >= 0.8

    with pytest.raises(QueryError, match="Unknown comparison operator 'contains'"):
        Comparison(value().field("score"), cast("Any", "contains"), value().field("tokens"))
    with pytest.raises(QueryError, match="Unknown Boolean operator 'xor'"):
        Boolean(predicate, cast("Any", "xor"), predicate)
    with pytest.raises(QueryError, match="Unknown aggregate function 'mode'"):
        Aggregation(cast("Any", "mode"), value().field("score"))
    with pytest.raises(QueryError, match="Unknown arithmetic operator 'concat'"):
        Arithmetic(value().field("tokens"), cast("Any", "concat"), value().field("tokens"))
    with pytest.raises(QueryError, match="Unknown unary arithmetic operator 'absolute'"):
        UnaryArithmetic(cast("Any", "absolute"), value().field("tokens"))
    with pytest.raises(QueryError, match="Unknown cast target 'id'"):
        Cast(value().field("tokens"), cast("Any", "id"))
    with pytest.raises(QueryError, match="Expected a query predicate, got the text 'tokens'"):
        when(cast("Any", "tokens"))


@pytest.mark.parametrize("quantile", [-0.1, 1.1, float("nan"), True, None])
def test_quantile_rejects_invalid_probabilities(quantile: object) -> None:
    with pytest.raises(QueryError, match="Quantile must be a number from 0 to 1"):
        value().field("score").quantile(cast("Any", quantile))


@pytest.mark.anyio
async def test_filter_selects_allowed_pairs_from_a_cartesian_product() -> None:
    models = Array.from_items([(("small",), "s"), (("large",), "l")], ("[model: str]", str))
    harnesses = Array.from_items([(("fast",), "f"), (("deep",), "d")], ("[harness: str]", str))
    pairs = Array.combine({"model_value": models, "harness_value": harnesses})
    allowed = (
        await pairs.query()
        .filter(
            ((dim("model") == "small") & (dim("harness") == "fast"))
            | ((dim("model") == "large") & (dim("harness") == "deep"))
        )
        .collect()
    )
    assert allowed.dim_names == ("model", "harness")
    assert list(allowed.keys()) == [("large", "deep"), ("small", "fast")]


@pytest.mark.anyio
async def test_select_dimensions_and_fixed_nested_fields() -> None:
    result = (
        await rows_array()
        .query()
        .select(
            {
                "coordinate": dim("run"),
                "model_name": value().field("model"),
                "rank": value().field("meta").field("rank"),
            }
        )
        .collect()
    )
    assert result.type == ArrayType.of(
        ("[run: int]", struct_of(coordinate=int, model_name=str, rank=int))
    )
    assert result[(2,)] == frozendict({"coordinate": 2, "model_name": "a", "rank": 1})


@pytest.mark.anyio
async def test_select_expression_replaces_the_complete_value() -> None:
    rows = rows_array()
    scores = await rows.select(value().field("score")).collect()
    assert scores.type == ArrayType.of(("[run: int]", float))
    assert scores.raw_rows == (((1,), 0.9), ((2,), 0.9), ((3,), 0.7))

    coordinates = await rows.select(dim("run")).collect()
    assert coordinates.type == ArrayType.of(("[run: int]", int))
    assert coordinates.raw_rows == (((1,), 1), ((2,), 2), ((3,), 3))


@pytest.mark.anyio
async def test_select_expression_preserves_struct_values_and_identity() -> None:
    rows = rows_array()
    metadata = await rows.select(value().field("meta")).collect()
    assert metadata.type == ArrayType.of(("[run: int]", struct_of(rank=int)))
    assert metadata.raw_rows == (
        ((1,), frozendict(rank=2)),
        ((2,), frozendict(rank=1)),
        ((3,), frozendict(rank=3)),
    )

    identity = await rows.select(value()).collect()
    assert identity == rows
    assert identity.content_id() == rows.content_id()


@pytest.mark.anyio
async def test_computed_values_select_arithmetic_casts_nulls_and_conditionals() -> None:
    score = value().field("score")
    tokens = value().field("tokens")
    error = value().field("error")
    result = (
        await rows_array()
        .select(
            {
                "percent": score * 100,
                "ratio": tokens / 4,
                "offset": 100 - tokens,
                "bucket": when(score.between(0.8, 1.0)).then("high").otherwise("low"),
                "size": when(tokens < 15)
                .then("small")
                .when(tokens < 25)
                .then("medium")
                .otherwise("large"),
                "error": error.fill_null("none"),
                "tokens_text": tokens.cast(str),
                "floor": tokens // 12,
                "remainder": tokens % 12,
                "squared": tokens**2,
                "negative": -tokens,
            }
        )
        .collect()
    )

    assert result.type == ArrayType.of(
        (
            "[run: int]",
            struct_of(
                percent=float,
                ratio=float,
                offset=int,
                bucket=str,
                size=str,
                error=str,
                tokens_text=str,
                floor=int,
                remainder=int,
                squared=float,
                negative=int,
            ),
        )
    )
    assert result[(1,)] == {
        "percent": 90.0,
        "ratio": 2.5,
        "offset": 90,
        "bucket": "high",
        "size": "small",
        "error": "none",
        "tokens_text": "10",
        "floor": 0,
        "remainder": 10,
        "squared": 100.0,
        "negative": -10,
    }
    assert result[(3,)]["bucket"] == "low"
    assert [result[(run,)]["size"] for run in (1, 2, 3)] == ["small", "medium", "large"]


@pytest.mark.anyio
async def test_predicates_are_selectable_and_keep_standard_null_behavior() -> None:
    error = value().field("error")
    selected = (
        await rows_array()
        .select(
            {
                "equals_bad": error == "bad",
                "listed": error.is_in(["bad", None]),
                "score_range": value().field("score").between(0.8, 0.9),
            }
        )
        .collect()
    )
    assert selected.type == ArrayType.of(
        (
            "[run: int]",
            struct_of(
                equals_bad=optional(bool),
                listed=optional(bool),
                score_range=bool,
            ),
        )
    )
    assert selected.raw_rows == (
        ((1,), frozendict(equals_bad=None, listed=None, score_range=True)),
        ((2,), frozendict(equals_bad=True, listed=True, score_range=True)),
        ((3,), frozendict(equals_bad=None, listed=None, score_range=False)),
    )

    # Negation preserves null. The outer filter discards null and false predicates.
    assert len(await rows_array().filter(~(error == "bad")).collect()) == 0
    assert len(await rows_array().filter(error.is_in([])).collect()) == 0


@pytest.mark.anyio
async def test_computed_expressions_work_in_aggregation_grouping_and_ordering() -> None:
    score = value().field("score")
    tokens = value().field("tokens")
    grouped = (
        await rows_array()
        .aggregate(
            {
                "adjusted_tokens": (tokens + 1).sum(),
                "mean_percent": (score * 100).mean(),
            },
            group_by={"bucket": when(score >= 0.8).then("high").otherwise("low")},
        )
        .collect()
    )
    assert grouped.type == ArrayType.of(
        (
            "[bucket: str]",
            struct_of(adjusted_tokens=int, mean_percent=optional(float)),
        )
    )
    assert grouped[("high",)] == {"adjusted_tokens": 32, "mean_percent": 90.0}
    assert grouped[("low",)] == {"adjusted_tokens": 31, "mean_percent": 70.0}

    ordered = await rows_array().limit(1, order_by=[desc(-tokens)]).collect()
    assert list(ordered.keys()) == [(1,)]


@pytest.mark.anyio
async def test_literal_can_give_a_standalone_null_an_explicit_type() -> None:
    result = await rows_array().select(literal(None, optional(int))).collect()
    assert result.type == ArrayType.of(("[run: int]", optional(int)))
    assert result.raw_rows == (((1,), None), ((2,), None), ((3,), None))


@pytest.mark.anyio
async def test_numeric_expressions_promote_types_and_track_nullability() -> None:
    rows = Array.from_items([((1,), None), ((2,), 2)], ("[row: int]", optional(int)))
    result = await rows.select(
        {
            "scaled": value() * 0.5,
            "filled": value().fill_null(0.5),
            "conditional": when(dim("row") == 1).then(value()).otherwise(1.5),
        }
    ).collect()
    assert result.type == ArrayType.of(
        (
            "[row: int]",
            struct_of(
                scaled=optional(float),
                filled=float,
                conditional=optional(float),
            ),
        )
    )
    assert result.raw_rows == (
        ((1,), frozendict(conditional=None, filled=0.5, scaled=None)),
        ((2,), frozendict(conditional=1.5, filled=2.0, scaled=1.0)),
    )


def test_computed_expression_type_errors_are_reported_before_execution() -> None:
    rows = rows_array()
    with pytest.raises(ArrayTypeError, match="Arithmetic left operand must be an int or float"):
        rows.select(value().field("model") + 1)
    with pytest.raises(ArrayTypeError, match=r"cast\(\) requires a leaf value"):
        rows.select(value().field("meta").cast(str))
    with pytest.raises(ArrayTypeError, match=r"fill_null\(\) must have compatible types"):
        rows.select(value().field("error").fill_null(7))
    with pytest.raises(ArrayTypeError, match=r"is_in\(\) values must be scalar and compatible"):
        rows.filter(value().field("model").is_in([1]))
    with pytest.raises(ArrayTypeError, match="conditional branches must have compatible types"):
        rows.select(when(value().field("score") > 0).then("yes").otherwise(1))
    with pytest.raises(QueryError, match=r"is_in\(\) expects a list or tuple"):
        value().field("model").is_in(cast("Any", "ab"))
    with pytest.raises(QueryError, match=r"cast\(\) target must be"):
        value().field("tokens").cast(cast("Any", complex))


@pytest.mark.anyio
async def test_cast_raises_instead_of_replacing_invalid_values_with_null() -> None:
    rows = Array.from_items([((1,), "12"), ((2,), "not-an-int")], ("[row: int]", str))
    with pytest.raises(QueryError, match="Could not convert") as failure:
        await rows.select(value().cast(int)).collect()
    assert isinstance(failure.value.__cause__, duckdb.Error)
    # The author of a typed plan never wrote SQL, so the SQL fxtr generated stays out of it.
    assert "__fxtr_input" not in str(failure.value)
    assert "LINE 1" not in str(failure.value)


def test_select_expression_rejects_untyped_null_and_non_expressions() -> None:
    rows = rows_array()
    with pytest.raises(QueryError, match="cannot be an untyped null literal"):
        rows.select(LiteralExpression(None, None))
    with pytest.raises(QueryError, match="takes a query expression or a mapping"):
        rows.select(cast("Any", 7))


@pytest.mark.anyio
async def test_limit_has_deterministic_tie_behavior() -> None:
    first = await rows_array().query().limit(1, order_by=[desc(value().field("score"))]).collect()
    assert list(first.keys()) == [(1,)]

    all_ties = (
        await rows_array()
        .query()
        .limit(1, order_by=[desc(value().field("score"))], ties="all")
        .collect()
    )
    assert list(all_ties.keys()) == [(1,), (2,)]
    assert (await rows_array().query().limit(2).collect()).rows == rows_array().rows[:2]


@pytest.mark.anyio
async def test_global_and_grouped_aggregations() -> None:
    grouped = (
        await rows_array()
        .query()
        .aggregate(
            {
                "n": count(),
                "tokens": value().field("tokens").sum(),
                "average": value().field("score").mean(),
            },
            group_by={"model": value().field("model")},
        )
        .collect()
    )
    assert grouped.type == ArrayType.of(
        ("[model: str]", struct_of(n=int, tokens=int, average=optional(float)))
    )
    assert grouped[("a",)] == frozendict({"n": 2, "tokens": 30, "average": 0.9})
    assert grouped[("b",)] == frozendict({"n": 1, "tokens": 30, "average": 0.7})

    global_result = (
        await rows_array()
        .query()
        .aggregate({"n": count(), "average": value().field("score").mean()})
        .collect()
    )
    assert global_result.item() == frozendict({"n": 3, "average": pytest.approx(5 / 6)})

    empty = Array.from_items([], ("[run: int]", struct_of(score=float)))
    empty_result = (
        await empty.query()
        .aggregate(
            {
                "n": count(),
                "total": value().field("score").sum(),
                "average": value().field("score").mean(),
            }
        )
        .collect()
    )
    assert empty_result.item() == frozendict({"n": 0, "total": 0.0, "average": None})


@pytest.mark.anyio
async def test_expression_aggregations_have_explicit_null_and_empty_semantics() -> None:
    score = value().field("score")
    error = value().field("error")
    summary = (
        await rows_array()
        .aggregate(
            {
                "rows": count(),
                "scores": score.count(),
                "errors": error.count(),
                "unique_errors": error.n_unique(),
                "total": score.sum(),
                "mean": score.mean(),
                "minimum": score.min(),
                "maximum": score.max(),
                "std": score.std(),
                "variance": score.var(),
                "median": score.median(),
                "p25": score.quantile(0.25),
            }
        )
        .collect()
    ).item()

    assert summary["rows"] == 3
    assert summary["scores"] == 3
    assert summary["errors"] == 1
    assert summary["unique_errors"] == 2
    assert summary["total"] == 2.5
    assert summary["mean"] == pytest.approx(5 / 6)
    assert summary["minimum"] == 0.7
    assert summary["maximum"] == 0.9
    assert summary["std"] == pytest.approx(0.11547005383792519)
    assert summary["variance"] == pytest.approx(0.01333333333333334)
    assert summary["median"] == 0.9
    assert summary["p25"] == 0.8

    empty = Array.from_items([], ("[run: int]", optional(float)))
    empty_summary = (
        await empty.aggregate(
            {
                "rows": count(),
                "values": value().count(),
                "unique": value().n_unique(),
                "total": value().sum(),
                "minimum": value().min(),
                "maximum": value().max(),
                "std": value().std(),
                "variance": value().var(),
                "median": value().median(),
            }
        ).collect()
    ).item()
    assert empty_summary == frozendict(
        {
            "rows": 0,
            "values": 0,
            "unique": 0,
            "total": 0.0,
            "minimum": None,
            "maximum": None,
            "std": None,
            "variance": None,
            "median": None,
        }
    )

    null_only = Array.from_items([((1,), None)], ("[run: int]", optional(float)))
    assert (
        await null_only.aggregate(
            {"values": value().count(), "unique": value().n_unique()}
        ).collect()
    ).item() == {"values": 0, "unique": 1}


def test_expression_aggregations_reject_incompatible_types() -> None:
    rows = rows_array()
    with pytest.raises(ArrayTypeError, match="sum requires an int or float"):
        rows.aggregate({"bad": value().field("model").sum()})
    with pytest.raises(ArrayTypeError, match="std requires an int or float"):
        rows.aggregate({"bad": value().field("error").std()})
    with pytest.raises(ArrayTypeError, match="n_unique requires a scalar"):
        rows.aggregate({"bad": value().n_unique()})


@pytest.mark.anyio
async def test_inner_join_supports_one_to_many_and_entity_reference_equality() -> None:
    models, runs = join_inputs()
    joined = (
        await models.query()
        .join(
            runs,
            on=[(value().field("entity"), value().field("entity"))],
            left_name="model_row",
            right_name="run_row",
        )
        .collect()
    )
    assert joined.type == ArrayType.of(
        (
            "[model: str, run: int]",
            struct_of(
                model_row=struct_of(entity=EntityID, family=str),
                run_row=struct_of(entity=EntityID, score=float),
            ),
        )
    )
    assert list(joined.keys()) == [("a", 1), ("a", 2)]


@pytest.mark.anyio
async def test_query_result_uses_the_same_canonical_content_identity() -> None:
    queried = await (
        rows_array()
        .query()
        .filter(value().field("error").is_null())
        .select({"model": value().field("model"), "score": value().field("score")})
    ).collect()
    manually_built = Array.from_records(
        [
            {"run": 1, "model": "a", "score": 0.9},
            {"run": 3, "model": "b", "score": 0.7},
        ],
        ("[run: int]", struct_of(model=str, score=float)),
    )
    assert queried == manually_built
    assert queried.content_id() == manually_built.content_id()


def test_dim_and_value_name_their_source_and_path() -> None:
    assert dim("model").source == "dim"
    assert value().source == "value"
    assert value().path == ()
    nested = value().field("metrics").field("score")
    assert nested.source == "value"
    assert nested.path == ("metrics", "score")
    assert value().field("metrics.score").path == ("metrics.score",)


@pytest.mark.parametrize("path", ["", "score", "metrics.score", None])
def test_value_rejects_path_arguments(path: object) -> None:
    invalid_value = cast("Any", value)
    with pytest.raises(TypeError, match="positional argument"):
        invalid_value(path)
    with pytest.raises(TypeError, match="unexpected keyword argument 'path'"):
        invalid_value(path=path)


@pytest.mark.parametrize("source", ["auto", "field", "", None])
def test_columns_reject_unspecified_or_unknown_sources(source: object) -> None:
    with pytest.raises(QueryError, match="Unknown column source.*use 'dim' or 'value'"):
        Column(cast("Any", source), ("model",))


@pytest.mark.parametrize("name", ["", None, 7, []])
def test_field_requires_a_nonempty_literal_name(name: object) -> None:
    with pytest.raises(QueryError, match="Field names must be non-empty strings"):
        value().field(cast("Any", name))


def test_dimensions_cannot_be_traversed_as_structs() -> None:
    with pytest.raises(QueryError, match="Dimensions have no struct fields"):
        dim("model").field("score")


@pytest.mark.parametrize(
    "on",
    [
        {"model": "model"},
        [("model", "model")],
        [(dim("model"), "model")],
        ["model"],
        [(dim("model"), dim("model"), dim("model"))],
    ],
)
def test_join_keys_reject_implicit_names_and_malformed_pairs(on: object) -> None:
    with pytest.raises(QueryError, match="[Jj]oin key"):
        as_join_keys(cast("Any", on))


def test_chained_fields_remain_immutable() -> None:
    root = value()
    metrics = root.field("metrics")
    score = metrics.field("score")
    assert root.path == ()
    assert metrics.path == ("metrics",)
    assert score.path == ("metrics", "score")


@pytest.mark.anyio
async def test_same_name_dimension_and_field_have_independent_filter_and_grouping() -> None:
    rows = Array.from_items(
        [
            (("agent-v3", 101), {"model": "preview", "tokens": 120}),
            (("agent-v3", 102), {"model": "production", "tokens": 80}),
            (("agent-v2", 103), {"model": "production", "tokens": 100}),
        ],
        ("[model: str, run: int]", struct_of(model=str, tokens=int)),
    )
    by_dim = await rows.filter(dim("model") == "agent-v3").collect()
    by_field = await rows.filter(value().field("model") == "production").collect()
    assert list(by_dim.keys()) == [("agent-v3", 101), ("agent-v3", 102)]
    assert list(by_field.keys()) == [("agent-v2", 103), ("agent-v3", 102)]
    selected = await rows.select(
        {"model_key": dim("model"), "model_field": value().field("model")}
    ).collect()
    assert selected[("agent-v3", 101)] == {
        "model_key": "agent-v3",
        "model_field": "preview",
    }
    grouped_dim = await rows.aggregate({"n": count()}, group_by={"model": dim("model")}).collect()
    grouped_field = await rows.aggregate(
        {"n": count()}, group_by={"model": value().field("model")}
    ).collect()
    assert dict(grouped_dim.items()) == {("agent-v2",): {"n": 1}, ("agent-v3",): {"n": 2}}
    assert dict(grouped_field.items()) == {("preview",): {"n": 1}, ("production",): {"n": 2}}
    assert rows._backing.arrow_conversions == 1


@pytest.mark.anyio
async def test_same_name_dimension_and_field_have_independent_limit_ordering() -> None:
    rows = Array.from_items(
        [((101,), {"run": 50}), ((102,), {"run": 10})],
        ("[run: int]", struct_of(run=int)),
    )
    by_dim = await rows.limit(1, order_by=[desc(dim("run"))]).collect()
    by_field = await rows.limit(1, order_by=[desc(value().field("run"))]).collect()
    assert list(by_dim.keys()) == [(102,)]
    assert list(by_field.keys()) == [(101,)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field_name", ["metrics.score", "reviewer's score", 'reviewer"score', "評価"]
)
async def test_literal_field_names_are_not_interpreted_as_nested_paths(field_name: str) -> None:
    rows = Array.from_items(
        [((1842,), {field_name: 0.81, "metrics": {"score": 0.95}})],
        ("[ticket: int]", struct_of(**{field_name: float, "metrics": struct_of(score=float)})),
    )
    result = await rows.select(
        {
            "literal": value().field(field_name),
            "nested": value().field("metrics").field("score"),
        }
    ).collect()
    assert result[(1842,)] == {"literal": 0.81, "nested": 0.95}


@pytest.mark.anyio
async def test_chained_fields_propagate_nullable_structs_and_validate_before_execution() -> None:
    rows = Array.from_items(
        [((1842,), {"metrics": {"score": 0.95}}), ((2091,), {"metrics": None})],
        ("[ticket: int]", struct_of(metrics=optional(struct_of(score=float)))),
    )
    score = value().field("metrics").field("score")
    query = rows.select({"score": score})
    with pytest.raises(ArrayTypeError, match="No value field path 'metrics.missing'"):
        rows.select({"missing": value().field("metrics").field("missing")})
    with pytest.raises(ArrayTypeError, match="No value field path 'metrics.score.invalid'"):
        rows.select({"missing": score.field("invalid")})
    assert query._executor.metrics.queries == 0
    assert not rows._backing.has_table
    result = await query.collect()
    assert result.schema == struct_of(score=optional(float))
    assert result[(1842,)] == {"score": 0.95}
    assert result[(2091,)] == {"score": None}
    assert list((await rows.filter(score.is_null()).collect()).keys()) == [(2091,)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("left_dim", "right_dim"), [(True, True), (False, False), (True, False), (False, True)]
)
async def test_join_keys_distinguish_same_name_dimensions_and_value_fields(
    left_dim: bool, right_dim: bool
) -> None:
    schema = struct_of(model=str)
    left = Array.from_items([(("agent-v3",), {"model": "production"})], ("[model: str]", schema))
    right = Array.from_items([(("production",), {"model": "agent-v3"})], ("[model: str]", schema))
    left_key = dim("model") if left_dim else value().field("model")
    right_key = dim("model") if right_dim else value().field("model")
    query = left.join(right, on=[(left_key, right_key)])
    result = await query.collect()
    assert result.dim_names == (("model",) if right_dim else ("model", "right_model"))
    if left_dim == right_dim:
        assert len(result) == 0
    else:
        expected_key = ("agent-v3",) if right_dim else ("agent-v3", "production")
        assert list(result.keys()) == [expected_key]
        assert result[expected_key] == {
            "left": {"model": "production"},
            "right": {"model": "agent-v3"},
        }
