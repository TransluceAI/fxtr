"""Unit tests for the OpenAI Responses ModelAPI implementation.

These cover the pure mappings where a mistake would be silent against the live
API: request rendering (instructions hoisting, reasoning/tool item shapes),
stream-event relaying, and terminal-response assembly. Failure classification
is shared with the Chat Completions implementation and tested with it. SDK
request and stream lifecycle tests use mocked HTTP transports.
"""

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

import anthropic
import httpx2
import openai
import pytest
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseTextDeltaEvent,
)

from behaviors.implementations.models.anthropic import AnthropicMessagesAPI
from behaviors.implementations.models.openai_common import ProviderReportedError
from behaviors.implementations.models.openai_compatible import OpenAICompatibleAPI
from behaviors.implementations.models.openai_responses import (
    OpenAIResponsesAPI,
    _reasoning_param,  # pyright: ignore[reportPrivateUsage]
    _render_context,  # pyright: ignore[reportPrivateUsage]
    _response_to_model_response,  # pyright: ignore[reportPrivateUsage]
    _StreamState,  # pyright: ignore[reportPrivateUsage]
    _tool_param,  # pyright: ignore[reportPrivateUsage]
)
from behaviors.interfaces.model_apis import StreamingModelAPI
from behaviors.types.content import (
    ContentReasoning,
    ContentRedactedReasoning,
    ContentSummarizedReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.failure import ModelCallFailure
from behaviors.types.message import AssistantMessage, SystemMessage, ToolMessage, UserMessage
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ReasoningParams,
    SamplingParams,
    StopEndTurn,
    StopMaxTokens,
    StopToolUse,
)
from behaviors.types.streaming import (
    StreamEvent,
    StreamSignatureDelta,
    StreamSummarizedReasoningDelta,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
)
from behaviors.types.tool import ToolParam


def test_conforms_to_streaming_model_api() -> None:
    api: StreamingModelAPI = OpenAIResponsesAPI("test-model", api_key="k")
    assert isinstance(api, StreamingModelAPI)


