"""Reference examples for the semantics of a correct execution.

Each test here is a small experiment whose execution pins down one aspect of what running a
workflow means: how scalars reach steps, how mapping slices and stacks, which joins apply, what
pathnames and cache keys invocations get, when two requests share one step run, how dynamic control
flow works, what mapping over nothing yields. Together they are the specification every backend must
satisfy: each test runs on every backend in :data:`~reference_backends.BACKENDS` (the ``execute``
fixture parametrizes over them) and asserts on the backend-neutral
:class:`~reference_backends.Execution` the backend reports, so a new backend is checked against
this whole module by adding one entry there. The tests are otherwise written against the
user-facing experiment API (:mod:`fxtr.experiment`).

The reference covers successful executions only. How a failure surfaces (a step raising, a wrong
return type, an exact join mismatch, a cache key conflict) is each backend's own business and is
tested with each backend. (:func:`test_one_step_slot_suffices_because_bodies_are_unbounded`
exercises Postgres's step concurrency limit directly.)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from reference_backends import BACKENDS, Execution, Executor, postgres_backend

from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.schema import optional, struct_of
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow

pytestmark = pytest.mark.anyio


@pytest.fixture(params=sorted(BACKENDS))
async def execute(request: pytest.FixtureRequest) -> AsyncIterator[Executor]:
    """One backend's executor; every test runs once per backend."""
    async with BACKENDS[request.param]() as executor:
        yield executor


@pytest.fixture(params=sorted(set(BACKENDS) - {"remote_postgres"}))
async def execute_queries(request: pytest.FixtureRequest) -> AsyncIterator[Executor]:
    """One executor of a backend that runs query nodes: every backend but the experimental remote
    layer, whose workflows cannot record one."""
    async with BACKENDS[request.param]() as executor:
        yield executor


def summarize(execution: Execution) -> dict[str, Any]:
    """A compact view of an execution, for writing expectations as plain dicts."""
    return {
        "workflows": {
            path: {
                "workflow": w.workflow,
                "prefix": w.prefix,
                "parent": w.parent,
                "nodes": len(w.node_types),
            }
            for path, w in sorted(execution.workflows.items())
        },
        "steps": {
            key: {"step": s.step, "requests": s.requests, "result": str(s.result_type)}
            for key, s in sorted(execution.steps.items())
        },
        "attempts": execution.attempts,
    }


# ---- a small library of steps ----


@step(inputs={"prompt": str, "n": int}, output=("[sample: int]", str))
async def generate(context: StepContext, prompt: str, n: int) -> Array[str]:
    """Deterministic stand-in for sampling: ``n`` completions of ``prompt``."""
    return Array.from_items([((i,), f"{prompt}#{i}") for i in range(n)], ("[sample: int]", str))


@step(inputs={"sample": str}, output=float)
async def score(context: StepContext, sample: str) -> float:
    """Deterministic stand-in for a judge: the length of the sample."""
    return float(len(sample))


@step(inputs={"scores": ("[sample: int]", float)}, output=float)
async def mean(context: StepContext, scores: Array[float]) -> float:
    values = list(scores.values())
    return sum(values) / len(values) if values else 0.0


ROLLS: list[str] = []


@step(inputs={"seed": str}, output=int)
async def roll(context: StepContext, seed: str) -> int:
    """A nondeterministic step: a fresh value every time it runs, whatever its input."""
    ROLLS.append(seed)
    return 1000 + len(ROLLS)


@step(inputs={"x": int, "y": int}, output=int)
async def add(context: StepContext, x: int, y: int) -> int:
    return x + y


def prompts_array(*prompts: str) -> Array[str]:
    return Array.from_items([((i,), p) for i, p in enumerate(prompts)], ("[prompt: int]", str))


# ---- 1. scalars ----


