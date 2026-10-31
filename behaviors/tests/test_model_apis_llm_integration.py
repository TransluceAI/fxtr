"""Live integration tests for the provider ModelAPI implementations.

Opt in with `pytest -m llm_integration` and provider API keys in the
environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`).
Each test verifies behavior the unit tests' constructed SDK objects cannot:
that real requests are accepted (rendering, cache breakpoints), that real
streams assemble consistently, and that real error responses classify.
"""

import json
import os
from collections.abc import Callable

import anyio
import pytest

from behaviors.implementations.models.anthropic import AnthropicMessagesAPI
from behaviors.implementations.models.openai_compatible import OpenAICompatibleAPI
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import CacheableContentText, ContentText, ContentToolUse
from behaviors.types.failure import ModelCallFailure
from behaviors.types.message import SystemMessage, UserMessage
from behaviors.types.response import ModelRequest, ModelResponse, SamplingParams, StopToolUse
from behaviors.types.streaming import (
    StreamEvent,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
)
from behaviors.types.tool import ToolChoiceTool, ToolParam

pytestmark = pytest.mark.llm_integration

ANTHROPIC_MODEL = os.environ.get("BEHAVIORS_TEST_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
OPENAI_MODEL = os.environ.get("BEHAVIORS_TEST_OPENAI_MODEL", "gpt-4o-mini")

needs_anthropic = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"
)
needs_openai = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set"
)

type _Api = AnthropicMessagesAPI | OpenAICompatibleAPI

_WEATHER_TOOL = ToolParam(
    name="get_weather",
    description="Get the current weather for a location.",
    input_schema={
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
    },
    strict=None,
    input_examples=None,
)


def _generate(
    make_api: Callable[[], _Api], request: ModelRequest
) -> ModelResponse | ModelCallFailure:
    """Run one generate call and client teardown in a single event loop."""

    async def main() -> ModelResponse | ModelCallFailure:
        api = make_api()
        try:
            return await api.generate(request)
        finally:
            await api.aclose()

    return anyio.run(main)


def _generate_streamed(
    make_api: Callable[[], _Api], request: ModelRequest
) -> tuple[list[StreamEvent], ModelResponse | ModelCallFailure]:
    events: list[StreamEvent] = []

    async def callback(event: StreamEvent) -> None:
        events.append(event)

    async def main() -> ModelResponse | ModelCallFailure:
        api = make_api()
        try:
            return await api.generate_with_events(request, callback)
        finally:
            await api.aclose()

    return events, anyio.run(main)


def _text_request(max_tokens: int = 64) -> ModelRequest:
    return ModelRequest(
        context=(UserMessage(content=(ContentText(text="Reply with the single word: hello"),)),),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(max_tokens=max_tokens),
    )


def _tool_request() -> ModelRequest:
    return ModelRequest(
        context=(UserMessage(content=(ContentText(text="What is the weather in Paris?"),)),),
        tools=(_WEATHER_TOOL,),
        tool_choice=ToolChoiceTool(name="get_weather"),
        parallel_tool_calls=None,
        sampling=SamplingParams(max_tokens=256),
    )


def _assert_text_response(response: ModelResponse | ModelCallFailure) -> None:
    assert isinstance(response, ModelResponse), response
    assert response.id
    assert len(response.output) == 1
    text = "".join(
        block.text for block in response.output[0].content if isinstance(block, ContentText)
    )
    assert "hello" in text.lower()
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0


def _assert_tool_response(response: ModelResponse | ModelCallFailure) -> None:
    assert isinstance(response, ModelResponse), response
    assert response.stop_reason == StopToolUse()
    tool_uses = tuple(
        block for block in response.output[0].content if isinstance(block, ContentToolUse)
    )
    assert len(tool_uses) == 1
    assert tool_uses[0].name == "get_weather"
    assert tool_uses[0].parse_error is None
    assert "paris" in str(tool_uses[0].input.get("location", "")).lower()


def _assert_events_match_response(events: list[StreamEvent], response: ModelResponse) -> None:
    """The relayed deltas must reassemble to exactly what the response contains."""
    streamed_text = "".join(e.text for e in events if isinstance(e, StreamTextDelta))
    response_text = "".join(
        block.text for block in response.output[0].content if isinstance(block, ContentText)
    )
    assert streamed_text == response_text
    starts = [e for e in events if isinstance(e, StreamToolUseStart)]
    tool_uses = [block for block in response.output[0].content if isinstance(block, ContentToolUse)]
    assert [(s.id, s.name) for s in starts] == [(t.id, t.name) for t in tool_uses]
    for ordinal, tool_use in enumerate(tool_uses):
        raw = "".join(
            e.partial_json
            for e in events
            if isinstance(e, StreamToolUseInputDelta) and e.index == ordinal
        )
        assert json.loads(raw or "{}") == tool_use.input


@needs_anthropic
def test_anthropic_text_generation() -> None:
    response = _generate(lambda: AnthropicMessagesAPI(ANTHROPIC_MODEL), _text_request())
    _assert_text_response(response)
    assert isinstance(response, ModelResponse)
    assert response.id.startswith("msg_")


@needs_anthropic
def test_anthropic_streamed_tool_call() -> None:
    events, response = _generate_streamed(
        lambda: AnthropicMessagesAPI(ANTHROPIC_MODEL), _tool_request()
    )
    _assert_tool_response(response)
    assert isinstance(response, ModelResponse)
    _assert_events_match_response(events, response)


@needs_anthropic
def test_anthropic_cache_breakpoint_is_accepted_and_reported() -> None:
    # Long enough to clear every model's minimum cacheable prefix (~4k tokens).
    filler = " ".join(f"fact {i}: the sky is blue" for i in range(600))
    request = ModelRequest(
        context=(
            SystemMessage(
                content=(
                    CacheableContentText(
                        text=f"You are a helpful assistant. Background: {filler}",
                        cache_control=CacheControl(ttl="5m"),
                    ),
                )
            ),
            UserMessage(content=(ContentText(text="Reply with the single word: hello"),)),
        ),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(max_tokens=64),
    )
    response = _generate(lambda: AnthropicMessagesAPI(ANTHROPIC_MODEL), request)
    assert isinstance(response, ModelResponse), response
    # First run writes the cache, later runs read it; either proves the
    # breakpoint reached the API.
    written = response.usage.cache_write_input_tokens or 0
    read = response.usage.cache_read_input_tokens or 0
    assert written + read > 0


@needs_anthropic
def test_anthropic_bad_key_classifies_as_authentication() -> None:
    response = _generate(
        lambda: AnthropicMessagesAPI(ANTHROPIC_MODEL, api_key="not-a-real-key"), _text_request()
    )
    assert isinstance(response, ModelCallFailure), response
    assert response.category == "authentication"
    assert response.provider_http_status == 401


@needs_openai
def test_openai_text_generation() -> None:
    response = _generate(lambda: OpenAICompatibleAPI(OPENAI_MODEL), _text_request())
    _assert_text_response(response)


@needs_openai
def test_openai_streamed_tool_call() -> None:
    events, response = _generate_streamed(
        lambda: OpenAICompatibleAPI(OPENAI_MODEL), _tool_request()
    )
    _assert_tool_response(response)
    assert isinstance(response, ModelResponse)
    _assert_events_match_response(events, response)


@needs_openai
def test_openai_bad_key_classifies_as_authentication() -> None:
    response = _generate(
        lambda: OpenAICompatibleAPI(OPENAI_MODEL, api_key="not-a-real-key"), _text_request()
    )
    assert isinstance(response, ModelCallFailure), response
    assert response.category == "authentication"
    assert response.provider_http_status == 401
