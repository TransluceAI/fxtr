"""Gemini generateContent: the codec's model side (request rendering, SSE decoding, failure
classification) and the thin ``GeminiGenerateContentAPI`` transport, exercised against an
in-process ``httpx2.MockTransport``."""

import dataclasses
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest

from behaviors.implementations.dialects.anthropic_messages import (
    AnthropicMessagesCodec,
    AnthropicRedactedThinking,
    AnthropicSignature,
)
from behaviors.implementations.dialects.gemini_generate_content import (
    GeminiGenerateContentCodec,
    GeminiSignaturePlacement,
    GeminiSseDecoder,
    GeminiThoughtSignature,
    decode_gemini_failure,
    decode_gemini_response,
)
from behaviors.implementations.dialects.openai_chat_completions import OpenRouterReasoning
from behaviors.implementations.dialects.openai_responses import (
    OpenAIResponsesReasoning,
)
from behaviors.implementations.dialects.reconstruction import compare_response
from behaviors.implementations.models._workarounds import RejectionWorkaround, WorkaroundLedger
from behaviors.implementations.models.gemini import (
    SKIP_SIGNATURE_VALIDATION,
    GeminiClient,
    GeminiGenerateContentAPI,
)
from behaviors.interfaces.model_apis import StreamingModelAPI
from behaviors.types.adaptation import AdaptationPolicy, RequestLoss
from behaviors.types.content import (
    ContentReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.dialect import EncodedRequest
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
    StopEndTurn,
    StopMaxTokens,
    StopRefusal,
    StopToolUse,
)
from behaviors.types.streaming import (
    StreamEvent,
    StreamReasoningDelta,
    StreamReasoningStart,
    StreamReasoningState,
    StreamRestart,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
    SubjectStreamItem,
)
from behaviors.types.tool import ToolChoiceTool, ToolParam
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData

pytestmark = pytest.mark.anyio

_CODEC = GeminiGenerateContentCodec()
_TOOL = ToolParam(
    name="get_weather",
    description="Weather for a city.",
    input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
    strict=False,
    input_examples=None,
)


def _request(
    context: tuple[ChatMessage, ...],
    *,
    tools: tuple[ToolParam, ...] = (),
    sampling: SamplingParams | None = None,
) -> ModelRequest:
    return ModelRequest(
        context=context,
        tools=tools,
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=sampling or SamplingParams(),
    )


def _encode(request: ModelRequest) -> JSONObject:
    """What is sent for `request`: adapted to the dialect, then rendered."""
    adapted = _CODEC.adapt_request("gemini-test", request).request
    return _CODEC.encode_request("gemini-test", adapted, streaming=True).body


def _gemini(
    signature: str, on: GeminiSignaturePlacement, model: str = "gemini-test"
) -> DynamicData:
    return DynamicData.from_tagged(GeminiThoughtSignature(model=model, signature=signature, on=on))


def _signed(text: str, signature: str, on: GeminiSignaturePlacement) -> ContentReasoning:
    return ContentReasoning(text=text, form="summary", reasoning_state=_gemini(signature, on))


def test_conforms_to_streaming_model_api() -> None:
    api = GeminiGenerateContentAPI("gemini-test", api_key="k")
    assert isinstance(api, StreamingModelAPI)


# --- request rendering ----------------------------------------------------------------------------


def test_request_path_streams_with_sse() -> None:
    encoded = _CODEC.encode_request("gemini-test", _request(()), streaming=True)
    assert encoded.path == "/v1beta/models/gemini-test:streamGenerateContent"
    assert encoded.query == (("alt", "sse"),)
    assert "model" not in encoded.body
    unary = _CODEC.encode_request("gemini-test", _request(()), streaming=False)
    assert (unary.path, unary.query) == ("/v1beta/models/gemini-test:generateContent", ())


def test_a_tool_loop_renders_as_alternating_contents() -> None:
    call = ContentToolUse(
        id="toolu_1", name="get_weather", input={"city": "Paris"}, parse_error=None
    )
    body = _encode(
        _request(
            (
                SystemMessage(content=(ContentText(text="Be brief."),)),
                UserMessage(content=(ContentText(text="Weather?"),)),
                AssistantMessage(content=(ContentText(text="Checking."), call)),
                ToolMessage(
                    content=(ContentText(text="Sunny"),), tool_call_id="toolu_1", is_error=None
                ),
                ToolMessage(content=(ContentText(text="boom"),), tool_call_id="x", is_error=True),
                UserMessage(content=(ContentText(text="Thanks"),)),
                SystemMessage(content=(ContentText(text="Wrap up."),)),
            ),
            tools=(_TOOL,),
        )
    )
    assert body["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    # The tool results and the user's next words are one user turn; a later system message has
    # no system role here and joins it.
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "Weather?"}]},
        {
            "role": "model",
            "parts": [
                {"text": "Checking."},
                {
                    "functionCall": {
                        "name": "get_weather",
                        "args": {"city": "Paris"},
                        "id": "toolu_1",
                    }
                },
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": "get_weather",
                        "response": {"output": "Sunny"},
                        "id": "toolu_1",
                    }
                },
                {"functionResponse": {"name": "", "response": {"error": "boom"}, "id": "x"}},
                {"text": "Thanks"},
                {"text": "Wrap up."},
            ],
        },
    ]
    assert body["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "get_weather",
                    "description": "Weather for a city.",
                    "parametersJsonSchema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ]
        }
    ]


