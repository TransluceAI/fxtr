"""What each contract-bearing wire request means, written from the providers' references rather
than from the decoders, and checked against what each decoder reads.

The cross-dialect invariant (`test_contract_invariant`) starts from a decoded request, so it
cannot see a decoder that misreads the wire; these fixtures can. Each states the request's
contracts in vocabulary terms (response format, each tool's strictness and output schema, the
tool choice) and the decode losses expected, by code. Sources:

- Anthropic: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- OpenAI Responses: https://developers.openai.com/api/docs/guides/structured-outputs and, for
  an omitted function `strict`, https://developers.openai.com/api/docs/guides/migrate-to-responses
  ("In Responses, omitting `strict` attempts strict mode"; Chat Completions functions are
  non-strict by default)
- Gemini: https://ai.google.dev/api/generate-content (`FunctionCallingConfig`, `Schema`)
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest

from behaviors.implementations.dialects.anthropic_messages import AnthropicMessagesCodec
from behaviors.implementations.dialects.gemini_generate_content import GeminiGenerateContentCodec
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesCodec
from behaviors.interfaces.dialect_codecs import HarnessSideCodec
from behaviors.types.dialect import Dialect
from behaviors.types.json import JSONObject
from behaviors.types.response import (
    ResponseFormat,
    ResponseFormatJsonObject,
    ResponseFormatJsonSchema,
)
from behaviors.types.tool import ToolChoice, ToolChoiceAuto, ToolChoiceRequired

_CODECS: dict[Dialect, tuple[HarnessSideCodec, str]] = {
    "anthropic_messages": (AnthropicMessagesCodec(), "/v1/messages"),
    "openai_responses": (OpenAIResponsesCodec(), "/v1/responses"),
    "gemini_generate_content": (
        GeminiGenerateContentCodec(),
        "/v1beta/models/gemini-probe:generateContent",
    ),
}

_OBJECT: JSONObject = {"type": "object", "properties": {}, "additionalProperties": False}


@dataclass(frozen=True)
class Meaning:
    response_format: ResponseFormat | None = None
    strict: tuple[Literal[True, False, "auto"], ...] = ()
    output_schemas: tuple[JSONObject | None, ...] | None = None
    tool_choice: ToolChoice | None = None
    losses: frozenset[str] = field(default_factory=frozenset[str])


def _anthropic(**extra: Any) -> dict[str, Any]:
    return {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "x"}], **extra}


def _responses(**extra: Any) -> dict[str, Any]:
    return {"model": "m", "input": "x", **extra}


def _gemini(**extra: Any) -> dict[str, Any]:
    return {"contents": [{"role": "user", "parts": [{"text": "x"}]}], **extra}


def _fn(**extra: Any) -> dict[str, Any]:
    return {"type": "function", "name": "f", "parameters": _OBJECT, **extra}


_CASES: list[tuple[str, Dialect, dict[str, Any], Meaning]] = [
    # --- Anthropic: a format is always enforced; a tool is strict only when it says so.
    (
        "anthropic output format",
        "anthropic_messages",
        _anthropic(output_config={"format": {"type": "json_schema", "schema": _OBJECT}}),
        Meaning(response_format=ResponseFormatJsonSchema(schema=_OBJECT, strict=True)),
    ),
    (
        "anthropic tool without strict",
        "anthropic_messages",
        _anthropic(tools=[{"name": "f", "input_schema": _OBJECT}]),
        Meaning(strict=(False,)),
    ),
    (
        "anthropic strict tool",
        "anthropic_messages",
        _anthropic(tools=[{"name": "f", "input_schema": _OBJECT, "strict": True}]),
        Meaning(strict=(True,)),
    ),
    # --- Responses: an omitted function `strict` is automatic, whichever branch the API takes.
    (
        "responses function without strict, compatible schema",
        "openai_responses",
        _responses(tools=[_fn()]),
        Meaning(strict=("auto",)),
    ),
    (
        "responses function without strict, incompatible schema",
        "openai_responses",
        _responses(tools=[_fn(parameters={"type": "object", "properties": {"a": {}}})]),
        Meaning(strict=("auto",)),
    ),
    (
        "responses function strict false",
        "openai_responses",
        _responses(tools=[_fn(strict=False)]),
        Meaning(strict=(False,)),
    ),
    (
        "responses function with an output schema",
        "openai_responses",
        _responses(tools=[_fn(strict=True, output_schema=_OBJECT)]),
        Meaning(strict=(True,), output_schemas=(_OBJECT,)),
    ),
    (
        "responses json_schema format without strict is advisory",
        "openai_responses",
        _responses(text={"format": {"type": "json_schema", "name": "n", "schema": _OBJECT}}),
        Meaning(response_format=ResponseFormatJsonSchema(schema=_OBJECT, strict=False, name="n")),
    ),
    (
        "responses json mode",
        "openai_responses",
        _responses(text={"format": {"type": "json_object"}}),
        Meaning(response_format=ResponseFormatJsonObject()),
    ),
    (
        "responses refusal in the history",
        "openai_responses",
        _responses(
            input=[
                {"role": "user", "content": "x"},
                {"role": "assistant", "content": [{"type": "refusal", "refusal": "No."}]},
            ]
        ),
        Meaning(losses=frozenset({"refusal"})),
    ),
    # --- Gemini: VALIDATED makes every call validated; ANY forces a call; the OpenAPI schema
    # is JSON Schema.
    (
        "gemini validated",
        "gemini_generate_content",
        _gemini(
            tools=[{"functionDeclarations": [{"name": "f", "parametersJsonSchema": _OBJECT}]}],
            toolConfig={"functionCallingConfig": {"mode": "VALIDATED"}},
        ),
        Meaning(strict=(True,), tool_choice=ToolChoiceAuto()),
    ),
    (
        "gemini any",
        "gemini_generate_content",
        _gemini(
            tools=[{"functionDeclarations": [{"name": "f", "parametersJsonSchema": _OBJECT}]}],
            toolConfig={"functionCallingConfig": {"mode": "ANY"}},
        ),
        Meaning(strict=(False,), tool_choice=ToolChoiceRequired()),
    ),
    (
        "gemini OpenAPI response schema with nullable",
        "gemini_generate_content",
        _gemini(
            generationConfig={
                "responseMimeType": "application/json",
                "responseSchema": {"type": "STRING", "nullable": True},
            }
        ),
        Meaning(
            response_format=ResponseFormatJsonSchema(
                schema={"type": ["string", "null"]}, strict=True
            )
        ),
    ),
    (
        "gemini json mode",
        "gemini_generate_content",
        _gemini(generationConfig={"responseMimeType": "application/json"}),
        Meaning(response_format=ResponseFormatJsonObject()),
    ),
]


@pytest.mark.parametrize(
    ("dialect", "body", "meaning"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
def test_a_decoder_reads_what_the_wire_means(
    dialect: Dialect, body: dict[str, Any], meaning: Meaning
) -> None:
    codec, target = _CODECS[dialect]
    decoded = codec.decode_request(target, (), json.dumps(body).encode())
    request = decoded.request
    assert request.response_format == meaning.response_format
    assert tuple(tool.strict for tool in request.tools) == meaning.strict
    if meaning.output_schemas is not None:
        assert tuple(tool.output_schema for tool in request.tools) == meaning.output_schemas
    if meaning.tool_choice is not None:
        assert request.tool_choice == meaning.tool_choice
    assert {loss.code for loss in decoded.decode_losses} == meaning.losses