def test_render_context_hoists_instructions_and_preserves_block_order() -> None:
    instructions, items = _render_context(
        (
            SystemMessage(content=(ContentText(text="sys"),)),
            UserMessage(content=(ContentText(text="hi"),)),
            AssistantMessage(
                content=(
                    ContentSummarizedReasoning(text="thought", signature="rs_orig:enc"),
                    ContentSummarizedReasoning(text="unsigned", signature=None),
                    ContentReasoning(text="literal"),
                    ContentRedactedReasoning(data="blob"),
                    ContentText(text="calling"),
                    ContentToolUse(id="call_1", name="run", input={"x": 1}, parse_error=None),
                )
            ),
            ToolMessage(content=(ContentText(text="ok"),), tool_call_id="call_1", is_error=True),
            SystemMessage(content=(ContentText(text="more sys"),)),
        )
    )

    assert instructions == "sys\n\nmore sys"
    # Unsigned/literal/redacted reasoning has no API representation and is dropped.
    assert items == [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {
            "type": "reasoning",
            "id": "rs_orig",
            "summary": [{"type": "summary_text", "text": "thought"}],
            "encrypted_content": "enc",
        },
        {"role": "assistant", "content": [{"type": "output_text", "text": "calling"}]},
        {"type": "function_call", "call_id": "call_1", "name": "run", "arguments": '{"x": 1}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]


def test_tool_param_uses_flattened_shape() -> None:
    assert _tool_param(
        ToolParam(
            name="run",
            description="d",
            input_schema={"type": "object"},
            strict=True,
            input_examples=None,
        )
    ) == {
        "type": "function",
        "name": "run",
        "parameters": {"type": "object"},
        "description": "d",
        "strict": True,
    }


def test_reasoning_param_shapes() -> None:
    assert _reasoning_param(None) is None
    assert _reasoning_param(ReasoningParams(enabled=False)) == {"effort": "none"}
    # Reasoning on always requests summaries; the effort rides along when derivable.
    assert _reasoning_param(ReasoningParams()) == {"summary": "auto"}
    assert _reasoning_param(ReasoningParams(effort="max")) == {"summary": "auto", "effort": "max"}
    assert _reasoning_param(ReasoningParams(budget_tokens=1024)) == {
        "summary": "auto",
        "effort": "low",
    }


_FUNCTION_CALL_ITEM: dict[str, Any] = {
    "type": "function_call",
    "id": "fc_1",
    "call_id": "call_9",
    "name": "run",
    "arguments": '{"x":2}',
    "status": "completed",
}

_REASONING_ITEM: dict[str, Any] = {
    "type": "reasoning",
    "id": "rs_1",
    "summary": [{"type": "summary_text", "text": "hm"}],
    "encrypted_content": "enc-blob",
}


def _response(payload: dict[str, Any]) -> Response:
    base: dict[str, Any] = {
        "id": "resp_1",
        "created_at": 0,
        "model": "test-model",
        "object": "response",
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "status": "completed",
    }
    return Response.model_validate(base | payload)


def test_stream_events_relay_deltas_and_signature() -> None:
    state = _StreamState()
    events = [
        *state.handle(
            ResponseOutputItemAddedEvent.model_validate(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "sequence_number": 0,
                    "item": _FUNCTION_CALL_ITEM | {"arguments": ""},
                }
            )
        ),
        *state.handle(
            ResponseFunctionCallArgumentsDeltaEvent.model_validate(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "output_index": 0,
                    "sequence_number": 1,
                    "delta": '{"x":2}',
                }
            )
        ),
        *state.handle(
            ResponseReasoningSummaryTextDeltaEvent.model_validate(
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": "rs_1",
                    "output_index": 1,
                    "summary_index": 0,
                    "sequence_number": 2,
                    "delta": "hm",
                }
            )
        ),
        *state.handle(
            ResponseOutputItemDoneEvent.model_validate(
                {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "sequence_number": 3,
                    "item": _REASONING_ITEM,
                }
            )
        ),
        *state.handle(
            ResponseTextDeltaEvent.model_validate(
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_1",
                    "output_index": 2,
                    "content_index": 0,
                    "sequence_number": 4,
                    "logprobs": [],
                    "delta": "Hello",
                }
            )
        ),
    ]
    assert events == [
        StreamToolUseStart(index=0, id="call_9", name="run"),
        StreamToolUseInputDelta(index=0, partial_json='{"x":2}'),
        StreamSummarizedReasoningDelta(text="hm"),
        StreamSignatureDelta(signature="rs_1:enc-blob"),
        StreamTextDelta(text="Hello"),
    ]

    completed = ResponseCompletedEvent.model_validate(
        {
            "type": "response.completed",
            "sequence_number": 5,
            "response": _response({"output": [_REASONING_ITEM, _FUNCTION_CALL_ITEM]}),
        }
    )
    assert state.handle(completed) == []
    assert state.response is not None


def test_terminal_response_assembly() -> None:
    response = _response(
        {
            "output": [
                _REASONING_ITEM,
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Hello", "annotations": []}],
                },
                _FUNCTION_CALL_ITEM,
            ],
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 30, "cache_write_tokens": 0},
                "output_tokens": 7,
                "output_tokens_details": {"reasoning_tokens": 4},
                "total_tokens": 107,
            },
        }
    )
    result = _response_to_model_response(response)
    assert result.id == "resp_1"
    assert result.output == (
        AssistantMessage(
            content=(
                ContentSummarizedReasoning(text="hm", signature="rs_1:enc-blob"),
                ContentText(text="Hello"),
                ContentToolUse(id="call_9", name="run", input={"x": 2}, parse_error=None),
            )
        ),
    )
    assert result.stop_reason == StopToolUse()
    # input_tokens is non-cached only; Responses reports the total.
    assert result.usage.input_tokens == 70
    assert result.usage.cache_read_input_tokens == 30
    assert result.usage.output_tokens == 7
    assert result.usage.reasoning_output_tokens == 4


def test_incomplete_response_maps_to_max_tokens() -> None:
    response = _response(
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
    )
    assert _response_to_model_response(response).stop_reason == StopMaxTokens()