def test_a_synthesized_call_id_is_left_out() -> None:
    # A call that came without an id goes back without one, paired by name and order.
    call = ContentToolUse(id="gemini_call_0", name="get_weather", input={}, parse_error=None)
    body = _encode(
        _request(
            (
                AssistantMessage(content=(call,)),
                ToolMessage(
                    content=(ContentText(text="ok"),), tool_call_id="gemini_call_0", is_error=None
                ),
            )
        )
    )
    contents = cast("list[dict[str, Any]]", body["contents"])
    assert contents[0]["parts"] == [{"functionCall": {"name": "get_weather", "args": {}}}]
    assert contents[1]["parts"] == [
        {"functionResponse": {"name": "get_weather", "response": {"output": "ok"}}}
    ]


@pytest.mark.parametrize(
    ("reasoning", "thinking"),
    [
        (ReasoningParams(enabled=False), {"thinkingBudget": 0}),
        (ReasoningParams(budget_tokens=2048), {"thinkingBudget": 2048}),
        (
            ReasoningParams(effort="low", summary=True),
            {"thinkingLevel": "LOW", "includeThoughts": True},
        ),
        (ReasoningParams(effort="xhigh"), {"thinkingLevel": "HIGH"}),
        (ReasoningParams(), {}),
    ],
)
def test_reasoning_renders_as_thinking_config(
    reasoning: ReasoningParams, thinking: dict[str, Any]
) -> None:
    body = _encode(_request((), sampling=SamplingParams(max_tokens=100, reasoning=reasoning)))
    assert body["generationConfig"] == {"maxOutputTokens": 100, "thinkingConfig": thinking}


def test_a_forced_tool_is_any_restricted_to_its_name() -> None:
    request = ModelRequest(
        context=(),
        tools=(_TOOL,),
        tool_choice=ToolChoiceTool(name="get_weather"),
        parallel_tool_calls=None,
        sampling=SamplingParams(),
    )
    assert _encode(request)["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["get_weather"]}
    }


def test_gemini_signatures_go_back_on_their_parts() -> None:
    message = AssistantMessage(
        content=(
            _signed("Plan.", "sig-thought", "thought"),
            _signed("", "sig-call", "next"),
            ContentToolUse(id="gemini_call_1", name="get_weather", input={}, parse_error=None),
            _signed("", "sig-end", "next"),
        )
    )
    body = _encode(_request((message,)))
    assert cast("list[dict[str, Any]]", body["contents"])[0]["parts"] == [
        {"text": "Plan.", "thought": True, "thoughtSignature": "sig-thought"},
        {"functionCall": {"name": "get_weather", "args": {}}, "thoughtSignature": "sig-call"},
        {"text": "", "thoughtSignature": "sig-end"},
    ]


def test_two_signatures_in_a_row_keep_their_own_parts() -> None:
    message = AssistantMessage(
        content=(
            _signed("", "sig-next", "next"),
            _signed("More.", "sig-own", "thought"),
        )
    )
    body = _encode(_request((message,)))
    assert cast("list[dict[str, Any]]", body["contents"])[0]["parts"] == [
        {"text": "", "thoughtSignature": "sig-next"},
        {"text": "More.", "thought": True, "thoughtSignature": "sig-own"},
    ]


def test_parallel_calls_re_paired_with_their_results_go_back_together() -> None:
    # A dialect that re-pairs each call with its result (Responses) splits one response's
    # parallel calls; the API refuses a signed call interleaved with results.
    signed = _signed("", "sig", "next")

    def call(n: int) -> ContentToolUse:
        return ContentToolUse(id=f"c{n}", name="get_weather", input={"n": n}, parse_error=None)

    def result(n: int) -> ToolMessage:
        return ToolMessage(
            content=(ContentText(text=f"r{n}"),), tool_call_id=f"c{n}", is_error=None
        )

    body = _encode(
        _request(
            (
                *_HI,
                AssistantMessage(content=(signed, call(1))),
                result(1),
                AssistantMessage(content=(call(2),)),
                result(2),
            )
        )
    )
    contents = cast("list[dict[str, Any]]", body["contents"])
    assert [content["role"] for content in contents] == ["user", "model", "user"]
    assert [list(part) for part in contents[1]["parts"]] == [
        ["functionCall", "thoughtSignature"],
        ["functionCall"],
    ]
    assert [part["functionResponse"]["response"] for part in contents[2]["parts"]] == [
        {"output": "r1"},
        {"output": "r2"},
    ]
    # A later step's call carries its own signature: a turn of its own.
    body = _encode(
        _request(
            (
                *_HI,
                AssistantMessage(content=(signed, call(1))),
                result(1),
                AssistantMessage(content=(signed, call(2))),
                result(2),
            )
        )
    )
    assert len(cast("list[Any]", body["contents"])) == 5


