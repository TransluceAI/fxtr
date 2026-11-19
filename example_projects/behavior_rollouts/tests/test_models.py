"""Model specs, provider prefixes, and the model names and sampling entities a launcher stores."""

import pytest

from behavior_rollouts.models import (
    SamplingParamsEntity,
    parse_model_spec,
    split_provider,
    store_model_specs,
)
from behaviors.types import ReasoningEffort, ReasoningParams, SamplingParams
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import ArrayType
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("spec", "model_name", "effort"),
    [
        ("gpt-x", "gpt-x", None),
        ("gpt-x:high", "gpt-x", "high"),
        ("anthropic/claude-x:low", "anthropic/claude-x", "low"),
    ],
)
async def test_specs_are_read_into_a_model_name_and_effort(
    spec: str, model_name: str, effort: ReasoningEffort | None
) -> None:
    assert parse_model_spec(spec) == (model_name, effort)


@pytest.mark.parametrize(
    ("spec", "problem"),
    [
        ("", "model name"),
        (":low", "model name"),
        ("gemini/g-x", "Unknown provider"),
        ("gpt-x:fast", "reasoning effort"),
    ],
)
async def test_specs_reject_a_missing_model_an_unknown_provider_or_effort(
    spec: str, problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        parse_model_spec(spec)


async def test_stored_specs_align_names_and_sampling_on_the_spec() -> None:
    storage = MemoryStorage()
    names, samplings = await store_model_specs(
        storage, ["gpt-x", "gpt-x:high", "gpt-y"], dim="judge", max_tokens=100
    )
    assert names.type == ArrayType.of(("[judge: str]", str))
    assert samplings.type == ArrayType.of(("[judge: str]", BoundID[SamplingParamsEntity]))
    assert dict(names.items()) == {
        ("gpt-x",): "gpt-x",
        ("gpt-x:high",): "gpt-x",
        ("gpt-y",): "gpt-y",
    }
    stored = {
        key: (await storage.load(reference, as_type=SamplingParamsEntity)).params
        for key, reference in samplings.items()
    }
    assert stored[("gpt-x:high",)].reasoning == ReasoningParams(effort="high")
    assert stored[("gpt-x",)] == stored[("gpt-y",)] == SamplingParams(max_tokens=100)
    # Identical sampling parameters are one stored entity.
    assert samplings[("gpt-x",)].entity_id == samplings[("gpt-y",)].entity_id


@pytest.mark.parametrize(
    ("specs", "max_tokens", "problem"),
    [([], 100, "unique"), (["gpt-x", "gpt-x"], 100, "unique"), (["gpt-x"], 0, "max_tokens")],
)
async def test_stored_specs_need_unique_specs_and_positive_tokens(
    specs: list[str], max_tokens: int, problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        await store_model_specs(MemoryStorage(), specs, dim="model", max_tokens=max_tokens)


async def test_sampling_entities_round_trip() -> None:
    storage = MemoryStorage()
    entity = SamplingParamsEntity(
        params=SamplingParams(
            max_tokens=100,
            temperature=0.5,
            stop_sequences=("</answer>",),
            reasoning=ReasoningParams(effort="high", summary=True),
            extra={"seed": 7},
        )
    )
    assert await storage.load(await storage.store(entity)) == entity


@pytest.mark.parametrize(
    ("model_name", "provider", "model_id"),
    [
        ("gpt-x", "openai", "gpt-x"),
        ("openai/gpt-x", "openai", "gpt-x"),
        ("anthropic/claude-x", "anthropic", "claude-x"),
    ],
)
async def test_split_provider_defaults_to_openai(
    model_name: str, provider: str, model_id: str
) -> None:
    assert split_provider(model_name) == (provider, model_id)


@pytest.mark.parametrize("model_name", ["gemini/g-x", "anthropic/", "anthropic/  "])
async def test_split_provider_rejects_unknown_providers_and_empty_ids(model_name: str) -> None:
    with pytest.raises(ValueError):
        split_provider(model_name)
