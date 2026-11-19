"""Response formats, strict tools, tool output schemas and refusals across the dialect codecs,
and each codec's `adapt_request`, which fits them to its dialect and records each change."""

import json
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

import pytest

from behaviors.implementations.dialects.anthropic_messages import AnthropicMessagesCodec
from behaviors.implementations.dialects.gemini_generate_content import (
    GeminiGenerateContentCodec,
    openapi_schema_to_json_schema,
)
from behaviors.implementations.dialects.openai_chat_completions import OpenAIChatCompletionsCodec
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesCodec
from behaviors.interfaces.dialect_codecs import AdaptedRequest, ModelSideCodec
from behaviors.types.adaptation import AdaptationPolicy
from behaviors.types.content import ContentText, ContentToolUse
from behaviors.types.dialect import Dialect
from behaviors.types.json import JSONObject
from behaviors.types.message import AssistantMessage, ToolMessage, UserMessage
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ResponseFormat,
    ResponseFormatJsonObject,
    ResponseFormatJsonSchema,
    SamplingParams,
    StopContentFilter,
    StopEndTurn,
    StopMaxTokens,
    StopOther,
    StopRefusal,
    StopStopSequence,
    StopToolUse,
    UnexpectedStopError,
    Usage,
)
from behaviors.types.streaming import StreamTextDelta, SubjectStreamItem
from behaviors.types.tool import ToolChoiceAuto, ToolParam

_GEMINI_TARGET = "/v1beta/models/gemini-probe:generateContent"
_USAGE = Usage(
    input_tokens=1,
    cache_read_input_tokens=None,
    cache_write_input_tokens=None,
    output_tokens=1,
    reasoning_output_tokens=None,
)

# Within every provider's subset: OpenAI strict mode accepts it, Anthropic and Gemini enforce all
# of it.
_STRICT_SCHEMA: JSONObject = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["verdict"],
    "additionalProperties": False,
}
# A judge's score: bounds Anthropic does not enforce, and an optional field OpenAI's strict mode
# rejects.
_BOUNDED_SCHEMA: JSONObject = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 10},
        "note": {"type": "string"},
    },
    "required": ["score"],
}


def _request(
    response_format: ResponseFormat | None = None, tools: tuple[ToolParam, ...] = ()
) -> ModelRequest:
    return ModelRequest(
        context=(UserMessage(content=(ContentText(text="judge"),)),),
        tools=tools,
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(),
        response_format=response_format,
    )


def _tool(
    schema: JSONObject = _STRICT_SCHEMA, *, strict: Literal[True, False, "auto"] = True
) -> ToolParam:
    return ToolParam(
        name="submit", description=None, input_schema=schema, strict=strict, input_examples=None
    )


_CODECS: dict[Dialect, ModelSideCodec] = {
    "anthropic_messages": AnthropicMessagesCodec(),
    "openai_responses": OpenAIResponsesCodec(),
    "openai_chat_completions": OpenAIChatCompletionsCodec(),
    "gemini_generate_content": GeminiGenerateContentCodec(),
}


def adapt_request(request: ModelRequest, dialect: Dialect) -> AdaptedRequest:
    return _CODECS[dialect].adapt_request("m", request)


def _losses(request: ModelRequest, dialect: Dialect) -> dict[str, str]:
    return {loss.code: loss.detail for loss in adapt_request(request, dialect).losses}


# --- each dialect's spelling, both directions ----------------------------------------------------


def test_anthropic_output_format_round_trips_beside_effort() -> None:
    body = {
        "model": "m",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "judge"}],
        "output_config": {"effort": "high", "format": {"type": "json_schema", "schema": {}}},
    }
    decoded = AnthropicMessagesCodec().decode_request("/v1/messages", (), json.dumps(body).encode())
    assert decoded.request.response_format == ResponseFormatJsonSchema(schema={}, strict=True)
    encoded = AnthropicMessagesCodec().encode_request("m", decoded.request, streaming=False)
    assert encoded.body["output_config"] == body["output_config"]


def test_responses_text_format_keeps_verbosity_as_an_extra() -> None:
    fmt = {"type": "json_schema", "name": "verdict", "schema": _STRICT_SCHEMA, "strict": True}
    body = {"model": "m", "input": "judge", "text": {"format": fmt, "verbosity": "low"}}
    codec = OpenAIResponsesCodec()
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    assert decoded.request.response_format == ResponseFormatJsonSchema(
        schema=_STRICT_SCHEMA, strict=True, name="verdict"
    )
    assert codec.encode_request("m", decoded.request, streaming=False).body["text"] == body["text"]


def test_chat_completions_nests_the_schema_and_names_it() -> None:
    request = _request(ResponseFormatJsonSchema(schema=_STRICT_SCHEMA, strict=True))
    body = OpenAIChatCompletionsCodec().encode_request("m", request, streaming=False).body
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": _STRICT_SCHEMA, "strict": True},
    }


