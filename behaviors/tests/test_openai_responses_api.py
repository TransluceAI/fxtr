"""OpenAI Responses: the codec's model side (request rendering, SSE decoding, failure
classification) and the thin ``OpenAIResponsesAPI`` transport over the SDK, exercised against an
in-process ``httpx2.MockTransport``."""

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any, cast

import httpx2
import pytest
from openai import AsyncAzureOpenAI, AsyncOpenAI

from behaviors.implementations.dialects._common import without_foreign_reasoning_state
from behaviors.implementations.dialects.anthropic_messages import (
    AnthropicRedactedThinking,
    AnthropicSignature,
)
from behaviors.implementations.dialects.gemini_generate_content import GeminiThoughtSignature
from behaviors.implementations.dialects.openai_chat_completions import OpenRouterReasoning
from behaviors.implementations.dialects.openai_responses import (
    OpenAIResponsesCodec,
    OpenAIResponsesReasoning,
    ResponsesSseDecoder,
    decode_responses_failure,
    decode_responses_response,
)
from behaviors.implementations.dialects.reconstruction import compare_request
from behaviors.implementations.models.openai_responses import OpenAIResponsesAPI
from behaviors.interfaces.dialect_codecs import CodecError, DecodedRequest
from behaviors.interfaces.model_apis import ForwardTransportError, StreamingModelAPI
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import (
    CacheableContentText,
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
    StopMaxTokens,
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

_CODEC = OpenAIResponsesCodec()
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


def _openai_state(item_id: str | None, encrypted: str) -> DynamicData:
    return DynamicData.from_tagged(
        OpenAIResponsesReasoning(id=item_id, encrypted_content=encrypted)
    )


@dataclass(frozen=True)
class _ElsewhereState(TaggedDataclass, fxtr_type="com.example.ElsewhereState.v1"):
    token: str


def _encode(request: ModelRequest, codec: OpenAIResponsesCodec = _CODEC) -> JSONObject:
    """What is sent for `request`: adapted to the dialect, then rendered."""
    adapted = codec.adapt_request("test-model", request).request
    return codec.encode_request("test-model", adapted, streaming=True).body


def test_conforms_to_streaming_model_api() -> None:
    api: StreamingModelAPI = OpenAIResponsesAPI("test-model", api_key="k")
    assert isinstance(api, StreamingModelAPI)
    assert api.dialect == "openai_responses"
    assert api.codec.dialect == "openai_responses"


# --- request rendering ---------------------------------------------------------------------------


@pytest.mark.parametrize("ttl", [None, "30m", "12h"])
@pytest.mark.parametrize(
    "container",
    ["user", "assistant", "system", "developer", "function_call_output", "custom_tool_call_output"],
)
def test_cache_breakpoint_lifetimes_round_trip(container: str, ttl: str | None) -> None:
    breakpoint: dict[str, Any] = {"mode": "explicit"}
    if ttl is not None:
        breakpoint["ttl"] = ttl
    part = {"type": "input_text", "text": "cached", "prompt_cache_breakpoint": breakpoint}
    items: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    if container == "custom_tool_call_output":
        items.append({"type": "custom_tool_call", "call_id": "c", "name": "run", "input": "hi"})
        tools.append({"type": "custom", "name": "run", "format": {"type": "text"}})
    if container.endswith("_output"):
        items.append({"type": container, "call_id": "c", "output": [part]})
    else:
        items.append({"role": container, "content": [part]})
    body: dict[str, Any] = {
        "model": "m",
        "input": items,
        # The breakpoint either overrides this default or inherits it by leaving TTL absent.
        "prompt_cache_options": {"ttl": "12h"},
    }
    if tools:
        body["tools"] = tools
    decoded = _CODEC.decode_request("/v1/responses", (), json.dumps(body).encode())
    assert decoded.request.context[-1].content == (
        CacheableContentText(text="cached", cache_control=CacheControl(ttl=ttl)),
    )
    assert decoded.decode_losses == ()
    adapted = _CODEC.adapt_request("m", decoded.request)
    assert adapted.losses == ()
    encoded = _CODEC.encode_request("m", adapted.request, streaming=True)
    differences, _ = compare_request("openai_responses", cast("JSONObject", body), encoded.body)
    assert differences == ()


@pytest.mark.parametrize("ttl", [None, "30m", "12h"])
def test_tool_result_cache_lifetime_goes_on_its_final_part(ttl: str | None) -> None:
    body = _encode(
        _request(
            context=(
                CacheableToolMessage(
                    content=(
                        CacheableContentText(text="first", cache_control=CacheControl("30m")),
                        ContentText(text="last"),
                    ),
                    tool_call_id="c",
                    is_error=None,
                    cache_control=CacheControl(ttl),
                ),
            )
        )
    )
    breakpoint: dict[str, Any] = {"mode": "explicit"}
    if ttl is not None:
        breakpoint["ttl"] = ttl
    assert body["input"] == [
        {
            "type": "function_call_output",
            "call_id": "c",
            "output": [
                {
                    "type": "input_text",
                    "text": "first",
                    "prompt_cache_breakpoint": {"mode": "explicit", "ttl": "30m"},
                },
                {"type": "input_text", "text": "last", "prompt_cache_breakpoint": breakpoint},
            ],
        }
    ]


def test_unsupported_cache_lifetime_requires_adaptation() -> None:
    request = _request(
        context=(
            UserMessage(content=(CacheableContentText("hi", cache_control=CacheControl("1h")),)),
        )
    )
    with pytest.raises(CodecError, match="unsupported cache breakpoint TTL"):
        _CODEC.encode_request("m", request, streaming=True)


_TWELVE_HOURS: JSONObject = {"prompt_cache_options": {"ttl": "12h"}}


@pytest.mark.parametrize(
    ("result_ttl", "part_ttl", "request_ttl_from", "sent"),
    [
        ("12h", "30m", None, "12h"),
        ("30m", "12h", None, "12h"),
        # An omitted TTL is the request's, here the longer of the two, wherever it is set.
        ("30m", None, "extras", "12h"),
        ("30m", None, "sampling", "12h"),
        ("30m", None, "extra_body", "12h"),
    ],
)
def test_a_tool_result_and_its_marked_final_part_keep_the_longer_lifetime(
    result_ttl: str | None, part_ttl: str | None, request_ttl_from: str | None, sent: str
) -> None:
    request = _request(
        context=(
            CacheableToolMessage(
                content=(CacheableContentText("result", cache_control=CacheControl(part_ttl)),),
                tool_call_id="c",
                is_error=None,
                cache_control=CacheControl(result_ttl),
            ),
        ),
        sampling=SamplingParams(extra=_TWELVE_HOURS) if request_ttl_from == "sampling" else None,
    )
    if request_ttl_from == "extras":
        request = dataclasses.replace(
            request, extras=DialectExtras(dialect="openai_responses", body=_TWELVE_HOURS)
        )
    codec = OpenAIResponsesCodec(
        extra_body=_TWELVE_HOURS if request_ttl_from == "extra_body" else None
    )
    adapted = codec.adapt_request("m", request)
    assert [(loss.code, loss.semantic) for loss in adapted.losses] == [("cache_breakpoint", False)]
    body = codec.encode_request("m", adapted.request, streaming=True).body
    (part,) = cast("list[dict[str, Any]]", body["input"])[0]["output"]
    request_ttl = cast("dict[str, Any]", body.get("prompt_cache_options", {})).get("ttl", "30m")
    assert part["prompt_cache_breakpoint"].get("ttl", request_ttl) == sent


@pytest.mark.parametrize(
    "state",
    [
        AnthropicSignature(signature="EqQBCkYIBxgC"),
        AnthropicRedactedThinking(data="EmwKAhgB"),
        OpenRouterReasoning(model="m", details=({"type": "reasoning.text"},)),
        GeminiThoughtSignature(model="gemini-3-pro", signature="c2ln", on="thought"),
        _ElsewhereState(token="t"),
    ],
)
def test_reasoning_state_of_another_kind_is_not_sent(state: TaggedDataclass) -> None:
    # An Anthropic signature sent as id-less `encrypted_content` is a 400
    # (`invalid_encrypted_content`) from the API, and so is any payload it did not issue.
    request = _request(
        context=(
            *_HI,
            AssistantMessage(
                content=(
                    ContentReasoning(
                        text="a", form="summary", reasoning_state=DynamicData.from_tagged(state)
                    ),
                    ContentReasoning(
                        text="b", form="summary", reasoning_state=_openai_state("rs_1", "enc")
                    ),
                    ContentText(text="done"),
                )
            ),
        )
    )
    prepared, rejected = without_foreign_reasoning_state(
        request, lambda s: _CODEC.accepts_reasoning_state(s, "gpt-test")
    )
    assert rejected == (DynamicData.from_tagged(state).type,)
    items = cast("list[dict[str, Any]]", _encode(prepared)["input"])
    assert [item.get("type", "message") for item in items] == ["message", "reasoning", "message"]
    assert items[1]["id"] == "rs_1"


def test_service_tier_maps_to_speed_and_back() -> None:
    codec = OpenAIResponsesCodec()

    def decoded(tier: str) -> DecodedRequest:
        body = {"model": "gpt-test", "input": "hi", "service_tier": tier}
        return codec.decode_request("/v1/responses", (), json.dumps(body).encode())

    assert decoded("fast").request.sampling.speed == "fast"
    assert decoded("priority").request.sampling.speed == "fast"  # the tier's older name
    assert decoded("default").request.sampling.speed == "standard"
    flex = decoded("flex")  # a tier the vocabulary does not model stays an extra
    assert flex.request.sampling.speed is None
    assert flex.request.extras is not None and flex.request.extras.body == {"service_tier": "flex"}
    assert (
        _encode(_request(context=_HI, sampling=SamplingParams(speed="fast")))["service_tier"]
        == "fast"
    )
    assert (
        _encode(_request(context=_HI, sampling=SamplingParams(speed="standard")))["service_tier"]
        == "default"
    )


def test_response_service_tier_is_the_speed_served() -> None:
    result = decode_responses_response(json.dumps(_response({"service_tier": "priority"})).encode())
    assert isinstance(result, ModelResponse)
    assert result.usage.speed == "fast"
    assert _CODEC.encode_response("gpt-test", result)["service_tier"] == "fast"
    downgraded = decode_responses_response(
        json.dumps(_response({"service_tier": "default"})).encode()
    )
    assert isinstance(downgraded, ModelResponse) and downgraded.usage.speed == "standard"


def test_xhigh_effort_is_a_level_of_its_own() -> None:
    # Codex's top effort level reaches a Responses subject as asked; a model whose ceiling is
    # lower answers with its own error, and nothing is silently promoted to `max`.
    codec = OpenAIResponsesCodec()
    body = {"model": "gpt-test", "input": "hi", "reasoning": {"effort": "xhigh"}}
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    assert decoded.request.sampling.reasoning is not None
    assert decoded.request.sampling.reasoning.effort == "xhigh"
    encoded = codec.encode_request("gpt-subject", decoded.request, streaming=True)
    assert cast("dict[str, Any]", encoded.body["reasoning"])["effort"] == "xhigh"


def test_reasoning_item_without_an_id_round_trips_without_one() -> None:
    # A Codex session resumed from its files sends reasoning items that carry
    # `encrypted_content` but no `id`. The API accepts them as they are and rejects an invented
    # id (the payload names its own item), so the item goes back exactly as it came.
    codec = OpenAIResponsesCodec()
    stored_item = {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "hm"}],
        "encrypted_content": "enc-blob",
    }
    body = {
        "model": "gpt-test",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            stored_item,
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello"}],
            },
        ],
    }
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    (assistant,) = [m for m in decoded.request.context if isinstance(m, AssistantMessage)]
    assert assistant.content[0] == ContentReasoning(
        text="hm", form="summary", reasoning_state=_openai_state(None, "enc-blob")
    )
    encoded = codec.encode_request("gpt-subject", decoded.request, streaming=True)
    items = cast("list[dict[str, Any]]", encoded.body["input"])
    assert items[1] == stored_item


