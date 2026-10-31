"""A deliberately large, slow sweep for watching a 5,000-cell mapped step in the viewer.

The root crosses 8 models [m], 5 prompt styles [p], 25 tasks [t], and 5 seeds [s], mapping one
mock rollout step over all four dims: 5,000 step invocations, each sleeping 0.5–2.5 seconds.
A second step maps over [m, p] only, so each of its 40 invocations receives a whole [t, s]
slice of 125 rollouts and summarizes it. At 16 concurrent steps the default run's sleeps alone
take about eight minutes.

``launch_huge_grid.py`` at the project root runs it as a Postgres job of this project:
``--tasks`` and ``--seeds`` resize the grid (each task adds 200 rollouts and each seed 1,000),
``--delay-scale`` stretches or skips the sleeps, and ``--concurrency`` (16 by default) controls
simultaneous steps. The Postgres runner slows down as the grid grows: at 10,000 cells it currently
stalls and exhausts its connection pool. Use ``--tasks 2 --seeds 2 --delay-scale 0`` for a small,
quick run. Each launch gets a fresh
cache namespace so rerunning it still does the sleeps; rollout values are seeded by their
coordinates, so reruns produce the same numbers.
"""

from __future__ import annotations

import random
from statistics import fmean
from typing import Annotated, TypedDict

import anyio

from fxtr.entity_defns.array import Array
from fxtr.experiment.handles import ArrayHandle, ScalarHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow


class ModelSpec(TypedDict):
    """A mock model: its name and how often it gets an easy task right."""

    name: str
    skill: float


class PromptStyle(TypedDict):
    """A mock prompting strategy: how much it helps, and how long its answers run."""

    name: str
    bonus: float
    verbosity: float


class Rollout(TypedDict):
    correct: bool
    score: float
    tokens: int


class CellSummary(TypedDict):
    rollouts: int
    accuracy: float
    mean_score: float
    mean_tokens: float


MODELS: dict[str, ModelSpec] = {
    key: {"name": key, "skill": skill}
    for key, skill in (
        ("atlas-small", 0.42),
        ("atlas-medium", 0.55),
        ("atlas-large", 0.68),
        ("borealis-7b", 0.38),
        ("borealis-70b", 0.61),
        ("cirrus-mini", 0.47),
        ("cirrus-pro", 0.73),
        ("delta-1", 0.52),
    )
}

PROMPT_STYLES: dict[str, PromptStyle] = {
    key: {"name": key, "bonus": bonus, "verbosity": verbosity}
    for key, bonus, verbosity in (
        ("zero_shot", 0.0, 1.0),
        ("few_shot", 0.06, 1.2),
        ("chain_of_thought", 0.12, 2.4),
        ("self_critique", 0.09, 3.1),
        ("terse", -0.05, 0.4),
    )
}


@step(name="huge_grid_rollout")
async def sample_rollout(
    context: StepContext,
    model: ModelSpec,
    prompt_style: PromptStyle,
    task: int,
    seed: int,
    delay_scale: float,
) -> Rollout:
    """Sample one mock rollout of a model on a task, taking 0.5–2.5 seconds.

    The chance of a correct answer grows with the model's skill and the prompt style's bonus
    and shrinks with task difficulty, which cycles through five levels by task number. All
    randomness is seeded by the model, prompt style, task, and seed.
    """
    rng = random.Random(f"{model['name']}/{prompt_style['name']}/{task}/{seed}")
    seconds = delay_scale * rng.uniform(0.5, 2.5)
    context.log(f"Sampling for {seconds:.1f} seconds")
    await anyio.sleep(seconds)
    difficulty = (task % 5) / 4
    p_correct = min(0.98, max(0.02, model["skill"] + prompt_style["bonus"] - 0.4 * difficulty))
    return {
        "correct": rng.random() < p_correct,
        "score": round(min(1.0, max(0.0, rng.gauss(p_correct, 0.12))), 3),
        "tokens": max(8, round(rng.gauss(320 * prompt_style["verbosity"], 40))),
    }


@step(name="huge_grid_summarize")
async def summarize_cell(
    context: StepContext,
    rollouts: Annotated[Array[Rollout], "[t: int, s: int]"],
    delay_scale: float,
) -> CellSummary:
    """Summarize every task and seed of one model and prompt style, taking 3 seconds."""
    await anyio.sleep(delay_scale * 3)
    values = list(rollouts.values())
    context.log(f"Summarized {len(values)} rollouts")
    return {
        "rollouts": len(values),
        "accuracy": round(fmean(r["correct"] for r in values), 3),
        "mean_score": round(fmean(r["score"] for r in values), 3),
        "mean_tokens": round(fmean(r["tokens"] for r in values), 1),
    }


@workflow(name="huge_grid_experiment")
async def huge_grid_experiment(
    context: WorkflowContext,
    tasks: int,
    seeds: int,
    run_id: str,
    delay_scale: ScalarHandle[float],
) -> Annotated[ArrayHandle[CellSummary], "[m: str, p: str]"]:
    """Sample every model × prompt style × task × seed, then summarize each model and style.

    The rollout step's key picker and output table page through all 5,000 cells (at the
    default size); the summary step shows each one's whole task-by-seed slice as its input.
    """
    models = context.add_source_array(
        "models", Array.from_items([((k,), v) for k, v in MODELS.items()], ("[m: str]", ModelSpec))
    )
    prompt_styles = context.add_source_array(
        "prompt_styles",
        Array.from_items([((k,), v) for k, v in PROMPT_STYLES.items()], ("[p: str]", PromptStyle)),
    )
    task_ids = context.add_source_array(
        "task_ids", Array.from_items([((t,), t) for t in range(tasks)], ("[t: int]", int))
    )
    seed_ids = context.add_source_array(
        "seed_ids", Array.from_items([((s,), s) for s in range(seeds)], ("[s: int]", int))
    )
    rollouts = context.run_step(
        "rollouts",
        sample_rollout,
        {
            "model": models,
            "prompt_style": prompt_styles,
            "task": task_ids,
            "seed": seed_ids,
            "delay_scale": delay_scale,
        },
        map_over=["m", "p", "t", "s"],
        override_cache_address_prefix=f"huge-grid/{run_id}/rollouts",
    )
    return context.run_step(
        "summaries",
        summarize_cell,
        {"rollouts": rollouts, "delay_scale": delay_scale},
        map_over=["m", "p"],
        override_cache_address_prefix=f"huge-grid/{run_id}/summaries",
    )
