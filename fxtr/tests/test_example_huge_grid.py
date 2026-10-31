"""Checks the example project's huge grid's shape on a small grid with the sleeps skipped."""

from __future__ import annotations

import itertools

import pytest
from reference_backends import Executor

from basic_project import huge_grid
from basic_project.huge_grid import (
    CellSummary,
    huge_grid_experiment,
    sample_rollout,
    summarize_cell,
)
from fxtr.entity_defns.array import Array, ArrayType

pytestmark = pytest.mark.anyio


async def test_rollouts_cover_the_grid_and_summaries_receive_whole_slices(
    execute: Executor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep every dimension nontrivial while bounding the number of durable step attempts.
    models = dict(list(huge_grid.MODELS.items())[:2])
    prompt_styles = dict(list(huge_grid.PROMPT_STYLES.items())[:2])
    monkeypatch.setattr(huge_grid, "MODELS", models)
    monkeypatch.setattr(huge_grid, "PROMPT_STYLES", prompt_styles)
    record = await execute(
        huge_grid_experiment,
        sample_rollout,
        summarize_cell,
        inputs={
            "tasks": Array.scalar(3, int),
            "seeds": Array.scalar(2, int),
            "run_id": Array.scalar("test", str),
            "delay_scale": Array.scalar(0.0, float),
        },
    )
    result = record.result
    assert result.type == ArrayType.of(("[m: str, p: str]", CellSummary))
    assert set(result.keys()) == set(itertools.product(models, prompt_styles))
    assert all(summary["rollouts"] == 3 * 2 for summary in result.values())

    step_names = [invocation.step for invocation in record.steps.values()]
    assert step_names.count(sample_rollout.name) == len(models) * len(prompt_styles) * 3 * 2
    assert step_names.count(summarize_cell.name) == len(models) * len(prompt_styles)
    for invocation in record.steps.values():
        if invocation.step == summarize_cell.name:
            rollouts = await record.storage.load(invocation.inputs["rollouts"])
            assert rollouts.dim_names == ("t", "s")
            assert set(rollouts.keys()) == set(itertools.product(range(3), range(2)))