@pytest.mark.parametrize(
    "generation",
    [
        {"responseMimeType": "application/json", "responseJsonSchema": _STRICT_SCHEMA},
        {"responseMimeType": "application/json", "_responseJsonSchema": _STRICT_SCHEMA},
        {"responseFormat": {"text": {"mimeType": "APPLICATION_JSON", "schema": _STRICT_SCHEMA}}},
    ],
)
def test_gemini_schema_spellings_decode_alike(generation: dict[str, Any]) -> None:
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": "judge"}]}],
        "generationConfig": generation,
    }
    decoded = GeminiGenerateContentCodec().decode_request(
        _GEMINI_TARGET, (), json.dumps(body).encode()
    )
    assert decoded.request.response_format == ResponseFormatJsonSchema(
        schema=_STRICT_SCHEMA, strict=True
    )
    assert decoded.request.extras is None or "generationConfig" not in decoded.request.extras.body


def test_gemini_openapi_response_schema_is_lowercased() -> None:
    body = {
        "contents": [{"role": "user", "parts": [{"text": "judge"}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {"type": "OBJECT", "properties": {"n": {"type": "INTEGER"}}},
        },
    }
    decoded = GeminiGenerateContentCodec().decode_request(
        _GEMINI_TARGET, (), json.dumps(body).encode()
    )
    assert decoded.request.response_format == ResponseFormatJsonSchema(
        schema={"type": "object", "properties": {"n": {"type": "integer"}}}, strict=True
    )


def test_gemini_enum_mime_type_stays_with_gemini() -> None:
    body = {
        "contents": [{"role": "user", "parts": [{"text": "judge"}]}],
        "generationConfig": {"responseMimeType": "text/x.enum"},
    }
    decoded = GeminiGenerateContentCodec().decode_request(
        _GEMINI_TARGET, (), json.dumps(body).encode()
    )
    assert decoded.request.response_format is None
    assert [d.code for d in decoded.decode_losses] == ["response_mime_type"]


def test_gemini_validated_is_strict_tools_both_ways() -> None:
    body = {
        "contents": [{"role": "user", "parts": [{"text": "judge"}]}],
        "tools": [{"functionDeclarations": [{"name": "a"}, {"name": "b"}]}],
        "toolConfig": {"functionCallingConfig": {"mode": "VALIDATED"}},
    }
    codec = GeminiGenerateContentCodec()
    decoded = codec.decode_request(_GEMINI_TARGET, (), json.dumps(body).encode())
    assert decoded.request.tool_choice == ToolChoiceAuto()
    assert [tool.strict for tool in decoded.request.tools] == [True, True]
    assert not decoded.decode_losses
    encoded = codec.encode_request("gemini-probe", decoded.request, streaming=False)
    assert encoded.body["toolConfig"] == body["toolConfig"]


# --- adaptation ----------------------------------------------------------------------------------


def test_a_schema_every_provider_enforces_is_unchanged() -> None:
    request = _request(ResponseFormatJsonSchema(schema=_STRICT_SCHEMA, strict=True))
    for dialect in ("anthropic_messages", "openai_responses", "gemini_generate_content"):
        adapted = adapt_request(request, dialect)
        assert adapted.request == request
        assert not adapted.losses


def test_anthropic_moves_bounds_into_descriptions_and_forbids_extra_keys() -> None:
    request = _request(ResponseFormatJsonSchema(schema=_BOUNDED_SCHEMA, strict=True))
    adapted = adapt_request(request, "anthropic_messages")
    fmt = cast("ResponseFormatJsonSchema", adapted.request.response_format)
    score = cast("dict[str, Any]", cast("dict[str, Any]", fmt.schema["properties"])["score"])
    assert "minimum" not in score and "minimum" in score["description"]
    assert fmt.schema["additionalProperties"] is False
    losses = {loss.code: loss.detail for loss in adapted.losses}
    assert set(losses) == {"response_schema_weakened", "response_schema_strengthened"}
    assert "maximum" in losses["response_schema_weakened"]


def test_every_contract_change_is_semantic_so_refuse_rejects_it() -> None:
    request = _request(ResponseFormatJsonSchema(schema=_BOUNDED_SCHEMA, strict=True))
    adapted = adapt_request(request, "anthropic_messages")
    assert all(loss.semantic for loss in adapted.losses)
    assert AdaptationPolicy(semantic_changes="refuse").refuses(adapted.losses) is not None


def test_a_ref_with_a_sibling_constraint_is_never_merged_away() -> None:
    # Both the definition's enum and the sibling's apply (only 2 is valid); fitting either into
    # the other would accept 1 or 3. The allOf cannot be enforced, and moving it out leaves a
    # node Anthropic rejects for having no type, so the format is left out; `refuse` stops it.
    schema: JSONObject = {
        "type": "object",
        "properties": {"x": {"allOf": [{"$ref": "#/$defs/small", "enum": [2, 3]}]}},
        "required": ["x"],
        "additionalProperties": False,
        "$defs": {"small": {"enum": [1, 2]}},
    }
    request = _request(ResponseFormatJsonSchema(schema=schema, strict=True))
    adapted = adapt_request(request, "anthropic_messages")
    assert [loss.code for loss in adapted.losses] == ["response_format_dropped"]
    assert adapted.request.response_format is None
    assert AdaptationPolicy(semantic_changes="refuse").refuses(adapted.losses) is not None


@pytest.mark.parametrize(
    "prop",
    [{"$ref": "https://example.com/s.json"}, {"type": ["object", "null"]}],
    ids=["external $ref", "nullable object"],
)
def test_anthropic_fitting_never_keeps_what_it_cannot_enforce(prop: JSONObject) -> None:
    schema = _closed_object(x=prop)
    request = _request(ResponseFormatJsonSchema(schema=schema, strict=True))
    adapted = adapt_request(request, "anthropic_messages")
    assert adapted.losses
    assert AdaptationPolicy(semantic_changes="refuse").refuses(adapted.losses) is not None


def test_gemini_records_keywords_it_ignores() -> None:
    schema = {**_STRICT_SCHEMA, "properties": {"name": {"type": "string", "minLength": 1}}}
    losses = _losses(
        _request(ResponseFormatJsonSchema(schema=schema, strict=True)), "gemini_generate_content"
    )
    assert losses == {"response_schema_weakened": "not enforced: minLength"}


def test_openai_strict_contract_it_would_reject_is_sent_unenforced() -> None:
    request = _request(ResponseFormatJsonSchema(schema=_BOUNDED_SCHEMA, strict=True))
    adapted = adapt_request(request, "openai_responses")
    assert cast("ResponseFormatJsonSchema", adapted.request.response_format).strict is False
    assert [loss.code for loss in adapted.losses] == ["response_format_unenforced"]


# A judge's answer with a field whose own `format` a local validator checks, which OpenAI's
# strict mode does not take.
_CITED_SCHEMA: JSONObject = {
    **_STRICT_SCHEMA,
    "properties": {
        **cast("dict[str, Any]", _STRICT_SCHEMA["properties"]),
        "why": {"type": "string", "description": "cite lines", "format": "citations"},
    },
    "required": ["verdict", "why"],
}


@pytest.mark.parametrize("dialect", ["openai_responses", "openai_chat_completions"])
def test_openai_moves_a_keyword_that_constrains_only_its_own_value_and_stays_strict(
    dialect: Dialect,
) -> None:
    request = _request(
        ResponseFormatJsonSchema(schema=_CITED_SCHEMA, strict=True), tools=(_tool(_CITED_SCHEMA),)
    )
    adapted = adapt_request(request, dialect)
    fmt = cast("ResponseFormatJsonSchema", adapted.request.response_format)
    (tool,) = adapted.request.tools
    for schema in (fmt.schema, tool.input_schema):
        why = cast("dict[str, Any]", cast("dict[str, Any]", schema["properties"])["why"])
        assert "format" not in why
        assert why["description"] == 'cite lines\n\n{format: "citations"}'
    assert fmt.strict is True and tool.strict is True
    assert _losses(request, dialect) == {
        "response_schema_weakened": "not enforced in strict mode, moved into descriptions: format",
        "tool_schema_weakened": (
            "tool submit: not enforced in strict mode, moved into descriptions: format"
        ),
    }


@pytest.mark.parametrize("dialect", ["openai_responses", "openai_chat_completions"])
def test_openai_sends_string_lengths_strict_and_as_given(dialect: Dialect) -> None:
    # OpenAI's strict mode enforces them, though its documentation leaves them out.
    schema = {**_STRICT_SCHEMA, "properties": {"verdict": {"type": "string", "minLength": 1}}}
    request = _request(ResponseFormatJsonSchema(schema=schema, strict=True), tools=(_tool(schema),))
    adapted = adapt_request(request, dialect)
    assert adapted.request == request and not adapted.losses


@pytest.mark.parametrize("dialect", ["openai_responses", "openai_chat_completions"])
def test_openai_sends_a_write_only_field_strict_and_as_given(dialect: Dialect) -> None:
    # An annotation OpenAI's strict mode takes.
    schema = {
        **_STRICT_SCHEMA,
        "properties": {"verdict": {"type": "string", "writeOnly": True}},
    }
    request = _request(ResponseFormatJsonSchema(schema=schema, strict=True), tools=(_tool(schema),))
    adapted = adapt_request(request, dialect)
    assert adapted.request == request and not adapted.losses


def test_openai_moves_nothing_when_strict_mode_is_off_either_way() -> None:
    # The optional property turns strict mode off, so the format is sent as given.
    schema = {**_CITED_SCHEMA, "required": ["verdict"]}
    request = _request(ResponseFormatJsonSchema(schema=schema, strict=True))
    adapted = adapt_request(request, "openai_responses")
    fmt = cast("ResponseFormatJsonSchema", adapted.request.response_format)
    assert fmt.strict is False and fmt.schema == schema
    assert [loss.code for loss in adapted.losses] == ["response_format_unenforced"]


def test_openai_leaves_an_advisory_schema_as_given() -> None:
    request = _request(ResponseFormatJsonSchema(schema=_CITED_SCHEMA, strict=False))
    adapted = adapt_request(request, "openai_responses")
    assert adapted.request == request and not adapted.losses


def test_an_advisory_schema_is_strengthened_where_enforcement_is_unconditional() -> None:
    request = _request(ResponseFormatJsonSchema(schema=_STRICT_SCHEMA, strict=False))
    assert _losses(request, "gemini_generate_content") == {
        "response_format_strengthened": "an advisory schema is enforced by this provider"
    }
    assert not _losses(request, "openai_chat_completions")


def test_json_mode_has_no_anthropic_form() -> None:
    adapted = adapt_request(_request(ResponseFormatJsonObject()), "anthropic_messages")
    assert adapted.request.response_format is None
    assert [loss.code for loss in adapted.losses] == ["json_mode"]


def test_strict_tools_openai_would_reject_are_sent_unenforced() -> None:
    request = _request(tools=(_tool(_BOUNDED_SCHEMA),))
    adapted = adapt_request(request, "openai_chat_completions")
    assert adapted.request.tools[0].strict is False
    assert [loss.code for loss in adapted.losses] == ["tool_strict_unenforced"]


def test_gemini_validates_every_tool_once_one_is_strict() -> None:
    request = _request(tools=(_tool(), _tool(strict=False)))
    assert set(_losses(request, "gemini_generate_content")) == {"tool_strict_strengthened"}
    encoded = GeminiGenerateContentCodec().encode_request("g", request, streaming=False)
    assert encoded.body["toolConfig"] == {"functionCallingConfig": {"mode": "VALIDATED"}}


def test_automatic_strictness_resolves_as_openai_would_without_a_loss() -> None:
    request = _request(tools=(_tool(strict="auto"), _tool(_BOUNDED_SCHEMA, strict="auto")))
    adapted = adapt_request(request, "openai_chat_completions")
    assert [tool.strict for tool in adapted.request.tools] == [True, False]
    assert not adapted.losses
    # A Responses subject keeps it, as an omitted flag.
    responses = adapt_request(request, "openai_responses")
    assert [tool.strict for tool in responses.request.tools] == ["auto", "auto"]
    body = OpenAIResponsesCodec().encode_request("m", responses.request, streaming=False).body
    assert all("strict" not in tool for tool in cast("list[Any]", body["tools"]))


def test_an_omitted_strict_flag_means_what_its_dialect_says() -> None:
    tool = {"type": "function", "name": "submit", "parameters": _STRICT_SCHEMA}
    responses = OpenAIResponsesCodec().decode_request(
        "/v1/responses", (), json.dumps({"model": "m", "input": "x", "tools": [tool]}).encode()
    )
    assert responses.request.tools[0].strict == "auto"
    anthropic_body = {
        "model": "m",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "submit", "input_schema": _STRICT_SCHEMA}],
    }
    anthropic = AnthropicMessagesCodec().decode_request(
        "/v1/messages", (), json.dumps(anthropic_body).encode()
    )
    assert anthropic.request.tools[0].strict is False


