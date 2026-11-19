"""No unreported change in meaning: the cross-dialect counterpart of the reconstruction check.

For every path of each harness dialect's request schema whose witness carries a contract (a
response format, a strict tool, a tool output schema), and for every other dialect: decode the
witness, adapt it to that dialect (its codec's `adapt_request`), encode it, and read the
contracts back from what was encoded. Losses are checked in two ways:

- **Wire-visible** ones through the read-back, field by field: a field that changed must have
  one of its own losses recorded, and a wire-visible loss recorded for a field must show as a
  change in it. A tool's `"auto"` strictness is expected to arrive resolved as OpenAI would
  resolve it, which is no loss; a target that then cannot enforce it records a loss.
- **Enforcement-only** ones (the schema goes out as given; the provider does not enforce part of
  it), which a read-back cannot see, against cases written from the providers' documentation
  (`test_enforcement_losses`).

Compared are the contracts' meanings: a response format's kind, schema and strictness (its
`name` is an identifier and its `description` is covered by `response_format_description`);
each tool's strictness and output schema, by position; plain text and no response format are
the same.
"""

import functools
import json
from dataclasses import dataclass
from typing import Any, Literal, cast

import pytest

from behaviors.implementations.dialects._openai_common import response_format_from_flat
from behaviors.implementations.dialects.anthropic_messages import AnthropicMessagesCodec
from behaviors.implementations.dialects.gemini_generate_content import GeminiGenerateContentCodec
from behaviors.implementations.dialects.openai_chat_completions import OpenAIChatCompletionsCodec
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesCodec
from behaviors.implementations.dialects.schema_support import openai_strict_violations
from behaviors.interfaces.dialect_codecs import DecodedRequest, ModelSideCodec
from behaviors.types.content import ContentText
from behaviors.types.dialect import ALL_DIALECTS, Dialect
from behaviors.types.json import JSONObject
from behaviors.types.message import UserMessage
from behaviors.types.response import (
    ModelRequest,
    ResponseFormat,
    ResponseFormatJsonObject,
    ResponseFormatJsonSchema,
    ResponseFormatText,
    SamplingParams,
)
from behaviors.types.tool import (
    ToolChoice,
    ToolChoiceAuto,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolChoiceTool,
    ToolParam,
)

from .audit import build_witness, schema_paths
from .dialects import AUDITS

_ENCODERS: dict[Dialect, ModelSideCodec] = {
    "anthropic_messages": AnthropicMessagesCodec(),
    "openai_responses": OpenAIResponsesCodec(),
    "gemini_generate_content": GeminiGenerateContentCodec(),
    "openai_chat_completions": OpenAIChatCompletionsCodec(),
}
_TARGETS: dict[Dialect, str] = {
    "anthropic_messages": "/v1/messages",
    "openai_responses": "/v1/responses",
    "gemini_generate_content": "/v1beta/models/probe:generateContent",
}

# Which losses may explain a difference in each part of the contract.
_FORMAT_LOSSES = frozenset(
    {
        "response_format_unenforced",
        "response_format_dropped",
        "response_format_strengthened",
        "response_schema_weakened",
        "response_schema_strengthened",
        "json_mode",
    }
)
_STRICT_LOSSES = frozenset({"tool_strict_unenforced", "tool_strict_strengthened"})
_OUTPUT_SCHEMA_LOSSES = frozenset({"tool_output_schema", "tool_result_shape"})
# Losses that are not compared here: a description is not part of a contract's meaning.
_UNCOMPARED = frozenset({"response_format_description"})
Strictness = Literal[True, False, "auto"]


def enforcement_only(target: Dialect) -> frozenset[str]:
    """The losses that leave the wire unchanged at `target`: the schema goes out as given and
    the provider does not enforce part of it. They are checked against
    `test_enforcement_losses`'s cases rather than through the read-back."""
    codes = {"tool_schema_weakened"}
    if target == "gemini_generate_content":
        codes.add("response_schema_weakened")
    return frozenset(codes)


@dataclass(frozen=True)
class Contracts:
    response_format: tuple[str, JSONObject | None, bool] | None
    strict: tuple[Strictness, ...]
    output_schemas: tuple[JSONObject | None, ...]
    input_schemas: tuple[JSONObject, ...]
    tool_choice: ToolChoice