@workflow(inputs={"prompt": str, "n": int}, output=float)
async def score_one_prompt(
    context: WorkflowContext, prompt: ArrayHandle[str], n: ArrayHandle[int]
) -> ArrayHandle[float]:
    samples = context.run_step("generate", generate, {"prompt": prompt, "n": n})
    scores = context.run_step("score", score, {"sample": samples}, map_over=["sample"])
    return context.run_step("mean", mean, {"scores": scores})


async def test_scalars_reach_steps_bare_and_results_are_wrapped(execute: Executor) -> None:
    """A step declared with zero-dim inputs receives bare values and may return a bare value; the
    workflow sees arrays throughout. ``generate`` gets ``str`` and ``int``, returns an array;
    ``score`` gets one ``str`` per mapped sample and returns a ``float``; ``mean`` gets the scores
    array whole (its dim is a core dim) and returns a ``float``."""
    execution = await execute(
        score_one_prompt,
        generate,
        score,
        mean,
        inputs={"prompt": Array.scalar("ab", str), "n": Array.scalar(2, int)},
    )
    assert execution.result == Array.scalar(4.0, float)  # len("ab#0") == len("ab#1") == 4
    assert summarize(execution)["steps"] == {
        "root/generate": {"step": "generate", "requests": 1, "result": "[sample: int]: str"},
        "root/score[sample:0]": {"step": "score", "requests": 1, "result": "[]: float"},
        "root/score[sample:1]": {"step": "score", "requests": 1, "result": "[]: float"},
        "root/mean": {"step": "mean", "requests": 1, "result": "[]: float"},
    }


# ---- 2. mapping ----


@workflow(inputs={"prompts": ("[prompt: int]", str), "n": int}, output=("[prompt: int]", float))
async def score_prompts(
    context: WorkflowContext, prompts: ArrayHandle[str], n: ArrayHandle[int]
) -> ArrayHandle[float]:
    samples = context.run_step(
        "generate", generate, {"prompt": prompts, "n": n}, map_over=["prompt"]
    )
    assert samples.type == ArrayType.of(("[prompt: int, sample: int]", str))
    scores = context.run_step("score", score, {"sample": samples}, map_over=["prompt", "sample"])
    assert scores.type == ArrayType.of(("[prompt: int, sample: int]", float))
    return context.run_step("mean", mean, {"scores": scores}, map_over=["prompt"])


async def test_mapping_broadcasts_slices_and_stacks(execute: Executor) -> None:
    """Mapping over ``prompt`` runs the step once per prompt on the prompt's slice; an input
    without that dim (``n``) is broadcast. Results stack with the mapped dims first, in
    ``map_over`` order, then the step's output dims. Mapping over two dims gives one run per joint
    key; mapping over ``prompt`` while the step declares ``sample`` as a core dim slices out the
    per-prompt sub-array."""
    execution = await execute(
        score_prompts,
        generate,
        score,
        mean,
        inputs={"prompts": prompts_array("a", "bcd"), "n": Array.scalar(2, int)},
    )
    assert execution.result == Array.from_items(
        [((0,), 3.0), ((1,), 5.0)], ("[prompt: int]", float)
    )
    assert sorted(execution.steps) == [
        "root/generate[prompt:0]",
        "root/generate[prompt:1]",
        "root/mean[prompt:0]",
        "root/mean[prompt:1]",
        "root/score[prompt:0,sample:0]",
        "root/score[prompt:0,sample:1]",
        "root/score[prompt:1,sample:0]",
        "root/score[prompt:1,sample:1]",
    ]
    # What the mapped mean invocation saw: the [sample] slice for its prompt.
    scores_seen = await execution.storage.load(
        execution.steps["root/mean[prompt:1]"].inputs["scores"]
    )
    assert scores_seen == Array.from_items([((0,), 5.0), ((1,), 5.0)], ("[sample: int]", float))
    # The root's nodes in creation order: the two inputs, generate, score, mean.
    scores_node_type = execution.workflows["root"].node_types[3]
    assert scores_node_type == ArrayType.of(("[prompt: int, sample: int]", float))


# ---- 3. joins ----