# --- tool output schemas: declarations go only where the results match them ----------------------


def test_gemini_structured_result_reaches_responses_as_the_value_its_schema_describes() -> None:
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": "count"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "count", "args": {}}}]},
            {
                "role": "user",
                "parts": [{"functionResponse": {"name": "count", "response": {"count": 1}}}],
            },
        ],
        "tools": [{"functionDeclarations": [{"name": "count", "responseJsonSchema": schema}]}],
    }
    decoded = GeminiGenerateContentCodec().decode_request(
        _GEMINI_TARGET, (), json.dumps(body).encode()
    )
    adapted = adapt_request(decoded.request, "openai_responses")
    assert not adapted.losses
    encoded = OpenAIResponsesCodec().encode_request("m", adapted.request, streaming=False).body
    assert cast("list[Any]", encoded["tools"])[0]["output_schema"] == schema
    output = next(
        item
        for item in cast("list[Any]", encoded["input"])
        if item.get("type") == "function_call_output"
    )
    assert json.loads(output["output"]) == {"count": 1}


def _read_request(result: str, *, output_schema: JSONObject | None) -> ModelRequest:
    tool = ToolParam(
        name="read",
        description=None,
        input_schema={},
        strict=False,
        input_examples=None,
        output_schema=output_schema,
    )
    return ModelRequest(
        context=(
            UserMessage(content=(ContentText(text="read"),)),
            AssistantMessage(
                content=(ContentToolUse(id="c1", name="read", input={}, parse_error=None),)
            ),
            ToolMessage(content=(ContentText(text=result),), tool_call_id="c1", is_error=None),
        ),
        tools=(tool,),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(),
    )