def _format_meaning(response_format: ResponseFormat | None) -> tuple[str, Any, bool] | None:
    if response_format is None or isinstance(response_format, ResponseFormatText):
        return None
    if isinstance(response_format, ResponseFormatJsonObject):
        return ("json_object", None, False)
    return ("json_schema", response_format.schema, response_format.strict)


def contracts(request: ModelRequest) -> Contracts:
    return Contracts(
        response_format=_format_meaning(request.response_format),
        strict=tuple(tool.strict for tool in request.tools),
        output_schemas=tuple(tool.output_schema for tool in request.tools),
        input_schemas=tuple(tool.input_schema for tool in request.tools),
        tool_choice=request.tool_choice or ToolChoiceAuto(),
    )


def _chat_completions_contracts(body: JSONObject) -> Contracts:
    """The contracts a chat completions body states (the dialect has no harness side)."""
    fmt = cast("dict[str, Any] | None", body.get("response_format"))
    response_format = None
    if fmt is not None:
        flat = {"type": fmt["type"], **cast("dict[str, Any]", fmt.get("json_schema") or {})}
        response_format = response_format_from_flat(flat)
    tools = cast("list[dict[str, Any]]", body.get("tools") or [])
    functions = [cast("dict[str, Any]", tool["function"]) for tool in tools]
    return Contracts(
        response_format=_format_meaning(response_format),
        strict=tuple(function.get("strict") is True for function in functions),
        output_schemas=tuple(None for _ in functions),
        input_schemas=tuple(cast("JSONObject", function["parameters"]) for function in functions),
        tool_choice=_chat_tool_choice(body.get("tool_choice")),
    )


def _chat_tool_choice(choice: Any) -> ToolChoice:
    if choice == "required":
        return ToolChoiceRequired()
    if choice == "none":
        return ToolChoiceNone()
    if isinstance(choice, dict):
        return ToolChoiceTool(name=cast("str", cast("dict[str, Any]", choice)["function"]["name"]))
    return ToolChoiceAuto()


def read_back(dialect: Dialect, body: JSONObject) -> Contracts:
    if dialect == "openai_chat_completions":
        return _chat_completions_contracts(body)
    codec = cast("Any", _ENCODERS[dialect])
    decoded: DecodedRequest = codec.decode_request(_TARGETS[dialect], (), json.dumps(body).encode())
    return contracts(decoded.request)


def expected_strict(asked: Contracts, target: Dialect) -> tuple[Strictness, ...]:
    """The strictness the target should receive with no loss: an automatic flag kept by a target
    that has it (Responses), else resolved as OpenAI would (strict when its strict rules accept
    the schema); a target that cannot enforce the result must record that."""
    if target == "openai_responses":
        return asked.strict
    return tuple(
        (not openai_strict_violations(schema)) if strict == "auto" else strict
        for strict, schema in zip(asked.strict, asked.input_schemas, strict=True)
    )


def unexplained(asked: Contracts, got: Contracts, losses: set[str], target: Dialect) -> list[str]:
    """Each field that changed with none of its own losses recorded, and each wire-visible loss
    whose field did not change."""
    problems: list[str] = []
    strict = expected_strict(asked, target)
    changed = {
        "response format": asked.response_format != got.response_format,
        "tool strictness": strict != got.strict,
        "output schemas": asked.output_schemas != got.output_schemas,
        "tool choice": asked.tool_choice != got.tool_choice,
    }
    codes = {
        "response format": _FORMAT_LOSSES,
        "tool strictness": _STRICT_LOSSES,
        "output schemas": _OUTPUT_SCHEMA_LOSSES,
        "tool choice": frozenset[str](),  # no contract step changes it
    }
    before = {
        "response format": (asked.response_format, got.response_format),
        "tool strictness": (strict, got.strict),
        "output schemas": (asked.output_schemas, got.output_schemas),
        "tool choice": (asked.tool_choice, got.tool_choice),
    }
    visible = losses - enforcement_only(target) - _UNCOMPARED
    for field_name, field_codes in codes.items():
        recorded = visible & field_codes
        if changed[field_name] and not (losses & field_codes):
            was, became = before[field_name]
            problems.append(f"{field_name} {was} became {became}, unrecorded")
        if recorded and not changed[field_name]:
            problems.append(f"{sorted(recorded)} recorded, but the {field_name} did not change")
    return problems