@workflow(
    inputs={"a": ("[k: int]", int), "b": ("[k: int]", int)},
    output=("[k: int]", struct_of(a=optional(int), b=optional(int))),
)
async def joins(
    context: WorkflowContext, a: ArrayHandle[int], b: ArrayHandle[int]
) -> ArrayHandle[Any]:
    added = context.run_step("add", add, {"x": a, "y": b}, map_over=["k"], join="inner")
    assert (await added.observe()) == Array.from_items([((2,), 22)], ("[k: int]", int))
    return context.combine({"a": a, "b": b}, join="outer")


async def test_join_modes(execute: Executor) -> None:
    """``join="inner"`` on a run intersects the inputs' keys along the mapped dims; ``combine``
    additionally offers ``outer``, the union with ``None`` for missing values and every field
    nullable. (``exact``, the default, requires the keys to agree; that it fails otherwise is
    tested with each backend.)"""
    a = Array.from_items([((1,), 1), ((2,), 2)], ("[k: int]", int))
    b = Array.from_items([((2,), 20), ((3,), 30)], ("[k: int]", int))
    execution = await execute(joins, add, inputs={"a": a, "b": b})
    assert execution.result == Array.from_items(
        [((1,), {"a": 1, "b": None}), ((2,), {"a": 2, "b": 20}), ((3,), {"a": None, "b": 30})],
        ("[k: int]", struct_of(a=optional(int), b=optional(int))),
    )
    assert list(execution.steps) == ["root/add[k:2]"]


# ---- 4. handle operations ----


@workflow(
    inputs={"rows": ("[id: int]", struct_of(x=int, y=str))}, output=("[copy: str, k: int]", int)
)
async def reshape(context: WorkflowContext, rows: ArrayHandle[Any]) -> ArrayHandle[int]:
    x = rows.field("x").collect()
    assert await rows.sel({"id": 2}).collect().item() == {"x": 20, "y": "q"}
    return context.stack({"one": x, "two": x}, dim="copy").rename({"id": "k"}).collect()


async def test_handle_operations_are_computed_by_the_system(execute_queries: Executor) -> None:
    """``field``, ``sel`` and ``rename`` on handles are query nodes, and ``combine`` and
    ``stack`` DAG nodes; the system computes both, without running any step."""
    execute = execute_queries
    rows = Array.from_records(
        [{"id": 1, "x": 10, "y": "p"}, {"id": 2, "x": 20, "y": "q"}],
        ("[id: int]", struct_of(x=int, y=str)),
    )
    execution = await execute(reshape, inputs={"rows": rows})
    assert execution.result == Array.from_items(
        [(("one", 1), 10), (("one", 2), 20), (("two", 1), 10), (("two", 2), 20)],
        ("[copy: str, k: int]", int),
    )
    assert execution.attempts == {"workflow": 1, "step": 0}


# ---- 5. child workflows ----


@workflow(inputs={"prompts": ("[prompt: int]", str), "n": int}, output=("[prompt: int]", float))
async def via_children(
    context: WorkflowContext, prompts: ArrayHandle[str], n: ArrayHandle[int]
) -> ArrayHandle[float]:
    per_prompt = context.run_child_workflow(
        "per_prompt", score_one_prompt, {"prompt": prompts, "n": n}, map_over=["prompt"]
    )
    baseline = context.run_child_workflow(
        "baseline",
        score_one_prompt,
        {"prompt": context.add_source_array("base", Array.scalar("z", str)), "n": n},
    )
    assert (await baseline.observe()).item() == 3.0
    return per_prompt


