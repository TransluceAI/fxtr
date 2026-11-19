"""Anthropic Messages: the codec's model side (request rendering, SSE decoding, failure
classification) and the thin ``AnthropicMessagesAPI`` transport over the SDK, exercised against
an in-process ``httpx2.MockTransport``."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast

import anyio
import httpx2
import pytest
from anthropic import AsyncAnthropic

from behaviors.implementations.dialects.anthropic_messages import (
    AnthropicMessagesCodec,
    AnthropicRedactedThinking,
    AnthropicSignature,
    AnthropicSseDecoder,
    decode_anthropic_failure,
    decode_anthropic_message,
)
from behaviors.implementations.dialects.gemini_generate_content import GeminiThoughtSignature
from behaviors.implementations.dialects.openai_chat_completions import OpenRouterReasoning
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesReasoning
from behaviors.implementations.models.anthropic import (
    AnthropicMessagesAPI,
)
from behaviors.implementations.models.overrides import (
    FinalBlockCacheControl,
    with_final_block_breakpoint,
)
from behaviors.interfaces.dialect_codecs import CodecError
from behaviors.interfaces.model_apis import ForwardTransportError, ModelError, StreamingModelAPI
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import (
    CacheableContentText,
    CacheableContentToolUse,
    ContentReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.dialect import DialectExtras, Headers
from behaviors.types.failure import ModelCallFailure
from behaviors.types.json import JSONObject
from behaviors.types.message import (
    AssistantMessage,
    CacheableToolMessage,
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
    StopRefusal,
    StopStopSequence,
    StopToolUse,
    Usage,
)
from behaviors.types.rollout import (
    ChatRollout,
    ContextTurn,
    ModelTurn,
    RolloutMaxTurns,
    SetSystemMessage,
)
from behaviors.types.streaming import (
    StreamEvent,
    StreamKeepalive,
    StreamReasoningDelta,
    StreamReasoningStart,
    StreamReasoningState,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
)
from behaviors.types.tool import (
    CacheableToolParam,
    ToolChoice,
    ToolChoiceRequired,
    ToolChoiceTool,
    ToolParam,
)
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio

_CODEC = AnthropicMessagesCodec(default_max_tokens=1000)
_HI = (UserMessage(content=(ContentText(text="hi"),)),)


def _request(
    context: tuple[ChatMessage, ...],
    *,
    tools: tuple[ToolParam, ...] = (),
    tool_choice: ToolChoice | None = None,
    parallel_tool_calls: bool | None = None,
    sampling: SamplingParams | None = None,
    extras: DialectExtras | None = None,
) -> ModelRequest:
    return ModelRequest(
        context=context,
        tools=tools,
        tool_choice=tool_choice,
        parallel_tool_calls=parallel_tool_calls,
        sampling=sampling or SamplingParams(),
        extras=extras,
    )


def _encode(request: ModelRequest, model: str = "claude-test") -> JSONObject:
    """What is sent for `request`: adapted to the dialect, then rendered."""
    adapted = _CODEC.adapt_request(model, request).request
    return _CODEC.encode_request(model, adapted, streaming=True).body


def _state(value: TaggedDataclass) -> DynamicData:
    return DynamicData.from_tagged(value)


def _signed(text: str, signature: str) -> ContentReasoning:
    return ContentReasoning(
        text=text, form="summary", reasoning_state=_state(AnthropicSignature(signature))
    )


@dataclass(frozen=True)
class _ElsewhereState(TaggedDataclass, fxtr_type="com.example.ElsewhereState.v1"):
    token: str


def test_conforms_to_streaming_model_api() -> None:
    api: StreamingModelAPI = AnthropicMessagesAPI("claude-test", api_key="k")
    assert isinstance(api, StreamingModelAPI)
    assert api.dialect == "anthropic_messages"
    assert api.codec.dialect == "anthropic_messages"


# --- request rendering ---------------------------------------------------------------------------


def test_only_anthropic_reasoning_state_is_accepted() -> None:
    accepted = [AnthropicSignature("sig"), AnthropicRedactedThinking("data")]
    rejected = [
        OpenAIResponsesReasoning(id="rs_1", encrypted_content="enc"),
        OpenRouterReasoning(model="m", details=({"type": "reasoning.text"},)),
        GeminiThoughtSignature(model="gemini-3-pro", signature="c2ln", on="thought"),
        _ElsewhereState(token="t"),
    ]
    assert all(_CODEC.accepts_reasoning_state(_state(s), "claude-test") for s in accepted)
    assert not any(_CODEC.accepts_reasoning_state(_state(s), "claude-test") for s in rejected)


def test_reasoning_renders_as_thinking_and_redacted_thinking_blocks() -> None:
    body = _encode(
        _request(
            context=(
                *_HI,
                AssistantMessage(
                    content=(
                        _signed("c", "EqQBCkYIBxgC"),
                        ContentReasoning(
                            text="",
                            form="redacted",
                            reasoning_state=_state(AnthropicRedactedThinking("opaque")),
                        ),
                        ContentText(text="done"),
                    )
                ),
            )
        )
    )
    messages = cast("list[dict[str, Any]]", body["messages"])
    assert messages[1]["content"] == [
        {"type": "thinking", "thinking": "c", "signature": "EqQBCkYIBxgC"},
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "text", "text": "done"},
    ]


def test_speed_renders_as_fast_mode_with_its_beta_header() -> None:
    encoded = _CODEC.encode_request(
        "claude-subject",
        _request(context=_HI, sampling=SamplingParams(speed="fast")),
        streaming=False,
    )
    assert encoded.body["speed"] == "fast"
    assert encoded.headers == (("anthropic-beta", "fast-mode-2026-02-01"),)
    # A harness that already asked for the beta (among others) does not get it twice.
    with_beta = _CODEC.encode_request(
        "claude-subject",
        _request(
            context=_HI,
            sampling=SamplingParams(speed="fast"),
            extras=DialectExtras(
                dialect="anthropic_messages",
                headers=(("anthropic-beta", "context-management-2025-06-27,fast-mode-2026-02-01"),),
            ),
        ),
        streaming=False,
    )
    assert with_beta.headers == (
        ("anthropic-beta", "context-management-2025-06-27,fast-mode-2026-02-01"),
    )
    assert "speed" not in _encode(_request(context=_HI))


def test_speed_is_decoded_and_an_unknown_value_stays_an_extra() -> None:
    body = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    fast = _CODEC.decode_request("/v1/messages", (), json.dumps(body | {"speed": "fast"}).encode())
    assert fast.request.sampling.speed == "fast"
    assert fast.request.extras is None
    odd = _CODEC.decode_request("/v1/messages", (), json.dumps(body | {"speed": "turbo"}).encode())
    assert odd.request.sampling.speed is None
    assert odd.request.extras is not None and odd.request.extras.body == {"speed": "turbo"}


def test_usage_speed_and_refusal_details_round_trip() -> None:
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-subject",
        "content": [{"type": "text", "text": "I can't help with that."}],
        "stop_reason": "refusal",
        "stop_sequence": None,
        "stop_details": {"type": "policy_violation", "policy": "cyber"},
        "usage": {"input_tokens": 3, "output_tokens": 7, "speed": "fast"},
    }
    result = _CODEC.decode_response(json.dumps(message).encode())
    assert isinstance(result, ModelResponse)
    assert result.usage.speed == "fast"
    assert result.stop_reason == StopRefusal(
        provider_reason="refusal",
        complete=None,
        detail={"type": "policy_violation", "policy": "cyber"},
    )
    rendered = _CODEC.encode_response("claude-test", result)
    assert rendered["stop_reason"] == "refusal"
    assert rendered["stop_details"] == {"type": "policy_violation", "policy": "cyber"}
    assert cast("dict[str, Any]", rendered["usage"])["speed"] == "fast"
    # Nothing reported, nothing rendered.
    plain = _CODEC.decode_response(
        json.dumps(
            message
            | {
                "stop_reason": "end_turn",
                "stop_details": None,
                "usage": {"input_tokens": 3, "output_tokens": 7},
            }
        ).encode()
    )
    assert isinstance(plain, ModelResponse) and plain.usage.speed is None
    assert "stop_details" not in _CODEC.encode_response("claude-test", plain)


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-test"])
def test_thinking_off_renders_the_canonical_spelling_for_any_model(model: str) -> None:
    # The codec knows no model: `disabled` always; a model that wants `between_tools` says so
    # in its 400 and the implementation adapts (the transport tests below).
    body = _encode(
        _request(context=_HI, sampling=SamplingParams(reasoning=ReasoningParams(enabled=False))),
        model=model,
    )
    assert body["thinking"] == {"type": "disabled"}


def test_thinking_display_defaults_to_summarized_only_when_thinking_is_on() -> None:
    # Current models return signed but empty thinking blocks unless the request asks for the
    # summarized display, so it is a declared default: filled in when thinking is on and the
    # request said nothing, never when the request (or a harness, through extras) chose.
    adaptive = _encode(_request(context=_HI, sampling=SamplingParams(reasoning=ReasoningParams())))
    assert adaptive["thinking"] == {"type": "adaptive", "display": "summarized"}
    budgeted = _encode(
        _request(
            context=_HI, sampling=SamplingParams(reasoning=ReasoningParams(budget_tokens=1024))
        )
    )
    assert budgeted["thinking"] == {
        "type": "enabled",
        "budget_tokens": 1024,
        "display": "summarized",
    }
    disabled = _encode(
        _request(context=_HI, sampling=SamplingParams(reasoning=ReasoningParams(enabled=False)))
    )
    assert disabled["thinking"] == {"type": "disabled"}
    assert "thinking" not in _encode(_request(context=_HI))
    chosen = _encode(
        _request(
            context=_HI,
            sampling=SamplingParams(reasoning=ReasoningParams()),
            extras=DialectExtras(
                dialect="anthropic_messages", body={"thinking": {"display": "omitted"}}
            ),
        )
    )
    assert chosen["thinking"] == {"type": "adaptive", "display": "omitted"}


def test_render_context_keeps_a_later_system_message_in_the_conversation() -> None:
    # Only the leading system messages are the top-level `system` prompt; one after the
    # conversation has started is rendered where it was, as a `system`-role message.
    body = _encode(
        _request(
            context=(
                SystemMessage(content=(ContentText(text="sys"),)),
                UserMessage(content=(ContentText(text="hi"),)),
                SystemMessage(content=(ContentText(text="reminder"),)),
                AssistantMessage(content=(ContentText(text="ok"),)),
                ToolMessage(content=(ContentText(text="out"),), tool_call_id="t1", is_error=None),
                SystemMessage(content=()),
                UserMessage(content=(ContentText(text="next"),)),
            )
        )
    )
    messages = cast("list[dict[str, Any]]", body["messages"])

    assert body["system"] == [{"type": "text", "text": "sys"}]
    assert [m["role"] for m in messages] == [
        "user",
        "system",
        "assistant",
        "user",
        "system",
        "user",
    ]
    assert messages[1]["content"] == [{"type": "text", "text": "reminder"}]
    # A system message splits the run of user-role messages around it, and an empty one still
    # holds its place (the API's directive-only form has empty content).
    assert messages[3]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "out"}]}
    ]
    assert messages[4] == {"role": "system", "content": []}


def test_render_context_hoists_system_and_merges_user_roles() -> None:
    body = _encode(
        _request(
            context=(
                SystemMessage(
                    content=(CacheableContentText(text="sys", cache_control=CacheControl()),)
                ),
                UserMessage(content=(ContentText(text="hi"),)),
                AssistantMessage(
                    content=(
                        _signed("thought", "sig"),
                        ContentReasoning(text="unsigned", form="summary"),
                        ContentReasoning(text="literal", form="full"),
                        ContentText(text="calling"),
                        ContentToolUse(id="t1", name="run", input={"x": 1}, parse_error=None),
                    )
                ),
                ToolMessage(content=(ContentText(text="ok"),), tool_call_id="t1", is_error=False),
                UserMessage(content=(ContentText(text="next"),)),
            )
        )
    )
    messages = cast("list[dict[str, Any]]", body["messages"])

    assert body["system"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    ]
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
    body = _encode(
        _request(
            context=(
                UserMessage(
                    content=(CacheableContentText(text="", cache_control=CacheControl(ttl="1h")),)
                ),
            )
        )
    )
    messages = cast("list[dict[str, Any]]", body["messages"])
    assert messages[0]["content"] == [{"type": "text", "text": ""}]


def test_tool_param_carries_schema_and_cache_control() -> None:
    body = _encode(
        _request(
            context=_HI,
            tools=(
                CacheableToolParam(
                    name="run",
                    description="d",
                    input_schema={"type": "object", "additionalProperties": False},
                    strict=True,
                    input_examples=None,
                    cache_control=CacheControl(ttl="5m"),
                ),
                ToolParam(
                    name="go", description=None, input_schema={}, strict=False, input_examples=None
                ),
            ),
        )
    )
    assert body["tools"] == [
        {
            "name": "run",
            "input_schema": {"type": "object", "additionalProperties": False},
            "description": "d",
            "strict": True,
            "cache_control": {"type": "ephemeral", "ttl": "5m"},
        },
        {"name": "go", "input_schema": {}},
    ]


def test_tool_result_carries_cache_control() -> None:
    plain = ToolMessage(content=(ContentText(text="ok"),), tool_call_id="t1", is_error=None)
    cached = CacheableToolMessage(
        content=(ContentText(text="ok"),),
        tool_call_id="t1",
        is_error=None,
        cache_control=CacheControl(ttl="1h"),
    )
    body = _encode(_request(context=(plain, cached)))
    (message,) = cast("list[dict[str, Any]]", body["messages"])
    plain_block, cached_block = message["content"]
    assert "cache_control" not in plain_block
    assert cached_block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_tool_use_carries_cache_control() -> None:
    plain = ContentToolUse(id="t1", name="read", input={"path": "a"}, parse_error=None)
    cached = CacheableContentToolUse(
        id="t2", name="read", input={"path": "b"}, parse_error=None, cache_control=CacheControl()
    )
    body = _encode(_request(context=(AssistantMessage(content=(plain, cached)),)))
    (message,) = cast("list[dict[str, Any]]", body["messages"])
    plain_block, cached_block = message["content"]
    assert "cache_control" not in plain_block
    assert cached_block["cache_control"] == {"type": "ephemeral"}
    assert cached_block["input"] == {"path": "b"}


def _sampling_body(sampling: SamplingParams) -> JSONObject:
    return _encode(_request(context=_HI, sampling=sampling))


def test_reasoning_params_map_to_thinking_and_output_config() -> None:
    assert "thinking" not in _sampling_body(SamplingParams())
    assert _sampling_body(SamplingParams(reasoning=ReasoningParams(enabled=False)))["thinking"] == {
        "type": "disabled"
    }
    assert _sampling_body(SamplingParams(reasoning=ReasoningParams(budget_tokens=2048)))[
        "thinking"
    ] == {"type": "enabled", "budget_tokens": 2048, "display": "summarized"}
    assert _sampling_body(SamplingParams(reasoning=ReasoningParams()))["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
    }

    assert "output_config" not in _sampling_body(SamplingParams())
    assert "output_config" not in _sampling_body(
        SamplingParams(reasoning=ReasoningParams(budget_tokens=2048))
    )
    assert "output_config" not in _sampling_body(
        SamplingParams(reasoning=ReasoningParams(enabled=False, effort="high"))
    )
    assert _sampling_body(SamplingParams(reasoning=ReasoningParams(effort="max")))[
        "output_config"
    ] == {"effort": "max"}
    assert _sampling_body(SamplingParams(reasoning=ReasoningParams(effort="minimal")))[
        "output_config"
    ] == {"effort": "low"}


def test_tool_choice_inverts_parallel_tool_calls() -> None:
    tool = ToolParam(
        name="go", description=None, input_schema={}, strict=False, input_examples=None
    )
    assert _encode(_request(context=_HI, tools=(tool,), parallel_tool_calls=False))[
        "tool_choice"
    ] == {"type": "auto", "disable_parallel_tool_use": True}
    assert _encode(
        _request(
            context=_HI, tools=(tool,), tool_choice=ToolChoiceRequired(), parallel_tool_calls=True
        )
    )["tool_choice"] == {"type": "any", "disable_parallel_tool_use": False}
    assert "tool_choice" not in _encode(_request(context=_HI, parallel_tool_calls=False))


def test_sampling_params_are_rendered_as_asked() -> None:
    # Nothing is dropped or clamped on the model's behalf: a model that rejects a combination
    # (temperature with top_p, sampling on a model that takes none) answers with its own error.
    body = _sampling_body(
        SamplingParams(
            max_tokens=5,
            temperature=0.2,
            top_p=0.9,
            stop_sequences=("END",),
            extra={"top_k": 3, "metadata": {"user_id": "u"}},
        )
    )
    assert body["max_tokens"] == 5
    assert body["temperature"] == 0.2
    assert body["top_p"] == 0.9
    assert body["stop_sequences"] == ["END"]
    assert body["top_k"] == 3
    assert body["metadata"] == {"user_id": "u"}
    assert body["stream"] is True
    with pytest.raises(ModelError):
        _sampling_body(SamplingParams(extra={"top_k": "lots"}))


def test_max_tokens_default_applies_only_when_absent() -> None:
    assert _sampling_body(SamplingParams())["max_tokens"] == 1000
    assert _sampling_body(SamplingParams(max_tokens=7))["max_tokens"] == 7
    unstreamed = _CODEC.encode_request("m", _request(context=_HI), streaming=False)
    assert "stream" not in unstreamed.body
    assert unstreamed.path == "/v1/messages"


# --- streamed response decoding ----------------------------------------------------------------


def _sse(event_type: str, data: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


def _message_start(input_tokens: int = 10) -> bytes:
    return _sse(
        "message_start",
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
        },
    )


def _block_start(index: int, block: dict[str, Any]) -> bytes:
    return _sse(
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": block},
    )


def _block_delta(index: int, delta: dict[str, Any]) -> bytes:
    return _sse(
        "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}
    )


def _block_stop(index: int) -> bytes:
    return _sse("content_block_stop", {"type": "content_block_stop", "index": index})


def _message_delta(stop_reason: str, stop_sequence: str | None = None) -> bytes:
    return _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": stop_sequence},
            "usage": {"output_tokens": 42},
        },
    )


_MESSAGE_STOP = _sse("message_stop", {"type": "message_stop"})


def _decode(*frames: bytes) -> tuple[list[StreamEvent], ModelResponse | ModelCallFailure]:
    decoder = AnthropicSseDecoder()
    events: list[StreamEvent] = []
    for frame in frames:
        events.extend(decoder.feed(frame))
    return events, decoder.finish()


_FULL_STREAM = (
    _message_start(),
    _sse("ping", {"type": "ping"}),
    _block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
    _block_delta(0, {"type": "thinking_delta", "thinking": "hm"}),
    _block_delta(0, {"type": "signature_delta", "signature": "sig"}),
    _block_stop(0),
    _block_start(1, {"type": "text", "text": ""}),
    _block_delta(1, {"type": "text_delta", "text": "Hello "}),
    _block_delta(1, {"type": "text_delta", "text": "world"}),
    _block_stop(1),
    _block_start(2, {"type": "tool_use", "id": "toolu_1", "name": "run", "input": {}}),
    _block_delta(2, {"type": "input_json_delta", "partial_json": '{"a":'}),
    _block_delta(2, {"type": "input_json_delta", "partial_json": "1}"}),
    _block_stop(2),
    _message_delta("tool_use"),
    _MESSAGE_STOP,
)

_FULL_RESPONSE = ModelResponse(
    id="msg_1",
    output=(
        AssistantMessage(
            content=(
                _signed("hm", "sig"),
                ContentText(text="Hello world"),
                ContentToolUse(id="toolu_1", name="run", input={"a": 1}, parse_error=None),
            )
        ),
    ),
    stop_reason=StopToolUse(),
    usage=Usage(
        input_tokens=10,
        cache_read_input_tokens=3,
        cache_write_input_tokens=2,
        output_tokens=42,
        reasoning_output_tokens=None,
    ),
)


def test_stream_decoding_events_and_response() -> None:
    events, response = _decode(*_FULL_STREAM)
    assert events == [
        StreamReasoningStart(form="summary"),
        StreamReasoningDelta(text="hm"),
        StreamReasoningState(state=_state(AnthropicSignature("sig"))),
        StreamTextDelta(text="Hello "),
        StreamTextDelta(text="world"),
        StreamToolUseStart(index=0, id="toolu_1", name="run"),
        StreamToolUseInputDelta(index=0, partial_json='{"a":'),
        StreamToolUseInputDelta(index=0, partial_json="1}"),
    ]
    assert response == _FULL_RESPONSE


def test_a_streamed_signature_is_one_state_and_redacted_thinking_streams_whole() -> None:
    events, response = _decode(
        _message_start(),
        _block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
        _block_delta(0, {"type": "thinking_delta", "thinking": "hm"}),
        _block_delta(0, {"type": "signature_delta", "signature": "si"}),
        _block_delta(0, {"type": "signature_delta", "signature": "g"}),
        _block_stop(0),
        _block_start(1, {"type": "redacted_thinking", "data": "opaque"}),
        _block_stop(1),
        _message_delta("end_turn"),
        _MESSAGE_STOP,
    )
    redacted = _state(AnthropicRedactedThinking("opaque"))
    assert events == [
        StreamReasoningStart(form="summary"),
        StreamReasoningDelta(text="hm"),
        StreamReasoningState(state=_state(AnthropicSignature("sig"))),
        StreamReasoningStart(form="redacted"),
        StreamReasoningState(state=redacted),
    ]
    assert isinstance(response, ModelResponse)
    assert response.output[0].content == (
        _signed("hm", "sig"),
        ContentReasoning(text="", form="redacted", reasoning_state=redacted),
    )


def test_stream_decoding_is_chunk_boundary_agnostic() -> None:
    whole = b"".join(_FULL_STREAM)
    decoder = AnthropicSseDecoder()
    events: list[StreamEvent] = []
    for i in range(0, len(whole), 7):
        events.extend(decoder.feed(whole[i : i + 7]))
    assert events == _decode(*_FULL_STREAM)[0]
    assert decoder.finish() == _FULL_RESPONSE


def test_raw_message_is_the_message_the_frames_described() -> None:
    decoder = AnthropicSseDecoder()
    assert decoder.raw_message() is None
    for frame in _FULL_STREAM:
        decoder.feed(frame)
    decoder.finish()
    assert decoder.raw_message() == {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [
            {"type": "thinking", "thinking": "hm", "signature": "sig"},
            {"type": "text", "text": "Hello world"},
            {"type": "tool_use", "id": "toolu_1", "name": "run", "input": {"a": 1}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 42,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
        },
    }


def test_thinking_token_counts_are_reasoning_tokens() -> None:
    _, response = _decode(
        _message_start(),
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 9, "output_tokens_details": {"thinking_tokens": 4}},
            },
        ),
    )
    assert isinstance(response, ModelResponse)
    assert response.usage.reasoning_output_tokens == 4


def test_stream_decoding_stop_variants_and_bad_tool_json() -> None:
    _, response = _decode(
        _message_start(),
        _block_start(0, {"type": "tool_use", "id": "t", "name": "run", "input": {}}),
        _block_delta(0, {"type": "input_json_delta", "partial_json": "{oops"}),
        _message_delta("stop_sequence", stop_sequence="END"),
    )
    assert isinstance(response, ModelResponse)
    assert response.stop_reason == StopStopSequence(stop_sequence="END")
    tool_use = response.output[0].content[0]
    assert isinstance(tool_use, ContentToolUse)
    assert tool_use.input == {}
    assert tool_use.parse_error is not None

    _, refusal = _decode(_message_start(), _message_delta("refusal"))
    assert isinstance(refusal, ModelResponse)
    assert refusal.stop_reason == StopRefusal(provider_reason="refusal", complete=None)


def test_stream_error_event_is_a_failure() -> None:
    _, result = _decode(
        _message_start(),
        _sse(
            "error",
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ),
    )
    assert result == ModelCallFailure(
        category="overloaded",
        description="overloaded_error: Overloaded",
        provider_http_status=None,
    )


def test_stream_without_message_start_is_a_failure() -> None:
    _, result = _decode(_MESSAGE_STOP)
    assert isinstance(result, ModelCallFailure)
    assert result.category == "server_error"


def test_non_streamed_message_decodes_like_the_stream() -> None:
    body = json.dumps(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": [
                {"type": "thinking", "thinking": "hm", "signature": "sig"},
                {"type": "text", "text": "Hello world"},
                {"type": "tool_use", "id": "toolu_1", "name": "run", "input": {"a": 1}},
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 42,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            },
        }
    ).encode()
    assert decode_anthropic_message(body) == _FULL_RESPONSE
    with pytest.raises(CodecError):
        _CODEC.decode_response(b"not json")


def test_malformed_frames_are_codec_errors_not_silent_drops() -> None:
    decoder = AnthropicSseDecoder()
    decoder.feed(_message_start())
    with pytest.raises(CodecError, match="not valid JSON"):
        decoder.feed(b'event: content_block_start\ndata: {"type": "content_block_st\n\n')
    with pytest.raises(CodecError, match="not a JSON object"):
        AnthropicSseDecoder().feed(b"data: [1, 2]\n\n")
    # A stream cut off inside a frame is malformed too, at the end.
    truncated = AnthropicSseDecoder()
    truncated.feed(_message_start() + b'event: message_delta\ndata: {"type": "mess')
    with pytest.raises(CodecError):
        truncated.finish()


def test_final_frame_without_terminator_is_still_decoded() -> None:
    decoder = AnthropicSseDecoder()
    for frame in _FULL_STREAM[:-2]:
        decoder.feed(frame)
    decoder.feed(_message_delta("tool_use").rstrip(b"\n"))  # server closed without the blank line
    assert decoder.finish() == _FULL_RESPONSE


def test_text_deltas_keep_unicode_line_separators() -> None:
    events, response = _decode(
        _message_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "a\u2028b"}),
        _message_delta("end_turn"),
    )
    assert events == [StreamTextDelta(text="a\u2028b")]
    assert isinstance(response, ModelResponse)
    assert response.output[0].content == (ContentText(text="a\u2028b"),)


def test_raw_message_keeps_fields_the_vocabulary_does_not_model() -> None:
    decoder = AnthropicSseDecoder()
    start = _sse(
        "message_start",
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
                "container": {"id": "ctr_1"},
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 2,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 2,
                        "ephemeral_1h_input_tokens": 0,
                    },
                    "service_tier": "standard",
                },
            },
        },
    )
    citation = {"type": "char_location", "cited_text": "x", "document_index": 0}
    events: list[StreamEvent] = []
    for frame in (
        start,
        _block_start(0, {"type": "text", "text": "", "citations": []}),
        _block_delta(0, {"type": "text_delta", "text": "Hi"}),
        _block_delta(0, {"type": "citations_delta", "citation": citation}),
        _block_start(
            1, {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}
        ),
        _block_delta(1, {"type": "input_json_delta", "partial_json": '{"query": '}),
        _block_delta(1, {"type": "input_json_delta", "partial_json": '"weather"}'}),
        _message_delta("end_turn"),
    ):
        events.extend(decoder.feed(frame))
    response = decoder.finish()
    assert isinstance(response, ModelResponse)
    # The modeled view drops what it has no slot for: no tool-use events for a server-side
    # tool block, no citations...
    assert events == [StreamTextDelta(text="Hi")]
    assert response.output[0].content == (ContentText(text="Hi"),)
    assert response.usage.cache_write_input_tokens == 2
    # ...while the raw view keeps every field the provider sent, deltas applied.
    raw = decoder.raw_message()
    assert raw is not None
    assert raw["container"] == {"id": "ctr_1"}
    assert raw["content"] == [
        {"type": "text", "text": "Hi", "citations": [citation]},
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "weather"},
        },
    ]
    assert raw["usage"] == {
        "input_tokens": 10,
        "output_tokens": 42,
        "cache_creation_input_tokens": 2,
        "cache_creation": {"ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 0},
        "service_tier": "standard",
    }


def test_encoded_body_shares_nothing_with_caller_owned_data() -> None:
    metadata: JSONObject = {"user_id": "u", "nested": {"k": [1]}}
    schema: JSONObject = {"type": "object", "properties": {"x": {"type": "string"}}}
    request = _request(
        _HI,
        tools=(
            ToolParam(
                name="t", description=None, input_schema=schema, strict=False, input_examples=None
            ),
        ),
        sampling=SamplingParams(extra={"metadata": metadata}),
    )
    body = _encode(request)
    assert body["metadata"] == metadata and body["metadata"] is not metadata
    cast("dict[str, Any]", body["metadata"])["nested"] = "edited"
    cast("list[dict[str, Any]]", body["tools"])[0]["input_schema"]["properties"]["y"] = {}
    assert metadata["nested"] == {"k": [1]}
    assert schema == {"type": "object", "properties": {"x": {"type": "string"}}}
    assert _encode(request)["metadata"] == metadata


# --- failure classification --------------------------------------------------------------------


def _error_body(error_type: str, message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": error_type, "message": message}}).encode()


@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (400, _error_body("invalid_request_error", "prompt is too long: 250000"), "context_length"),
        (
            400,
            _error_body("invalid_request_error", "blocked by content filtering"),
            "content_filter",
        ),
        (400, _error_body("invalid_request_error", "bad request"), "invalid_request"),
        (401, _error_body("authentication_error", "bad key"), "authentication"),
        (404, _error_body("not_found_error", "no such model"), "invalid_request"),
        (429, _error_body("rate_limit_error", "rate limited"), "rate_limited"),
        (500, _error_body("api_error", "Overloaded"), "overloaded"),
        (529, _error_body("overloaded_error", "overloaded"), "overloaded"),
        (500, _error_body("api_error", "boom"), "server_error"),
        (502, b"<html>bad gateway</html>", "server_error"),
    ],
)
def test_status_error_classification(status: int, body: bytes, category: str) -> None:
    failure = decode_anthropic_failure(status, body)
    assert failure.category == category
    assert failure.provider_http_status == status
    assert failure.description


# --- the transport -------------------------------------------------------------------------------


@dataclass
class _Held:
    """An answer the fake endpoint holds back: `arrived` is set when its request reaches the
    endpoint, and the answer goes out once `release` is set."""

    response: httpx2.Response
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Upstream:
    """A fake Anthropic endpoint: canned answers in order, every request kept."""

    answers: list[httpx2.Response | Exception | _Held]
    seen: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, _Held):
            answer.arrived.set()
            await answer.release.wait()
            return answer.response
        return answer

    def api(
        self, default_query: dict[str, object] | None = None, **options: Any
    ) -> AnthropicMessagesAPI:
        """An API over this endpoint; `options` are `AnthropicMessagesAPI` request options."""
        client = AsyncAnthropic(
            api_key="k",
            base_url="https://anthropic.test",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler)),
            default_query=default_query,
        )
        return AnthropicMessagesAPI("claude-test", client=client, **options)


def _stream_response(*frames: bytes) -> httpx2.Response:
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=b"".join(frames)
    )


_BETWEEN_TOOLS_400 = httpx2.Response(
    400,
    content=_error_body(
        "invalid_request_error",
        'To turn thinking off on this model, send "thinking": {"type": "between_tools"} instead '
        'of {"type": "disabled"}. The model does not think before responding. The short updates '
        "it writes between tool calls come back as thinking blocks.",
    ),
)
_NO_OFF_SWITCH_400 = httpx2.Response(
    400,
    content=_error_body(
        "invalid_request_error",
        '"thinking.type.disabled" is not supported for this model. Use "thinking.type.adaptive" '
        'and "output_config.effort" to control thinking behavior.',
    ),
)
_OFF = _request(context=_HI, sampling=SamplingParams(reasoning=ReasoningParams(enabled=False)))


def _thinking_sent(upstream: _Upstream) -> list[Any]:
    return [json.loads(r.content).get("thinking") for r in upstream.seen]


async def test_thinking_off_is_respelled_once_the_api_asks_and_remembered_on_success() -> None:
    # Canonical `disabled` first; the API's instruction to send `between_tools` is followed
    # once, and after it succeeds the instance renders that spelling directly. Thinking on is
    # never touched.
    upstream = _Upstream(
        [_BETWEEN_TOOLS_400, _stream_response(*_FULL_STREAM), _stream_response(*_FULL_STREAM)]
    )
    api = upstream.api()
    assert await api.generate(_OFF) == _FULL_RESPONSE
    assert _thinking_sent(upstream) == [{"type": "disabled"}, {"type": "between_tools"}]
    assert await api.generate(_OFF) == _FULL_RESPONSE
    assert _thinking_sent(upstream)[2:] == [{"type": "between_tools"}]
    upstream.answers.append(_stream_response(*_FULL_STREAM))
    on = _request(context=_HI, sampling=SamplingParams(reasoning=ReasoningParams()))
    assert await api.generate(on) == _FULL_RESPONSE
    assert _thinking_sent(upstream)[3] == {"type": "adaptive", "display": "summarized"}


async def test_an_unconfirmed_respelling_applies_only_to_the_request_it_was_recognized_on() -> None:
    # The retry fails for an unrelated reason: the failure is the result, and the respelling is
    # provisional. The same request goes out respelled; another request does not, until a
    # success confirms the respelling.
    other = _request(
        (UserMessage(content=(ContentText(text="something else"),)),),
        sampling=SamplingParams(reasoning=ReasoningParams(enabled=False)),
    )
    upstream = _Upstream(
        [
            _BETWEEN_TOOLS_400,
            httpx2.Response(429, content=_error_body("rate_limit_error", "rate limited")),
            _stream_response(*_FULL_STREAM),  # another request: not respelled
            _stream_response(*_FULL_STREAM),  # the same request: respelled, and confirmed
            _stream_response(*_FULL_STREAM),  # another request: respelled from now on
        ]
    )
    api = upstream.api()
    first = await api.generate(_OFF)
    assert isinstance(first, ModelCallFailure) and first.category == "rate_limited"
    assert isinstance(await api.generate(other), ModelResponse)
    assert await api.generate(_OFF) == _FULL_RESPONSE
    assert isinstance(await api.generate(other), ModelResponse)
    assert _thinking_sent(upstream) == [
        {"type": "disabled"},
        {"type": "between_tools"},
        {"type": "disabled"},
        {"type": "between_tools"},
        {"type": "between_tools"},
    ]


async def test_a_recommendation_to_use_adaptive_stays_a_failure() -> None:
    # A model with no off switch recommends adaptive thinking, which is not what was asked:
    # one request, the classified failure returned as is.
    upstream = _Upstream([_NO_OFF_SWITCH_400])
    result = await upstream.api().generate(_OFF)
    assert isinstance(result, ModelCallFailure) and result.category == "invalid_request"
    assert "thinking.type.adaptive" in result.description
    assert _thinking_sent(upstream) == [{"type": "disabled"}]


async def test_concurrent_first_calls_each_keep_their_retry() -> None:
    # Two calls send the canonical spelling; the second learns `between_tools` while the
    # first's 400 is still in flight. The first call's retry rests on what it sent, not on
    # the cache it finds afterwards, so both succeed and both cache the same spelling.
    held = _Held(_BETWEEN_TOOLS_400)
    upstream = _Upstream(
        [
            held,
            _BETWEEN_TOOLS_400,
            _stream_response(*_FULL_STREAM),
            _stream_response(*_FULL_STREAM),
        ]
    )
    api = upstream.api()
    first = asyncio.create_task(api.generate(_OFF))
    await held.arrived.wait()
    assert await api.generate(_OFF) == _FULL_RESPONSE
    held.release.set()
    assert await first == _FULL_RESPONSE
    assert _thinking_sent(upstream) == [
        {"type": "disabled"},
        {"type": "disabled"},
        {"type": "between_tools"},
        {"type": "between_tools"},
    ]


async def test_the_instruction_in_a_started_stream_is_not_retried() -> None:
    # The same words as an in-stream error event, after text has reached the callback: a retry
    # would deliver a response on top of what was streamed, so the failure stands as is.
    frames = (
        _message_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _block_delta(0, {"type": "text_delta", "text": "Hello "}),
        _sse("error", json.loads(_BETWEEN_TOOLS_400.content)),
    )
    upstream = _Upstream([_stream_response(*frames)])
    delivered: list[StreamEvent] = []

    async def collect(event: StreamEvent) -> None:
        delivered.append(event)

    result = await upstream.api().generate_with_events(_OFF, collect)
    assert isinstance(result, ModelCallFailure)
    assert result.category == "invalid_request" and result.provider_http_status is None
    assert "between_tools" in result.description
    assert delivered == [StreamTextDelta(text="Hello ")]
    assert _thinking_sent(upstream) == [{"type": "disabled"}]


async def test_transport_sends_the_encoded_body_and_decodes_the_stream() -> None:
    upstream = _Upstream([_stream_response(*_FULL_STREAM)])
    api = upstream.api()
    seen_events: list[StreamEvent] = []

    async def collect(event: StreamEvent) -> None:
        seen_events.append(event)

    result = await api.generate_with_events(
        _request(context=_HI, sampling=SamplingParams(temperature=0.5)), collect
    )
    assert result == _FULL_RESPONSE
    assert seen_events == _decode(*_FULL_STREAM)[0]
    (request,) = upstream.seen
    assert str(request.url) == "https://anthropic.test/v1/messages"
    assert {k: v for k, v in request.headers.items() if k in {"x-api-key", "authorization"}} in (
        {"x-api-key": "k"},
        {"authorization": "Bearer k"},
    )
    assert json.loads(request.content) == {
        "model": "claude-test",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "temperature": 0.5,
        "stream": True,
        "max_tokens": 16384,
    }


class _ChunkedBody(httpx2.AsyncByteStream):
    """A response body delivered one frame per chunk, as a live provider stream arrives."""

    def __init__(self, frames: tuple[bytes, ...]) -> None:
        self._frames = frames

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield frame


async def test_chunks_without_content_are_relayed_as_keepalives() -> None:
    response = httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_ChunkedBody(tuple(_FULL_STREAM)),
    )
    api = _Upstream([response]).api()
    seen_events: list[StreamEvent] = []

    async def collect(event: StreamEvent) -> None:
        seen_events.append(event)

    result = await api.generate_with_events(_request(context=_HI), collect)
    assert result == _FULL_RESPONSE
    content = [event for event in seen_events if not isinstance(event, StreamKeepalive)]
    assert content == _decode(*_FULL_STREAM)[0]  # the same events as the stream read whole
    # The ping frame, among others, decodes to nothing and arrives as a keepalive.
    assert len(seen_events) - len(content) >= 1


class _SlowCloseBody(httpx2.AsyncByteStream):
    """A response body that sends its first frame and then nothing, and whose close takes an
    await to complete (as a real connection's does)."""

    def __init__(self, first: bytes) -> None:
        self._first = first
        self.close_started = False
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        await anyio.sleep_forever()

    async def aclose(self) -> None:
        self.close_started = True
        await anyio.sleep(0.05)
        self.closed = True


async def test_a_cancelled_stream_still_finishes_closing_the_upstream_response() -> None:
    body = _SlowCloseBody(_FULL_STREAM[0])
    response = httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
    api = _Upstream([response]).api()
    started = anyio.Event()

    async def collect(event: StreamEvent) -> None:
        started.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(api.generate_with_events, _request(context=_HI), collect)
        with anyio.fail_after(5):
            await started.wait()
        tasks.cancel_scope.cancel()  # the harness went away; the call is cancelled
    assert body.close_started and body.closed, "the upstream close was cut off"


async def test_transport_keeps_repeated_extras_headers_and_query() -> None:
    upstream = _Upstream([_stream_response(*_FULL_STREAM)])
    request = _request(
        _HI,
        extras=DialectExtras(
            dialect="anthropic_messages",
            headers=(("anthropic-beta", "flag-a"), ("anthropic-beta", "flag-b")),
            query=(("beta", "true"), ("flag", "a"), ("flag", "b")),
        ),
    )
    result = await upstream.api().generate(request)
    assert isinstance(result, ModelResponse)
    (sent,) = upstream.seen
    # Both values reach the provider: joined as one list-valued header, repeated in the query.
    assert sent.headers["anthropic-beta"] == "flag-a, flag-b"
    assert str(sent.url) == "https://anthropic.test/v1/messages?beta=true&flag=a&flag=b"


async def test_transport_keeps_the_clients_default_query_next_to_the_extras_query() -> None:
    extras = DialectExtras(dialect="anthropic_messages", query=(("flag", "a"), ("flag", "b")))
    upstream = _Upstream([_stream_response(*_FULL_STREAM), _stream_response(*_FULL_STREAM)])
    api = upstream.api(default_query={"api-version": "2"})
    assert isinstance(await api.generate(_request(_HI, extras=extras)), ModelResponse)
    assert isinstance(await api.generate(_request(_HI)), ModelResponse)
    with_extras, without = upstream.seen
    # The SDK's configured defaults stay on the URL in both cases, next to the extras' repeats.
    assert str(with_extras.url) == "https://anthropic.test/v1/messages?api-version=2&flag=a&flag=b"
    assert str(without.url) == "https://anthropic.test/v1/messages?api-version=2"


async def test_transport_returns_status_errors_as_failures_without_retrying() -> None:
    upstream = _Upstream(
        [httpx2.Response(529, content=_error_body("overloaded_error", "Overloaded"))]
    )
    result = await upstream.api().generate(_request(context=_HI))
    assert result == ModelCallFailure(
        category="overloaded",
        description="overloaded_error: Overloaded",
        provider_http_status=529,
    )
    assert len(upstream.seen) == 1  # the SDK would have retried a 529 on its own


async def test_transport_returns_connection_errors_as_overloaded() -> None:
    upstream = _Upstream([httpx2.ConnectError("refused")])
    result = await upstream.api().generate(_request(context=_HI))
    assert isinstance(result, ModelCallFailure)
    assert result.category == "overloaded"
    assert result.provider_http_status is None


# --- forwarding ----------------------------------------------------------------------------------

_HARNESS_HEADERS: Headers = (
    ("host", "proxy.test"),
    ("content-type", "application/json"),
    ("content-length", "99"),
    ("accept-encoding", "gzip"),
    ("x-api-key", "run-key"),
    ("authorization", "Bearer run-key"),
    ("proxy-authorization", "Basic xyz"),
    ("connection", "keep-alive, x-hop-token"),
    ("x-hop-token", "per-connection"),
    ("user-agent", "harness/9.9"),
    ("anthropic-version", "2023-06-01"),
    ("anthropic-beta", "flag-a"),
    ("anthropic-beta", "flag-b"),
)
_HARNESS_BODY = json.dumps(
    {"model": "claude-harness", "max_tokens": 5, "messages": [], "metadata": {"user_id": "u"}}
).encode()


async def test_forward_request_sends_the_harness_request_with_this_apis_credentials() -> None:
    upstream = _Upstream([_stream_response(*_FULL_STREAM)])
    api = upstream.api(default_query={"api-version": "2"})
    forwarded = await api.forward_request("/v1/messages?beta=true", _HARNESS_HEADERS, _HARNESS_BODY)
    (sent,) = upstream.seen
    assert str(sent.url) == "https://anthropic.test/v1/messages?api-version=2&beta=true"
    # SDK 1.x and 2.x spell API-key auth differently; either must use this client's key and
    # leave no harness credential on the request, including under the other spelling.
    assert {k: v for k, v in sent.headers.items() if k in {"x-api-key", "authorization"}} in (
        {"x-api-key": "k"},
        {"authorization": "Bearer k"},
    )
    assert "proxy-authorization" not in sent.headers
    assert "x-hop-token" not in sent.headers
    assert sent.headers["accept-encoding"] == "identity"
    assert sent.headers["user-agent"] == "harness/9.9"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert sent.headers["anthropic-beta"] == "flag-a, flag-b"
    assert json.loads(sent.content) == {
        "model": "claude-test",  # rewritten to this API's model
        "max_tokens": 5,
        "messages": [],
        "metadata": {"user_id": "u"},
    }
    assert forwarded.status == 200
    assert dict(forwarded.headers)["content-type"] == "text/event-stream"
    assert "content-length" not in dict(forwarded.headers)
    assert b"".join([chunk async for chunk in forwarded.body]) == b"".join(_FULL_STREAM)
    await forwarded.aclose()


async def test_forward_request_returns_a_non_success_status_as_is() -> None:
    body = _error_body("overloaded_error", "Overloaded")
    upstream = _Upstream(
        [httpx2.Response(529, headers={"content-type": "application/json"}, content=body)]
    )
    forwarded = await upstream.api().forward_request(
        "/v1/messages", _HARNESS_HEADERS, _HARNESS_BODY
    )
    assert forwarded.status == 529
    assert b"".join([chunk async for chunk in forwarded.body]) == body
    await forwarded.aclose()
    assert len(upstream.seen) == 1  # no retry


@pytest.mark.parametrize(
    "target", ["https://evil.example/v1/messages", "//evil.example/v1/messages", "v1/messages"]
)
async def test_forward_request_refuses_a_target_that_is_not_a_path(target: str) -> None:
    upstream = _Upstream([_stream_response(*_FULL_STREAM)])
    with pytest.raises(ValueError, match="path with an optional query"):
        await upstream.api().forward_request(target, _HARNESS_HEADERS, _HARNESS_BODY)
    assert upstream.seen == []


async def test_forward_request_keeps_the_clients_timeout() -> None:
    upstream = _Upstream([_stream_response(*_FULL_STREAM)])
    client = AsyncAnthropic(
        api_key="k",
        base_url="https://anthropic.test",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler)),
        timeout=12.5,
    )
    api = AnthropicMessagesAPI("claude-test", client=client)
    forwarded = await api.forward_request("/v1/messages", _HARNESS_HEADERS, _HARNESS_BODY)
    await forwarded.aclose()
    (sent,) = upstream.seen
    assert sent.extensions["timeout"] == {
        "connect": 12.5,
        "read": 12.5,
        "write": 12.5,
        "pool": 12.5,
    }


