"""Unit tests for the OpenAI-compatible ModelAPI implementation.

These cover the pure mappings where a mistake would be silent against a live
endpoint: request rendering, chunk assembly (including the cached-token usage
arithmetic and deferred tool-call starts), provider-reported error objects,
and exception classification.
"""

from collections.abc import AsyncIterator

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletionChunk

from behaviors.implementations.models.openai_common import (
    ProviderReportedError,
    failure_from_exception,
    reasoning_effort,
)
from behaviors.implementations.models.openai_compatible import (
    OpenAICompatibleAPI,
    _render_context,  # pyright: ignore[reportPrivateUsage]
    _StreamAssembler,  # pyright: ignore[reportPrivateUsage]
    _tool_param,  # pyright: ignore[reportPrivateUsage]
)
from behaviors.interfaces.model_apis import ModelError, StreamingModelAPI
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import (
    CacheableContentText,
    ContentReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.failure import ModelCallFailure
from behaviors.types.message import AssistantMessage, SystemMessage, ToolMessage, UserMessage
from behaviors.types.response import ModelRequest, ReasoningParams, SamplingParams, StopToolUse
from behaviors.types.streaming import (
    StreamReasoningDelta,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
)
from behaviors.types.tool import ToolParam


def test_conforms_to_streaming_model_api() -> None:
    api: StreamingModelAPI = OpenAICompatibleAPI("test-model", api_key="k")
    assert isinstance(api, StreamingModelAPI)


def test_reasoning_effort_mapping() -> None:
    assert reasoning_effort(None) is None
    assert reasoning_effort(ReasoningParams()) is None  # provider default
    assert reasoning_effort(ReasoningParams(enabled=False)) == "none"
    assert reasoning_effort(ReasoningParams(effort="medium")) == "medium"
    assert reasoning_effort(ReasoningParams(effort="max")) == "max"
    assert reasoning_effort(ReasoningParams(effort="xhigh")) == "xhigh"
    # Effort wins over a budget; a bare budget maps through the documented cut points.
    assert reasoning_effort(ReasoningParams(budget_tokens=1024, effort="high")) == "high"
    assert reasoning_effort(ReasoningParams(budget_tokens=1024)) == "low"
    assert reasoning_effort(ReasoningParams(budget_tokens=8192)) == "medium"
    assert reasoning_effort(ReasoningParams(budget_tokens=32768)) == "high"


_CONTEXT = (
    SystemMessage(content=(ContentText(text="sys"),)),
    UserMessage(content=(CacheableContentText(text="hi", cache_control=CacheControl(ttl="5m")),)),
    AssistantMessage(
        content=(
            ContentReasoning(text="thinking..."),
            ContentText(text="calling"),
            ContentToolUse(id="call_1", name="run", input={"x": 1}, parse_error=None),
        )
    ),
    ToolMessage(content=(ContentText(text="ok"),), tool_call_id="call_1", is_error=True),
)


def test_render_context_openai_shapes() -> None:
    assert _render_context(_CONTEXT, forward_cache_control=False) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run", "arguments": '{"x": 1}'},
                }
            ],
        },
        {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
    ]


def test_render_context_forwards_cache_control_only_when_enabled() -> None:
    messages = _render_context(_CONTEXT, forward_cache_control=True)
    assert messages[1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral", "ttl": "5m"}}
        ],
    }


def test_tool_param_maps_schema_fields() -> None:
    assert _tool_param(
        ToolParam(
            name="run",
            description="d",
            input_schema={"type": "object"},
            strict=False,
            input_examples=None,
        )
    ) == {
        "type": "function",
        "function": {
            "name": "run",
            "parameters": {"type": "object"},
            "description": "d",
            "strict": False,
        },
    }


def _chunk(payload: dict[str, object]) -> ChatCompletionChunk:
    base: dict[str, object] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [],
    }
    return ChatCompletionChunk.model_validate(base | payload)


def test_chunk_assembly_events_and_response() -> None:
    assembler = _StreamAssembler()
    events = [
        *assembler.handle(
            _chunk(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "reasoning_content": "hm"},
                            "finish_reason": None,
                        }
                    ]
                }
            )
        ),
        *assembler.handle(
            _chunk(
                {"choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]}
            )
        ),
        *assembler.handle(
            _chunk(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_9",
                                        "function": {"name": "run", "arguments": ""},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            )
        ),
        *assembler.handle(
            _chunk(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": '{"x":2}'}}]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
        ),
        *assembler.handle(
            _chunk(
                {
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 7,
                        "total_tokens": 107,
                        "prompt_tokens_details": {"cached_tokens": 30},
                        "completion_tokens_details": {"reasoning_tokens": 4},
                    }
                }
            )
        ),
    ]

    assert events == [
        StreamReasoningDelta(text="hm"),
        StreamTextDelta(text="Hello"),
        StreamToolUseStart(index=0, id="call_9", name="run"),
        StreamToolUseInputDelta(index=0, partial_json='{"x":2}'),
    ]

    response = assembler.finish()
    assert response.id == "chatcmpl-1"
    assert response.output == (
        AssistantMessage(
            content=(
                ContentReasoning(text="hm"),
                ContentText(text="Hello"),
                ContentToolUse(id="call_9", name="run", input={"x": 2}, parse_error=None),
            )
        ),
    )
    assert response.stop_reason == StopToolUse()
    # prompt_tokens is the total including cached tokens; input_tokens is not.
    assert response.usage.input_tokens == 70
    assert response.usage.cache_read_input_tokens == 30
    assert response.usage.output_tokens == 7
    assert response.usage.reasoning_output_tokens == 4