async def test_child_workflows_nest_pathnames_and_cache_key_prefixes(execute: Executor) -> None:
    """A child runs at ``parent/name``, mapped children at ``parent/name[key]``; each child's
    default cache key prefix is its pathname, so the steps it schedules are keyed under it. The
    parent's node takes the child's *result node* value, stacked like a mapped step."""
    execution = await execute(
        via_children,
        score_one_prompt,
        generate,
        score,
        mean,
        inputs={"prompts": prompts_array("a", "bcd"), "n": Array.scalar(1, int)},
    )
    assert execution.result == Array.from_items(
        [((0,), 3.0), ((1,), 5.0)], ("[prompt: int]", float)
    )
    summary = summarize(execution)
    assert summary["workflows"] == {
        "root": {"workflow": "via_children", "prefix": "root", "parent": None, "nodes": 5},
        "root/baseline": {
            "workflow": "score_one_prompt",
            "prefix": "root/baseline",
            "parent": ("root", ()),
            "nodes": 5,
        },
        "root/per_prompt[prompt:0]": {
            "workflow": "score_one_prompt",
            "prefix": "root/per_prompt[prompt:0]",
            "parent": ("root", (0,)),
            "nodes": 5,
        },
        "root/per_prompt[prompt:1]": {
            "workflow": "score_one_prompt",
            "prefix": "root/per_prompt[prompt:1]",
            "parent": ("root", (1,)),
            "nodes": 5,
        },
    }
    assert sorted(summary["steps"]) == [
        "root/baseline/generate",
        "root/baseline/mean",
        "root/baseline/score[sample:0]",
        "root/per_prompt[prompt:0]/generate",
        "root/per_prompt[prompt:0]/mean",
        "root/per_prompt[prompt:0]/score[sample:0]",
        "root/per_prompt[prompt:1]/generate",
        "root/per_prompt[prompt:1]/mean",
        "root/per_prompt[prompt:1]/score[sample:0]",
    ]
    assert summary["attempts"] == {"workflow": 4, "step": 9}


# ---- 6. the cache ----


@workflow(inputs={}, output=struct_of(a=int, b=int, c=int, d=int))
async def rolls(context: WorkflowContext) -> ArrayHandle[Any]:
    seed = context.add_source_array("seed", Array.scalar("s", str))
    a = context.run_step("a", roll, {"seed": seed}, override_cache_address_prefix="shared/roll")
    b = context.run_step("b", roll, {"seed": seed}, override_cache_address_prefix="shared/roll")
    c = context.run_step("c", roll, {"seed": seed})
    d = context.run_step("d", roll, {"seed": seed})
    return context.combine({"a": a, "b": b, "c": c, "d": d})


async def test_cache_addresses_break_symmetry_and_deduplicate(execute: Executor) -> None:
    """A step run is identified by its cache key. Two requests with the same key and the same
    configuration (step, version, inputs) are one run, so a nondeterministic step yields the same
    value to both. Requests with different keys are different runs even with identical
    configuration: the address breaks the symmetry, like a seed. (Same key with a different
    configuration is an error, tested with each backend.)"""
    ROLLS.clear()
    execution = await execute(rolls, roll)
    r = execution.result.item()
    assert r["a"] == r["b"]
    assert len({r["a"], r["c"], r["d"]}) == 3
    assert len(ROLLS) == 3
    assert summarize(execution)["steps"] == {
        "shared/roll": {"step": "roll", "requests": 2, "result": "[]: int"},
        "root/c": {"step": "roll", "requests": 1, "result": "[]: int"},
        "root/d": {"step": "roll", "requests": 1, "result": "[]: int"},
    }


# ---- 6b. replicas ----


@workflow(inputs={"seeds": ("[seed: str]", str)}, output=("[seed: str, rep: int]", int))
async def replicated_rolls(context: WorkflowContext, seeds: ArrayHandle[str]) -> ArrayHandle[int]:
    return context.run_step(
        "roll", roll, {"seed": seeds}, map_over=["seed"], replicas={"rep": [0, 1, 2]}
    )


