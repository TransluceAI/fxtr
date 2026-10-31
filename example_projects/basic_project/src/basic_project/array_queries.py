"""An example experiment that analyzes its results with array queries.

Four models each attempt five problems (:func:`solve`, mapped over both), and the experiment then
answers questions about the attempts: how accurate and how expensive each model was, which models
solved each problem, which attempts failed and why, and which model solved each problem most
cheaply. Every one of those answers is an array query, and the example asks them in each place a
query can run and in both ways a query can be written:

- **The query builder over a workflow's handles** (:func:`evaluate_models`): ``query()`` starts
  a query on the ``attempts`` handle, and ``aggregate`` with ``group_by``, ``join``, and
  ``select`` extend it. The workflow never holds the rows: ``collect()`` records the chain as
  one query node of the workflow's graph, run by DuckDB on the host, whose value is the query's
  result.
- **SQL over a workflow's handles**: ``fxtr.sql`` reads a builder query as one of its tables, so
  the two compose into one DuckDB statement, and does what the builder does not (collect a list
  per group).
- **Observing a query in a workflow**: ``await ... .collect().item()`` records the query, waits
  for it to run, and hands its single value to the workflow body, which logs it.
- **Queries built by a plain function over handles** (:func:`triage`): a filter and a select,
  written as a function of the ``attempts`` handle that the workflow calls. What it returns is
  still a query over handles, run on the host like any other.
- **SQL with a window function** (:func:`cheapest_solver`): a ranking over two joined handles,
  then a builder ``select`` over the SQL result.
- **Queries over concrete arrays** (:func:`report`): a step receives concrete arrays, and
  the same builder methods and ``fxtr.sql`` run in the step's own process with ``collect()``.

The expressions used along the way are the builder's vocabulary: ``dim`` and ``value`` column
references, ``field`` access into structs, null tests, ``when``/``then``/``otherwise``,
arithmetic, ``cast``, ``fill_null``, aggregations (``count()`` of rows, ``count()`` of an
expression's non-null values, ``sum``, ``mean``), and ``limit`` with ``order_by``
(``asc``/``desc``) and ``ties="all"``. Entity references pass through queries: a failed
attempt's transcript id comes out of :func:`triage`'s query pointing at the transcript
:func:`solve` stored.

Nothing calls a model: :func:`solve` decides deterministically from a model's skill and a
problem's difficulty whether the attempt succeeds, fails, or runs out of tokens. The
``entity_array`` helper comes from :mod:`basic_project.rollout_experiment`.

``launch_array_queries.py`` at the project root runs the experiment as a job of this project.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, TypedDict

import fxtr
from basic_project.rollout_experiment import entity_array
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.query import asc, count, desc, dim, value, when
from fxtr.entity_defns.schema import struct_of
from fxtr.experiment.handles import ArrayHandle, ArrayHandleQuery, ScalarHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow

# ---- entity types ----

PROBLEM = "org.example.queries.Problem"
TRANSCRIPT = "org.example.queries.Transcript"


@dataclasses.dataclass(frozen=True, kw_only=True)
class Problem(DataclassEntity, fxtr_type=PROBLEM):
    """A question with a known answer, and how hard it is."""

    question: str
    answer: str
    difficulty: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class Transcript(DataclassEntity, fxtr_type=TRANSCRIPT):
    """What a model wrote for a problem."""

    model: str
    problem: BoundID[Problem]
    text: str


# ---- value types ----


class ModelInfo(TypedDict):
    """A model under evaluation: the value type of the models array."""

    name: str
    skill: int
    price_per_1k_tokens: float


class Attempt(TypedDict):
    """One model's attempt at one problem. ``error`` is set when the attempt did not finish."""

    transcript: BoundID[Transcript]
    correct: bool
    tokens: int
    error: str | None


class Standing(TypedDict):
    """A model's results over all its attempts, whether they finished or not."""

    failed: int
    accuracy: float | None
    cost: float


class Failure(TypedDict):
    """Why an attempt did not finish, and where to read it."""

    error: str
    transcript: BoundID[Transcript]


class Solver(TypedDict):
    """The model that solved a problem most cheaply, and what that cost."""

    model: str
    cost: float


class Report(TypedDict):
    """The experiment's conclusions: the workflow's result."""

    most_accurate: list[str]
    best_value: str
    total_cost: float
    unsolved: list[str]
    failures: int
    cheapest_solvers: list[str]


