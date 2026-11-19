"""OpenAI Chat Completions: the codec's model side (request rendering, SSE decoding, failure
classification) and the thin ``OpenAICompatibleAPI`` transport over the SDK, exercised against an
in-process ``httpx2.MockTransport``."""

import json
from dataclasses import dataclass, field
from typing import Any, cast

import httpx2
import pytest
from openai import AsyncOpenAI

from behaviors.implementations.dialects.anthropic_messages import AnthropicSignature
from behaviors.implementations.dialects.openai_chat_completions import (
    ChatCompletionsSseDecoder,
    ChatCompletionsVariant,
    OpenAIChatCompletionsCodec,
    OpenRouterReasoning,
    ReasoningEchoField,
    decode_chat_completion,
    decode_chat_completions_failure,
)
from behaviors.implementations.models.openai_compatible import OpenAICompatibleAPI
from behaviors.interfaces.dialect_codecs import CodecError
from behaviors.interfaces.model_apis import StreamingModelAPI
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import (
    CacheableContentText,
    ContentReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.failure import ModelCallFailure
from behaviors.types.json import JSONObject
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
    StopEndTurn,
    StopToolUse,
)
from behaviors.types.streaming import (
    StreamEvent,
    StreamReasoningDelta,
    StreamReasoningStart,
    StreamReasoningState,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
)
from behaviors.types.tool import ToolChoice, ToolChoiceTool, ToolParam
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData

pytestmark = pytest.mark.anyio

_CODEC = OpenAIChatCompletionsCodec()
_OPENROUTER = OpenAIChatCompletionsCodec(variant="openrouter")
_HI = (UserMessage(content=(ContentText(text="hi"),)),)


def _request(
    context: tuple[ChatMessage, ...],
    *,
    tools: tuple[ToolParam, ...] = (),
    tool_choice: ToolChoice | None = None,
    parallel_tool_calls: bool | None = None,
    sampling: SamplingParams | None = None,
) -> ModelRequest:
    return ModelRequest(
        context=context,
        tools=tools,
        tool_choice=tool_choice,
        parallel_tool_calls=parallel_tool_calls,
        sampling=sampling or SamplingParams(),
    )


def _encode(
    request: ModelRequest, codec: OpenAIChatCompletionsCodec = _CODEC, model: str = "test-model"
) -> JSONObject:
    """What is sent for `request`: adapted to the dialect, then rendered."""
    adapted = codec.adapt_request(model, request).request
    return codec.encode_request(model, adapted, streaming=True).body


def test_conforms_to_streaming_model_api() -> None:
    api: StreamingModelAPI = OpenAICompatibleAPI("test-model", api_key="k")
    assert isinstance(api, StreamingModelAPI)
    assert api.dialect == "openai_chat_completions"
    assert api.codec.dialect == "openai_chat_completions"


# --- request rendering ---------------------------------------------------------------------------


def _effort(reasoning: ReasoningParams | None) -> object:
    body = _encode(_request(context=_HI, sampling=SamplingParams(reasoning=reasoning)))
    return body.get("reasoning_effort")


def test_reasoning_effort_mapping() -> None:
    assert _effort(None) is None
    assert _effort(ReasoningParams()) is None  # provider default
    assert _effort(ReasoningParams(enabled=False)) == "none"
    assert _effort(ReasoningParams(effort="medium")) == "medium"
    # Effort passes through verbatim: models that accept `max` (GPT 5.6, OpenRouter) get it; a
    # model with a lower ceiling validates server-side, so the caller pins an accepted level.
    assert _effort(ReasoningParams(effort="max")) == "max"
    # Effort wins over a budget; a bare budget maps through the documented cut points.
    assert _effort(ReasoningParams(budget_tokens=1024, effort="high")) == "high"
    assert _effort(ReasoningParams(budget_tokens=1024)) == "low"
    assert _effort(ReasoningParams(budget_tokens=8192)) == "medium"
    assert _effort(ReasoningParams(budget_tokens=32768)) == "high"


_CONTEXT = (
    SystemMessage(content=(ContentText(text="sys"),)),
    UserMessage(content=(CacheableContentText(text="hi", cache_control=CacheControl(ttl="5m")),)),
    AssistantMessage(
        content=(
            ContentReasoning(text="thinking...", form="full"),
            ContentText(text="calling"),
            ContentToolUse(id="call_1", name="run", input={"x": 1}, parse_error=None),
        )
    ),
    ToolMessage(content=(ContentText(text="ok"),), tool_call_id="call_1", is_error=True),
)


def _messages(codec: OpenAIChatCompletionsCodec = _CODEC) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", _encode(_request(context=_CONTEXT), codec)["messages"])


def test_render_context_openai_shapes() -> None:
    assert _messages() == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "calling",
            "reasoning_content": "thinking...",
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


def test_render_context_reasoning_echo_field() -> None:
    def assistant(echo_field: ReasoningEchoField | None) -> dict[str, Any]:
        return _messages(OpenAIChatCompletionsCodec(reasoning_echo_field=echo_field))[2]

    assert assistant("reasoning")["reasoning"] == "thinking..."
    assert "reasoning_content" not in assistant("reasoning")
    assert not {"reasoning", "reasoning_content"} & assistant(None).keys()


def test_render_context_forwards_cache_control_only_when_enabled() -> None:
    assert _messages(OpenAIChatCompletionsCodec(forward_cache_control=True))[1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral", "ttl": "5m"}}
        ],
    }


def test_tool_param_maps_schema_fields() -> None:
    body = _encode(
        _request(
            context=_HI,
            tools=(
                ToolParam(
                    name="run",
                    description="d",
                    input_schema={"type": "object"},
                    strict=False,
                    input_examples=None,
                ),
            ),
        )
    )
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "run",
                "parameters": {"type": "object"},
                "description": "d",
            },
        }
    ]


