"""Unit tests for the sampling-override model API wrapper."""

from dataclasses import dataclass, field

import pytest

from behaviors.implementations.models.overrides import SamplingOverrides
from behaviors.interfaces.model_apis import StreamingModelAPI, describe
from behaviors.types.dialect import Dialect
from behaviors.types.failure import ModelCallFailure
from behaviors.types.message import AssistantMessage
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ReasoningParams,
    SamplingParams,
    StopEndTurn,
    Usage,
)
from behaviors.types.streaming import StreamEventCallback

pytestmark = pytest.mark.anyio

_RESPONSE = ModelResponse(
    id="r",
    output=(AssistantMessage(content=()),),
    stop_reason=StopEndTurn(),
    usage=Usage(
        input_tokens=0,
        cache_read_input_tokens=None,
        cache_write_input_tokens=None,
        output_tokens=0,
        reasoning_output_tokens=None,
    ),
)


@dataclass
class _RecordingModel:
    seen: list[ModelRequest] = field(default_factory=list[ModelRequest])

    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        self.seen.append(request)
        return _RESPONSE

    async def generate_with_events(
        self, request: ModelRequest, callback: StreamEventCallback
    ) -> ModelResponse | ModelCallFailure:
        self.seen.append(request)
        return _RESPONSE


def _request(sampling: SamplingParams) -> ModelRequest:
    return ModelRequest(
        context=(), tools=(), tool_choice=None, parallel_tool_calls=None, sampling=sampling
    )


async def _noop(event: object) -> None:
    pass


async def test_reasoning_override_replaces_the_request_config() -> None:
    inner = _RecordingModel()
    pinned = ReasoningParams(effort="high")
    api: StreamingModelAPI = SamplingOverrides(inner, reasoning=pinned)

    await api.generate(_request(SamplingParams(reasoning=ReasoningParams(budget_tokens=1024))))
    await api.generate_with_events(_request(SamplingParams(max_tokens=7)), _noop)

    assert [request.sampling.reasoning for request in inner.seen] == [pinned, pinned]
    # Everything else passes through untouched.
    assert inner.seen[1].sampling.max_tokens == 7


async def test_speed_override_pins_the_tier_and_leaves_the_rest() -> None:
    inner = _RecordingModel()
    api: StreamingModelAPI = SamplingOverrides(inner, speed="fast")
    reasoning = ReasoningParams(budget_tokens=1024)
    await api.generate(_request(SamplingParams(reasoning=reasoning, speed="standard")))
    assert inner.seen[0].sampling.speed == "fast"
    assert inner.seen[0].sampling.reasoning == reasoning


async def test_no_override_passes_requests_through() -> None:
    inner = _RecordingModel()
    sampling = SamplingParams(reasoning=ReasoningParams(budget_tokens=1024))
    await SamplingOverrides(inner).generate(_request(sampling))
    assert inner.seen[0].sampling == sampling


class _Described:
    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        raise NotImplementedError

    async def generate_with_events(
        self, request: ModelRequest, callback: StreamEventCallback
    ) -> ModelResponse | ModelCallFailure:
        raise NotImplementedError

    @property
    def model_id(self) -> str:
        return "described-model"

    @property
    def dialect(self) -> Dialect:
        return "openai_responses"


class _Undescribed:
    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        raise NotImplementedError

    async def generate_with_events(
        self, request: ModelRequest, callback: StreamEventCallback
    ) -> ModelResponse | ModelCallFailure:
        raise NotImplementedError


def test_describe_looks_through_overrides() -> None:
    described = describe(SamplingOverrides(inner=SamplingOverrides(inner=_Described())))
    assert described is not None
    assert described.model_id == "described-model"
    assert described.dialect == "openai_responses"


def test_describe_is_none_for_an_undescribed_api() -> None:
    assert describe(SamplingOverrides(inner=_Undescribed())) is None
