"""Tool types."""

from dataclasses import dataclass
from typing import Literal

from behaviors.types.cache_control import CacheControl
from behaviors.types.json import JSONObject
from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class ToolParam(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("ToolParam.v2"),
):
    """Specification of a tool exposed to the model.

    Corresponds to an entry in the `tools` array: `{name, description,
    input_schema}` in the Anthropic Messages API
    (https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview),
    `tools[].function` (`{name, description, parameters}`) in the OpenAI Chat
    Completions API (https://platform.openai.com/docs/guides/function-calling),
    and `tools[].functionDeclarations[]` (`{name, description, parameters}`) in
    the Gemini API (https://ai.google.dev/gemini-api/docs/function-calling).

    Attributes:
        name: The name of the tool.
        description: A description of what the tool does.
        input_schema: JSON Schema describing the tool's input (Anthropic
            `input_schema` / OpenAI `parameters` / Gemini `parameters`): draft 2020-12, the
            providers' format, unless its `$schema` names another draft. Codecs pass it
            through as given (Gemini's OpenAPI `Schema` is converted on decode).
        strict: Whether the provider is to constrain the model's calls to
            `input_schema` (Anthropic and OpenAI `strict`, Gemini `VALIDATED`): True or
            False, or `"auto"` for strict when the schema can be made compatible with the
            provider's strict mode and non-strict otherwise, which is what an OpenAI
            Responses tool without the flag means. Decoders resolve an omitted flag to its
            dialect's meaning (False on Anthropic, `"auto"` on Responses); adapting a request
            to a dialect without `"auto"` resolves it as OpenAI would (strict when OpenAI's
            strict rules accept the schema), and records it if that dialect cannot enforce it.
        input_examples: Example inputs conforming to `input_schema` (Anthropic
            `input_examples`), or None if the tool definition did not include
            any.
        namespace: The group the tool was declared in (the name of an OpenAI
            Responses `namespace` tool), or None for a tool declared on its
            own. A call to a grouped tool names the group as well
            (`ContentToolUse.namespace`). Dialects without namespaces render
            the tool under its own name, prefixed with the group only when two
            groups declare the same name.
        namespace_description: The description of the group named by
            `namespace` (OpenAI Responses requires one on every `namespace`
            tool), carried on each member because the vocabulary has no
            group record; members of one group must agree on it. None when
            the tool is not grouped or the group had no description.
        freeform_format: For a tool whose input is one free-text string rather
            than an object (an OpenAI Responses `custom` tool): the input's
            format, verbatim, `{"type": "text"}` or a grammar
            (`{"type": "grammar", "syntax": "lark" | "regex", "definition":
            ...}`). `input_schema` is then the object with the single string
            field `input`, which is how such a call's input is carried in
            `ContentToolUse.input`, and how dialects without freeform tools
            offer it. None for an ordinary tool, which is what makes an
            ordinary tool with a single `input` field an ordinary tool.
        output_schema: JSON Schema (as for `input_schema`) describing what the tool
            returns (Gemini `responseJsonSchema` / `response`, OpenAI Responses
            `output_schema`), or None if the declaration has none. The tool's results are
            text; for a tool with an output schema, that text is the JSON value the schema
            describes.
    """

    name: str
    description: str | None
    input_schema: JSONObject
    strict: Literal[True, False, "auto"]
    input_examples: tuple[JSONObject, ...] | None
    namespace: str | None = None
    namespace_description: str | None = None
    freeform_format: JSONObject | None = None
    output_schema: JSONObject | None = None


@dataclass(frozen=True)
class CacheableToolParam(
    ToolParam,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("CacheableToolParam.v2"),
):
    """A tool definition that may carry a prompt-cache breakpoint.

    Attributes:
        cache_control: The breakpoint, or None for a plain tool definition.
    """

    cache_control: CacheControl | None = None


ToolDefinition = ToolParam | CacheableToolParam


@dataclass(frozen=True)
class ToolChoiceAuto(TaggedDataclass, fxtr_type=behaviors_type("ToolChoiceAuto.v1")):
    """The model decides whether to call a tool or respond in text.

    This is every provider's default. Corresponds to Anthropic `tool_choice:
    {"type": "auto"}`
    (https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview),
    OpenAI `tool_choice: "auto"`
    (https://platform.openai.com/docs/guides/function-calling), and Gemini
    `toolConfig.functionCallingConfig.mode: "AUTO"`
    (https://ai.google.dev/gemini-api/docs/function-calling).
    """

    type: Literal["auto"] = "auto"


@dataclass(frozen=True)
class ToolChoiceRequired(TaggedDataclass, fxtr_type=behaviors_type("ToolChoiceRequired.v1")):
    """The model must call at least one tool; a text-only response is not allowed.

    Corresponds to Anthropic `tool_choice: {"type": "any"}`, OpenAI
    `tool_choice: "required"`, and Gemini
    `toolConfig.functionCallingConfig.mode: "ANY"`.
    """

    type: Literal["required"] = "required"


@dataclass(frozen=True)
class ToolChoiceTool(TaggedDataclass, fxtr_type=behaviors_type("ToolChoiceTool.v1")):
    """The model must call the named tool.

    Corresponds to Anthropic `tool_choice: {"type": "tool", "name": ...}`,
    OpenAI `tool_choice: {"type": "function", "function": {"name": ...}}`, and
    Gemini `toolConfig.functionCallingConfig.mode: "ANY"` with
    `allowedFunctionNames: [name]`.

    Attributes:
        name: The name of the tool the model must call.
    """

    name: str
    type: Literal["tool"] = "tool"


@dataclass(frozen=True)
class ToolChoiceNone(TaggedDataclass, fxtr_type=behaviors_type("ToolChoiceNone.v1")):
    """The model may not call any tool and must respond in text.

    Corresponds to Anthropic `tool_choice: {"type": "none"}`, OpenAI
    `tool_choice: "none"`, and Gemini
    `toolConfig.functionCallingConfig.mode: "NONE"`.
    """

    type: Literal["none"] = "none"


ToolChoice = ToolChoiceAuto | ToolChoiceRequired | ToolChoiceTool | ToolChoiceNone