def test_a_user_turn_keeps_its_order() -> None:
    body: dict[str, Any] = {
        "contents": [
            {"role": "model", "parts": [{"functionCall": {"name": "run", "args": {}}}]},
            {
                "role": "user",
                "parts": [
                    {"text": "before"},
                    {"functionResponse": {"name": "run", "response": {"output": "ok"}}},
                    {"text": "after"},
                ],
            },
        ]
    }
    target = "/v1beta/models/gemini-test:streamGenerateContent?alt=sse"
    decoded = _CODEC.decode_request(target, (), json.dumps(body).encode())
    assert [type(m).__name__ for m in decoded.request.context] == [
        "AssistantMessage",
        "UserMessage",
        "ToolMessage",
        "UserMessage",
    ]
    encoded = _CODEC.encode_request("gemini-test", decoded.request, streaming=True)
    assert encoded.body["contents"] == body["contents"]


@dataclass(frozen=True)
class _ElsewhereState(TaggedDataclass, fxtr_type="com.example.ElsewhereState.v1"):
    token: str


@pytest.mark.parametrize(
    "state",
    [
        AnthropicSignature(signature="EqQBCkYIBRgC"),
        AnthropicRedactedThinking(data="opaque"),
        OpenAIResponsesReasoning(id="rs_1", encrypted_content="enc"),
        OpenRouterReasoning(model="m/x", details=({"type": "reasoning.text"},)),
        _ElsewhereState(token="t"),
        GeminiThoughtSignature(model="gemini-other", signature="sig", on="thought"),
    ],
    ids=lambda state: type(state).__name__,
)
def test_the_codec_accepts_only_gemini_signatures_issued_for_the_model(
    state: TaggedDataclass,
) -> None:
    # Whether the API accepts one Gemini model's signature from another is unverified, so a
    # signature recorded for another model is not sent either.
    assert not _CODEC.accepts_reasoning_state(DynamicData.from_tagged(state), "gemini-test")


def test_a_signature_for_the_model_is_accepted_however_the_model_is_spelled() -> None:
    assert _CODEC.accepts_reasoning_state(_gemini("sig", "thought"), "models/gemini-test")
    assert _CODEC.accepts_reasoning_state(
        _gemini("sig", "thought", model="gemini-test"), "gemini-test"
    )


def test_only_geminis_reasoning_state_and_unsigned_summaries_are_sent() -> None:
    # Adaptation removes a block whose state the codec rejects, and literal reasoning, which has
    # no Gemini form, recording each; an unsigned summary goes as a thought part.
    foreign = ContentReasoning(
        text="hm",
        form="summary",
        reasoning_state=DynamicData.from_tagged(AnthropicSignature(signature="EqQBCkYIBRgC")),
    )
    unsigned = ContentReasoning(text="a summary", form="summary")
    literal = ContentReasoning(text="raw thoughts", form="full")
    request = _request(
        (AssistantMessage(content=(foreign, unsigned, literal, ContentText(text="hi"))),)
    )
    adapted = _CODEC.adapt_request("gemini-test", request)
    assert [loss.code for loss in adapted.losses] == ["foreign_reasoning", "literal_reasoning"]
    body = _CODEC.encode_request("gemini-test", adapted.request, streaming=True).body
    assert cast("list[dict[str, Any]]", body["contents"])[0]["parts"] == [
        {"text": "a summary", "thought": True},
        {"text": "hi"},
    ]


# --- response decoding ----------------------------------------------------------------------------


def _frame(parts: list[dict[str, Any]], **extra: Any) -> bytes:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": parts}, "index": 0}
    for key in ("finishReason", "finishMessage", "safetyRatings"):
        if key in extra:
            candidate[key] = extra.pop(key)
    payload = {"candidates": [candidate], "modelVersion": "gemini-test", **extra}
    return f"data: {json.dumps(payload)}\r\n\r\n".encode()


_USAGE = {
    "promptTokenCount": 120,
    "cachedContentTokenCount": 20,
    "candidatesTokenCount": 30,
    "thoughtsTokenCount": 50,
    "totalTokenCount": 200,
}
_STREAM = [
    _frame([{"text": "Thinking about ", "thought": True}]),
    _frame([{"text": "Paris.", "thought": True}]),
    _frame([{"text": "Let me "}]),
    _frame([{"text": "check."}]),
    _frame(
        [
            {
                "functionCall": {"name": "get_weather", "args": {"city": "Paris"}},
                "thoughtSignature": "sig",
            }
        ],
        finishReason="STOP",
        usageMetadata=_USAGE,
        responseId="resp-1",
    ),
]


def _decode(*frames: bytes) -> tuple[list[StreamEvent], GeminiSseDecoder, Any]:
    decoder = GeminiSseDecoder()
    events: list[StreamEvent] = []
    for frame in frames:
        events.extend(decoder.feed(frame))
    return events, decoder, decoder.finish()