def test_sampling_and_defaults() -> None:
    body = _encode(
        _request(
            context=_HI,
            sampling=SamplingParams(
                max_tokens=5, temperature=0.1, top_p=0.9, stop_sequences=("END",), extra={"seed": 1}
            ),
        ),
        OpenAIChatCompletionsCodec(
            max_tokens_param="max_completion_tokens", extra_body={"seed": 2, "provider": {"x": 1}}
        ),
    )
    assert body["max_completion_tokens"] == 5
    assert "max_tokens" not in body
    assert body["temperature"] == 0.1
    assert body["top_p"] == 0.9
    assert body["stop"] == ["END"]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    # The request's own value wins; extra_body fills in only what is absent.
    assert body["seed"] == 1
    assert body["provider"] == {"x": 1}


# --- streamed response decoding ----------------------------------------------------------------


def _chunk(payload: dict[str, Any]) -> bytes:
    base: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [],
    }
    return f"data: {json.dumps(base | payload)}\n\n".encode()


_DONE = b"data: [DONE]\n\n"


def _decode(
    *frames: bytes, variant: ChatCompletionsVariant = "standard"
) -> tuple[list[StreamEvent], ModelResponse | ModelCallFailure]:
    decoder = ChatCompletionsSseDecoder(variant)
    events: list[StreamEvent] = []
    for frame in frames:
        events.extend(decoder.feed(frame))
    return events, decoder.finish()


def _decode_openrouter(
    *frames: bytes,
) -> tuple[list[StreamEvent], ModelResponse | ModelCallFailure]:
    return _decode(*frames, variant="openrouter")


def _choice(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


_USAGE: dict[str, Any] = {
    "prompt_tokens": 100,
    "completion_tokens": 7,
    "total_tokens": 107,
    "prompt_tokens_details": {"cached_tokens": 30},
    "completion_tokens_details": {"reasoning_tokens": 4},
}

_FULL_STREAM = (
    _chunk(_choice({"role": "assistant", "reasoning_content": "hm"})),
    _chunk(_choice({"content": "Hello"})),
    _chunk(
        _choice(
            {
                "tool_calls": [
                    {"index": 0, "id": "call_9", "function": {"name": "run", "arguments": ""}}
                ]
            }
        )
    ),
    _chunk(
        _choice({"tool_calls": [{"index": 0, "function": {"arguments": '{"x":2}'}}]}, "tool_calls")
    ),
    _chunk({"usage": _USAGE}),
    _DONE,
)

_FULL_EVENTS = [
    StreamReasoningStart(form="full"),
    StreamReasoningDelta(text="hm"),
    StreamTextDelta(text="Hello"),
    StreamToolUseStart(index=0, id="call_9", name="run"),
    StreamToolUseInputDelta(index=0, partial_json='{"x":2}'),
]


def _assert_full_response(result: ModelResponse | ModelCallFailure) -> None:
    assert isinstance(result, ModelResponse)
    assert result.id == "chatcmpl-1"
    assert result.output == (
        AssistantMessage(
            content=(
                ContentReasoning(text="hm", form="full"),
                ContentText(text="Hello"),
                ContentToolUse(id="call_9", name="run", input={"x": 2}, parse_error=None),
            )
        ),
    )
    assert result.stop_reason == StopToolUse()
    # prompt_tokens is the total including cached tokens; input_tokens is not.
    assert result.usage.input_tokens == 70
    assert result.usage.cache_read_input_tokens == 30
    assert result.usage.output_tokens == 7
    assert result.usage.reasoning_output_tokens == 4


# --- OpenRouter reasoning_details --------------------------------------------------------------

_CLAUDE_FORMAT = "anthropic-claude-v1"
_GPT_FORMAT = "openai-responses-v1"


def _details(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"reasoning_details": list(entries)}


def _echoed_details(result: ModelResponse | ModelCallFailure) -> list[dict[str, Any]] | None:
    """What the codec would send back as `reasoning_details` for the response's assistant turn:
    decode → neutral content → encode, the whole path a continuation travels."""
    assert isinstance(result, ModelResponse)
    body = _encode(_request(context=(*_HI, result.output[0])), _OPENROUTER)
    return cast("list[dict[str, Any]]", body["messages"])[1].get("reasoning_details")


def _buffered(
    entries: list[dict[str, Any]], content: str = "ok"
) -> ModelResponse | ModelCallFailure:
    body = {
        "id": "gen-1",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "reasoning_details": entries},
                "finish_reason": "stop",
            }
        ],
    }
    return decode_chat_completion(json.dumps(body).encode(), "openrouter")


def _state(model: str, entries: list[dict[str, Any]]) -> DynamicData:
    return DynamicData.from_tagged(OpenRouterReasoning(model=model, details=tuple(entries)))


def _states(result: ModelResponse | ModelCallFailure) -> list[OpenRouterReasoning]:
    """The OpenRouter state of each reasoning block in the response that carries one."""
    assert isinstance(result, ModelResponse)
    return [
        block.reasoning_state.as_type(OpenRouterReasoning)
        for block in result.output[0].content
        if isinstance(block, ContentReasoning) and block.reasoning_state is not None
    ]