def test_cache_write_tokens_are_modelled_and_rendered_back() -> None:
    # What gpt-5.6 reports on a first call over a long prompt: nearly everything written to the
    # cache, nothing read. The total stays the total on the way back.
    usage = {
        "input_tokens": 4016,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 4013},
        "output_tokens": 5,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 4021,
    }
    result = decode_responses_response(json.dumps(_response({"usage": usage})).encode())
    assert isinstance(result, ModelResponse)
    assert result.usage.input_tokens == 3
    assert result.usage.cache_write_input_tokens == 4013
    assert result.usage.cache_read_input_tokens == 0
    rendered = _CODEC.encode_response("gpt-test", result)
    assert rendered["usage"] == usage
    # A response that never reported cache writes renders without the field.
    older = decode_responses_response(
        json.dumps(
            _response({"usage": {**usage, "input_tokens_details": {"cached_tokens": 0}}})
        ).encode()
    )
    assert isinstance(older, ModelResponse)
    assert older.usage.cache_write_input_tokens is None
    assert "cache_write_tokens" not in cast(
        "dict[str, Any]",
        cast("dict[str, Any]", _CODEC.encode_response("gpt-test", older)["usage"])[
            "input_tokens_details"
        ],
    )


def test_multi_part_developer_item_round_trips_with_its_parts() -> None:
    # Codex's developer preamble is one item with two input_text parts; each part stays its own
    # text block through the vocabulary so the item renders back the same way.
    codec = OpenAIResponsesCodec()
    body = {
        "model": "gpt-test",
        "instructions": "base prompt",
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [
                    {"type": "input_text", "text": "<permissions>"},
                    {"type": "input_text", "text": "<skills>"},
                ],
            },
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
    }
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    preamble = decoded.request.context[1]
    assert isinstance(preamble, SystemMessage)
    assert [block.text for block in preamble.content] == ["<permissions>", "<skills>"]
    encoded = codec.encode_request("gpt-subject", decoded.request, streaming=True)
    assert encoded.body["instructions"] == "base prompt"
    input_items = cast("list[dict[str, Any]]", encoded.body["input"])
    assert input_items[0] == {
        "role": "developer",
        "content": [
            {"type": "input_text", "text": "<permissions>"},
            {"type": "input_text", "text": "<skills>"},
        ],
    }