ATTEMPTS = "[model: str, problem: str]"

# ---- steps ----

TOKEN_BUDGET = 400


@step(name="solve_problem")
async def solve(context: StepContext, model: ModelInfo, problem: BoundID[Problem]) -> Attempt:
    """Attempt ``problem`` with ``model`` (fakely).

    A model at least as skilled as the problem is difficult answers correctly; one skill short
    answers wrongly and at greater length; further short, it runs out of its token budget.
    """
    loaded = await context.load(problem)
    shortfall = loaded.difficulty - model["skill"]
    if shortfall <= 0:
        text, tokens, error = loaded.answer, 50 * loaded.difficulty, None
    elif shortfall == 1:
        text, tokens, error = "I believe it is 42.", 80 * loaded.difficulty, None
    else:
        text, tokens, error = "Let me think step by step...", TOKEN_BUDGET, "ran out of tokens"
    transcript = Transcript(model=model["name"], problem=problem, text=text)
    return {
        "transcript": await context.store(transcript),
        "correct": shortfall <= 0,
        "tokens": tokens,
        "error": error,
    }


# ---- queries the workflow builds ----


def triage(attempts: ArrayHandle[Attempt]) -> ArrayHandleQuery[Failure]:
    """The attempts that did not finish, with their error and transcript.

    This builds a query over the workflow's handle and returns it without recording anything;
    the workflow collects it into a node. ``error`` is still optional after the
    filter as far as the query's type knows, so ``fill_null`` makes it a plain ``str``. The
    transcript ids are copied through, still referring to what :func:`solve` stored.
    """
    error = value().field("error")
    return (
        attempts.query()
        .filter(error.is_not_null())
        .select(
            {
                "error": error.fill_null("unknown"),
                "transcript": value().field("transcript"),
            }
        )
    )


def cheapest_solver(
    attempts: ArrayHandle[Attempt], models: ArrayHandle[ModelInfo]
) -> ArrayHandleQuery[Solver]:
    """For each problem some model solved, the model that solved it most cheaply.

    Ranking within a group is a window function, which the query builder does not have, so this
    is SQL over the two handles; it runs on the host like a builder query. The SQL returns every
    column of a correct attempt's cost, and a builder ``select`` over its result keeps the ones
    the report needs, so the two compose into one statement.
    """
    ranked = fxtr.sql(
        """
        SELECT {problem: a.dims.problem} AS dims,
               {model: a.dims.model,
                tokens: a.value.tokens,
                cost: a.value.tokens / 1000 * m.value.price_per_1k_tokens} AS value
        FROM attempts AS a JOIN models AS m ON a.dims.model = m.dims.model
        WHERE a.value.correct
        QUALIFY row_number() OVER (
            PARTITION BY a.dims.problem
            ORDER BY a.value.tokens * m.value.price_per_1k_tokens, a.dims.model
        ) = 1
        """,
        tables={"attempts": attempts, "models": models},
        output_type=("[problem: str]", struct_of(model=str, tokens=int, cost=float)),
    )
    return ranked.select({"model": value().field("model"), "cost": value().field("cost")})


# ---- the report ----


@step(name="report_evaluation")
async def report(
    context: StepContext,
    standings: Annotated[Array[Standing], "[model: str]"],
    solved_by: Annotated[Array[list[str]], "[problem: str]"],
    failures: Annotated[Array[Failure], ATTEMPTS],
    cheapest: Annotated[Array[Solver], "[problem: str]"],
) -> Report:
    """Summarize the analysis, querying the arrays in this process.

    A step receives concrete arrays. The same query methods apply, and run in DuckDB locally:
    ``collect()`` returns a concrete array, and ``item()`` on it reads a single value.
    """
    accuracy = value().field("accuracy")
    # Every model tied at the top accuracy, and the one of those that cost least. Selecting the
    # `model` dim makes it each row's value.
    most_accurate = await (
        standings.limit(1, order_by=desc(accuracy), ties="all").select(dim("model")).collect()
    )
    (best_value,) = (
        await standings.limit(1, order_by=[desc(accuracy), asc(value().field("cost"))])
        .select(dim("model"))
        .collect()
    ).values()
    total_cost = (
        await fxtr.sql(
            "SELECT sum(value.cost) AS value FROM standings",
            tables={"standings": standings},
            output_type=float,
        ).collect()
    ).item()
    unsolved = (
        await fxtr.sql(
            "SELECT coalesce(list(dims.problem ORDER BY dims.problem)"
            " FILTER (WHERE len(value) = 0), []) AS value FROM solved_by",
            tables={"solved_by": solved_by},
            output_type=list[str],
        ).collect()
    ).item()
    n_failures = (await failures.aggregate({"n": count()}).field("n").collect()).item()
    return {
        "most_accurate": list(most_accurate.values()),
        "best_value": best_value,
        "total_cost": total_cost,
        "unsolved": unsolved,
        "failures": n_failures,
        "cheapest_solvers": [
            f"{problem}: {solver['model']} (${solver['cost']:.3f})"
            for (problem,), solver in cheapest.items()
        ],
    }