def test_signed_reasoning_text_entries_become_one_block_carrying_them() -> None:
    # Claude through OpenRouter: text fragments, then the signature, all on index 0; the
    # `reasoning` string beside each fragment is the same text and is not read twice.
    events, result = _decode_openrouter(
        _chunk(
            _choice(
                {"role": "assistant", "reasoning": "The user"}
                | _details(
                    {
                        "type": "reasoning.text",
                        "text": "The user",
                        "format": _CLAUDE_FORMAT,
                        "index": 0,
                    }
                )
            )
        ),
        _chunk(
            _choice(
                {"reasoning": " asks."}
                | _details(
                    {
                        "type": "reasoning.text",
                        "text": " asks.",
                        "format": _CLAUDE_FORMAT,
                        "index": 0,
                    }
                )
            )
        ),
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.text",
                        "signature": "SIG",
                        "format": _CLAUDE_FORMAT,
                        "index": 0,
                    }
                )
            )
        ),
        _chunk(_choice({"content": "Ankara."}, "stop")),
        _DONE,
    )
    merged = [
        {
            "type": "reasoning.text",
            "text": "The user asks.",
            "format": _CLAUDE_FORMAT,
            "index": 0,
            "signature": "SIG",
        }
    ]
    state = _state("test-model", merged)
    assert events == [
        StreamReasoningStart(form="full"),
        StreamReasoningDelta(text="The user"),
        StreamReasoningDelta(text=" asks."),
        StreamReasoningState(state=state),
        StreamTextDelta(text="Ankara."),
    ]
    assert isinstance(result, ModelResponse)
    assert result.output[0].content == (
        ContentReasoning(text="The user asks.", form="full", reasoning_state=state),
        ContentText(text="Ankara."),
    )


def test_summary_and_encrypted_entries_form_one_block_sealed_by_the_encrypted_item() -> None:
    # GPT through OpenRouter: summary fragments on index 0, then the encrypted item on index 1.
    events, result = _decode_openrouter(
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.summary",
                        "summary": "**Weather**",
                        "format": _GPT_FORMAT,
                        "index": 0,
                    }
                )
            )
        ),
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.summary",
                        "summary": " in Ankara",
                        "format": _GPT_FORMAT,
                        "index": 0,
                    }
                )
            )
        ),
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.encrypted",
                        "data": "gAAAA",
                        "id": "rs_1",
                        "format": _GPT_FORMAT,
                        "index": 1,
                    }
                )
            )
        ),
        _chunk(_choice({"content": "Sunny."}, "stop")),
        _DONE,
    )
    merged = [
        {
            "type": "reasoning.summary",
            "summary": "**Weather** in Ankara",
            "format": _GPT_FORMAT,
            "index": 0,
        },
        {
            "type": "reasoning.encrypted",
            "data": "gAAAA",
            "id": "rs_1",
            "format": _GPT_FORMAT,
            "index": 1,
        },
    ]
    state = _state("test-model", merged)
    assert events[:4] == [
        StreamReasoningStart(form="summary"),
        StreamReasoningDelta(text="**Weather**"),
        StreamReasoningDelta(text=" in Ankara"),
        StreamReasoningState(state=state),
    ]
    assert isinstance(result, ModelResponse)
    assert result.output[0].content[0] == ContentReasoning(
        text="**Weather** in Ankara", form="summary", reasoning_state=state
    )


def test_unsigned_text_entries_are_literal_reasoning_once() -> None:
    # Kimi through OpenRouter: `reasoning.text` entries of format `unknown` beside the same
    # text in `reasoning`, never a signature: literal, and not doubled.
    events, result = _decode_openrouter(
        _chunk(_choice({"role": "assistant", "content": "", "reasoning": None})),
        _chunk(
            _choice(
                {"reasoning": "Let me"}
                | _details(
                    {"type": "reasoning.text", "text": "Let me", "format": "unknown", "index": 0}
                )
            )
        ),
        _chunk(
            _choice(
                {"reasoning": " think."}
                | _details(
                    {"type": "reasoning.text", "text": " think.", "format": "unknown", "index": 0}
                )
            )
        ),
        _chunk(_choice({"content": "Four."}, "stop")),
        _DONE,
    )
    assert events == [
        StreamReasoningStart(form="full"),
        StreamReasoningDelta(text="Let me"),
        StreamReasoningDelta(text=" think."),
        StreamTextDelta(text="Four."),
    ]
    assert isinstance(result, ModelResponse)
    assert result.output[0].content == (
        ContentReasoning(text="Let me think.", form="full"),
        ContentText(text="Four."),
    )


def test_fragmented_signature_and_format_only_on_the_first_fragment_accumulate() -> None:
    # Anthropic signatures stream in pieces; OpenRouter may put the format only on an entry's
    # first fragment. Both fragments belong to the one entry, which is complete only when
    # something else follows it.
    events, result = _decode_openrouter(
        _chunk(
            _choice(
                _details(
                    {"type": "reasoning.text", "text": "Hm", "format": _CLAUDE_FORMAT, "index": 0}
                )
            )
        ),
        _chunk(_choice(_details({"type": "reasoning.text", "text": ", ok", "index": 0}))),
        _chunk(_choice(_details({"type": "reasoning.text", "signature": "SIG-A", "index": 0}))),
        _chunk(_choice(_details({"type": "reasoning.text", "signature": "-B", "index": 0}))),
        _chunk(_choice({"content": "Yes."}, "stop")),
        _DONE,
    )
    merged = [
        {
            "type": "reasoning.text",
            "text": "Hm, ok",
            "format": _CLAUDE_FORMAT,
            "index": 0,
            "signature": "SIG-A-B",
        }
    ]
    state = _state("test-model", merged)
    assert events == [
        StreamReasoningStart(form="full"),
        StreamReasoningDelta(text="Hm"),
        StreamReasoningDelta(text=", ok"),
        StreamReasoningState(state=state),
        StreamTextDelta(text="Yes."),
    ]
    assert isinstance(result, ModelResponse)
    assert result.output[0].content[0] == ContentReasoning(
        text="Hm, ok", form="full", reasoning_state=state
    )