def test_a_stream_relays_deltas_and_assembles_the_response() -> None:
    events, decoder, result = _decode(*_STREAM)
    assert decoder.complete()
    assert isinstance(result, ModelResponse)
    (message,) = result.output
    assert isinstance(message, AssistantMessage)
    reasoning, text, signed, call = message.content
    assert reasoning == ContentReasoning(text="Thinking about Paris.", form="summary")
    assert text == ContentText(text="Let me check.")
    # A decoder given no model records the model the response names.
    assert signed == _signed("", "sig", "next")
    assert isinstance(call, ContentToolUse)
    assert (call.name, dict(call.input)) == ("get_weather", {"city": "Paris"})
    assert call.id.startswith("gemini_call_")
    assert result.id == "resp-1"
    assert result.stop_reason == StopToolUse()
    assert (result.usage.input_tokens, result.usage.cache_read_input_tokens) == (100, 20)
    assert (result.usage.output_tokens, result.usage.reasoning_output_tokens) == (80, 50)
    # The events carry the same call id as the assembled response, signature first.
    assert events == [
        StreamReasoningStart(form="summary"),
        StreamReasoningDelta(text="Thinking about "),
        StreamReasoningDelta(text="Paris."),
        StreamTextDelta(text="Let me "),
        StreamTextDelta(text="check."),
        StreamReasoningStart(form="summary"),
        StreamReasoningState(state=_gemini("sig", "next")),
        StreamToolUseStart(index=0, id=call.id, name="get_weather"),
        StreamToolUseInputDelta(index=0, partial_json='{"city": "Paris"}'),
    ]


def test_signatures_are_recorded_for_the_requested_model_not_the_version_served() -> None:
    # The next request names the requested model, which a dated `modelVersion` would not match.
    decoder = GeminiSseDecoder("models/gemini-3-pro")
    decoder.feed(_STREAM[-1])
    result = decoder.finish()
    assert isinstance(result, ModelResponse)
    (signed, _call) = result.output[0].content
    assert signed == ContentReasoning(
        text="", form="summary", reasoning_state=_gemini("sig", "next", model="gemini-3-pro")
    )


def test_the_raw_message_is_what_the_harness_side_renders_back() -> None:
    # The passthrough check compares the provider's response with the harness-side rendering of
    # its decoding: the two must agree on a faithful decode.
    _, decoder, result = _decode(*_STREAM)
    raw = decoder.raw_message()
    assert raw is not None and isinstance(result, ModelResponse)
    encoded = _CODEC.encode_response("gemini-test", result)
    differences, _ = compare_response("gemini_generate_content", raw, encoded)
    assert differences == ()


def test_the_signature_returned_goes_back_on_the_call() -> None:
    _, _, result = _decode(*_STREAM)
    assert isinstance(result, ModelResponse)
    body = _encode(_request(result.output))
    parts = cast("list[dict[str, Any]]", body["contents"])[0]["parts"]
    assert parts[-1] == {
        "functionCall": {"name": "get_weather", "args": {"city": "Paris"}},
        "thoughtSignature": "sig",
    }


def test_a_content_filter_stop_carries_the_providers_detail() -> None:
    ratings: JSONValue = [{"category": "HARM_CATEGORY_HARASSMENT", "probability": "HIGH"}]
    _, _, result = _decode(
        _frame(
            [{"text": "x"}], finishReason="SAFETY", finishMessage="blocked", safetyRatings=ratings
        )
    )
    assert isinstance(result, ModelResponse)
    assert result.stop_reason == StopRefusal(
        provider_reason="SAFETY",
        complete=False,
        detail={"finishMessage": "blocked", "safetyRatings": ratings},
    )


def test_finish_reasons() -> None:
    def stop(reason: str) -> Any:
        _, _, result = _decode(_frame([{"text": "x"}], finishReason=reason))
        return cast("ModelResponse", result).stop_reason

    assert stop("STOP") == StopEndTurn()
    assert stop("MAX_TOKENS") == StopMaxTokens()
    assert stop("SAFETY").type == "refusal"
    assert stop("RECITATION").type == "content_filter"
    assert stop("MALFORMED_FUNCTION_CALL").type == "other"


def test_an_error_frame_ends_the_stream_as_a_failure() -> None:
    error = {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}
    _, decoder, result = _decode(_frame([{"text": "x"}]), f"data: {json.dumps(error)}\n\n".encode())
    assert decoder.complete()
    assert isinstance(result, ModelCallFailure)
    assert result.category == "overloaded"


def test_a_stream_without_a_finish_reason_is_a_failure() -> None:
    _, decoder, result = _decode(_frame([{"text": "x"}]))
    assert not decoder.complete()
    assert isinstance(result, ModelCallFailure)


def test_a_blocked_prompt_ends_the_stream_as_filtered() -> None:
    frame = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 4}}
    _, decoder, result = _decode(f"data: {json.dumps(frame)}\r\n\r\n".encode())
    assert decoder.complete()
    assert isinstance(result, ModelResponse)
    assert result.stop_reason.type == "refusal"
    assert result.output == ()


def test_non_streamed_body_keeps_its_parts() -> None:
    body = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "a"}, {"text": "b"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
    }
    result = decode_gemini_response(json.dumps(body).encode())
    assert isinstance(result, ModelResponse)
    assert result.output == (
        AssistantMessage(content=(ContentText(text="a"), ContentText(text="b"))),
    )