def test_tool_call_start_defers_until_name_is_known() -> None:
    assembler = _StreamAssembler()
    first = assembler.handle(
        _chunk(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"a"'}}]},
                        "finish_reason": None,
                    }
                ]
            }
        )
    )
    assert first == []
    second = assembler.handle(
        _chunk(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"name": "run", "arguments": ":1}"}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
    )
    assert second == [
        StreamToolUseStart(index=0, id="tool_call_0", name="run"),
        StreamToolUseInputDelta(index=0, partial_json='{"a"'),
        StreamToolUseInputDelta(index=0, partial_json=":1}"),
    ]
    response = assembler.finish()
    tool_use = response.output[0].content[0]
    assert isinstance(tool_use, ContentToolUse)
    assert tool_use.input == {"a": 1}


def test_provider_reported_error_chunk_raises() -> None:
    assembler = _StreamAssembler()
    with pytest.raises(ProviderReportedError) as excinfo:
        assembler.handle(_chunk({"error": {"code": 429, "message": "provider is rate limited"}}))
    assert excinfo.value.status == 429


def _status_error(status: int, message: str, code: str | None = None) -> openai.APIStatusError:
    response = httpx2.Response(
        status, request=httpx2.Request("POST", "https://example.test/v1/chat/completions")
    )
    body = {"code": code} if code is not None else None
    return openai.APIStatusError(message, response=response, body=body)


@pytest.mark.parametrize(
    ("status", "message", "code", "category"),
    [
        (400, "this model's maximum context length is 128000 tokens", None, "context_length"),
        (400, "bad request", "context_length_exceeded", "context_length"),
        (400, "flagged", "content_policy_violation", "content_filter"),
        (400, "bad request", None, "invalid_request"),
        (401, "bad key", None, "authentication"),
        (408, "provider timeout", None, "overloaded"),
        (429, "rate limited", None, "rate_limited"),
        (503, "no provider available", None, "overloaded"),
        (500, "boom", None, "server_error"),
    ],
)
def test_status_error_classification(
    status: int, message: str, code: str | None, category: str
) -> None:
    exc = _status_error(status, message, code)
    failure = failure_from_exception(exc)
    assert failure.category == category
    assert failure.provider_http_status == status


def test_provider_reported_and_connection_errors_classify() -> None:
    assert failure_from_exception(ProviderReportedError(503, "down")).category == "overloaded"
    request = httpx2.Request("POST", "https://example.test/v1/chat/completions")
    failure = failure_from_exception(openai.APIConnectionError(request=request))
    assert failure.category == "overloaded"
    assert failure.provider_http_status is None


def test_connection_error_from_a_client_on_another_event_loop_raises() -> None:
    request = httpx2.Request("POST", "https://example.test/v1/chat/completions")
    exc = openai.APIConnectionError(request=request)
    exc.__cause__ = RuntimeError("Event loop is closed")
    with pytest.raises(ModelError):
        failure_from_exception(exc)


@pytest.mark.anyio
async def test_mid_stream_transport_error_returns_failure() -> None:
    class StallingStream(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            chunk = _chunk({"choices": [{"index": 0, "delta": {"content": "hi"}}]})
            yield f"data: {chunk.model_dump_json()}\n\n".encode()
            raise httpx2.ReadTimeout("mid-stream stall")

    async def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, stream=StallingStream(), headers={"content-type": "text/event-stream"}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http_client:
        client = openai.AsyncOpenAI(api_key="test-key", http_client=http_client)
        result = await OpenAICompatibleAPI("test-model", client=client).generate(
            ModelRequest(
                context=(UserMessage(content=(ContentText(text="hi"),)),),
                tools=(),
                tool_choice=None,
                parallel_tool_calls=None,
                sampling=SamplingParams(),
            )
        )

    assert isinstance(result, ModelCallFailure)
    assert result.category == "overloaded"


def test_statusless_api_error_classifies_by_content() -> None:
    # The Responses API delivers quota errors inside an already-200 stream; the SDK
    # raises them as a bare APIError carrying a code but no HTTP status.
    request = httpx2.Request("POST", "https://example.test/v1/responses")
    exc = openai.APIError(
        "You have no credits remaining.", request, body={"code": "credit_balance_exhausted"}
    )
    failure = failure_from_exception(exc)
    assert failure.category == "rate_limited"
    assert failure.provider_http_status is None
    boom = failure_from_exception(openai.APIError("boom", request, body=None))
    assert boom.category == "server_error"