def test_entries_of_different_types_at_one_index_stay_separate_and_ordered() -> None:
    events, result = _decode_openrouter(
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.summary",
                        "summary": "Plan",
                        "format": _GPT_FORMAT,
                        "index": 0,
                    }
                )
            )
        ),
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.encrypted",
                        "data": "gAAA",
                        "id": "rs_1",
                        "format": _GPT_FORMAT,
                        "index": 0,
                    }
                )
            )
        ),
        _chunk(_choice({"content": "Done."}, "stop")),
        _DONE,
    )
    merged = [
        {"type": "reasoning.summary", "summary": "Plan", "format": _GPT_FORMAT, "index": 0},
        {
            "type": "reasoning.encrypted",
            "data": "gAAA",
            "id": "rs_1",
            "format": _GPT_FORMAT,
            "index": 0,
        },
    ]
    assert events[2] == StreamReasoningState(state=_state("test-model", merged))
    assert _echoed_details(result) == merged


def test_consecutive_signed_blocks_are_separate_and_the_last_completes_at_the_end() -> None:
    # Two thinking blocks, and nothing after the second: the stream's end completes it.
    events, result = _decode_openrouter(
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.text",
                        "text": "One",
                        "format": _CLAUDE_FORMAT,
                        "index": 0,
                        "signature": "S1",
                    }
                )
            )
        ),
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.text",
                        "text": "Two",
                        "format": _CLAUDE_FORMAT,
                        "index": 1,
                        "signature": "S2",
                    }
                )
            )
        ),
        _chunk(_choice({}, "stop")),
        _DONE,
    )
    first = _state(
        "test-model",
        [
            {
                "type": "reasoning.text",
                "text": "One",
                "format": _CLAUDE_FORMAT,
                "index": 0,
                "signature": "S1",
            }
        ],
    )
    second = _state(
        "test-model",
        [
            {
                "type": "reasoning.text",
                "text": "Two",
                "format": _CLAUDE_FORMAT,
                "index": 1,
                "signature": "S2",
            }
        ],
    )
    assert events == [
        StreamReasoningStart(form="full"),
        StreamReasoningDelta(text="One"),
        StreamReasoningState(state=first),
        StreamReasoningStart(form="full"),
        StreamReasoningDelta(text="Two"),
        StreamReasoningState(state=second),
    ]
    assert isinstance(result, ModelResponse)
    assert result.output[0].content == (
        ContentReasoning(text="One", form="full", reasoning_state=first),
        ContentReasoning(text="Two", form="full", reasoning_state=second),
    )


def test_a_signed_block_ending_the_stream_is_completed_by_done() -> None:
    events, _ = _decode_openrouter(
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.text",
                        "text": "Only",
                        "format": _CLAUDE_FORMAT,
                        "index": 0,
                        "signature": "S",
                    }
                )
            )
        ),
        _DONE,
    )
    assert isinstance(events[-1], StreamReasoningState)


def test_non_streamed_reasoning_details_carry_the_response_model() -> None:
    entry = {
        "type": "reasoning.text",
        "text": "hm",
        "format": _CLAUDE_FORMAT,
        "index": 0,
        "signature": "SIG",
    }
    body = {
        "id": "gen-1",
        "model": "anthropic/claude-sonnet-4.5",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "reasoning": "hm",
                    "reasoning_details": [entry],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    result = decode_chat_completion(json.dumps(body).encode(), "openrouter")
    assert isinstance(result, ModelResponse)
    assert result.output[0].content[0] == ContentReasoning(
        text="hm", form="full", reasoning_state=_state("anthropic/claude-sonnet-4.5", [entry])
    )


def test_openrouter_state_is_echoed_unmodified_and_other_state_is_not() -> None:
    merged = [
        {
            "type": "reasoning.text",
            "text": "hm",
            "format": _CLAUDE_FORMAT,
            "index": 0,
            "signature": "SIG",
        }
    ]
    call = ContentToolUse(id="call_1", name="run", input={}, parse_error=None)

    def assistant(state: DynamicData) -> AssistantMessage:
        return AssistantMessage(
            content=(ContentReasoning(text="hm", form="full", reasoning_state=state), call)
        )

    own = _encode(_request(context=(*_HI, assistant(_state("test-model", merged)))), _OPENROUTER)
    message = cast("list[dict[str, Any]]", own["messages"])[1]
    assert message["reasoning_details"] == merged
    assert "reasoning_content" not in message  # the entries' text is not also echoed as literal
    # The encoder renders any OpenRouter state it is given; whose state a request may carry is
    # adaptation's rule (`accepts_reasoning_state`), which leaves another's out and records it.
    other = _request(context=(*_HI, assistant(_state("other/model", merged))))
    rendered = _OPENROUTER.encode_request("test-model", other, streaming=True).body
    assert cast("list[dict[str, Any]]", rendered["messages"])[1]["reasoning_details"] == merged
    adapted = _OPENROUTER.adapt_request("test-model", other)
    assert [loss.code for loss in adapted.losses] == ["foreign_reasoning"]
    foreign = _OPENROUTER.adapt_request(
        "test-model",
        _request(
            context=(*_HI, assistant(DynamicData.from_tagged(AnthropicSignature(signature="EqQB"))))
        ),
    )
    assert [loss.code for loss in foreign.losses] == ["foreign_reasoning"]
    sent = _OPENROUTER.encode_request("test-model", foreign.request, streaming=True).body
    assert "reasoning_details" not in cast("list[dict[str, Any]]", sent["messages"])[1]


@dataclass(frozen=True)
class _ElsewhereState(TaggedDataclass, fxtr_type="com.example.ElsewhereState.v1"):
    token: str


def test_the_openrouter_variant_accepts_only_openrouter_state_from_the_requested_model() -> None:
    entries = [{"type": "reasoning.text", "text": "hm", "signature": "SIG"}]
    model = "anthropic/claude-sonnet-4.5"
    assert _OPENROUTER.accepts_reasoning_state(_state(model, entries), model)
    assert _OPENROUTER.accepts_reasoning_state(_state(model, entries), f"{model}:nitro")
    assert not _OPENROUTER.accepts_reasoning_state(_state("openai/gpt-5", entries), model)
    for other in (
        DynamicData.from_tagged(AnthropicSignature(signature="EqQB")),
        DynamicData.from_tagged(_ElsewhereState(token="t")),
    ):
        assert not _OPENROUTER.accepts_reasoning_state(other, model)
    assert not _CODEC.accepts_reasoning_state(_state(model, entries), model)


def test_id_less_entries_with_distinct_ids_stay_separate() -> None:
    # Two encrypted items with ids and no index, streamed and buffered: two entries, in order.
    first: JSONObject = {
        "type": "reasoning.encrypted",
        "data": "ENC-A",
        "id": "rs_a",
        "format": _GPT_FORMAT,
    }
    second: JSONObject = {
        "type": "reasoning.encrypted",
        "data": "ENC-B",
        "id": "rs_b",
        "format": _GPT_FORMAT,
    }
    events, result = _decode_openrouter(
        _chunk(_choice(_details(first))),
        _chunk(_choice(_details(second))),
        _chunk(_choice({"content": "ok"}, "stop")),
        _DONE,
    )
    states = _states(result)
    assert states == [
        OpenRouterReasoning("test-model", (first,)),
        OpenRouterReasoning("test-model", (second,)),
    ]
    assert [
        e.state.as_type(OpenRouterReasoning) for e in events if isinstance(e, StreamReasoningState)
    ] == states
    body = {
        "id": "gen-1",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "reasoning_details": [first, second],
                },
                "finish_reason": "stop",
            }
        ],
    }
    buffered = decode_chat_completion(json.dumps(body).encode(), "openrouter")
    assert _states(buffered) == [
        OpenRouterReasoning("test-model", (first,)),
        OpenRouterReasoning("test-model", (second,)),
    ]