def test_render_context_keeps_later_system_messages_as_developer_items() -> None:
    # Codex sends `instructions` plus a developer preamble as the first input item; only the
    # opening system message is the top-level prompt, the rest keep their place.
    body = _encode(
        _request(
            context=(
                SystemMessage(content=(ContentText(text="base prompt"),)),
                SystemMessage(content=(ContentText(text="<permissions>"),)),
                UserMessage(content=(ContentText(text="hi"),)),
                SystemMessage(content=(ContentText(text="reminder"),)),
            )
        )
    )
    assert body["instructions"] == "base prompt"
    assert body["input"] == [
        {"role": "developer", "content": [{"type": "input_text", "text": "<permissions>"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"role": "developer", "content": [{"type": "input_text", "text": "reminder"}]},
    ]


def test_render_context_hoists_instructions_and_preserves_block_order() -> None:
    body = _encode(
        _request(
            context=(
                SystemMessage(content=(ContentText(text="sys"),)),
                UserMessage(content=(ContentText(text="hi"),)),
                AssistantMessage(
                    content=(
                        ContentReasoning(
                            text="thought",
                            form="summary",
                            reasoning_state=_openai_state("rs_orig", "enc"),
                        ),
                        ContentReasoning(text="unsigned", form="summary"),
                        ContentReasoning(text="literal", form="full"),
                        ContentReasoning(
                            text="",
                            form="redacted",
                            reasoning_state=DynamicData.from_tagged(
                                AnthropicRedactedThinking(data="blob")
                            ),
                        ),
                        ContentText(text="calling"),
                        ContentToolUse(id="call_1", name="run", input={"x": 1}, parse_error=None),
                    )
                ),
                ToolMessage(
                    content=(ContentText(text="ok"),), tool_call_id="call_1", is_error=True
                ),
            )
        )
    )

    assert body["instructions"] == "sys"
    # A summary or redaction without OpenAI's state has no API representation and is dropped;
    # full reasoning goes back as an item's `content`.
    assert body["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {
            "type": "reasoning",
            "id": "rs_orig",
            "summary": [{"type": "summary_text", "text": "thought"}],
            "encrypted_content": "enc",
        },
        {
            "type": "reasoning",
            "id": "rs_behaviors0",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": "literal"}],
        },
        {"role": "assistant", "content": [{"type": "output_text", "text": "calling"}]},
        {"type": "function_call", "call_id": "call_1", "name": "run", "arguments": '{"x": 1}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]


def test_synthetic_reasoning_ids_are_unique_within_a_request() -> None:
    body = _encode(
        _request(
            context=(
                UserMessage(content=(ContentText(text="hi"),)),
                AssistantMessage(
                    content=(ContentReasoning(text="first", form="full"), ContentText(text="a"))
                ),
                UserMessage(content=(ContentText(text="more"),)),
                AssistantMessage(
                    content=(
                        ContentReasoning(text="second", form="full"),
                        ContentReasoning(
                            text="", form="summary", reasoning_state=_openai_state(None, "blob")
                        ),
                    )
                ),
            )
        )
    )
    items = cast("list[dict[str, Any]]", body["input"])
    ids = [item.get("id") for item in items if item.get("type") == "reasoning"]
    # Full reasoning without state gets a synthetic id each; an item that had no id goes back
    # without one (the API verifies the payload against the id it names, so none is invented).
    assert ids == ["rs_behaviors0", "rs_behaviors1", None]


# A schema every provider's strict mode accepts, so a strict tool stays strict.
_CLOSED_OBJECT: JSONObject = {"type": "object", "properties": {}, "additionalProperties": False}


def test_tool_param_uses_flattened_shape() -> None:
    body = _encode(
        _request(
            context=_HI,
            tools=(
                ToolParam(
                    name="run",
                    description="d",
                    input_schema=_CLOSED_OBJECT,
                    strict=True,
                    input_examples=None,
                ),
            ),
        )
    )
    assert body["tools"] == [
        {
            "type": "function",
            "name": "run",
            "parameters": _CLOSED_OBJECT,
            "description": "d",
            "strict": True,
        }
    ]


def _sampling_body(sampling: SamplingParams) -> JSONObject:
    return _encode(_request(context=_HI, sampling=sampling))


def test_reasoning_param_shapes() -> None:
    assert "reasoning" not in _sampling_body(SamplingParams())
    assert _sampling_body(SamplingParams(reasoning=ReasoningParams(enabled=False)))[
        "reasoning"
    ] == {"effort": "none"}
    # Reasoning on requests summaries by default; the effort rides along when derivable.
    assert _sampling_body(SamplingParams(reasoning=ReasoningParams()))["reasoning"] == {
        "summary": "auto"
    }
    assert _sampling_body(SamplingParams(reasoning=ReasoningParams(effort="max")))["reasoning"] == {
        "effort": "max",
        "summary": "auto",
    }
    assert _sampling_body(SamplingParams(reasoning=ReasoningParams(budget_tokens=1024)))[
        "reasoning"
    ] == {"effort": "low", "summary": "auto"}


def test_declared_defaults_apply_only_when_absent() -> None:
    body = _sampling_body(SamplingParams(max_tokens=5, temperature=0.1, top_p=0.9))
    assert body["store"] is False
    assert "include" not in body
    assert body["max_output_tokens"] == 5
    assert body["temperature"] == 0.1
    assert body["top_p"] == 0.9
    assert body["stream"] is True

    # A request that chose its own store / summary mode keeps them.
    chosen = _sampling_body(
        SamplingParams(
            extra={"store": True, "reasoning": {"effort": "high", "summary": "detailed"}}
        )
    )
    assert chosen["store"] is True
    assert chosen["reasoning"] == {"effort": "high", "summary": "detailed"}

    # The codec's own extra_body fills in absent keys only, and outranks the built-in defaults.
    routed = OpenAIResponsesCodec(extra_body={"service_tier": "flex", "store": True})
    assert _encode(_request(_HI), routed)["store"] is True
    body = _encode(_request(context=_HI, sampling=SamplingParams(extra={"store": False})), routed)
    assert body["service_tier"] == "flex"
    assert body["store"] is False


def test_nested_defaults_never_edit_caller_owned_settings() -> None:
    reasoning: JSONObject = {"effort": "high"}
    sampling = SamplingParams(extra={"reasoning": reasoning})
    codec = OpenAIResponsesCodec(extra_body={"reasoning": {"effort": "low"}, "metadata": {"a": 1}})

    body = _encode(_request(_HI, sampling=sampling), codec)
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert reasoning == {"effort": "high"}  # the request's own dict is untouched

    defaulted = _encode(_request(_HI), codec)
    assert defaulted["reasoning"] == {"effort": "low", "summary": "auto"}
    cast("dict[str, Any]", defaulted["metadata"])["a"] = "edited"
    # Neither the codec's configuration nor a later request sees either edit.
    assert _encode(_request(_HI), codec)["reasoning"] == {"effort": "low", "summary": "auto"}
    assert _encode(_request(_HI), codec)["metadata"] == {"a": 1}


# --- streamed response decoding ----------------------------------------------------------------

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


def _response(payload: dict[str, Any]) -> dict[str, Any]:
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
    return base | payload


def _sse(data: dict[str, Any]) -> bytes:
    return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n".encode()


def _decode(*frames: bytes) -> tuple[list[StreamEvent], ModelResponse | ModelCallFailure]:
    decoder = ResponsesSseDecoder()
    events: list[StreamEvent] = []
    for frame in frames:
        events.extend(decoder.feed(frame))
    return events, decoder.finish()


_TERMINAL = _response(
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
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens": 7,
            "output_tokens_details": {"reasoning_tokens": 4},
            "total_tokens": 107,
        },
    }
)

_FULL_STREAM = (
    _sse({"type": "response.created", "sequence_number": 0, "response": _response({})}),
    _sse(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "sequence_number": 1,
            "item": _FUNCTION_CALL_ITEM | {"arguments": ""},
        }
    ),
    _sse(
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 0,
            "sequence_number": 2,
            "delta": '{"x":2}',
        }
    ),
    _sse(
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs_1",
            "output_index": 1,
            "summary_index": 0,
            "sequence_number": 3,
            "delta": "hm",
        }
    ),
    _sse(
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "sequence_number": 4,
            "item": _REASONING_ITEM,
        }
    ),
    _sse(
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 2,
            "content_index": 0,
            "sequence_number": 5,
            "logprobs": [],
            "delta": "Hello",
        }
    ),
    _sse({"type": "response.completed", "sequence_number": 6, "response": _TERMINAL}),
)

