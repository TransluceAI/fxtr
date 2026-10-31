"""Unit tests for the Anthropic ModelAPI implementation.

These cover the pure mappings where a mistake would be silent against the live
API: request rendering (message/tool/cache-breakpoint conversion), stream
assembly (events and the assembled response), and exception classification.
SDK request and stream lifecycle tests use mocked HTTP transports.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import (
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
)

from behaviors.implementations.models.anthropic import (
    AnthropicMessagesAPI,
    _add_final_block_cache_control,  # pyright: ignore[reportPrivateUsage]
    _failure_from_exception,  # pyright: ignore[reportPrivateUsage]
    _output_config_param,  # pyright: ignore[reportPrivateUsage]
    _render_context,  # pyright: ignore[reportPrivateUsage]
    _sampling_body,  # pyright: ignore[reportPrivateUsage]
    _StreamAssembler,  # pyright: ignore[reportPrivateUsage]
    _thinking_param,  # pyright: ignore[reportPrivateUsage]
    _tool_choice_param,  # pyright: ignore[reportPrivateUsage]
    _tool_param,  # pyright: ignore[reportPrivateUsage]
)
from behaviors.interfaces.model_apis import ModelError, StreamingModelAPI
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import (
    CacheableContentText,
    ContentReasoning,
    ContentSummarizedReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.failure import ModelCallFailure
from behaviors.types.json import JSONObject, JSONValue
from behaviors.types.message import (
    AssistantMessage,
    ChatMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ReasoningParams,
    SamplingParams,
    StopContentFilter,
    StopStopSequence,
    StopToolUse,
)
from behaviors.types.rollout import (
    ChatRollout,
    ContextTurn,
    ModelTurn,
    RolloutMaxTurns,
    SetSystemMessage,
)
from behaviors.types.streaming import (
    StreamSignatureDelta,
    StreamSummarizedReasoningDelta,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
)
from behaviors.types.tool import CacheableToolParam, ToolChoiceRequired, ToolParam
from fxtr.storage.memory_storage import MemoryStorage


def test_conforms_to_streaming_model_api() -> None:
    api: StreamingModelAPI = AnthropicMessagesAPI("claude-test", api_key="k")
    assert isinstance(api, StreamingModelAPI)


def test_render_context_hoists_system_and_merges_user_roles() -> None:
    system, messages = _render_context(
        (
            SystemMessage(
                content=(CacheableContentText(text="sys", cache_control=CacheControl()),)
            ),
            UserMessage(content=(ContentText(text="hi"),)),
            AssistantMessage(
                content=(
                    ContentSummarizedReasoning(text="thought", signature="sig"),
                    ContentSummarizedReasoning(text="unsigned", signature=None),
                    ContentReasoning(text="literal"),
                    ContentText(text="calling"),
                    ContentToolUse(id="t1", name="run", input={"x": 1}, parse_error=None),
                )
            ),
            ToolMessage(content=(ContentText(text="ok"),), tool_call_id="t1", is_error=False),
            UserMessage(content=(ContentText(text="next"),)),
        )
    )

    assert system == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    # Unsigned/literal reasoning has no API representation and is dropped.
    assert messages[1]["content"] == [
        {"type": "thinking", "thinking": "thought", "signature": "sig"},
        {"type": "text", "text": "calling"},
        {"type": "tool_use", "id": "t1", "name": "run", "input": {"x": 1}},
    ]
    # The tool result and the following user message merge into one user message.
    assert messages[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "text", "text": "ok"}],
            "is_error": False,
        },
        {"type": "text", "text": "next"},
    ]


def test_cache_breakpoint_on_empty_text_is_dropped() -> None:
    _, messages = _render_context(
        (
            UserMessage(
                content=(CacheableContentText(text="", cache_control=CacheControl(ttl="1h")),)
            ),
        )
    )
    assert messages[0]["content"] == [{"type": "text", "text": ""}]


def _with_final_block(
    context: tuple[ChatMessage, ...],
    cache_control: CacheControl,
    tools: tuple[ToolParam, ...] = (),
) -> tuple[Any, list[Any]]:
    system, messages = _render_context(context)
    _add_final_block_cache_control(
        [_tool_param(tool) for tool in tools], system, messages, cache_control
    )
    return system, messages


def test_final_block_cache_control_marks_only_the_trailing_block() -> None:
    system, messages = _with_final_block(
        (
            SystemMessage(
                content=(CacheableContentText(text="sys", cache_control=CacheControl(ttl="1h")),)
            ),
            UserMessage(content=(ContentText(text="first"),)),
            AssistantMessage(content=(ContentText(text="reply"),)),
            UserMessage(content=(ContentText(text="a"), ContentText(text="b"))),
        ),
        CacheControl(),
    )
    assert system == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]
    assert messages[0]["content"] == [{"type": "text", "text": "first"}]
    assert messages[2]["content"] == [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}},
    ]


def test_final_block_cache_control_keeps_a_caller_marker_and_skips_ineligible_blocks() -> None:
    final = CacheControl(ttl="1h")
    _, marked = _with_final_block(
        (
            UserMessage(
                content=(CacheableContentText(text="q", cache_control=CacheControl(ttl="5m")),)
            ),
        ),
        final,
    )
    assert marked[0]["content"] == [
        {"type": "text", "text": "q", "cache_control": {"type": "ephemeral", "ttl": "5m"}}
    ]

    _, tool_result = _with_final_block(
        (
            AssistantMessage(
                content=(ContentToolUse(id="t1", name="run", input={}, parse_error=None),)
            ),
            ToolMessage(content=(ContentText(text="ok"),), tool_call_id="t1", is_error=None),
        ),
        final,
    )
    assert tool_result[1]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "text", "text": "ok"}],
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]

    _, empty_text = _with_final_block((UserMessage(content=(ContentText(text=""),)),), final)
    assert empty_text[0]["content"] == [{"type": "text", "text": ""}]

    _, tool_use = _with_final_block(
        (
            UserMessage(content=(ContentText(text="q"),)),
            AssistantMessage(
                content=(ContentToolUse(id="t1", name="run", input={}, parse_error=None),)
            ),
        ),
        final,
    )
    assert tool_use[1]["content"] == [{"type": "tool_use", "id": "t1", "name": "run", "input": {}}]


def test_final_block_cache_control_rejects_breakpoints_anthropic_would_refuse() -> None:
    five_minute_system = SystemMessage(
        content=(CacheableContentText(text="sys", cache_control=CacheControl()),)
    )
    question = UserMessage(content=(ContentText(text="q"),))
    with pytest.raises(ModelError, match="TTL"):
        _with_final_block((five_minute_system, question), CacheControl(ttl="1h"))

    marked_tools = tuple(
        CacheableToolParam(
            name=f"tool{i}",
            description=None,
            input_schema={},
            strict=None,
            input_examples=None,
            cache_control=CacheControl(),
        )
        for i in range(3)
    )
    with pytest.raises(ModelError, match="at most"):
        _with_final_block((five_minute_system, question), CacheControl(), tools=marked_tools)
    # One fewer existing breakpoint leaves room for the trailing one.
    _, messages = _with_final_block((question,), CacheControl(), tools=marked_tools)
    assert messages[0]["content"] == [
        {"type": "text", "text": "q", "cache_control": {"type": "ephemeral"}}
    ]


def test_tool_param_carries_schema_and_cache_control() -> None:
    assert _tool_param(
        CacheableToolParam(
            name="run",
            description="d",
            input_schema={"type": "object"},
            strict=True,
            input_examples=None,
            cache_control=CacheControl(ttl="5m"),
        )
    ) == {
        "name": "run",
        "input_schema": {"type": "object"},
        "description": "d",
        "strict": True,
        "cache_control": {"type": "ephemeral", "ttl": "5m"},
    }
    assert _tool_param(
        ToolParam(name="go", description=None, input_schema={}, strict=None, input_examples=None)
    ) == {"name": "go", "input_schema": {}}


@pytest.mark.anyio
@pytest.mark.parametrize("cache_control", [None, CacheControl(ttl="5m")])
async def test_stored_cache_hints_reach_provider_request(
    cache_control: CacheControl | None,
) -> None:
    block = CacheableContentText(text="Reusable instructions", cache_control=cache_control)
    tool = CacheableToolParam(
        name="lookup",
        description=None,
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        strict=None,
        input_examples=({"query": "example"},),
        cache_control=cache_control,
    )
    initialization = SetSystemMessage(message=SystemMessage(content=(block,)))
    rollout = ChatRollout(
        context_turns=(
            ContextTurn(messages=(UserMessage(content=(block,)),), available_tools=(tool,)),
        ),
        model_turns=(
            ModelTurn(messages=(AssistantMessage(content=(ContentText(text="Hello"),)),)),
        ),
        status=RolloutMaxTurns(max_turns=1),
        end=None,
    )
    store = MemoryStorage()
    restored = await store.load(await store.store(rollout))
    restored_initialization = await store.load(await store.store(initialization))
    restored_block = restored_initialization.message.content[0]
    restored_tools = restored.context_turns[0].available_tools
    assert restored_tools is not None
    restored_tool = restored_tools[0]
    assert type(restored_block) is CacheableContentText
    assert type(restored_tool) is CacheableToolParam
    assert restored_block.cache_control == cache_control
    assert restored_tool.cache_control == cache_control

    system, messages = _render_context(
        (restored_initialization.message, *restored.context_turns[0].messages)
    )
    expected_text: dict[str, object] = {"type": "text", "text": block.text}
    if cache_control is not None:
        expected_text["cache_control"] = {"type": "ephemeral", "ttl": "5m"}
    assert system == [expected_text]
    assert messages == [{"role": "user", "content": [expected_text]}]
    rendered_tool = _tool_param(restored_tool)
    assert rendered_tool == _tool_param(tool)
    assert rendered_tool.get("cache_control") == expected_text.get("cache_control")
    assert "$type" not in rendered_tool


def test_reasoning_params_map_to_thinking_and_output_config() -> None:
    assert isinstance(_thinking_param(None, "summarized"), anthropic.Omit)
    assert _thinking_param(ReasoningParams(enabled=False), "summarized") == {"type": "disabled"}
    assert _thinking_param(ReasoningParams(budget_tokens=2048), "summarized") == {
        "type": "enabled",
        "budget_tokens": 2048,
        "display": "summarized",
    }
    assert _thinking_param(ReasoningParams(), "omitted") == {
        "type": "adaptive",
        "display": "omitted",
    }

    assert isinstance(_output_config_param(None), anthropic.Omit)
    assert isinstance(_output_config_param(ReasoningParams(budget_tokens=2048)), anthropic.Omit)
    assert isinstance(
        _output_config_param(ReasoningParams(enabled=False, effort="high")), anthropic.Omit
    )
    assert _output_config_param(ReasoningParams(effort="max")) == {"effort": "max"}
    assert _output_config_param(ReasoningParams(effort="minimal")) == {"effort": "low"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("sampling", "expected"),
    [
        (SamplingParams(), {}),
        (SamplingParams(temperature=0.0), {"temperature": 0.0}),
        (SamplingParams(top_p=0.0), {"top_p": 0.0}),
        (SamplingParams(extra={"top_k": 0}), {"top_k": 0}),
        (SamplingParams(extra={"top_k": None, "custom": None}), {"custom": None}),
        (SamplingParams(temperature=0.5, extra={"temperature": 0.7}), {"temperature": 0.7}),
        (
            SamplingParams(temperature=0.5, top_p=0.9, extra={"temperature": None, "top_p": None}),
            {"temperature": None, "top_p": None},
        ),
        (
            SamplingParams(reasoning=ReasoningParams(effort="high"), extra={"output_config": None}),
            {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": None},
        ),
        (
            SamplingParams(temperature=0.5, top_p=0.9, extra={"top_k": 5, "custom": True}),
            {"temperature": 0.5, "top_p": 0.9, "top_k": 5, "custom": True},
        ),
    ],
)
async def test_sampling_parameters_reach_http_body(
    sampling: SamplingParams, expected: JSONObject
) -> None:
    requests: list[httpx2.Request] = []
    original_extra = dict(sampling.extra)

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        events = (_message_start(), _message_delta("end_turn"))
        content = "".join(
            f"event: {event.type}\ndata: {event.model_dump_json()}\n\n" for event in events
        )
        return httpx2.Response(200, content=content, headers={"content-type": "text/event-stream"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http_client:
        client = anthropic.AsyncAnthropic(api_key="test-key", http_client=http_client)
        api = AnthropicMessagesAPI("claude-opus-4-7", client=client)
        result = await api.generate(
            ModelRequest(
                context=(UserMessage(content=(ContentText(text="hi"),)),),
                tools=(),
                tool_choice=None,
                parallel_tool_calls=None,
                sampling=sampling,
            )
        )

    assert isinstance(result, ModelResponse)
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "model": "claude-opus-4-7",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "max_tokens": 16384,
        "stream": True,
        **expected,
    }
    assert sampling.extra == original_extra


@pytest.mark.parametrize("top_k", ["invalid", True, 1.5])
def test_non_int_top_k_is_rejected(top_k: JSONValue) -> None:
    with pytest.raises(ModelError):
        _sampling_body(SamplingParams(extra={"top_k": top_k}))


def test_tool_choice_inverts_parallel_tool_calls() -> None:
    assert _tool_choice_param(None, False, has_tools=True) == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }
    assert _tool_choice_param(ToolChoiceRequired(), True, has_tools=True) == {
        "type": "any",
        "disable_parallel_tool_use": False,
    }
    assert isinstance(_tool_choice_param(None, False, has_tools=False), anthropic.Omit)


def _message_start(input_tokens: int = 10) -> RawMessageStartEvent:
    return RawMessageStartEvent.model_validate(
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 2,
                },
            },
        }
    )


def _block_start(index: int, block: dict[str, object]) -> RawContentBlockStartEvent:
    return RawContentBlockStartEvent.model_validate(
        {"type": "content_block_start", "index": index, "content_block": block}
    )


def _block_delta(index: int, delta: dict[str, object]) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent.model_validate(
        {"type": "content_block_delta", "index": index, "delta": delta}
    )


def _message_delta(stop_reason: str, stop_sequence: str | None = None) -> RawMessageDeltaEvent:
    return RawMessageDeltaEvent.model_validate(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": stop_sequence},
            "usage": {"output_tokens": 42, "output_tokens_details": {"thinking_tokens": 17}},
        }
    )


def test_stream_assembly_events_and_response() -> None:
    assembler = _StreamAssembler()
    events = [
        *assembler.handle(_message_start()),
        *assembler.handle(_block_start(0, {"type": "thinking", "thinking": "", "signature": ""})),
        *assembler.handle(_block_delta(0, {"type": "thinking_delta", "thinking": "hm"})),
        *assembler.handle(_block_delta(0, {"type": "signature_delta", "signature": "sig"})),
        *assembler.handle(_block_start(1, {"type": "text", "text": ""})),
        *assembler.handle(_block_delta(1, {"type": "text_delta", "text": "Hello "})),
        *assembler.handle(_block_delta(1, {"type": "text_delta", "text": "world"})),
        *assembler.handle(
            _block_start(2, {"type": "tool_use", "id": "toolu_1", "name": "run", "input": {}})
        ),
        *assembler.handle(_block_delta(2, {"type": "input_json_delta", "partial_json": '{"a":'})),
        *assembler.handle(_block_delta(2, {"type": "input_json_delta", "partial_json": "1}"})),
        *assembler.handle(_message_delta("tool_use")),
    ]

    assert events == [
        StreamSummarizedReasoningDelta(text="hm"),
        StreamSignatureDelta(signature="sig"),
        StreamTextDelta(text="Hello "),
        StreamTextDelta(text="world"),
        StreamToolUseStart(index=0, id="toolu_1", name="run"),
        StreamToolUseInputDelta(index=0, partial_json='{"a":'),
        StreamToolUseInputDelta(index=0, partial_json="1}"),
    ]

    response = assembler.finish()
    assert response.id == "msg_1"
    assert response.output == (
        AssistantMessage(
            content=(
                ContentSummarizedReasoning(text="hm", signature="sig"),
                ContentText(text="Hello world"),
                ContentToolUse(id="toolu_1", name="run", input={"a": 1}, parse_error=None),
            )
        ),
    )
    assert response.stop_reason == StopToolUse()
    assert response.usage.input_tokens == 10
    assert response.usage.cache_read_input_tokens == 3
    assert response.usage.cache_write_input_tokens == 2
    assert response.usage.output_tokens == 42
    assert response.usage.reasoning_output_tokens == 17


def test_stream_assembly_stop_variants_and_bad_tool_json() -> None:
    assembler = _StreamAssembler()
    assembler.handle(_message_start())
    assembler.handle(_block_start(0, {"type": "tool_use", "id": "t", "name": "run", "input": {}}))
    assembler.handle(_block_delta(0, {"type": "input_json_delta", "partial_json": "{oops"}))
    assembler.handle(_message_delta("stop_sequence", stop_sequence="END"))
    response = assembler.finish()
    assert response.stop_reason == StopStopSequence(stop_sequence="END")
    tool_use = response.output[0].content[0]
    assert isinstance(tool_use, ContentToolUse)
    assert tool_use.input == {}
    assert tool_use.parse_error is not None

    refusal = _StreamAssembler()
    refusal.handle(_message_start())
    refusal.handle(_message_delta("refusal"))
    assert refusal.finish().stop_reason == StopContentFilter(provider_reason="refusal")


def _status_error(
    status: int, message: str, body_message: str | None = None
) -> anthropic.APIStatusError:
    response = httpx2.Response(
        status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    body = {"error": {"message": body_message}} if body_message is not None else None
    return anthropic.APIStatusError(message, response=response, body=body)


@pytest.mark.parametrize(
    ("status", "message", "body_message", "category"),
    [
        (400, "prompt is too long: 250000 tokens", None, "context_length"),
        (400, "bad request", "blocked by content filtering policy", "content_filter"),
        (400, "bad request", None, "invalid_request"),
        (401, "bad key", None, "authentication"),
        (404, "no such model", None, "invalid_request"),
        (429, "rate limited", None, "rate_limited"),
        (500, "boom", "Overloaded", "overloaded"),
        (529, "overloaded", None, "overloaded"),
        (500, "boom", None, "server_error"),
    ],
)
def test_status_error_classification(
    status: int, message: str, body_message: str | None, category: str
) -> None:
    failure = _failure_from_exception(_status_error(status, message, body_message))
    assert failure.category == category
    assert failure.provider_http_status == status


def test_connection_errors_classify_as_overloaded() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    for exc in (
        anthropic.APIConnectionError(request=request),
        httpx2.ReadTimeout("mid-stream stall"),
    ):
        failure = _failure_from_exception(exc)
        assert failure.category == "overloaded"
        assert failure.provider_http_status is None


def test_connection_error_from_a_client_on_another_event_loop_raises() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.APIConnectionError(request=request)
    exc.__cause__ = RuntimeError("Event loop is closed")
    with pytest.raises(ModelError):
        _failure_from_exception(exc)


@pytest.mark.anyio
async def test_mid_stream_transport_error_returns_failure() -> None:
    class StallingStream(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            event = _message_start()
            yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()
            raise httpx2.ReadTimeout("mid-stream stall")

    async def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, stream=StallingStream(), headers={"content-type": "text/event-stream"}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http_client:
        client = anthropic.AsyncAnthropic(api_key="test-key", http_client=http_client)
        result = await AnthropicMessagesAPI("claude-test", client=client).generate(
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