# ---- the workflow ----

MODELS: list[ModelInfo] = [
    {"name": "tiny", "skill": 1, "price_per_1k_tokens": 0.1},
    {"name": "small", "skill": 2, "price_per_1k_tokens": 0.4},
    {"name": "large", "skill": 3, "price_per_1k_tokens": 2.0},
    {"name": "xl", "skill": 4, "price_per_1k_tokens": 5.0},
]

PROBLEMS = {
    "sum": Problem(question="What is 2 + 3?", answer="5", difficulty=1),
    "primes": Problem(question="How many primes are below 20?", answer="8", difficulty=2),
    "triangle": Problem(question="The area of a 3-4-5 triangle?", answer="6", difficulty=2),
    "proof": Problem(question="Is the square root of 2 rational?", answer="No", difficulty=3),
    "riemann": Problem(question="Is the Riemann hypothesis true?", answer="?", difficulty=5),
}


@workflow(name="evaluate_models")
async def evaluate_models(context: WorkflowContext) -> ScalarHandle[Report]:
    """Run every model on every problem, analyze the attempts with queries, and report."""
    models = context.add_source_array(
        "models",
        Array.from_items([((m["name"],), m) for m in MODELS], ("[model: str]", ModelInfo)),
    )
    problems = await entity_array(context, "problems", "problem", PROBLEMS)
    attempts = context.run_step(
        "solve", solve, {"model": models, "problem": problems}, map_over=["model", "problem"]
    ).expect_type((ATTEMPTS, Attempt))

    # Each model's accuracy, failures, and cost. The chain describes one query, which `collect()`
    # records as a single node. `error.count()` counts the rows whose error is not null, where
    # `count()` would count every row.
    error, correct = value().field("error"), value().field("correct")
    score = value().field("score")
    standings = (
        attempts.query()
        .aggregate(
            {
                "failed": error.count(),
                "accuracy": when(correct).then(1.0).otherwise(0.0).mean(),
                "tokens": value().field("tokens").sum(),
            },
            group_by={"model": dim("model")},
        )
        .join(models, on=[(dim("model"), dim("model"))], left_name="score", right_name="info")
        .select(
            {
                "failed": score.field("failed"),
                "accuracy": score.field("accuracy"),
                "cost": score.field("tokens").cast(float)
                / 1000
                * value().field("info").field("price_per_1k_tokens"),
            }
        )
        .expect_type(("[model: str]", Standing))
        .collect()
    )

    # The models that solved each problem, in a list. The builder has no list aggregation, so
    # this is SQL, reading the builder query `finished` as its table.
    finished = attempts.query().filter(error.is_null())
    solved_by = fxtr.sql(
        """
        SELECT {problem: dims.problem} AS dims,
               coalesce(list(dims.model ORDER BY dims.model) FILTER (WHERE value.correct), [])
                   AS value
        FROM finished
        GROUP BY dims.problem
        """,
        tables={"finished": finished},
        output_type=("[problem: str]", list[str]),
    ).collect()

    # Observing a collected query waits for it to run and gives the workflow its value.
    failed = attempts.query().filter(error.is_not_null()).aggregate({"n": count()}).field("n")
    n_failed = await failed.collect().item()
    context.log(f"{n_failed} of {len(MODELS) * len(PROBLEMS)} attempts did not finish")

    return context.run_step(
        "report",
        report,
        {
            "standings": standings,
            "solved_by": solved_by,
            "failures": triage(attempts).expect_type((ATTEMPTS, Failure)).collect(),
            "cheapest": cheapest_solver(attempts, models)
            .expect_type(("[problem: str]", Solver))
            .collect(),
        },
    )