async def test_forward_request_raises_on_transport_failure() -> None:
    upstream = _Upstream([httpx2.ConnectError("refused")])
    with pytest.raises(ForwardTransportError):
        await upstream.api().forward_request("/v1/messages", _HARNESS_HEADERS, _HARNESS_BODY)


# --- the API's own request option -----------------------------------------------------------------


async def _sent_request(
    request: ModelRequest, **options: Any
) -> tuple[dict[str, Any], httpx2.Headers]:
    """The body and headers `AnthropicMessagesAPI` sends for `request` with `options`."""
    upstream = _Upstream([_stream_response(*_FULL_STREAM)])
    result = await upstream.api(**options).generate(request)
    assert isinstance(result, ModelResponse)
    (sent,) = upstream.seen
    return json.loads(sent.content), sent.headers


async def test_forwarded_requests_go_out_without_the_request_option() -> None:
    upstream = _Upstream([_stream_response(*_FULL_STREAM)])
    api = upstream.api(final_block_cache_control=CacheControl())
    body = {
        **json.loads(_HARNESS_BODY),
        "messages": [{"role": "user", "content": [{"type": "text", "text": "q"}]}],
    }
    forwarded = await api.forward_request(
        "/v1/messages", _HARNESS_HEADERS, json.dumps(body).encode()
    )
    await forwarded.aclose()
    (sent,) = upstream.seen
    assert json.loads(sent.content) == {**body, "model": "claude-test"}