@pytest.mark.parametrize(
    ("status", "message", "category"),
    [
        (
            400,
            "The input token count (2000000) exceeds the maximum number of tokens allowed (1048).",
            "context_length",
        ),
        (400, "Invalid JSON payload received.", "invalid_request"),
        (403, "API key not valid.", "authentication"),
        (429, "Resource has been exhausted (e.g. check quota).", "rate_limited"),
        (500, "Internal error encountered.", "server_error"),
        (503, "The model is overloaded. Please try again later.", "overloaded"),
    ],
)
def test_failure_classification(status: int, message: str, category: str) -> None:
    body = json.dumps({"error": {"code": status, "message": message, "status": "X"}}).encode()
    failure = decode_gemini_failure(status, body)
    assert (failure.category, failure.provider_http_status) == (category, status)


# --- the transport ------------------------------------------------------------------------------


@dataclass
class _Upstream:
    answers: list[httpx2.Response]
    seen: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        return self.answers.pop(0)

    def client(self) -> GeminiClient:
        return GeminiClient(
            api_key="k",
            base_url="https://gemini.test",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler)),
        )

    def api(self) -> GeminiGenerateContentAPI:
        return GeminiGenerateContentAPI("gemini-test", client=self.client())


def _ok() -> httpx2.Response:
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=b"".join(_STREAM)
    )


_HI = (UserMessage(content=(ContentText(text="hi"),)),)


async def test_transport_sends_the_encoded_body_and_decodes_the_stream() -> None:
    upstream = _Upstream([_ok()])
    seen: list[StreamEvent] = []

    async def collect(event: StreamEvent) -> None:
        seen.append(event)

    result = await upstream.api().generate_with_events(_request(_HI), collect)
    assert isinstance(result, ModelResponse)
    assert len(seen) == 9
    (request,) = upstream.seen
    assert (
        str(request.url)
        == "https://gemini.test/v1beta/models/gemini-test:streamGenerateContent?alt=sse"
    )
    assert request.headers["x-goog-api-key"] == "k"
    assert json.loads(request.content) == {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}]
    }


_UNSIGNED_400 = httpx2.Response(
    400,
    content=json.dumps(
        {
            "error": {
                "code": 400,
                "message": "Function call is missing a thought_signature in functionCall parts.",
                "status": "INVALID_ARGUMENT",
            }
        }
    ).encode(),
)


async def test_unsigned_calls_get_the_placeholder_once_the_model_asks_and_remembered() -> None:
    call = ContentToolUse(id="toolu_1", name="get_weather", input={}, parse_error=None)
    history = _request(
        (
            *_HI,
            AssistantMessage(content=(call,)),
            ToolMessage(content=(ContentText(text="ok"),), tool_call_id="toolu_1", is_error=None),
        )
    )
    upstream = _Upstream([_UNSIGNED_400, _ok(), _ok()])
    api = upstream.api()
    assert isinstance(await api.generate(history), ModelResponse)

    def signatures(request: httpx2.Request) -> list[Any]:
        model = json.loads(request.content)["contents"][1]
        return [part.get("thoughtSignature") for part in model["parts"]]

    assert [signatures(r) for r in upstream.seen] == [[None], [SKIP_SIGNATURE_VALIDATION]]
    assert isinstance(await api.generate(history), ModelResponse)
    assert signatures(upstream.seen[2]) == [SKIP_SIGNATURE_VALIDATION]


_INVALID_400 = httpx2.Response(
    400,
    content=json.dumps(
        {
            "error": {
                "code": 400,
                "message": "Request contains an invalid argument.",
                "status": "INVALID_ARGUMENT",
            }
        }
    ).encode(),
)


async def test_thinking_off_is_respelled_once_the_model_refuses_and_remembered() -> None:
    off = _request(_HI, sampling=SamplingParams(reasoning=ReasoningParams(enabled=False)))
    upstream = _Upstream([_INVALID_400, _ok(), _ok()])
    api = upstream.api()
    assert isinstance(await api.generate(off), ModelResponse)

    def thinking(request: httpx2.Request) -> Any:
        return json.loads(request.content)["generationConfig"]["thinkingConfig"]

    assert [thinking(r) for r in upstream.seen] == [
        {"thinkingBudget": 0},
        {"thinkingLevel": "MINIMAL"},
    ]
    assert isinstance(await api.generate(off), ModelResponse)
    assert thinking(upstream.seen[2]) == {"thinkingLevel": "MINIMAL"}


async def test_an_invalid_argument_with_thinking_on_is_not_retried() -> None:
    upstream = _Upstream([_INVALID_400])
    result = await upstream.api().generate(_request(_HI))
    assert isinstance(result, ModelCallFailure)
    assert len(upstream.seen) == 1


_OFF = _request(_HI, sampling=SamplingParams(reasoning=ReasoningParams(enabled=False)))
_REFUSE = AdaptationPolicy(semantic_changes="refuse")


def _thinking(request: httpx2.Request) -> Any:
    return json.loads(request.content)["generationConfig"]["thinkingConfig"]