def _gemini_result(request: ModelRequest) -> tuple[dict[str, Any], Any]:
    adapted = adapt_request(request, "gemini_generate_content")
    body = GeminiGenerateContentCodec().encode_request("g", adapted.request, streaming=False).body
    declaration = cast("list[Any]", body["tools"])[0]["functionDeclarations"][0]
    result = cast("list[Any]", body["contents"])[-1]["parts"][0]["functionResponse"]["response"]
    return declaration, result


def test_a_gemini_subject_gets_a_declared_result_as_the_object_it_describes() -> None:
    declaration, result = _gemini_result(
        _read_request('{"x": 1}', output_schema={"type": "object"})
    )
    assert declaration["responseJsonSchema"] == {"type": "object"}
    assert result == {"x": 1}


def test_a_result_that_is_not_the_declared_object_drops_the_schema() -> None:
    request = _read_request("done", output_schema={"type": "object"})
    assert [loss.code for loss in adapt_request(request, "gemini_generate_content").losses] == [
        "tool_result_shape"
    ]
    declaration, result = _gemini_result(request)
    assert "responseJsonSchema" not in declaration
    assert result == {"output": "done"}


def test_without_a_schema_a_result_that_looks_like_json_stays_text() -> None:
    _, result = _gemini_result(_read_request('{"x": 1}', output_schema=None))
    assert result == {"output": '{"x": 1}'}