async def test_final_block_cache_control_reaches_the_sent_request() -> None:
    body, _ = await _sent_request(
        _request((UserMessage(content=(ContentText(text="q"),)),)),
        final_block_cache_control=CacheControl(ttl="1h"),
    )
    assert body["messages"][-1]["content"] == [
        {"type": "text", "text": "q", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]


async def test_the_wrapper_sends_what_the_option_sends() -> None:
    request = _request(
        (
            UserMessage(content=(ContentText(text="q"),)),
            AssistantMessage(content=(ContentText(text="a"),)),
            UserMessage(content=(ContentText(text="next"),)),
        )
    )
    with_option, _ = await _sent_request(request, final_block_cache_control=CacheControl())
    upstream = _Upstream([_stream_response(*_FULL_STREAM)])
    result = await FinalBlockCacheControl(upstream.api()).generate(request)
    assert isinstance(result, ModelResponse)
    (sent,) = upstream.seen
    assert json.loads(sent.content) == with_option


def _with_final_block(
    context: tuple[ChatMessage, ...],
    cache_control: CacheControl,
    *,
    tools: tuple[ToolParam, ...] = (),
) -> tuple[list[Any], list[Any]]:
    """The rendered system blocks and messages once the final-block breakpoint is applied and the
    request adapted to Anthropic."""
    request = with_final_block_breakpoint(_request(context, tools=tools), cache_control)
    body = _encode(request)
    return cast(list[Any], body.get("system", [])), cast(list[Any], body["messages"])


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
    assert tool_result[1]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

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
    assert "cache_control" not in tool_use[1]["content"][-1]


def test_breakpoints_anthropic_would_refuse_are_adapted_with_a_delivery_loss() -> None:
    five_minute_system = SystemMessage(
        content=(CacheableContentText(text="sys", cache_control=CacheControl()),)
    )
    question = UserMessage(content=(ContentText(text="q"),))
    # A 1-hour breakpoint after a 5-minute one is shortened to 5 minutes.
    system, messages = _with_final_block((five_minute_system, question), CacheControl(ttl="1h"))
    assert messages[0]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    request = with_final_block_breakpoint(
        _request((five_minute_system, question)), CacheControl(ttl="1h")
    )
    losses = _CODEC.adapt_request("claude-test", request).losses
    assert [(loss.code, loss.semantic) for loss in losses] == [("cache_breakpoint", False)]

    marked_tools = tuple(
        CacheableToolParam(
            name=f"tool{i}",
            description=None,
            input_schema={},
            strict=False,
            input_examples=None,
            cache_control=CacheControl(),
        )
        for i in range(4)
    )
    # A fifth breakpoint: the earliest goes, and the trailing one stays.
    _, messages = _with_final_block((question,), CacheControl(), tools=marked_tools)
    assert messages[0]["content"] == [
        {"type": "text", "text": "q", "cache_control": {"type": "ephemeral"}}
    ]


async def test_a_closed_client_raises_model_error_instead_of_a_failure() -> None:
    upstream = _Upstream([_stream_response(*_FULL_STREAM)])
    api = upstream.api()
    await api._client.close()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ModelError, match="closed"):
        await api.generate(_request(_HI))
    assert upstream.seen == []