def _contract_request(
    *, response_schema: JSONObject | None = None, tool_schema: JSONObject | None = None
) -> ModelRequest:
    tools: tuple[ToolParam, ...] = ()
    if tool_schema is not None:
        tools = (
            ToolParam(
                name="t",
                description=None,
                input_schema=tool_schema,
                strict=True,
                input_examples=None,
            ),
        )
    return ModelRequest(
        context=(UserMessage(content=(ContentText(text="x"),)),),
        tools=tools,
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(),
        response_format=(
            ResponseFormatJsonSchema(schema=response_schema, strict=True)
            if response_schema is not None
            else None
        ),
    )


@functools.cache
def _witnessed(harness: Dialect) -> tuple[tuple[str, ModelRequest], ...]:
    """The decoded witnesses of the harness dialect's schema paths that carry a contract."""
    spec = AUDITS[harness]()
    out: list[tuple[str, ModelRequest]] = []
    for path in schema_paths(spec.schema):
        if path.unwalkable is not None:
            continue
        try:
            witness = build_witness(spec.schema, path)
        except Exception:  # noqa: BLE001 - a path the audit reports as unprobed
            continue
        for body in (witness.body, witness.alternate):
            if body is None:
                continue
            headers: Any = ()
            if spec.adjust is not None:
                headers, _ = spec.adjust(body, witness.pointer)
            try:
                decoded = spec.codec.decode_request(spec.target, headers, json.dumps(body).encode())
            except Exception:  # noqa: BLE001 - not a request the codec reads; the audit says so
                continue
            request = decoded.request
            if request.response_format is not None or any(
                tool.strict or tool.output_schema is not None for tool in request.tools
            ):
                out.append((path.label, request))
    return tuple(out)


@pytest.mark.parametrize("harness", sorted(AUDITS))
def test_every_change_in_a_contract_is_recorded(harness: Dialect) -> None:
    witnessed = _witnessed(harness)
    assert witnessed, f"no {harness} witness carries a contract"
    problems: list[str] = []
    for label, request in witnessed:
        asked = contracts(request)
        for target in ALL_DIALECTS:
            if target == harness:
                continue
            adapted = _ENCODERS[target].adapt_request("probe", request)
            body = _ENCODERS[target].encode_request("probe", adapted.request, streaming=False).body
            losses = {loss.code for loss in adapted.losses}
            for problem in unexplained(asked, read_back(target, body), losses, target):
                problems.append(f"{label} -> {target}: {problem}")
    assert not problems, f"{len(problems)} unreported change(s):\n" + "\n".join(problems[:20])


_ENFORCEMENT_CASES: list[tuple[str, Dialect, ModelRequest, dict[str, str]]] = [
    # Gemini structured output strips these keywords and does not enforce them
    # (https://ai.google.dev/gemini-api/docs/structured-output).
    (
        "gemini ignores minLength",
        "gemini_generate_content",
        _contract_request(
            response_schema={
                "type": "object",
                "properties": {"s": {"type": "string", "minLength": 1}},
            }
        ),
        {"response_schema_weakened": "not enforced: minLength"},
    ),
    (
        "gemini ignores uniqueItems in a validated tool",
        "gemini_generate_content",
        _contract_request(
            tool_schema={
                "type": "object",
                "properties": {"a": {"type": "array", "uniqueItems": True}},
            }
        ),
        {"tool_schema_weakened": "tool t: not enforced: uniqueItems"},
    ),
    (
        "gemini enforces a plain schema whole",
        "gemini_generate_content",
        _contract_request(
            response_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
            tool_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        ),
        {},
    ),
]


@pytest.mark.parametrize(
    ("target", "request_", "expected"),
    [case[1:] for case in _ENFORCEMENT_CASES],
    ids=[case[0] for case in _ENFORCEMENT_CASES],
)
def test_enforcement_losses(
    target: Dialect, request_: ModelRequest, expected: dict[str, str]
) -> None:
    adapted = _ENCODERS[target].adapt_request("probe", request_)
    assert {loss.code: loss.detail for loss in adapted.losses} == expected
    assert adapted.request == request_  # sent as given