_FULL_EVENTS = [
    StreamToolUseStart(index=0, id="call_9", name="run"),
    StreamToolUseInputDelta(index=0, partial_json='{"x":2}'),
    StreamReasoningStart(form="summary"),
    StreamReasoningDelta(text="hm"),
    StreamReasoningState(state=_openai_state("rs_1", "enc-blob")),
    StreamTextDelta(text="Hello"),
]


def _assert_full_response(result: ModelResponse | ModelCallFailure) -> None:
    assert isinstance(result, ModelResponse)
    assert result.id == "resp_1"
    assert result.output == (
        AssistantMessage(
            content=(
                ContentReasoning(
                    text="hm", form="summary", reasoning_state=_openai_state("rs_1", "enc-blob")
                ),
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


def test_stream_events_relay_deltas_and_signature_then_terminal_assembly() -> None:
    events, result = _decode(*_FULL_STREAM)
    assert events == _FULL_EVENTS
    _assert_full_response(result)


def test_raw_message_is_the_terminal_response() -> None:
    decoder = ResponsesSseDecoder()
    for frame in _FULL_STREAM[:-1]:
        decoder.feed(frame)
    assert decoder.raw_message() is None
    decoder.feed(_FULL_STREAM[-1])
    decoder.finish()
    assert decoder.raw_message() == _TERMINAL


def test_non_streamed_response_decodes_like_the_stream() -> None:
    _assert_full_response(decode_responses_response(json.dumps(_TERMINAL).encode()))


def test_content_only_reasoning_item_is_literal_reasoning() -> None:
    body = _response(
        {
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_tmp_1",
                    "summary": [],
                    "content": [{"type": "reasoning_text", "text": "raw thoughts"}],
                },
                _FUNCTION_CALL_ITEM,
            ]
        }
    )
    result = decode_responses_response(json.dumps(body).encode())
    assert isinstance(result, ModelResponse)
    assert result.output[0].content[0] == ContentReasoning(text="raw thoughts", form="full")


def test_incomplete_response_maps_to_max_tokens() -> None:
    body = _response(
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
    )
    _, result = _decode(
        _sse({"type": "response.incomplete", "sequence_number": 0, "response": body})
    )
    assert isinstance(result, ModelResponse)
    assert result.stop_reason == StopMaxTokens()


def test_error_events_end_the_stream_as_failures() -> None:
    _, result = _decode(
        _sse(
            {"type": "error", "code": None, "message": "boom", "param": None, "sequence_number": 0}
        )
    )
    assert result == ModelCallFailure(
        category="server_error", description="boom", provider_http_status=None
    )

    failed = _response(
        {"status": "failed", "error": {"code": "rate_limit_exceeded", "message": "slow down"}}
    )
    _, result = _decode(_sse({"type": "response.failed", "sequence_number": 0, "response": failed}))
    assert result == ModelCallFailure(
        category="rate_limited", description="slow down", provider_http_status=None
    )


def test_in_stream_errors_classify_by_code_and_message() -> None:
    _, result = _decode(
        _sse(
            {
                "type": "error",
                "code": "context_length_exceeded",
                "message": "Your input exceeds the context window of this model.",
                "param": None,
                "sequence_number": 0,
            }
        )
    )
    assert isinstance(result, ModelCallFailure)
    assert result.category == "context_length"

    def failed(code: str, message: str) -> ModelCallFailure:
        body = _response({"status": "failed", "error": {"code": code, "message": message}})
        _, outcome = _decode(
            _sse({"type": "response.failed", "sequence_number": 0, "response": body})
        )
        assert isinstance(outcome, ModelCallFailure)
        return outcome

    assert failed("server_error", "maximum context length is 128000 tokens").category == (
        "context_length"
    )
    assert failed("invalid_image", "bad image").category == "invalid_request"
    assert failed("invalid_prompt", "flagged").category == "content_filter"


def test_malformed_frames_are_codec_errors_not_silent_drops() -> None:
    with pytest.raises(CodecError, match="not valid JSON"):
        ResponsesSseDecoder().feed(b'event: response.created\ndata: {"type": "resp\n\n')
    truncated = ResponsesSseDecoder()
    truncated.feed(_FULL_STREAM[0] + b'event: response.completed\ndata: {"type"')
    with pytest.raises(CodecError):
        truncated.finish()
    with pytest.raises(CodecError):
        decode_responses_response(b"<html>not json</html>")


def test_final_frame_without_terminator_is_still_decoded() -> None:
    decoder = ResponsesSseDecoder()
    for frame in _FULL_STREAM[:-1]:
        decoder.feed(frame)
    decoder.feed(_FULL_STREAM[-1].rstrip(b"\n"))
    _assert_full_response(decoder.finish())


def test_a_done_terminator_after_the_terminal_event_is_ignored() -> None:
    events, result = _decode(*_FULL_STREAM, b"data: [DONE]\n\n")
    assert events == _FULL_EVENTS
    _assert_full_response(result)


def test_stream_without_terminal_event_is_a_failure() -> None:
    _, result = _decode(*_FULL_STREAM[:-1])
    assert isinstance(result, ModelCallFailure)
    assert result.category == "server_error"


# --- failure classification --------------------------------------------------------------------


def _error_body(message: str, code: str | None = None) -> bytes:
    return json.dumps({"error": {"message": message, "code": code, "type": "x"}}).encode()


@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (400, _error_body("maximum context length is 128000 tokens"), "context_length"),
        (400, _error_body("bad", "context_length_exceeded"), "context_length"),
        (400, _error_body("flagged", "content_policy_violation"), "content_filter"),
        (400, _error_body("bad request"), "invalid_request"),
        (401, _error_body("bad key"), "authentication"),
        (429, _error_body("rate limited"), "rate_limited"),
        (503, _error_body("no provider available"), "overloaded"),
        (500, _error_body("boom"), "server_error"),
        (502, b"bad gateway", "overloaded"),  # a non-JSON gateway page
    ],
)
def test_status_error_classification(status: int, body: bytes, category: str) -> None:
    failure = decode_responses_failure(status, body)
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

    def api(self) -> OpenAIResponsesAPI:
        client = AsyncOpenAI(
            api_key="k",
            base_url="https://openai.test/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler)),
        )
        return OpenAIResponsesAPI("test-model", client=client)


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
    assert str(request.url) == "https://openai.test/v1/responses"
    assert request.headers["authorization"] == "Bearer k"
    assert json.loads(request.content) == {
        "model": "test-model",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "stream": True,
        "store": False,
    }