def test_fragments_without_index_or_id_continue_the_last_unsealed_entry_of_their_type() -> None:
    events, result = _decode_openrouter(
        _chunk(
            _choice(_details({"type": "reasoning.text", "text": "One", "format": _CLAUDE_FORMAT}))
        ),
        _chunk(_choice(_details({"type": "reasoning.text", "text": " two"}))),
        _chunk(_choice(_details({"type": "reasoning.text", "signature": "S1"}))),
        # The last text entry is sealed now: a positionless text fragment starts a new entry.
        _chunk(
            _choice(_details({"type": "reasoning.text", "text": "Three", "format": _CLAUDE_FORMAT}))
        ),
        _chunk(_choice(_details({"type": "reasoning.text", "signature": "S2"}))),
        _chunk(_choice({"content": "ok"}, "stop")),
        _DONE,
    )
    assert isinstance(result, ModelResponse)
    blocks = [b for b in result.output[0].content if isinstance(b, ContentReasoning)]
    assert [b.text for b in blocks] == ["One two", "Three"]
    assert _states(result) == [
        OpenRouterReasoning(
            "test-model",
            (
                {
                    "type": "reasoning.text",
                    "text": "One two",
                    "format": _CLAUDE_FORMAT,
                    "signature": "S1",
                },
            ),
        ),
        OpenRouterReasoning(
            "test-model",
            (
                {
                    "type": "reasoning.text",
                    "text": "Three",
                    "format": _CLAUDE_FORMAT,
                    "signature": "S2",
                },
            ),
        ),
    ]
    assert len([e for e in events if isinstance(e, StreamReasoningState)]) == 2


def test_a_buffered_array_is_taken_as_it_is_even_when_entries_look_alike() -> None:
    # Streamed, two text fragments at index 0 would be one entry; a buffered array is complete,
    # so two entries that happen to share an index and type both stay.
    entries = [
        {
            "type": "reasoning.text",
            "text": "a",
            "format": _CLAUDE_FORMAT,
            "index": 0,
            "signature": "S1",
        },
        {
            "type": "reasoning.text",
            "text": "b",
            "format": _CLAUDE_FORMAT,
            "index": 0,
            "signature": "S2",
        },
    ]
    body = {
        "id": "gen-1",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok", "reasoning_details": entries},
                "finish_reason": "stop",
            }
        ],
    }
    result = decode_chat_completion(json.dumps(body).encode(), "openrouter")
    assert _states(result) == [
        OpenRouterReasoning("test-model", (entries[0],)),
        OpenRouterReasoning("test-model", (entries[1],)),
    ]


def test_an_encrypted_entry_followed_by_plain_text_is_still_echoed() -> None:
    # Switching to literal reasoning is a boundary like any other: the sealed group before it
    # is complete and keeps its state, streamed or buffered.
    encrypted = {
        "type": "reasoning.encrypted",
        "data": "gAAA",
        "id": "rs_1",
        "format": _GPT_FORMAT,
        "index": 0,
    }
    plain = {"type": "reasoning.text", "text": "and then", "format": "unknown", "index": 1}
    events, streamed = _decode_openrouter(
        _chunk(_choice(_details(encrypted))),
        _chunk(_choice(_details(plain))),
        _chunk(_choice({"content": "ok"}, "stop")),
        _DONE,
    )
    assert _echoed_details(streamed) == [encrypted]
    assert isinstance(streamed, ModelResponse)
    assert streamed.output[0].content[1] == ContentReasoning(text="and then", form="full")
    assert [type(e).__name__ for e in events] == [
        "StreamReasoningStart",
        "StreamReasoningState",
        "StreamReasoningStart",
        "StreamReasoningDelta",
        "StreamTextDelta",
    ]
    assert _echoed_details(_buffered([encrypted, plain])) == [encrypted]


