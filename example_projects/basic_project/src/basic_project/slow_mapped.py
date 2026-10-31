"""A deliberately slow experiment for exploring the viewer while work is in flight.

Two child workflows each prepare and score 40 samples, mapping both stages over
``sample``. Eight steps run at a time, with staggered sleeps and progress logs;
the default run takes roughly four minutes and produces 80 result rows.

``launch_slow_mapped.py`` at the project root runs it as a Postgres job of this project. Use
``--delay-scale 0`` to skip the sleeps, ``--delay-scale 2`` for a longer run,
or ``--items 100`` for more mapped samples in each lane; ``--concurrency`` controls simultaneous
steps. Each launch gets a fresh cache namespace so rerunning it still does the sleeps.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

import anyio

from fxtr.entity_defns.array import Array
from fxtr.experiment.handles import ArrayHandle, ScalarHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow


class PreparedSample(TypedDict):
    lane: str
    sample: int
    signal: int


class ScoredSample(TypedDict):
    lane: str
    sample: int
    score: float
    label: str


async def sleep_with_progress(context: StepContext, seconds: float) -> None:
    """Leave four progress updates in the attempt's log while it is running."""
    context.log(f"Starting simulated work ({seconds:g} seconds)")
    for tick in range(1, 5):
        await anyio.sleep(seconds / 4)
        context.log(f"Progress: {tick * 25}%")


@step(name="slow_demo_prepare")
async def prepare_sample(
    context: StepContext, lane: str, sample: int, delay_scale: float
) -> PreparedSample:
    """Prepare one sample in 6–14 seconds, staggered by sample number."""
    await sleep_with_progress(context, delay_scale * (6 + 2 * (sample % 5)))
    return {"lane": lane, "sample": sample, "signal": sample * sample + len(lane)}


@step(name="slow_demo_score")
async def score_sample(
    context: StepContext, prepared: PreparedSample, delay_scale: float
) -> ScoredSample:
    """Score one prepared sample in 10–18 seconds; results arrive out of order."""
    await sleep_with_progress(context, delay_scale * (10 + 2 * (prepared["sample"] % 5)))
    score = (prepared["signal"] % 101) / 100
    return {
        "lane": prepared["lane"],
        "sample": prepared["sample"],
        "score": score,
        "label": "pass" if score >= 0.5 else "review",
    }


@workflow(name="slow_demo_lane")
async def slow_lane(
    context: WorkflowContext,
    lane: ScalarHandle[str],
    samples: Annotated[ArrayHandle[int], "[sample: int]"],
    delay_scale: ScalarHandle[float],
) -> Annotated[ArrayHandle[ScoredSample], "[sample: int]"]:
    """Two mapped stages with enough samples to page through their keys and outputs."""
    prepared = context.run_step(
        "prepare",
        prepare_sample,
        {"lane": lane, "sample": samples, "delay_scale": delay_scale},
        map_over=["sample"],
    )
    return context.run_step(
        "score",
        score_sample,
        {"prepared": prepared, "delay_scale": delay_scale},
        map_over=["sample"],
    )


@workflow(name="slow_mapped_experiment")
async def slow_mapped_experiment(
    context: WorkflowContext, items: int, run_id: str, delay_scale: ScalarHandle[float]
) -> Annotated[ArrayHandle[ScoredSample], "[lane: str, sample: int]"]:
    """Map two child workflows over lanes, each with its own mapped sample pipeline."""
    lanes = context.add_source_array(
        "lanes",
        Array.from_items([((lane,), lane) for lane in ("alpha", "beta")], ("[lane: str]", str)),
    )
    samples = context.add_source_array(
        "samples", Array.from_items([((i,), i) for i in range(items)], ("[sample: int]", int))
    )
    return context.run_child_workflow(
        "process_lanes",
        slow_lane,
        {"lane": lanes, "samples": samples, "delay_scale": delay_scale},
        map_over=["lane"],
        override_cache_address_prefix=f"slow-demo/{run_id}",
    )