# --- refusals: a stop reason, rendered in each harness's form ----------------------------------


def _responses_body(status: str, content: list[dict[str, Any]], **extra: Any) -> bytes:
    return json.dumps(
        {
            "id": "resp_1",
            "status": status,
            "output": [{"type": "message", "role": "assistant", "content": content}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            **extra,
        }
    ).encode()


_NO = "I can't help with that."


def _decoded_responses(body: bytes) -> ModelResponse:
    response = OpenAIResponsesCodec().decode_response(body)
    assert isinstance(response, ModelResponse)
    return response


def test_an_openai_refusal_is_text_and_a_complete_refusal() -> None:
    response = _decoded_responses(
        _responses_body("completed", [{"type": "refusal", "refusal": _NO}])
    )
    assert response.output[0].content == (ContentText(text=_NO),)
    assert response.stop_reason == StopRefusal(provider_reason="refusal", complete=True)
    # To an Anthropic harness: its refusal stop; back to Responses: a completed refusal part.
    anthropic = AnthropicMessagesCodec().encode_response("m", response)
    assert anthropic["stop_reason"] == "refusal"
    assert anthropic["content"] == [{"type": "text", "text": _NO}]
    back = OpenAIResponsesCodec().encode_response("m", response)
    assert back["status"] == "completed"
    assert cast("list[Any]", back["output"])[0]["content"] == [{"type": "refusal", "refusal": _NO}]


def test_a_content_filter_is_an_incomplete_refusal() -> None:
    response = _decoded_responses(
        _responses_body("incomplete", [], incomplete_details={"reason": "content_filter"})
    )
    assert response.stop_reason == StopRefusal(provider_reason="content_filter", complete=False)
    back = OpenAIResponsesCodec().encode_response("m", response)
    assert (back["status"], back["incomplete_details"]) == (
        "incomplete",
        {"reason": "content_filter"},
    )


def test_an_anthropic_refusal_does_not_say_whether_it_is_complete() -> None:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "m",
        "content": [],
        "stop_reason": "refusal",
        "stop_details": {"type": "refusal", "category": "cyber"},
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }
    response = AnthropicMessagesCodec().decode_response(json.dumps(body).encode())
    assert isinstance(response, ModelResponse)
    assert response.stop_reason == StopRefusal(
        provider_reason="refusal", complete=None, detail={"type": "refusal", "category": "cyber"}
    )
    # Rendered as the safer of Responses' two forms, and as Gemini's safety stop.
    responses = OpenAIResponsesCodec().encode_response("m", response)
    assert responses["incomplete_details"] == {"reason": "content_filter"}
    gemini = GeminiGenerateContentCodec().encode_response("g", response)
    assert cast("list[Any]", gemini["candidates"])[0]["finishReason"] == "SAFETY"