def test_a_fragment_for_a_completed_entry_breaks_the_contract() -> None:
    # Entries complete in order (the providers stream reasoning as sequential blocks); a
    # fragment for an entry a later entry or the content has already closed would change what
    # was passed on, so it is a codec error rather than a silent divergence.
    decoder = ChatCompletionsSseDecoder("openrouter")
    decoder.feed(
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.encrypted",
                        "data": "AAA",
                        "id": "rs_a",
                        "format": _GPT_FORMAT,
                    }
                )
            )
        )
    )
    decoder.feed(
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.encrypted",
                        "data": "CCC",
                        "id": "rs_c",
                        "format": _GPT_FORMAT,
                    }
                )
            )
        )
    )
    with pytest.raises(CodecError, match="already complete"):
        decoder.feed(
            _chunk(
                _choice(
                    _details(
                        {
                            "type": "reasoning.encrypted",
                            "data": "BBB",
                            "id": "rs_a",
                            "format": _GPT_FORMAT,
                        }
                    )
                )
            )
        )
    decoder = ChatCompletionsSseDecoder("openrouter")
    decoder.feed(
        _chunk(
            _choice(
                _details(
                    {"type": "reasoning.text", "text": "Hm", "format": _CLAUDE_FORMAT, "index": 0}
                )
            )
        )
    )
    decoder.feed(_chunk(_choice({"content": "Answer"})))
    with pytest.raises(CodecError, match="already complete"):
        decoder.feed(
            _chunk(_choice(_details({"type": "reasoning.text", "signature": "S", "index": 0})))
        )


_CLAUDE_WHOLE = {
    "type": "reasoning.text",
    "text": "Hm, ok",
    "format": _CLAUDE_FORMAT,
    "index": 0,
    "signature": "SIG",
}
_GPT_SUMMARY = {"type": "reasoning.summary", "summary": "Plan", "format": _GPT_FORMAT, "index": 0}
_GPT_ENCRYPTED = {
    "type": "reasoning.encrypted",
    "data": "gAAA",
    "id": "rs_1",
    "format": _GPT_FORMAT,
    "index": 1,
}


@pytest.mark.parametrize(
    ("fragments", "echoed"),
    [
        pytest.param([_CLAUDE_WHOLE], [_CLAUDE_WHOLE], id="claude-whole"),
        pytest.param(
            [
                {"type": "reasoning.text", "text": "Hm", "format": _CLAUDE_FORMAT, "index": 0},
                {"type": "reasoning.text", "text": ", ok", "index": 0},
                {"type": "reasoning.text", "signature": "SI", "index": 0},
                {"type": "reasoning.text", "signature": "G", "index": 0},
            ],
            [_CLAUDE_WHOLE],
            id="claude-fragmented",
        ),
        pytest.param(
            [
                {
                    "type": "reasoning.text",
                    "text": "One",
                    "format": _CLAUDE_FORMAT,
                    "index": 0,
                    "signature": "S1",
                },
                {
                    "type": "reasoning.text",
                    "text": "Two",
                    "format": _CLAUDE_FORMAT,
                    "index": 1,
                    "signature": "S2",
                },
            ],
            [
                {
                    "type": "reasoning.text",
                    "text": "One",
                    "format": _CLAUDE_FORMAT,
                    "index": 0,
                    "signature": "S1",
                },
                {
                    "type": "reasoning.text",
                    "text": "Two",
                    "format": _CLAUDE_FORMAT,
                    "index": 1,
                    "signature": "S2",
                },
            ],
            id="claude-two-blocks",
        ),
        pytest.param(
            [_GPT_SUMMARY, _GPT_ENCRYPTED],
            [_GPT_SUMMARY, _GPT_ENCRYPTED],
            id="gpt-summary-then-encrypted",
        ),
        pytest.param(
            [
                {"type": "reasoning.summary", "summary": "Pl", "format": _GPT_FORMAT, "index": 0},
                {"type": "reasoning.summary", "summary": "an", "index": 0},
                {**_GPT_ENCRYPTED, "index": 0},
            ],
            [_GPT_SUMMARY, {**_GPT_ENCRYPTED, "index": 0}],
            id="gpt-shared-index",
        ),
        pytest.param(
            [
                {**_GPT_ENCRYPTED, "index": 0},
                {"type": "reasoning.text", "text": "then", "format": "unknown", "index": 1},
            ],
            [{**_GPT_ENCRYPTED, "index": 0}],
            id="encrypted-then-plain",
        ),
        pytest.param(
            [
                {"type": "reasoning.text", "text": "Let me", "format": "unknown", "index": 0},
                {"type": "reasoning.text", "text": " think", "index": 0},
            ],
            None,
            id="plain-only-is-literal",
        ),
    ],
)
def test_decoding_then_encoding_preserves_the_native_continuation(
    fragments: list[dict[str, Any]], echoed: list[dict[str, Any]] | None
) -> None:
    """The invariant behind the state: whatever the stream's fragmentation, the entries the
    model produced come back as it produced them, in order; plain text comes back as literal
    reasoning instead. Streamed with content after the reasoning, streamed with the reasoning
    last, and buffered."""
    frames = [_chunk(_choice(_details(fragment))) for fragment in fragments]
    _, with_content = _decode_openrouter(*frames, _chunk(_choice({"content": "ok"}, "stop")), _DONE)
    assert _echoed_details(with_content) == echoed
    _, reasoning_last = _decode_openrouter(*frames, _DONE)
    assert _echoed_details(reasoning_last) == echoed
    if echoed is not None:
        assert _echoed_details(_buffered(echoed)) == echoed