async def test_transport_returns_status_errors_as_failures_without_retrying() -> None:
    upstream = _Upstream([httpx2.Response(429, content=_error_body("slow down"))])
    result = await upstream.api().generate(_request(context=_HI))
    assert result == ModelCallFailure(
        category="rate_limited", description="slow down", provider_http_status=429
    )
    assert len(upstream.seen) == 1


_BREAKPOINTS_400 = httpx2.Response(
    400,
    content=json.dumps(
        {
            "error": {
                "message": "prompt_cache_breakpoint is not supported on this model",
                "type": "invalid_request_error",
                "param": "prompt_cache_breakpoint",
                "code": "invalid_parameter",
            }
        }
    ).encode(),
)
_CACHED = _request(
    context=(UserMessage(content=(CacheableContentText(text="hi", cache_control=CacheControl()),)),)
)


def _parts_sent(upstream: _Upstream) -> list[Any]:
    return [json.loads(r.content)["input"][0]["content"] for r in upstream.seen]


def _ok() -> httpx2.Response:
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=b"".join(_FULL_STREAM)
    )


async def test_breakpoints_are_dropped_once_the_model_rejects_them_and_remembered() -> None:
    # A model before gpt-5.6 rejects `prompt_cache_breakpoint`: the call is retried once without
    # it, and after that succeeds the instance sends requests without breakpoints directly.
    upstream = _Upstream([_BREAKPOINTS_400, _ok(), _ok()])
    api = upstream.api()
    _assert_full_response(await api.generate(_CACHED))
    marked = {"type": "input_text", "text": "hi", "prompt_cache_breakpoint": {"mode": "explicit"}}
    plain = {"type": "input_text", "text": "hi"}
    assert _parts_sent(upstream) == [[marked], [plain]]
    _assert_full_response(await api.generate(_CACHED))
    assert _parts_sent(upstream)[2:] == [[plain]]