def _rate_limited() -> httpx2.Response:
    return httpx2.Response(
        429,
        content=json.dumps(
            {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
        ).encode(),
    )


async def test_a_learned_workaround_is_reported_on_the_result() -> None:
    upstream = _Upstream([_INVALID_400, _ok()])
    result = await upstream.api().generate(_OFF)
    assert isinstance(result, ModelResponse)
    assert [(loss.code, loss.semantic) for loss in result.adaptation_losses] == [
        ("reasoning_off", True)
    ]


async def test_a_false_match_does_not_refuse_later_valid_calls() -> None:
    # An "invalid argument" on a request with thinking off is recognized as the refusal of
    # thinkingBudget 0; the retry fails for another reason, so the workaround stays provisional,
    # for that request only. An unrelated success does not confirm it, and a later request
    # under `refuse` reaches the provider as asked rather than being refused locally.
    other_off = _request(
        (UserMessage(content=(ContentText(text="later"),)),),
        sampling=SamplingParams(reasoning=ReasoningParams(enabled=False)),
    )
    upstream = _Upstream([_INVALID_400, _rate_limited(), _ok(), _ok()])
    api = upstream.api()
    assert isinstance(await api.generate(_OFF), ModelCallFailure)
    assert isinstance(await api.generate(_request(_HI)), ModelResponse)
    result = await api.generate(replace(other_off, adaptation_policy=_REFUSE))
    assert isinstance(result, ModelResponse)
    assert _thinking(upstream.seen[-1]) == {"thinkingBudget": 0}


async def test_a_confirmed_workaround_that_changes_meaning_is_refused_locally() -> None:
    upstream = _Upstream([_INVALID_400, _ok()])
    api = upstream.api()
    assert isinstance(await api.generate(_OFF), ModelResponse)  # learned and confirmed
    result = await api.generate(replace(_OFF, adaptation_policy=_REFUSE))
    assert isinstance(result, ModelCallFailure)
    assert result.category == "adaptation_refused"
    assert result.provider_http_status is None
    assert result.adaptation_losses == ()  # nothing was sent
    assert [loss.code for loss in result.proposed_adaptation_losses if loss.semantic] == [
        "reasoning_off"
    ]
    assert len(upstream.seen) == 2  # nothing more was sent


async def test_a_refused_fix_leaves_the_providers_rejection_annotated() -> None:
    upstream = _Upstream([_INVALID_400])
    result = await upstream.api().generate(replace(_OFF, adaptation_policy=_REFUSE))
    assert isinstance(result, ModelCallFailure)
    assert result.category == "adaptation_refused"
    assert result.provider_http_status == 400
    assert [loss.code for loss in result.proposed_adaptation_losses if loss.semantic] == [
        "reasoning_off"
    ]
    assert not any(loss.semantic for loss in result.adaptation_losses)  # what was sent
    assert "semantic_changes='allow'" in result.description
    assert len(upstream.seen) == 1


def _with_header(
    name: str,
) -> Callable[[EncodedRequest], tuple[EncodedRequest, tuple[RequestLoss, ...]]]:
    """A wire change for a test workaround: a header naming it (a workaround counts only when it
    changes what is sent)."""

    def on_wire(encoded: EncodedRequest) -> tuple[EncodedRequest, tuple[RequestLoss, ...]]:
        return dataclasses.replace(encoded, headers=(*encoded.headers, ("x-test", name))), ()

    return on_wire


async def test_a_retry_after_streamed_events_is_preceded_by_a_restart() -> None:
    # A workaround for a rejection that arrives mid-stream: the events already delivered are
    # discarded by a `StreamRestart` before the retry's.
    error_frame = b'data: {"error": {"code": 500, "message": "boom", "status": "INTERNAL"}}\n\n'
    upstream = _Upstream(
        [
            httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_STREAM[0] + error_frame,
            ),
            _ok(),
        ]
    )
    api = upstream.api()
    api._workarounds = WorkaroundLedger(  # pyright: ignore[reportPrivateUsage]
        (
            RejectionWorkaround(
                name="boom",
                recognizes=lambda _prepared, failure: "boom" in failure.description,
                apply_to_wire=_with_header("boom"),
            ),
        )
    )
    seen: list[StreamEvent] = []

    async def collect(event: StreamEvent) -> None:
        seen.append(event)

    result = await api.generate_with_events(_request(_HI), collect)
    assert isinstance(result, ModelResponse)
    restarts = [i for i, event in enumerate(seen) if isinstance(event, StreamRestart)]
    assert len(restarts) == 1 and 0 < restarts[0] < len(seen) - 1


async def test_a_disproven_workaround_is_not_confirmed_by_another_ones_success() -> None:
    # Both recognize the rejection; the first is tried and the rejection comes again, so it is
    # dropped from the attempt before the second is tried, and only the second is confirmed.
    def boom(name: str) -> RejectionWorkaround:
        return RejectionWorkaround(
            name=name,
            recognizes=lambda _prepared, failure: "boom" in failure.description,
            apply_to_wire=_with_header(name),
        )

    rejection = httpx2.Response(
        400,
        content=json.dumps(
            {"error": {"code": 400, "message": "boom", "status": "INVALID_ARGUMENT"}}
        ).encode(),
    )
    upstream = _Upstream([rejection, rejection, _ok()])
    api = upstream.api()
    first, second = boom("first"), boom("second")
    ledger = WorkaroundLedger((first, second))
    api._workarounds = ledger  # pyright: ignore[reportPrivateUsage]
    assert isinstance(await api.generate(_request(_HI)), ModelResponse)
    assert ledger.applicable("another request") == (second,)