async def test_replicas_rerun_with_the_same_inputs_under_distinct_cache_keys(
    execute: Executor,
) -> None:
    """A replica dim repeats a run without changing what the body sees: each joint key runs once
    per replica value with identical inputs, the replica value appears in the cache key after the
    mapped key (so the runs are distinct cache entries) and in the result after the mapped dims,
    and the body itself never receives it."""
    ROLLS.clear()
    seeds = Array.from_items([(("x",), "x"), (("y",), "y")], ("[seed: str]", str))
    execution = await execute(replicated_rolls, roll, inputs={"seeds": seeds})
    result = execution.result
    assert result.type == ArrayType.of(("[seed: str, rep: int]", int))
    assert sorted(result.keys()) == [("x", 0), ("x", 1), ("x", 2), ("y", 0), ("y", 1), ("y", 2)]
    assert len(set(result.values())) == 6
    assert sorted(ROLLS) == ["x", "x", "x", "y", "y", "y"]
    assert sorted(execution.steps) == [
        'root/roll[seed:"x",rep:0]',
        'root/roll[seed:"x",rep:1]',
        'root/roll[seed:"x",rep:2]',
        'root/roll[seed:"y",rep:0]',
        'root/roll[seed:"y",rep:1]',
        'root/roll[seed:"y",rep:2]',
    ]


# ---- 7. dynamic control flow ----


@workflow(inputs={"prompt": str}, output=("[sample: int]", str))
async def sample_until_enough(
    context: WorkflowContext, prompt: ArrayHandle[str]
) -> ArrayHandle[str]:
    """Keeps generating with a larger ``n`` until the mean score is at least 5, or gives up after
    three rounds and returns the last samples."""
    samples: ArrayHandle[str] | None = None
    for n in (1, 2, 3):
        n_handle = context.add_source_array(f"n{n}", Array.scalar(n, int))
        samples = context.run_step(f"generate{n}", generate, {"prompt": prompt, "n": n_handle})
        scores = context.run_step(f"score{n}", score, {"sample": samples}, map_over=["sample"])
        m = await context.run_step(f"mean{n}", mean, {"scores": scores}).item()
        if m >= 5:
            break
    assert samples is not None
    return samples


async def test_dynamic_control_flow_observes_before_scheduling_more(execute: Executor) -> None:
    """A workflow may ``observe`` a result and decide what to schedule next. Nothing after the
    observation exists in the DAG until the observed value is available, and names must stay
    unique across the loop."""
    execution = await execute(
        sample_until_enough, generate, score, mean, inputs={"prompt": Array.scalar("abc", str)}
    )
    # len("abc#0") == 5, so the first round already suffices.
    assert execution.result == Array.from_items([((0,), "abc#0")], ("[sample: int]", str))
    assert sorted(execution.steps) == ["root/generate1", "root/mean1", "root/score1[sample:0]"]
    assert execution.observes == 1
    execution = await execute(
        sample_until_enough, generate, score, mean, inputs={"prompt": Array.scalar("a", str)}
    )
    # len("a#0") == 3 < 5 every round: all three rounds run and the last samples are returned.
    assert execution.result == Array.from_items(
        [((0,), "a#0"), ((1,), "a#1"), ((2,), "a#2")], ("[sample: int]", str)
    )
    assert len(execution.steps) == 3 * 2 + (1 + 2 + 3)
    assert execution.observes == 3


# ---- 8. empty mapped key sets ----


@workflow(inputs={"prompts": ("[prompt: int]", str)}, output=("[prompt: int]", float))
async def score_each_prompt(
    context: WorkflowContext, prompts: ArrayHandle[str]
) -> ArrayHandle[float]:
    n = context.add_source_array("n", Array.scalar(1, int))
    samples = context.run_step(
        "generate", generate, {"prompt": prompts, "n": n}, map_over=["prompt"]
    )
    scores = context.run_step("score", score, {"sample": samples}, map_over=["prompt", "sample"])
    return context.run_step("mean", mean, {"scores": scores}, map_over=["prompt"])