@pytest.mark.parametrize("field", ["prompt_cache_breakpoint", "prompt_cache_options"])
async def test_a_rejected_cache_field_drops_both(field: str) -> None:
    # Models before gpt-5.6 reject either field, `prompt_cache_options` in either mode; the
    # retry sends neither, which is the API's implicit caching.
    explicit = dataclasses.replace(
        _CACHED,
        extras=DialectExtras(
            dialect="openai_responses", body={"prompt_cache_options": {"mode": "explicit"}}
        ),
    )
    rejection = httpx2.Response(
        400, content=_error_body(f"{field} is not supported on this model", "invalid_parameter")
    )
    upstream = _Upstream([rejection, _ok()])
    _assert_full_response(await upstream.api().generate(explicit))
    first, retry = (json.loads(r.content) for r in upstream.seen)
    assert first["prompt_cache_options"] == {"mode": "explicit"}
    assert "prompt_cache_options" not in retry
    assert retry["input"][0]["content"] == [{"type": "input_text", "text": "hi"}]


async def test_breakpoints_are_kept_for_a_model_that_takes_them() -> None:
    upstream = _Upstream([_ok()])
    _assert_full_response(await upstream.api().generate(_CACHED))
    assert _parts_sent(upstream)[0][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}


async def test_transport_returns_connection_errors_as_overloaded() -> None:
    upstream = _Upstream([httpx2.ConnectError("refused")])
    result = await upstream.api().generate(_request(context=_HI))
    assert isinstance(result, ModelCallFailure)
    assert result.category == "overloaded"
    assert result.provider_http_status is None


# --- forwarding ----------------------------------------------------------------------------------


async def test_forward_request_maps_the_path_and_keeps_the_harness_request() -> None:
    upstream = _Upstream(
        [
            httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=b"".join(_FULL_STREAM)
            )
        ]
    )
    body = json.dumps({"model": "gpt-harness", "input": "hi", "store": True}).encode()
    headers: Headers = (
        ("authorization", "Bearer run-key"),
        ("openai-organization", "org-harness"),
        ("openai-beta", "responses=v1"),
        ("content-type", "application/json"),
    )
    forwarded = await upstream.api().forward_request("/v1/responses?x=1", headers, body)
    (sent,) = upstream.seen
    assert str(sent.url) == "https://openai.test/v1/responses?x=1"
    assert sent.headers["authorization"] == "Bearer k"
    assert "openai-organization" not in sent.headers
    assert sent.headers["openai-beta"] == "responses=v1"
    assert json.loads(sent.content) == {"model": "test-model", "input": "hi", "store": True}
    assert forwarded.status == 200
    assert b"".join([chunk async for chunk in forwarded.body]) == b"".join(_FULL_STREAM)
    await forwarded.aclose()