@pytest.mark.parametrize(
    ("finish", "stop"),
    [
        ("SAFETY", StopRefusal(provider_reason="SAFETY", complete=False)),
        ("PROHIBITED_CONTENT", StopRefusal(provider_reason="PROHIBITED_CONTENT", complete=False)),
        ("RECITATION", StopContentFilter(provider_reason="RECITATION")),
        ("SPII", StopContentFilter(provider_reason="SPII")),
    ],
)
def test_gemini_safety_stops_are_refusals_and_other_withholding_is_a_filter(
    finish: str, stop: Any
) -> None:
    body = {
        "candidates": [{"content": {"role": "model", "parts": []}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 0},
    }
    response = GeminiGenerateContentCodec().decode_response(json.dumps(body).encode())
    assert isinstance(response, ModelResponse)
    assert response.stop_reason == stop
    rendered = GeminiGenerateContentCodec().encode_response("g", response)
    assert cast("list[Any]", rendered["candidates"])[0]["finishReason"] == finish


@pytest.mark.parametrize(
    ("block_reason", "kind"), [("SAFETY", StopRefusal), ("BLOCKLIST", StopContentFilter)]
)
def test_a_blocked_gemini_prompt(block_reason: str, kind: type) -> None:
    body = {"promptFeedback": {"blockReason": block_reason}, "usageMetadata": {}}
    response = GeminiGenerateContentCodec().decode_response(json.dumps(body).encode())
    assert isinstance(response, ModelResponse)
    stop = response.stop_reason
    assert isinstance(stop, StopRefusal | StopContentFilter) and isinstance(stop, kind)
    assert stop.provider_reason == block_reason


@pytest.mark.anyio
async def test_a_streamed_refusal_to_a_responses_harness_stays_text() -> None:
    decoder = OpenAIResponsesCodec().sse_decoder()
    frame = {"type": "response.refusal.delta", "item_id": "m1", "delta": "No."}
    assert decoder.feed(f"data: {json.dumps(frame)}\n\n".encode()) == [StreamTextDelta("No.")]

    async def items() -> AsyncIterator[SubjectStreamItem]:
        yield StreamTextDelta("No.")
        yield ModelResponse(
            id="r",
            output=(AssistantMessage(content=(ContentText("No."),)),),
            stop_reason=StopRefusal(provider_reason="refusal", complete=True),
            usage=_USAGE,
        )

    frames = b"".join([chunk async for chunk in OpenAIResponsesCodec().encode_sse("m", items())])
    assert b"response.output_text.delta" in frames
    assert b'"type":"refusal"' not in frames.replace(b" ", b"")
    assert b"response.completed" in frames


def test_chat_completions_refusal_is_read() -> None:
    body = {
        "id": "c",
        "model": "m",
        "choices": [
            {
                "message": {"role": "assistant", "content": None, "refusal": "No."},
                "finish_reason": "stop",
            }
        ],
    }
    response = OpenAIChatCompletionsCodec().decode_response(json.dumps(body).encode())
    assert isinstance(response, ModelResponse)
    assert response.output[0].content == (ContentText(text="No."),)
    assert response.stop_reason == StopRefusal(provider_reason="refusal", complete=True)


def test_a_refusal_in_the_history_is_text_and_a_loss() -> None:
    body = {
        "model": "m",
        "input": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": [{"type": "refusal", "refusal": "No."}]},
            {"role": "user", "content": "please"},
        ],
    }
    decoded = OpenAIResponsesCodec().decode_request("/v1/responses", (), json.dumps(body).encode())
    assert decoded.request.context[1] == AssistantMessage(content=(ContentText(text="No."),))
    assert [loss.code for loss in decoded.decode_losses] == ["refusal"]


def _answer(*content: Any, stop: Any = None) -> ModelResponse:
    return ModelResponse(
        id="r",
        output=(AssistantMessage(content=tuple(content)),),
        stop_reason=stop or StopEndTurn(),
        usage=_USAGE,
    )


def _stopped(stop: Any) -> ModelResponse:
    return _answer(ContentText("No."), stop=stop)


# What each harness dialect is sent for each stop reason, with the response losses and whether
# the call is answered as an error. The wire column is read off the encoded response: Anthropic's
# `stop_reason`, Responses' status (with its incomplete reason, and `+refusal` for a refusal
# part), Gemini's `finishReason`; `error` when the harness is sent a failure instead.
_REFUSED = StopRefusal("refusal", complete=True)
_CUT_OFF = StopRefusal("content_filter", complete=False)
_UNDECIDED = StopRefusal("refusal", complete=None)
_RECITATION = StopContentFilter("RECITATION")
_MALFORMED = StopOther("MALFORMED_FUNCTION_CALL")
_PAUSED = StopOther("pause_turn")
_INCOMPLETE = StopOther("incomplete: something_new")
_STOP_RENDERINGS: list[tuple[Dialect, Any, str, list[str]]] = [
    ("anthropic_messages", StopEndTurn(), "end_turn", []),
    ("anthropic_messages", StopMaxTokens(), "max_tokens", []),
    ("anthropic_messages", StopStopSequence("##"), "stop_sequence", []),
    ("anthropic_messages", StopToolUse(), "tool_use", []),
    ("anthropic_messages", _REFUSED, "refusal", ["refusal_completeness"]),
    ("anthropic_messages", _CUT_OFF, "refusal", ["refusal_completeness"]),
    ("anthropic_messages", _UNDECIDED, "refusal", []),
    ("anthropic_messages", _RECITATION, "refusal", ["content_filter_as_refusal"]),
    ("anthropic_messages", _MALFORMED, "error", ["stop_reason"]),
    ("anthropic_messages", _PAUSED, "pause_turn", []),
    ("anthropic_messages", _INCOMPLETE, "error", ["stop_reason"]),
    ("openai_responses", StopEndTurn(), "completed", []),
    ("openai_responses", StopMaxTokens(), "incomplete:max_output_tokens", []),
    ("openai_responses", StopStopSequence("##"), "completed", []),
    ("openai_responses", StopToolUse(), "completed", []),
    ("openai_responses", _REFUSED, "completed+refusal", []),
    ("openai_responses", _CUT_OFF, "incomplete:content_filter", []),
    ("openai_responses", _UNDECIDED, "incomplete:content_filter", ["refusal_completeness"]),
    ("openai_responses", _RECITATION, "incomplete:content_filter", []),
    ("openai_responses", _MALFORMED, "error", ["stop_reason"]),
    ("openai_responses", _PAUSED, "error", ["stop_reason"]),
    ("openai_responses", _INCOMPLETE, "incomplete:something_new", []),
    ("gemini_generate_content", StopEndTurn(), "STOP", []),
    ("gemini_generate_content", StopMaxTokens(), "MAX_TOKENS", []),
    ("gemini_generate_content", StopStopSequence("##"), "STOP", []),
    ("gemini_generate_content", StopToolUse(), "STOP", []),
    ("gemini_generate_content", _REFUSED, "SAFETY", ["refusal_as_safety_stop"]),
    ("gemini_generate_content", StopRefusal("SAFETY", complete=False), "SAFETY", []),
    ("gemini_generate_content", _CUT_OFF, "SAFETY", []),
    ("gemini_generate_content", _UNDECIDED, "SAFETY", ["refusal_completeness"]),
    ("gemini_generate_content", _RECITATION, "RECITATION", []),
    ("gemini_generate_content", _MALFORMED, "MALFORMED_FUNCTION_CALL", []),
    ("gemini_generate_content", _PAUSED, "OTHER", ["stop_reason"]),
    ("gemini_generate_content", _INCOMPLETE, "OTHER", ["stop_reason"]),
]


