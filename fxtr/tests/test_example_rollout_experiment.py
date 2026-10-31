"""Runs the example project's rollout experiment on Postgres."""

from __future__ import annotations

import itertools

import pytest
from reference_backends import Executor

from basic_project.rollout_experiment import (
    Grade,
    Transcript,
    grade,
    rollout,
    rollout_desugared,
    rollout_experiment,
    rollout_experiment_desugared,
)
from fxtr.entity_defns.array import Array, ArrayType

pytestmark = pytest.mark.anyio


# The type the experiment's grade array comes out as.
GRADES_TYPE = ("[model: str, scenario: str, sample: int, rubric: str]", Grade)


async def test_rollout_experiment_grades_every_sample_against_every_rubric(
    execute: Executor,
) -> None:
    record = await execute(
        rollout_experiment, rollout, grade, inputs={"n_samples": Array.scalar(2, int)}
    )
    grades = record.result
    assert grades.type == ArrayType.of(GRADES_TYPE)
    assert sorted(grades.keys()) == sorted(
        itertools.product(
            ["small", "large"], ["greeting", "refusal"], [0, 1], ["politeness", "brevity"]
        )
    )
    for value in grades.values():
        assert 0.0 <= value["score"] <= 1.0
        assert value["rationale"].startswith(("politeness:", "brevity:"))
    # Rollouts ran once per (model, scenario, sample) under cache keys carrying the replica key,
    # and each stored a transcript that refers back to its inputs.
    rollouts = {k: s for k, s in record.steps.items() if s.step == rollout.name}
    assert sorted(rollouts) == sorted(
        f'root/rollout[model:"{m}",scenario:"{s}",sample:{i}]'
        for m, s, i in itertools.product(["small", "large"], ["greeting", "refusal"], [0, 1])
    )
    produced = await record.storage.load(
        rollouts['root/rollout[model:"small",scenario:"greeting",sample:0]'].result
    )
    transcript = await record.storage.load(produced.item(), as_type=Transcript)
    assert (await record.storage.load(transcript.model)).name == "small-1"
    assert (await record.storage.load(transcript.scenario)).title == "Greeting"
    assert transcript.messages[0].content == "Say hello politely."


async def test_desugared_rollout_produces_the_same_experiment_shape(execute: Executor) -> None:
    record = await execute(
        rollout_experiment_desugared,
        rollout_desugared,
        grade,
        inputs={"n_samples": Array.scalar(2, int)},
    )
    grades = record.result
    assert grades.type == ArrayType.of(GRADES_TYPE)
    assert len(grades) == 2 * 2 * 2 * 2
    assert {s.step for s in record.steps.values()} == {rollout_desugared.name, grade.name}
