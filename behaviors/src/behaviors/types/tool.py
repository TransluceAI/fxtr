"""Tool types."""

from dataclasses import dataclass
from typing import Literal

from behaviors.types.cache_control import CacheControl
from behaviors.types.json import JSONObject
from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class ToolParam(TaggedDataclass, fxtr_type=behaviors_type("ToolParam.v1")):
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
            `input_schema` / OpenAI `parameters` / Gemini `parameters`).
        strict: Whether the provider enforces that tool inputs validate against
            `input_schema` exactly (Anthropic `strict` / OpenAI `strict`), or
            None if the tool definition did not specify it.
        input_examples: Example inputs conforming to `input_schema` (Anthropic
            `input_examples`), or None if the tool definition did not include
            any.
    """

    name: str
    description: str | None
    input_schema: JSONObject
    strict: bool | None
    input_examples: tuple[JSONObject, ...] | None


@dataclass(frozen=True)
class CacheableToolParam(ToolParam, fxtr_type=behaviors_type("CacheableToolParam.v1")):
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