async def test_forward_request_returns_a_non_success_status_as_is() -> None:
    upstream = _Upstream([httpx2.Response(429, content=_error_body("slow down"))])
    forwarded = await upstream.api().forward_request("/v1/responses", (), b'{"model": "m"}')
    assert forwarded.status == 429
    assert b"".join([chunk async for chunk in forwarded.body]) == _error_body("slow down")


@pytest.mark.parametrize("target", ["https://evil.example/v1/responses", "//evil.example/x"])
async def test_forward_request_refuses_a_target_that_is_not_a_path(target: str) -> None:
    upstream = _Upstream([httpx2.Response(200, content=b"".join(_FULL_STREAM))])
    with pytest.raises(ValueError, match="path with an optional query"):
        await upstream.api().forward_request(target, (), b'{"model": "m"}')
    assert upstream.seen == []


async def test_forward_request_runs_the_sdks_dynamic_authentication() -> None:
    tokens = iter(["tok-1", "tok-2"])

    async def provider() -> str:
        return next(tokens)

    upstream = _Upstream([httpx2.Response(200, content=b"{}"), httpx2.Response(200, content=b"{}")])
    client = AsyncOpenAI(
        api_key=provider,
        base_url="https://openai.test/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler)),
    )
    api = OpenAIResponsesAPI("test-model", client=client)
    for _ in range(2):
        forwarded = await api.forward_request("/v1/responses", (), b'{"model": "m"}')
        await forwarded.aclose()
    first, second = upstream.seen
    # The provider is consulted for every forwarded request, as the SDK does for its own.
    assert first.headers["authorization"] == "Bearer tok-1"
    assert second.headers["authorization"] == "Bearer tok-2"


async def test_forward_request_carries_an_azure_ad_token() -> None:
    upstream = _Upstream([httpx2.Response(200, content=b"{}")])
    client = AsyncAzureOpenAI(
        azure_endpoint="https://azure.test",
        api_version="2025-04-01-preview",
        azure_ad_token_provider=lambda: "azure-token",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler)),
    )
    api = OpenAIResponsesAPI("deployment-1", client=client)
    forwarded = await api.forward_request("/v1/responses?x=1", (), b'{"model": "m"}')
    await forwarded.aclose()
    (sent,) = upstream.seen
    assert sent.headers["authorization"] == "Bearer azure-token"
    assert "api-key" not in sent.headers
    assert str(sent.url).startswith("https://azure.test/openai/")
    assert "api-version=2025-04-01-preview" in str(sent.url) and "x=1" in str(sent.url)


async def test_forward_request_raises_on_transport_failure() -> None:
    upstream = _Upstream([httpx2.ConnectError("refused")])
    with pytest.raises(ForwardTransportError):
        await upstream.api().forward_request("/v1/responses", (), b'{"model": "m"}')


# --- custom (freeform) tools and namespaces on the model side ----------------------------------

_LARK: JSONObject = {"type": "grammar", "syntax": "lark", "definition": "start: /.*/"}


def _tool(
    name: str,
    *,
    namespace: str | None = None,
    namespace_description: str | None = None,
    freeform: JSONObject | None = None,
) -> ToolParam:
    return ToolParam(
        name=name,
        description=f"{name} tool",
        input_schema=(
            {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}
            if freeform is not None
            else {"type": "object"}
        ),
        strict="auto",  # no flag, as a Responses harness usually sends
        input_examples=None,
        namespace=namespace,
        namespace_description=namespace_description,
        freeform_format=freeform,
    )


def test_tools_render_freeform_tools_as_custom_and_regroup_namespaces() -> None:
    # The group's description rides on its members; a group no member describes gets the
    # empty description the API insists on.
    exec_tool = _tool("exec", namespace="functions", namespace_description="Core.", freeform=_LARK)
    plain = _tool("plain")
    wait = _tool("wait", namespace="functions", namespace_description="Core.")
    spawn = _tool("spawn_agent", namespace="collaboration")
    body = _encode(_request(context=_HI, tools=(exec_tool, plain, wait, spawn)))
    assert body["tools"] == [
        {
            "type": "namespace",
            "name": "functions",
            "description": "Core.",
            "tools": [
                {"type": "custom", "name": "exec", "format": _LARK, "description": "exec tool"},
                {
                    "type": "function",
                    "name": "wait",
                    "parameters": {"type": "object"},
                    "description": "wait tool",
                },
            ],
        },
        {
            "type": "function",
            "name": "plain",
            "parameters": {"type": "object"},
            "description": "plain tool",
        },
        {
            "type": "namespace",
            "name": "collaboration",
            "description": "",
            "tools": [
                {
                    "type": "function",
                    "name": "spawn_agent",
                    "parameters": {"type": "object"},
                    "description": "spawn_agent tool",
                }
            ],
        },
    ]


def test_members_describing_their_group_two_ways_is_an_error() -> None:
    tools = (
        _tool("a", namespace="ns", namespace_description="one"),
        _tool("b", namespace="ns", namespace_description="two"),
    )
    with pytest.raises(CodecError, match="described two ways"):
        _encode(_request(context=_HI, tools=tools))


def test_forced_choice_of_a_freeform_tool_is_a_custom_choice() -> None:
    tools = (_tool("exec", namespace="functions", freeform=_LARK), _tool("plain"))
    body = _encode(_request(context=_HI, tools=tools, tool_choice=ToolChoiceTool(name="exec")))
    assert body["tool_choice"] == {"type": "custom", "name": "exec"}
    body = _encode(_request(context=_HI, tools=tools, tool_choice=ToolChoiceTool(name="plain")))
    assert body["tool_choice"] == {"type": "function", "name": "plain"}
    # Undeclared: as sent, the API's complaint is the right one.
    body = _encode(_request(context=_HI, tools=tools, tool_choice=ToolChoiceTool(name="nope")))
    assert body["tool_choice"] == {"type": "function", "name": "nope"}