def test_error_event_raises_provider_reported_error() -> None:
    state = _StreamState()
    event = ResponseErrorEvent.model_validate(
        {"type": "error", "code": None, "message": "boom", "param": None, "sequence_number": 0}
    )
    with pytest.raises(ProviderReportedError):
        state.handle(event)


# Exercise the common transport contract using real SDKs and mocked HTTP streams.
# This covers retry suppression and cleanup, which pure rendering tests cannot see.
type _Provider = Literal["anthropic", "chat_completions", "responses"]


class _TrackedStream(httpx2.AsyncByteStream):
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for event in self.events:
            # Anthropic dispatches on the SSE event name; OpenAI reads the JSON type.
            name = f"event: {event['type']}\n" if "type" in event else ""
            yield f"{name}data: {json.dumps(event)}\n\n".encode()

    async def aclose(self) -> None:
        self.closed = True


def _stream_events(provider: _Provider) -> list[dict[str, Any]]:
    if provider == "anthropic":
        return [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "hello"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
    if provider == "chat_completions":
        return [
            {
                "id": "chatcmpl_1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test-model",
                "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }
        ]
    return [
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "sequence_number": 0,
            "delta": "hello",
            "logprobs": [],
        },
        {
            "type": "response.completed",
            "sequence_number": 1,
            "response": _response(
                {
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_1",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "hello", "annotations": []}
                            ],
                        }
                    ]
                }
            ).model_dump(),
        },
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["anthropic", "chat_completions", "responses"])
@pytest.mark.parametrize("outcome", ["success", "rate_limited", "callback_error"])
async def test_provider_transport_contract(
    provider: _Provider, outcome: Literal["success", "rate_limited", "callback_error"]
) -> None:
    requests: list[httpx2.Request] = []
    stream = _TrackedStream(_stream_events(provider))

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if outcome == "rate_limited":
            return httpx2.Response(
                429,
                json={"error": {"type": "rate_limit_error", "message": "rate limited"}},
                headers={"retry-after": "0"},
            )
        return httpx2.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http_client:
        api: AnthropicMessagesAPI | OpenAICompatibleAPI | OpenAIResponsesAPI
        if provider == "anthropic":
            client = anthropic.AsyncAnthropic(
                api_key="test-key", http_client=http_client, max_retries=3
            )
            api = AnthropicMessagesAPI("test-model", client=client)
        else:
            openai_client = openai.AsyncOpenAI(
                api_key="test-key", http_client=http_client, max_retries=3
            )
            api = (
                OpenAICompatibleAPI("test-model", client=openai_client)
                if provider == "chat_completions"
                else OpenAIResponsesAPI("test-model", client=openai_client)
            )
        events: list[StreamEvent] = []

        async def callback(event: StreamEvent) -> None:
            if outcome == "callback_error":
                raise ValueError("consumer failed")
            events.append(event)

        request = ModelRequest(
            context=(UserMessage(content=(ContentText(text="hi"),)),),
            tools=(),
            tool_choice=None,
            parallel_tool_calls=None,
            sampling=SamplingParams(max_tokens=7, extra={"test_extension": "sent"}),
        )
        try:
            if outcome == "callback_error":
                with pytest.raises(ValueError, match="consumer failed"):
                    await api.generate_with_events(request, callback)
            else:
                response = await api.generate_with_events(request, callback)
                if outcome == "rate_limited":
                    assert isinstance(response, ModelCallFailure)
                    assert response.category == "rate_limited"
                else:
                    assert isinstance(response, ModelResponse)
                    assert response.output == (
                        AssistantMessage(content=(ContentText(text="hello"),)),
                    )
                    assert response.stop_reason == StopEndTurn()
                    assert events == [StreamTextDelta(text="hello")]
        finally:
            await api.aclose()
        assert not http_client.is_closed  # The caller owns an injected client.
        assert len(requests) == 1  # Retries belong to the behaviors retry wrapper.
        body = json.loads(requests[0].content)
        assert body["model"] == "test-model"
        assert body["stream"] is True
        assert body["test_extension"] == "sent"
        token_field = "max_output_tokens" if provider == "responses" else "max_tokens"
        assert body[token_field] == 7
        if provider == "responses":
            assert body["store"] is False
        if outcome != "rate_limited":
            assert stream.closed