async def test_a_workaround_that_changes_nothing_is_not_retried_or_reported() -> None:
    # A recognized rejection, but nothing for the fix to change (no unsigned function call to
    # sign): no retry, no loss, and nothing learned.
    upstream = _Upstream([_UNSIGNED_400])
    api = upstream.api()
    result = await api.generate(_request(_HI))
    assert isinstance(result, ModelCallFailure) and result.adaptation_losses == ()
    assert len(upstream.seen) == 1
    assert api._workarounds.applicable("anything") == ()  # pyright: ignore[reportPrivateUsage]


def test_another_gemini_models_signature_is_left_out() -> None:
    # A thought signature names the model it was issued for; another Gemini model rejects it.
    def history(issued_for: str) -> ModelRequest:
        thought = ContentReasoning(
            text="hm", form="summary", reasoning_state=_gemini("sig", "next", model=issued_for)
        )
        return _request((*_HI, AssistantMessage(content=(thought, ContentText(text="a")))))

    own = _CODEC.adapt_request("gemini-test", history("gemini-test"))
    assert own.losses == ()
    other = _CODEC.adapt_request("gemini-test", history("gemini-other"))
    assert [loss.code for loss in other.losses] == ["foreign_reasoning"]


async def test_a_workaround_that_does_not_help_is_forgotten() -> None:
    # Applied (confirmed earlier), and the same rejection comes anyway: it was not the fix.
    upstream = _Upstream([_INVALID_400, _ok(), _INVALID_400, _ok()])
    api = upstream.api()
    assert isinstance(await api.generate(_OFF), ModelResponse)
    assert isinstance(await api.generate(_OFF), ModelCallFailure)
    assert isinstance(await api.generate(_request(_HI)), ModelResponse)
    assert [_thinking(r) for r in upstream.seen[:3]] == [
        {"thinkingBudget": 0},
        {"thinkingLevel": "MINIMAL"},
        {"thinkingLevel": "MINIMAL"},
    ]
    assert api._workarounds.applicable("anything") == ()  # pyright: ignore[reportPrivateUsage]


async def test_forwarding_puts_the_model_in_the_path_and_drops_the_harness_credentials() -> None:
    upstream = _Upstream([_ok()])
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}).encode()
    response = await upstream.api().forward_request(
        "/v1beta/models/harness-model:streamGenerateContent?alt=sse&key=harness-key",
        (("x-goog-api-key", "harness-key"), ("x-goog-api-client", "genai-js/1.0")),
        body,
    )
    await response.aclose()
    (request,) = upstream.seen
    assert (
        str(request.url)
        == "https://gemini.test/v1beta/models/gemini-test:streamGenerateContent?alt=sse"
    )
    assert request.headers["x-goog-api-key"] == "k"
    assert request.headers["x-goog-api-client"] == "genai-js/1.0"
    assert json.loads(request.content) == json.loads(body)


# --- recorded streams ----------------------------------------------------------------------------

_RECORDED = Path(__file__).parent / "rollouts" / "agents" / "fixtures" / "gemini_streams"


@pytest.mark.parametrize("name", sorted(path.name for path in _RECORDED.glob("*.sse")))
def test_a_recorded_stream_reconstructs_exactly(name: str) -> None:
    # Streams the API returned (2026-10-15: gemini-3.8-flash with and without tools and thought
    # summaries, gemini-4-argon, gemini-3.5-flash-lite): decoded, then rendered back by the
    # harness side, they are what the passthrough check compares against what the API sent.
    stream = (_RECORDED / name).read_bytes()
    decoder = GeminiSseDecoder()
    for start in range(0, len(stream), 97):  # in pieces, as a network delivers them
        decoder.feed(stream[start : start + 97])
    result = decoder.finish()
    assert decoder.complete() and isinstance(result, ModelResponse)
    raw = decoder.raw_message()
    assert raw is not None
    encoded = _CODEC.encode_response("gemini-test", result)
    differences, _ = compare_response("gemini_generate_content", raw, encoded)
    assert differences == ()


# --- the harness side's stream, and the translated path ------------------------------------------


async def _items(*items: SubjectStreamItem) -> AsyncIterator[SubjectStreamItem]:
    for item in items:
        yield item


async def test_text_after_a_call_streams_after_it() -> None:
    # The calls render whole from the terminal response, so what follows the first call waits
    # for them: the harness gets the response in its order.
    call = ContentToolUse(id="gemini_call_x", name="get_weather", input={}, parse_error=None)
    response = ModelResponse(
        id="r",
        output=(
            AssistantMessage(
                content=(ContentText(text="Checking."), call, ContentText(text="Then this."))
            ),
        ),
        stop_reason=StopToolUse(),
        usage=_decode(*_STREAM)[2].usage,
    )
    frames = [
        frame
        async for frame in _CODEC.encode_sse(
            "gemini-test",
            _items(
                StreamTextDelta(text="Checking."),
                StreamToolUseStart(index=0, id="gemini_call_x", name="get_weather"),
                StreamToolUseInputDelta(index=0, partial_json="{}"),
                StreamTextDelta(text="Then this."),
                response,
            ),
        )
    ]
    parts = [
        part
        for frame in frames
        for part in json.loads(frame.decode()[len("data: ") :])["candidates"][0]["content"]["parts"]
    ]
    assert parts == [
        {"text": "Checking."},
        {"functionCall": {"name": "get_weather", "args": {}}},
        {"text": "Then this."},
    ]