def test_history_renders_freeform_calls_as_custom_tool_calls_with_their_namespace() -> None:
    # ...and the result of such a call as a `custom_tool_call_output`. A call carries the
    # namespace it was made with: the `exec` call below was made without one (as the API
    # returns a code-mode call) and stays that way; the `spawn_agent` call keeps its group.
    tools = (
        _tool("exec", namespace="functions", freeform=_LARK),
        _tool("spawn_agent", namespace="collaboration"),
    )
    context = (
        *_HI,
        AssistantMessage(
            content=(
                ContentToolUse(
                    id="call_e", name="exec", input={"input": "text(1)"}, parse_error=None
                ),
                ContentToolUse(
                    id="call_s",
                    name="spawn_agent",
                    input={"task": "t"},
                    parse_error=None,
                    namespace="collaboration",
                ),
            )
        ),
        ToolMessage(content=(ContentText(text="1"),), tool_call_id="call_e", is_error=None),
    )
    body = _encode(_request(context=context, tools=tools))
    items = cast("list[dict[str, Any]]", body["input"])[1:]
    assert items == [
        {
            "type": "custom_tool_call",
            "call_id": "call_e",
            "name": "exec",
            "input": "text(1)",
        },
        {
            "type": "function_call",
            "call_id": "call_s",
            "name": "spawn_agent",
            "arguments": json.dumps({"task": "t"}),
            "namespace": "collaboration",
        },
        {"type": "custom_tool_call_output", "call_id": "call_e", "output": "1"},
    ]


_CUSTOM_CALL_ITEM: dict[str, Any] = {
    "id": "ctc_1",
    "type": "custom_tool_call",
    "status": "completed",
    "call_id": "call_e",
    "name": "exec",
    "input": "console.log(1)",
    "namespace": "functions",
}


def test_custom_tool_call_items_stream_and_assemble_as_tool_use() -> None:
    frames = (
        _sse({"type": "response.created", "sequence_number": 0, "response": _response({})}),
        _sse(
            {
                "type": "response.output_item.added",
                "sequence_number": 1,
                "output_index": 0,
                "item": _CUSTOM_CALL_ITEM | {"input": "", "status": "in_progress"},
            }
        ),
        _sse(
            {
                "type": "response.custom_tool_call_input.delta",
                "sequence_number": 2,
                "item_id": "ctc_1",
                "output_index": 0,
                "delta": "console.log(1)",
            }
        ),
        _sse(
            {
                "type": "response.output_item.done",
                "sequence_number": 3,
                "output_index": 0,
                "item": _CUSTOM_CALL_ITEM,
            }
        ),
        _sse(
            {
                "type": "response.completed",
                "sequence_number": 4,
                "response": _response({"output": [_CUSTOM_CALL_ITEM]}),
            }
        ),
    )
    events, result = _decode(*frames)
    # Freeform text is not JSON, so the one-field object is relayed whole when the item is done.
    assert events == [
        StreamToolUseStart(index=0, id="call_e", name="exec", namespace="functions"),
        StreamToolUseInputDelta(index=0, partial_json=json.dumps({"input": "console.log(1)"})),
    ]
    assert isinstance(result, ModelResponse)
    (message,) = result.output
    assert message.content == (
        ContentToolUse(
            id="call_e",
            name="exec",
            input={"input": "console.log(1)"},
            parse_error=None,
            namespace="functions",
        ),
    )
    assert result.stop_reason == StopToolUse()


def test_history_preserves_which_declaration_each_call_identifies() -> None:
    # Two groups share `status`. A call that carried its group keeps it; a call made under the
    # flattened alias a namespace-less subject used identifies the same declaration, so it is
    # rendered with the group restored; a call by an unqualified own name stays unqualified.
    tools = (
        _tool("status", namespace="collaboration"),
        _tool("status", namespace="workflows"),
        _tool("spawn", namespace="collaboration"),
        _tool("exec", namespace="functions", freeform=_LARK),
    )
    context = (
        *_HI,
        AssistantMessage(
            content=(
                ContentToolUse(
                    id="c1", name="status", input={}, parse_error=None, namespace="collaboration"
                ),
                ContentToolUse(id="c2", name="workflows__status", input={}, parse_error=None),
                ContentToolUse(id="c3", name="spawn", input={}, parse_error=None),
                ContentToolUse(id="c4", name="exec", input={"input": "x()"}, parse_error=None),
            )
        ),
    )
    body = _encode(_request(context=context, tools=tools))
    items = cast("list[dict[str, Any]]", body["input"])[1:]
    assert [(i["type"], i["name"], i.get("namespace")) for i in items] == [
        ("function_call", "status", "collaboration"),
        ("function_call", "status", "workflows"),
        ("function_call", "spawn", None),
        ("custom_tool_call", "exec", None),
    ]


def test_summary_choice_renders_as_reasoning_summary() -> None:
    def reasoning(params: ReasoningParams) -> JSONObject:
        return cast("JSONObject", _sampling_body(SamplingParams(reasoning=params))["reasoning"])

    assert reasoning(ReasoningParams(effort="high", summary=True)) == {
        "effort": "high",
        "summary": "detailed",
    }
    # False: no summary, and the declared default does not fill it in.
    assert reasoning(ReasoningParams(effort="high", summary=False)) == {"effort": "high"}
    assert reasoning(ReasoningParams(effort="high")) == {"effort": "high", "summary": "auto"}
    assert reasoning(ReasoningParams(enabled=False, summary=True)) == {"effort": "none"}
