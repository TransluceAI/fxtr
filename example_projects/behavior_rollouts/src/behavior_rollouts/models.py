"""How a step gets the model it calls: a model name and a stored sampling entity.

A step that calls a model takes two inputs for it:

- ``model_name``: ``[openai/|anthropic/]MODEL``, naming the provider and the model. The step builds
  the ``ModelAPI`` from it with ``make_api``, and the string is the stored record of which model
  the step called.
- ``sampling``: a ``SamplingParamsEntity``, whose ``params`` the step passes to behaviors.

``SamplingParams`` is a tagged dataclass, and an array cannot hold one as a value. An array can
hold a reference to a ``SamplingParamsEntity``, so each model or judge in a dimension can have
sampling parameters of its own. Identical parameters are stored once.

A launcher names each model with a spec, ``[openai/|anthropic/]MODEL[:EFFORT]``
(``parse_model_spec``), and stores the specs as a step's two inputs with ``store_model_specs``.
"""

import dataclasses
from typing import Literal, cast, get_args

from behaviors.implementations.models.anthropic import AnthropicMessagesAPI
from behaviors.implementations.models.openai_responses import OpenAIResponsesAPI
from behaviors.types import ReasoningEffort, ReasoningParams, SamplingParams
from fxtr.core.entities import BoundID, FxtrEntityStorage
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_entity import DataclassEntity


@dataclasses.dataclass(frozen=True)
class SamplingParamsEntity(
    DataclassEntity,
    fxtr_type="org.transluce.examples.behavior_rollouts.SamplingParamsEntity.v1",
):
    """Sampling parameters stored as an entity, so a step can take them as an input.

    Attributes:
        params: The sampling parameters every request of the step uses.
    """

    params: SamplingParams


ProviderName = Literal["openai", "anthropic"]
"""The providers a model name can name. A name without a provider prefix is an OpenAI model."""


def split_provider(model_name: str) -> tuple[ProviderName, str]:
    """The provider and model ID of ``[openai/|anthropic/]MODEL``. With no prefix, it is OpenAI.

    Raises:
        ValueError: The prefix names another provider, or the model ID is empty.
    """
    provider, separator, model_id = model_name.partition("/")
    if not separator:
        provider, model_id = "openai", model_name
    if provider not in get_args(ProviderName):
        raise ValueError(f"Unknown provider {provider!r} in {model_name!r}")
    if not model_id.strip():
        raise ValueError(f"A model needs an ID after its provider: {model_name!r}")
    return cast(ProviderName, provider), model_id


def make_api(model_name: str) -> OpenAIResponsesAPI | AnthropicMessagesAPI:
    """The provider's adapter for ``[openai/|anthropic/]MODEL``. The caller closes it."""
    provider, model_id = split_provider(model_name)
    if provider == "anthropic":
        return AnthropicMessagesAPI(model_id)
    return OpenAIResponsesAPI(model_id)


def parse_model_spec(spec: str) -> tuple[str, ReasoningEffort | None]:
    """The model name and reasoning effort of ``[openai/|anthropic/]MODEL[:EFFORT]``.

    The model name keeps its provider prefix. The effort is None when the spec gives none, which
    leaves it to the provider.

    Raises:
        ValueError: The spec's model name is not one ``split_provider`` accepts, or its effort is
            not one behaviors knows.
    """
    model_name, _, effort = spec.partition(":")
    if not model_name.strip():
        raise ValueError(f"A model spec needs a model name: {spec!r}")
    split_provider(model_name)
    efforts: tuple[str, ...] = get_args(ReasoningEffort)
    if effort and effort not in efforts:
        raise ValueError(f"A reasoning effort is one of {', '.join(efforts)}: {spec!r}")
    return model_name, cast(ReasoningEffort, effort) if effort else None


async def store_model_specs(
    storage: FxtrEntityStorage, specs: list[str], *, dim: str, max_tokens: int
) -> tuple[Array[str], Array[BoundID[SamplingParamsEntity]]]:
    """Store each spec's sampling, and return the two inputs of a step mapped over ``dim``.

    Args:
        storage: Where the sampling entities are stored, such as the launcher's client.
        specs: One spec per key of ``dim``, as ``parse_model_spec`` reads them.
        dim: The name of the dimension the specs are the keys of, such as ``"judge"``.
        max_tokens: The output limit of every spec's sampling, reasoning included.

    Returns:
        The model names, and references to the stored ``SamplingParamsEntity`` of each spec. Both
        arrays are keyed by the specs, so ``gpt-x`` and ``gpt-x:high`` are two keys with the same
        model name.

    Raises:
        ValueError: ``specs`` is empty or repeats a spec, ``parse_model_spec`` rejects one, or
            ``max_tokens`` is not positive.
    """
    if not specs or len(set(specs)) != len(specs):
        raise ValueError(f"Provide unique model specs for {dim}: {specs!r}")
    if max_tokens <= 0:
        raise ValueError(f"max_tokens must be positive, not {max_tokens}")
    names: list[tuple[tuple[str], str]] = []
    samplings: list[tuple[tuple[str], BoundID[SamplingParamsEntity]]] = []
    for spec in specs:
        model_name, effort = parse_model_spec(spec)
        reasoning = None if effort is None else ReasoningParams(effort=effort)
        sampling = SamplingParamsEntity(
            params=SamplingParams(max_tokens=max_tokens, reasoning=reasoning)
        )
        names.append(((spec,), model_name))
        samplings.append(((spec,), await storage.store(sampling)))
    dims = f"[{dim}: str]"
    return (
        Array.from_items(names, type=(dims, str)),
        Array.from_items(samplings, type=(dims, BoundID[SamplingParamsEntity])),
    )