def test_the_standard_variant_reads_only_the_literal_string_and_carries_no_signed_reasoning() -> (
    None
):
    # The same OpenRouter-shaped stream through the default codec: `reasoning_details` are not
    # read (the `reasoning` string beside them is), and on the way back OpenRouter state has no
    # representation: adaptation leaves a block carrying it out, text and all, and records it.
    events, result = _decode(
        _chunk(
            _choice(
                {"role": "assistant", "reasoning": "Hm"}
                | _details(
                    {"type": "reasoning.text", "text": "Hm", "format": _CLAUDE_FORMAT, "index": 0}
                )
            )
        ),
        _chunk(
            _choice(
                _details(
                    {
                        "type": "reasoning.text",
                        "signature": "SIG",
                        "format": _CLAUDE_FORMAT,
                        "index": 0,
                    }
                )
            )
        ),
        _chunk(_choice({"content": "ok"}, "stop")),
        _DONE,
    )
    assert isinstance(result, ModelResponse)
    assert result.output[0].content == (
        ContentReasoning(text="Hm", form="full"),
        ContentText(text="ok"),
    )
    assert events[:2] == [StreamReasoningStart(form="full"), StreamReasoningDelta(text="Hm")]
    merged = [
        {
            "type": "reasoning.text",
            "text": "Hm",
            "format": _CLAUDE_FORMAT,
            "index": 0,
            "signature": "SIG",
        }
    ]

    for form in ("full", "summary"):
        request = _request(
            context=(
                *_HI,
                AssistantMessage(
                    content=(
                        ContentReasoning(
                            text="Hm", form=form, reasoning_state=_state("test-model", merged)
                        ),
                        ContentText(text="ok"),
                    )
                ),
            )
        )
        adapted = _CODEC.adapt_request("test-model", request)
        assert [loss.code for loss in adapted.losses] == ["foreign_reasoning"]
        sent = _CODEC.encode_request("test-model", adapted.request, streaming=True).body
        message = cast("list[dict[str, Any]]", sent["messages"])[1]
        assert "reasoning_details" not in message and "reasoning_content" not in message
    assert OpenAIChatCompletionsCodec().variant == "standard"


def test_openrouter_cost_and_cache_writes_are_read() -> None:
    usage = {
        "prompt_tokens": 4016,
        "completion_tokens": 5,
        "total_tokens": 4021,
        "cost": 0.0014436,
        "prompt_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 4000},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    _, result = _decode_openrouter(
        _chunk(_choice({"content": "x"}, "stop")), _chunk({"usage": usage}), _DONE
    )
    assert isinstance(result, ModelResponse)
    assert result.usage.input_tokens == 13
    assert result.usage.cache_read_input_tokens == 3
    assert result.usage.cache_write_input_tokens == 4000
    assert result.usage.billed_cost_usd == 0.0014436


def test_chunk_decoding_events_and_response() -> None:
    events, result = _decode(*_FULL_STREAM)
    assert events == _FULL_EVENTS
    _assert_full_response(result)


def test_non_streamed_completion_decodes_like_the_stream() -> None:
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "reasoning_content": "hm",
                    "content": "Hello",
                    "tool_calls": [
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {"name": "run", "arguments": '{"x":2}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": _USAGE,
    }
    _assert_full_response(decode_chat_completion(json.dumps(body).encode()))


def test_tool_call_start_defers_until_name_is_known() -> None:
    decoder = ChatCompletionsSseDecoder()
    first = decoder.feed(
        _chunk(_choice({"tool_calls": [{"index": 0, "function": {"arguments": '{"a"'}}]}))
    )
    assert first == []
    second = decoder.feed(
        _chunk(
            _choice({"tool_calls": [{"index": 0, "function": {"name": "run", "arguments": ":1}"}}]})
        )
    )
    assert second == [
        StreamToolUseStart(index=0, id="tool_call_0", name="run"),
        StreamToolUseInputDelta(index=0, partial_json='{"a"'),
        StreamToolUseInputDelta(index=0, partial_json=":1}"),
    ]
    result = decoder.finish()
    assert isinstance(result, ModelResponse)
    tool_use = result.output[0].content[0]
    assert isinstance(tool_use, ContentToolUse)
    assert tool_use.input == {"a": 1}


def test_forced_tool_call_with_stop_finish_reason_is_tool_use() -> None:
    _, result = _decode(
        _chunk(
            _choice(
                {
                    "tool_calls": [
                        {"index": 0, "id": "c", "function": {"name": "run", "arguments": "{}"}}
                    ]
                },
                "stop",
            )
        ),
        _DONE,
    )
    assert isinstance(result, ModelResponse)
    assert result.stop_reason == StopToolUse()
    _, plain = _decode(_chunk(_choice({"content": "hi"}, "stop")), _DONE)
    assert isinstance(plain, ModelResponse)
    assert plain.stop_reason == StopEndTurn()


def test_provider_reported_error_chunk_is_a_failure() -> None:
    _, result = _decode(
        _chunk({"error": {"code": 429, "message": "provider is rate limited"}}),
        _chunk(_choice({"content": "ignored after the error"})),
    )
    assert result == ModelCallFailure(
        category="rate_limited", description="provider is rate limited", provider_http_status=429
    )


def test_error_chunks_without_a_relayed_status_classify_by_code_and_message() -> None:
    _, result = _decode(
        _chunk(
            {
                "error": {
                    "code": "context_length_exceeded",
                    "message": "This model's maximum context length is 128000 tokens.",
                }
            }
        )
    )
    assert isinstance(result, ModelCallFailure)
    assert result.category == "context_length"
    assert result.provider_http_status is None
    _, by_message = _decode(_chunk({"error": {"message": "rate limit exceeded"}}))
    assert isinstance(by_message, ModelCallFailure)
    assert by_message.category == "rate_limited"


def test_malformed_frames_are_codec_errors_not_silent_drops() -> None:
    with pytest.raises(CodecError, match="not valid JSON"):
        ChatCompletionsSseDecoder().feed(b'data: {"id": "chatcmpl-1", "choices": [\n\n')
    truncated = ChatCompletionsSseDecoder()
    truncated.feed(_FULL_STREAM[0] + b'data: {"id": ')
    with pytest.raises(CodecError):
        truncated.finish()
    with pytest.raises(CodecError):
        decode_chat_completion(b"not json")


def test_done_sentinel_without_terminator_still_finishes() -> None:
    decoder = ChatCompletionsSseDecoder()
    for frame in _FULL_STREAM[:-1]:
        decoder.feed(frame)
    decoder.feed(b"data: [DONE]")
    _assert_full_response(decoder.finish())


def test_encoded_body_shares_nothing_with_caller_owned_data() -> None:
    provider = {"order": ["a"]}
    codec = OpenAIChatCompletionsCodec(extra_body={"provider": provider})
    body = _encode(_request(_HI), codec)
    cast("dict[str, Any]", body["provider"])["order"].append("b")
    assert provider == {"order": ["a"]}
    assert _encode(_request(_HI), codec)["provider"] == {"order": ["a"]}


def test_stream_without_chunks_is_a_failure() -> None:
    _, result = _decode(_DONE)
    assert isinstance(result, ModelCallFailure)
    assert result.category == "server_error"


# --- failure classification --------------------------------------------------------------------


def _error_body(message: str, code: str | None = None) -> bytes:
    return json.dumps({"error": {"message": message, "code": code, "type": "x"}}).encode()


@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (
            400,
            _error_body("this model's maximum context length is 128000 tokens"),
            "context_length",
        ),
        (400, _error_body("bad request", "context_length_exceeded"), "context_length"),
        (400, _error_body("flagged", "content_policy_violation"), "content_filter"),
        (400, _error_body("bad request"), "invalid_request"),
        (401, _error_body("bad key"), "authentication"),
        (408, _error_body("provider timeout"), "overloaded"),
        (429, _error_body("rate limited"), "rate_limited"),
        (503, _error_body("no provider available"), "overloaded"),
        (500, _error_body("boom"), "server_error"),
        (502, b"<html>bad gateway</html>", "overloaded"),  # a non-JSON gateway page
    ],
)
def test_status_error_classification(status: int, body: bytes, category: str) -> None:
    failure = decode_chat_completions_failure(status, body)
    assert failure.category == category
    assert failure.provider_http_status == status
    assert failure.description