def _streamed_expectation(dialect: Dialect, stop: Any, wire: str, codes: list[str]) -> Any:
    # A streamed part's type is fixed at its first delta, before the stop reason exists: a
    # Responses refusal part stays output_text.
    if dialect == "openai_responses" and wire == "completed+refusal":
        return "completed", ["refusal_as_text"]
    return wire, codes


def _sse_payloads(frames: bytes) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in frames.decode().splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            payloads.append(json.loads(line.removeprefix("data: ")))
    return payloads


def _unary_wire(dialect: Dialect, body: dict[str, Any]) -> str:
    if dialect == "anthropic_messages":
        return body["stop_reason"]
    if dialect == "gemini_generate_content":
        return body["candidates"][0]["finishReason"]
    wire = body["status"]
    if wire == "incomplete":
        wire += ":" + body["incomplete_details"]["reason"]
    parts = [part for item in body["output"] for part in item.get("content", [])]
    if any(part["type"] == "refusal" for part in parts):
        wire += "+refusal"
    return wire


def _streamed_wire(dialect: Dialect, frames: bytes) -> str:
    payloads = _sse_payloads(frames)
    if dialect == "anthropic_messages":
        (delta,) = [p for p in payloads if p.get("type") == "message_delta"]
        return delta["delta"]["stop_reason"]
    if dialect == "gemini_generate_content":
        return payloads[-1]["candidates"][0]["finishReason"]
    last = payloads[-1]
    if last["type"] == "response.incomplete":
        return "incomplete:" + last["response"]["incomplete_details"]["reason"]
    return last["response"]["status"]


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True], ids=["unary", "streamed"])
@pytest.mark.parametrize(
    ("dialect", "stop", "wire", "codes"),
    _STOP_RENDERINGS,
    ids=[f"{row[0]}-{row[1].type}-{row[2]}" for row in _STOP_RENDERINGS],
)
async def test_what_a_harness_is_sent_for_each_stop_reason(
    dialect: Dialect, stop: Any, wire: str, codes: list[str], streaming: bool
) -> None:
    if streaming:
        wire, codes = _streamed_expectation(dialect, stop, wire, codes)
    codec = cast("Any", _CODECS[dialect])
    rendering = codec.render_stop(stop, streaming=streaming)
    assert [loss.code for loss in rendering.losses] == codes
    assert rendering.as_error == (wire == "error")
    if rendering.as_error:
        return  # the call is answered as a failure; the encoders never see the response
    response = _stopped(stop)
    if not streaming:
        assert _unary_wire(dialect, codec.encode_response("m", response)) == wire
        return

    async def items() -> AsyncIterator[SubjectStreamItem]:
        yield StreamTextDelta("No.")
        yield response

    frames = b"".join([chunk async for chunk in codec.encode_sse("m", items())])
    assert _streamed_wire(dialect, frames) == wire


# --- callers check how a response ended (BEH-319) ------------------------------------------------


def test_an_empty_refusal_raises_naming_what_the_provider_said() -> None:
    # Anthropic's classifier blocking a judge call: no text, only the stop reason says why.
    refused = ModelResponse(
        id="r",
        output=(AssistantMessage(content=()),),
        stop_reason=StopRefusal(
            provider_reason="refusal", complete=None, detail={"category": "cyber"}
        ),
        usage=_USAGE,
    )
    with pytest.raises(UnexpectedStopError, match="refused.*cyber") as caught:
        refused.raise_for_stop_reason()
    assert caught.value.stop_reason == refused.stop_reason


def test_raise_for_stop_reason_accepts_a_finished_answer_and_what_the_caller_allows() -> None:
    _answer(ContentText("yes")).raise_for_stop_reason()
    called = _answer(ContentText(""), stop=StopToolUse())
    with pytest.raises(UnexpectedStopError):
        called.raise_for_stop_reason()
    called.raise_for_stop_reason(allow=(StopEndTurn, StopToolUse))
    with pytest.raises(UnexpectedStopError, match="token limit"):
        _answer(ContentText('{"verd'), stop=StopMaxTokens()).raise_for_stop_reason()


# --- Gemini's OpenAPI schemas as JSON Schema ------------------------------------------------------