async def test_empty_mapped_key_sets(execute: Executor) -> None:
    """Mapping over an array with no rows runs nothing and yields an empty array of the right
    type."""
    execution = await execute(
        score_each_prompt, generate, score, mean, inputs={"prompts": prompts_array("xy")}
    )
    assert execution.result == Array.from_items([((0,), 4.0)], ("[prompt: int]", float))
    execution = await execute(
        score_each_prompt, generate, score, mean, inputs={"prompts": prompts_array()}
    )
    assert execution.result == Array.from_items([], ("[prompt: int]", float))
    assert execution.attempts == {"workflow": 1, "step": 0}


# ---- 9. blocking observation with one step slot ----


@workflow(inputs={"prompt": str}, output=("[sample: int]", str))
async def generate_and_look(context: WorkflowContext, prompt: ArrayHandle[str]) -> ArrayHandle[str]:
    n = context.add_source_array("n", Array.scalar(1, int))
    samples = context.run_step("generate", generate, {"prompt": prompt, "n": n})
    await samples.observe()
    return samples


async def test_one_step_slot_suffices_because_bodies_are_unbounded() -> None:
    """The concurrency limit bounds step bodies only. A workflow body blocked in ``observe`` holds
    no slot, so a workflow waiting on a step completes with a single slot; bounding bodies too
    could only deadlock the steps they wait for."""
    async with postgres_backend(max_concurrency=1) as execute:
        record = await execute(
            generate_and_look, generate, inputs={"prompt": Array.scalar("a", str)}
        )
        assert record.result == Array.from_items([((0,), "a#0")], ("[sample: int]", str))


# ---- 10. everything together ----


@workflow(
    inputs={"prompts": ("[prompt: int]", str), "n": int}, output=struct_of(best=str, mean=float)
)
async def experiment(
    context: WorkflowContext, prompts: ArrayHandle[str], n: ArrayHandle[int]
) -> ArrayHandle[Any]:
    """Score every prompt via a child workflow, pick the best, and report its mean alongside it."""
    means = context.run_child_workflow(
        "score", score_one_prompt, {"prompt": prompts, "n": n}, map_over=["prompt"]
    )
    array = await context.combine({"prompt": prompts, "mean": means}).observe()
    best_key = max(array.keys(), key=lambda k: array[k]["mean"])
    best = context.add_source_array("best", Array.scalar(array[best_key]["prompt"], str))
    best_mean = context.add_source_array("best_mean", Array.scalar(array[best_key]["mean"], float))
    return context.combine({"best": best, "mean": best_mean})


async def test_full_experiment(execute: Executor) -> None:
    """A root with mapped children, a data-dependent choice, and derived nodes, with its whole
    execution summarized: the shape every backend must reproduce."""
    execution = await execute(
        experiment,
        score_one_prompt,
        generate,
        score,
        mean,
        inputs={"prompts": prompts_array("a", "bcd", "xy"), "n": Array.scalar(2, int)},
    )
    assert execution.result.item() == {"best": "bcd", "mean": 5.0}
    assert summarize(execution) == {
        "workflows": {
            "root": {"workflow": "experiment", "prefix": "root", "parent": None, "nodes": 7},
            **{
                f"root/score[prompt:{i}]": {
                    "workflow": "score_one_prompt",
                    "prefix": f"root/score[prompt:{i}]",
                    "parent": ("root", (i,)),
                    "nodes": 5,
                }
                for i in range(3)
            },
        },
        "steps": {
            **{
                f"root/score[prompt:{i}]/generate": {
                    "step": "generate",
                    "requests": 1,
                    "result": "[sample: int]: str",
                }
                for i in range(3)
            },
            **{
                f"root/score[prompt:{i}]/score[sample:{j}]": {
                    "step": "score",
                    "requests": 1,
                    "result": "[]: float",
                }
                for i in range(3)
                for j in range(2)
            },
            **{
                f"root/score[prompt:{i}]/mean": {
                    "step": "mean",
                    "requests": 1,
                    "result": "[]: float",
                }
                for i in range(3)
            },
        },
        "attempts": {"workflow": 4, "step": 12},
    }