@pytest.mark.parametrize("cache_control", [None, CacheControl(ttl="5m")])
async def test_stored_cache_hints_reach_provider_request(
    cache_control: CacheControl | None,
) -> None:
    block = CacheableContentText(text="Reusable instructions", cache_control=cache_control)
    tool = CacheableToolParam(
        name="lookup",
        description=None,
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        strict=False,
        input_examples=({"query": "example"},),
        cache_control=cache_control,
    )
    result = CacheableToolMessage(
        content=(ContentText(text="found"),),
        tool_call_id="t1",
        is_error=None,
        cache_control=cache_control,
    )
    initialization = SetSystemMessage(message=SystemMessage(content=(block,)))
    rollout = ChatRollout(
        context_turns=(
            ContextTurn(messages=(UserMessage(content=(block,)),), available_tools=(tool,)),
            ContextTurn(messages=(result,), available_tools=(tool,)),
        ),
        model_turns=(
            ModelTurn(
                messages=(
                    AssistantMessage(
                        content=(
                            ContentToolUse(id="t1", name="lookup", input={}, parse_error=None),
                        )
                    ),
                )
            ),
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
    restored_result = restored.context_turns[1].messages[0]
    assert type(restored_block) is CacheableContentText
    assert type(restored_tool) is CacheableToolParam
    assert type(restored_result) is CacheableToolMessage
    assert restored_block.cache_control == cache_control
    assert restored_tool.cache_control == cache_control
    assert restored_result.cache_control == cache_control

    context = (
        restored_initialization.message,
        *restored.context_turns[0].messages,
        *restored.model_turns[0].messages,
        restored_result,
    )
    body = _encode(_request(context, tools=(restored_tool,)))
    expected_marker = None if cache_control is None else {"type": "ephemeral", "ttl": "5m"}
    rendered = cast(dict[str, Any], body)
    assert rendered["system"][0].get("cache_control") == expected_marker
    assert rendered["messages"][0]["content"][0].get("cache_control") == expected_marker
    assert rendered["messages"][-1]["content"][-1].get("cache_control") == expected_marker
    assert rendered["tools"][0].get("cache_control") == expected_marker
    assert "$type" not in json.dumps(rendered)


def test_grouped_tools_render_under_their_own_names_unless_they_collide() -> None:
    def tool(name: str, namespace: str | None) -> ToolParam:
        return ToolParam(
            name=name,
            description=None,
            input_schema={"type": "object"},
            strict=False,
            input_examples=None,
            namespace=namespace,
        )

    tools = (
        tool("spawn", "collaboration"),
        tool("status", "collaboration"),
        tool("status", "workflows"),
    )
    context = (
        *_HI,
        AssistantMessage(
            content=(
                ContentToolUse(
                    id="c1", name="status", input={}, parse_error=None, namespace="workflows"
                ),
            )
        ),
        ToolMessage(content=(ContentText(text="ok"),), tool_call_id="c1", is_error=None),
    )
    body = _encode(_request(context, tools=tools))
    assert [t["name"] for t in cast("list[dict[str, Any]]", body["tools"])] == [
        "spawn",
        "collaboration__status",
        "workflows__status",
    ]
    messages = cast("list[dict[str, Any]]", body["messages"])
    assert messages[1]["content"][0]["name"] == "workflows__status"


def test_two_tools_with_one_flattened_name_are_refused() -> None:
    def tool(name: str, namespace: str | None) -> ToolParam:
        return ToolParam(
            name=name,
            description=None,
            input_schema={"type": "object"},
            strict=False,
            input_examples=None,
            namespace=namespace,
        )

    tools = (tool("status", "a"), tool("status", "b"), tool("a__status", None))
    with pytest.raises(CodecError, match="collide once namespaces are flattened"):
        _encode(_request(_HI, tools=tools, tool_choice=ToolChoiceTool(name="a__status")))


def test_a_forced_choice_of_a_grouped_tool_uses_its_wire_name() -> None:
    def tool(name: str, namespace: str | None) -> ToolParam:
        return ToolParam(
            name=name,
            description=None,
            input_schema={"type": "object"},
            strict=False,
            input_examples=None,
            namespace=namespace,
        )

    tools = (tool("run", "a"), tool("run", "b"), tool("go", "a"))
    body = _encode(_request(_HI, tools=tools, tool_choice=ToolChoiceTool(name="go")))
    assert cast("dict[str, Any]", body["tool_choice"])["name"] == "go"
    with pytest.raises(CodecError, match="declared in several namespaces"):
        _encode(_request(_HI, tools=tools, tool_choice=ToolChoiceTool(name="run")))


def test_a_grouped_tool_carries_its_group_description_when_flattened() -> None:
    def tool(name: str, namespace: str | None, description: str | None) -> ToolParam:
        return ToolParam(
            name=name,
            description=description,
            input_schema={"type": "object"},
            strict=False,
            input_examples=None,
            namespace=namespace,
            namespace_description=(
                "Paths must be relative to the workspace. Use read before edit."
                if namespace
                else None
            ),
        )

    tools = (
        tool("edit", "files", "Edit a file."),
        tool("ls", "files", None),
        tool("top", None, "Top."),
    )
    body = _encode(_request(_HI, tools=tools))
    assert [t.get("description") for t in cast("list[dict[str, Any]]", body["tools"])] == [
        "Part of the `files` tool group: Paths must be relative to the workspace. Use read "
        "before edit.\n\nEdit a file.",
        "Part of the `files` tool group: Paths must be relative to the workspace. Use read "
        "before edit.",
        "Top.",
    ]


def test_summary_choice_renders_as_thinking_display() -> None:
    def thinking(reasoning: ReasoningParams) -> JSONObject:
        body = _encode(_request(context=_HI, sampling=SamplingParams(reasoning=reasoning)))
        return cast("JSONObject", body["thinking"])

    assert thinking(ReasoningParams(summary=True)) == {"type": "adaptive", "display": "summarized"}
    assert thinking(ReasoningParams(summary=False)) == {"type": "adaptive", "display": "omitted"}
    assert thinking(ReasoningParams(budget_tokens=1024, summary=False)) == {
        "type": "enabled",
        "budget_tokens": 1024,
        "display": "omitted",
    }
    # Unspecified: the declared default fills in; disabled: no display at all.
    assert thinking(ReasoningParams()) == {"type": "adaptive", "display": "summarized"}
    assert thinking(ReasoningParams(enabled=False, summary=True)) == {"type": "disabled"}


def test_thinking_display_decodes_to_the_summary_choice() -> None:
    def decoded(thinking: dict[str, Any]) -> ReasoningParams | None:
        body: dict[str, Any] = {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": thinking,
        }
        request = _CODEC.decode_request("/v1/messages", (), json.dumps(body).encode()).request
        assert request.extras is None or "thinking" not in request.extras.body
        return request.sampling.reasoning

    assert decoded({"type": "adaptive", "display": "summarized"}) == ReasoningParams(summary=True)
    assert decoded({"type": "adaptive", "display": "omitted"}) == ReasoningParams(summary=False)
    assert decoded({"type": "enabled", "budget_tokens": 2048, "display": "omitted"}) == (
        ReasoningParams(budget_tokens=2048, summary=False)
    )
    assert decoded({"type": "adaptive"}) == ReasoningParams()