def test_an_openapi_schema_becomes_json_schema() -> None:
    openapi = {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING", "nullable": True, "example": "Ada", "maxLength": "40"},
            "kind": {"type": "STRING", "enum": ["a", "b"], "nullable": True},
            "tags": {"type": "ARRAY", "items": {"type": "STRING"}, "minItems": "1"},
        },
        "required": ["name"],
        "propertyOrdering": ["name", "kind", "tags"],
    }
    assert openapi_schema_to_json_schema(openapi) == {
        "type": "object",
        "properties": {
            "name": {"type": ["string", "null"], "examples": ["Ada"], "maxLength": 40},
            "kind": {"type": ["string", "null"], "enum": ["a", "b", None]},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["name"],
        "propertyOrdering": ["name", "kind", "tags"],
    }


def test_an_openapi_field_with_no_json_schema_form_is_a_loss() -> None:
    body = {
        "contents": [{"role": "user", "parts": [{"text": "x"}]}],
        "tools": [
            {
                "functionDeclarations": [
                    {"name": "f", "parameters": {"type": "OBJECT", "discriminator": "kind"}}
                ]
            }
        ],
    }
    decoded = GeminiGenerateContentCodec().decode_request(
        _GEMINI_TARGET, (), json.dumps(body).encode()
    )
    assert decoded.request.tools[0].input_schema == {"type": "object"}
    assert [loss.code for loss in decoded.decode_losses] == ["schema_keyword"]


def test_automatic_strictness_the_target_cannot_enforce_is_a_recorded_weakening() -> None:
    # OpenAI enforces a numeric minimum in strict mode, so the omitted flag means strict;
    # Anthropic cannot enforce it, so the tool reaches it non-strict, with a loss that `refuse`
    # rejects.
    schema = _closed_object(n={"type": "number", "minimum": 0})
    adapted = adapt_request(_request(tools=(_tool(schema, strict="auto"),)), "anthropic_messages")
    assert adapted.request.tools[0].strict is False
    assert [loss.code for loss in adapted.losses] == ["tool_strict_unenforced"]
    assert AdaptationPolicy(semantic_changes="refuse").refuses(adapted.losses) is not None


def _closed_object(**properties: Any) -> JSONObject:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }
    return schema


def _namespaced_request(result: str, called: str = "b") -> ModelRequest:
    def tool(namespace: str, schema: JSONObject | None) -> ToolParam:
        return ToolParam(
            name="run",
            description=None,
            input_schema={},
            strict=False,
            input_examples=None,
            namespace=namespace,
            output_schema=schema,
        )

    return ModelRequest(
        context=(
            UserMessage(content=(ContentText(text="go"),)),
            AssistantMessage(
                content=(
                    ContentToolUse(
                        id="c1", name="run", input={}, parse_error=None, namespace=called
                    ),
                )
            ),
            ToolMessage(content=(ContentText(text=result),), tool_call_id="c1", is_error=None),
        ),
        tools=(tool("a", {"type": "object"}), tool("b", None)),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(),
    )


def test_gemini_output_schemas_belong_to_the_namespaced_tool() -> None:
    # Only a.run declares a schema; b.run's results are b.run's.
    plain = adapt_request(_namespaced_request("done"), "gemini_generate_content")
    # a.run's schema is not dropped for b.run's text (the names are flattened, a loss of its own).
    assert "tool_result_shape" not in {loss.code for loss in plain.losses}
    looks_like_json = _namespaced_request('{"x": 1}')
    body = GeminiGenerateContentCodec().encode_request("g", looks_like_json, streaming=False).body
    result = cast("list[Any]", body["contents"])[-1]["parts"][0]["functionResponse"]["response"]
    assert result == {"output": '{"x": 1}'}


def test_a_default_draft_declaration_is_not_a_loss() -> None:
    schema = _closed_object(s={"type": "string"})
    declared = {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
    request = _request(ResponseFormatJsonSchema(schema=declared, strict=True))
    adapted = adapt_request(request, "anthropic_messages")
    assert not adapted.losses
    fmt = cast("ResponseFormatJsonSchema", adapted.request.response_format)
    assert "$schema" not in fmt.schema
    other = {"$schema": "http://json-schema.org/draft-07/schema#", **schema}
    losses = _losses(
        _request(ResponseFormatJsonSchema(schema=other, strict=True)), "anthropic_messages"
    )
    assert set(losses) == {"response_schema_weakened"}


def test_a_gemini_structured_result_with_nan_is_not_json() -> None:
    # a.run declares an output schema; a NaN result is not the JSON object it describes, so the
    # schema is dropped with a loss and the text goes as text.
    request = _namespaced_request('{"x": NaN}', called="a")
    adapted = adapt_request(request, "gemini_generate_content")
    assert "tool_result_shape" in {loss.code for loss in adapted.losses}
    body = GeminiGenerateContentCodec().encode_request("g", adapted.request, streaming=False).body
    result = cast("list[Any]", body["contents"])[-1]["parts"][0]["functionResponse"]["response"]
    assert result == {"output": '{"x": NaN}'}


def test_a_dropped_anthropic_format_is_not_also_recorded_as_enforced() -> None:
    advisory = ResponseFormatJsonSchema(schema=_closed_object(note={}), strict=False)
    losses = _losses(_request(advisory), "anthropic_messages")
    assert set(losses) == {"response_format_dropped"}