# --- the transport -------------------------------------------------------------------------------


@dataclass
class _Upstream:
    answers: list[httpx2.Response | Exception]
    seen: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def api(self) -> OpenAICompatibleAPI:
        client = AsyncOpenAI(
            api_key="k",
            base_url="https://router.test/api/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler)),
        )
        return OpenAICompatibleAPI("test-model", client=client, extra_body={"provider": {"o": 1}})


async def test_transport_sends_the_encoded_body_and_decodes_the_stream() -> None:
    upstream = _Upstream(
        [
            httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=b"".join(_FULL_STREAM)
            )
        ]
    )
    seen_events: list[StreamEvent] = []

    async def collect(event: StreamEvent) -> None:
        seen_events.append(event)

    result = await upstream.api().generate_with_events(_request(context=_HI), collect)
    _assert_full_response(result)
    assert seen_events == _FULL_EVENTS
    (request,) = upstream.seen
    assert str(request.url) == "https://router.test/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer k"
    assert json.loads(request.content) == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "provider": {"o": 1},
    }


async def test_transport_returns_status_errors_as_failures_without_retrying() -> None:
    upstream = _Upstream([httpx2.Response(503, content=_error_body("no provider available"))])
    result = await upstream.api().generate(_request(context=_HI))
    assert result == ModelCallFailure(
        category="overloaded", description="no provider available", provider_http_status=503
    )
    assert len(upstream.seen) == 1


async def test_transport_returns_connection_errors_as_overloaded() -> None:
    upstream = _Upstream([httpx2.ConnectError("refused")])
    result = await upstream.api().generate(_request(context=_HI))
    assert isinstance(result, ModelCallFailure)
    assert result.category == "overloaded"
    assert result.provider_http_status is None


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
                    id="c1", name="status", input={}, parse_error=None, namespace="collaboration"
                ),
            )
        ),
        ToolMessage(content=(ContentText(text="ok"),), tool_call_id="c1", is_error=None),
    )
    body = _encode(_request(context=context, tools=tools))
    assert [t["function"]["name"] for t in cast("list[dict[str, Any]]", body["tools"])] == [
        "spawn",
        "collaboration__status",
        "workflows__status",
    ]
    messages = cast("list[dict[str, Any]]", body["messages"])
    assert messages[1]["tool_calls"][0]["function"]["name"] == "collaboration__status"


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
        _encode(_request(context=_HI, tools=tools, tool_choice=ToolChoiceTool(name="a__status")))


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
    body = _encode(_request(context=_HI, tools=tools, tool_choice=ToolChoiceTool(name="go")))
    choice = cast("dict[str, Any]", body["tool_choice"])
    assert cast("dict[str, Any]", choice["function"])["name"] == "go"
    with pytest.raises(CodecError, match="declared in several namespaces"):
        _encode(_request(context=_HI, tools=tools, tool_choice=ToolChoiceTool(name="run")))


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
    body = _encode(_request(context=_HI, tools=tools))
    assert [
        cast("dict[str, Any]", t["function"]).get("description")
        for t in cast("list[dict[str, Any]]", body["tools"])
    ] == [
        "Part of the `files` tool group: Paths must be relative to the workspace. Use read "
        "before edit.\n\nEdit a file.",
        "Part of the `files` tool group: Paths must be relative to the workspace. Use read "
        "before edit.",
        "Top.",
    ]