def test_tool_results_move_first_for_a_subject_that_requires_it() -> None:
    # Gemini interleaves a turn's text and results; Anthropic wants a turn's results first.
    call = ContentToolUse(id="c1", name="run", input={}, parse_error=None)
    before = UserMessage(content=(ContentText(text="before"),))
    result = ToolMessage(content=(ContentText(text="ok"),), tool_call_id="c1", is_error=None)
    request = _request((AssistantMessage(content=(call,)), before, result))
    adapted = AnthropicMessagesCodec().adapt_request("claude-test", request)
    assert adapted.request.context[1:] == (result, before)
    assert [loss.code for loss in adapted.losses] == ["tool_result_order"]


# --- filtered responses --------------------------------------------------------------------------

_RATINGS = [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "probability": "HIGH", "blocked": True}]
_FILTERED_CANDIDATE: dict[str, Any] = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "I can"}]},
            "finishReason": "SAFETY",
            "finishMessage": "The response was blocked.",
            "safetyRatings": _RATINGS,
            "index": 0,
        }
    ],
    "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 2, "totalTokenCount": 11},
    "modelVersion": "gemini-test",
}
_BLOCKED_PROMPT: dict[str, Any] = {
    "promptFeedback": {
        "blockReason": "PROHIBITED_CONTENT",
        "blockReasonMessage": "The prompt was blocked.",
        "safetyRatings": _RATINGS,
    },
    "usageMetadata": {"promptTokenCount": 9, "totalTokenCount": 9},
    "modelVersion": "gemini-test",
}


@pytest.mark.parametrize(
    "body", [_FILTERED_CANDIDATE, _BLOCKED_PROMPT], ids=["candidate", "prompt"]
)
def test_a_filtered_response_reconstructs_with_what_the_filter_said(body: dict[str, Any]) -> None:
    # Streamed and not: the decoded detail renders back into the fields it came from, which the
    # check compares on a filtered response.
    stream = f"data: {json.dumps(body)}\r\n\r\n".encode()
    _, decoder, streamed = _decode(stream)
    assert decoder.complete() and isinstance(streamed, ModelResponse)
    assert streamed.stop_reason.type == "refusal"
    raw = decoder.raw_message()
    assert raw is not None
    encoded = _CODEC.encode_response("gemini-test", streamed)
    assert compare_response("gemini_generate_content", raw, encoded)[0] == ()
    unary = decode_gemini_response(json.dumps(body).encode())
    assert isinstance(unary, ModelResponse)
    rendered = _CODEC.encode_response("gemini-test", unary)
    assert compare_response("gemini_generate_content", cast("JSONObject", body), rendered)[0] == ()
    if "promptFeedback" in body:
        assert "candidates" not in rendered
        assert rendered["promptFeedback"] == body["promptFeedback"]
    else:
        candidate = cast("list[dict[str, Any]]", rendered["candidates"])[0]
        assert candidate["finishMessage"] == "The response was blocked."
        assert candidate["safetyRatings"] == _RATINGS


def test_the_check_sees_a_dropped_filter_detail() -> None:
    unary = decode_gemini_response(json.dumps(_FILTERED_CANDIDATE).encode())
    assert isinstance(unary, ModelResponse)
    stripped = dataclasses.replace(
        unary, stop_reason=StopRefusal(provider_reason="SAFETY", complete=False, detail=None)
    )
    rendered = _CODEC.encode_response("gemini-test", stripped)
    differences, _ = compare_response(
        "gemini_generate_content", cast("JSONObject", _FILTERED_CANDIDATE), rendered
    )
    assert {d.path for d in differences} == {"/finishMessage", "/safetyRatings"}


def test_a_gemini_filter_detail_is_not_rendered_as_anthropic_stop_details() -> None:
    unary = decode_gemini_response(json.dumps(_FILTERED_CANDIDATE).encode())
    assert isinstance(unary, ModelResponse)
    message = AnthropicMessagesCodec().encode_response("claude-test", unary)
    assert message["stop_reason"] == "refusal"
    assert message.get("stop_details") is None


async def test_a_streamed_blocked_prompt_renders_as_its_feedback() -> None:
    unary = decode_gemini_response(json.dumps(_BLOCKED_PROMPT).encode())
    assert isinstance(unary, ModelResponse)
    frames = [frame async for frame in _CODEC.encode_sse("gemini-test", _items(unary))]
    (frame,) = frames
    payload = json.loads(frame.decode()[len("data: ") :])
    assert "candidates" not in payload
    assert payload["promptFeedback"] == _BLOCKED_PROMPT["promptFeedback"]
