"""Checks Cartesian and sparse sweeps, including the actual core-dimension inputs."""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict

import pytest
from reference_backends import Executor

from basic_project.model_harness_sweep import (
    Model,
    TaskResult,
    Transcript,
    cross_provider,
    full_grid,
    model_harness_experiment,
    run_task,
)
from fxtr.entity_defns.array import ArrayType

pytestmark = pytest.mark.anyio


async def test_sweeps_preserve_ragged_model_context_without_mapping_over_it(
    execute: Executor,
) -> None:
    record = await execute(model_harness_experiment, full_grid, cross_provider, run_task)
    result = record.result
    assert result.type == ArrayType.of(("[selection: str, m: str, h: str, t: str]", TaskResult))
    full_keys = set(
        itertools.product(
            ["anthropic-model", "openai-model"],
            ["anthropic-harness", "openai-harness"],
            ["task-1", "task-2", "task-3"],
        )
    )
    filtered_keys = {(m, h, t) for m, h, t in full_keys if m.split("-")[0] != h.split("-")[0]}
    assert set(result.keys()) == {
        (selection, *key)
        for selection, keys in (("full_grid", full_keys), ("cross_provider", filtered_keys))
        for key in keys
    }
    assert set(record.workflows) == {"root", "root/full_grid", "root/cross_provider"}
    assert Counter(key.split("/")[1] for key in record.steps) == {
        "full_grid": 12,
        "cross_provider": 6,
    }

    expected_stuff = {
        "anthropic": ((0, 2), ("A_tools", "A_examples")),
        "openai": ((1,), ("O_tools",)),
    }
    for (_, m, h, _), value in result.items():
        assert isinstance(m, str) and isinstance(h, str)
        provider = m.split("-")[0]
        keys, labels = expected_stuff[provider]
        assert tuple(value["stuff_keys"]) == keys
        assert tuple(value["stuff_labels"]) == labels
        assert value["stuff_count"] == len(keys)
        assert value["model_provider"] == provider
        assert value["harness_provider"] == h.split("-")[0]
        transcript = await record.storage.load(value["transcript"], as_type=Transcript)
        assert transcript.stuff_keys == keys
        assert transcript.messages[-1].content == value["answer"]
        assert (await record.storage.load(transcript.model)).provider == provider
        assert (await record.storage.load(transcript.harness)).provider == h.split("-")[0]
        for ref, label in zip(transcript.stuff, labels, strict=True):
            item = await record.storage.load(ref)
            assert item.label == label
            assert item.model == transcript.model
            assert item.content in transcript.messages[0].content

    # Inspect the stored step arguments, independently of what the step reports in its output.
    # Harness, task, and selection changes must all reuse the same [s] slice for a model.
    slice_ids = defaultdict(set)
    for invocation in record.steps.values():
        assert invocation.step == run_task.name
        model_array = await record.storage.load(invocation.inputs["model"])
        model = await record.storage.load(model_array.item(), as_type=Model)
        stuff_array = await record.storage.load(invocation.inputs["stuff"])
        assert stuff_array.dim_names == ("s",)
        assert tuple(key[0] for key in stuff_array.keys()) == expected_stuff[model.provider][0]
        slice_ids[model.provider].add(invocation.inputs["stuff"].entity_id.cid)
    assert {provider: len(ids) for provider, ids in slice_ids.items()} == {
        "anthropic": 1,
        "openai": 1,
    }
