"""Content blocks that make up chat messages."""

from dataclasses import dataclass
from typing import Literal

from behaviors.types.cache_control import CacheControl
from behaviors.types.json import JSONObject
from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class ContentText(TaggedDataclass, fxtr_type=behaviors_type("ContentText.v1")):
    """Plain text content in a chat message.

    Corresponds to a `text` content block in the Anthropic Messages API
    (https://platform.claude.com/docs/en/api/messages), to string content or a
    text content part in the OpenAI Chat Completions API
    (https://platform.openai.com/docs/api-reference/chat/create), and to a text
    part (`parts[].text`) in the Gemini API
    (https://ai.google.dev/gemini-api/docs/text-generation).

    Attributes:
        text: The actual text content.
    """

    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ContentReasoning(TaggedDataclass, fxtr_type=behaviors_type("ContentReasoning.v1")):
    """A direct (literal) reasoning trace, e.g. from an open-weight model.

    Corresponds to the `reasoning_content` field returned by OpenAI-compatible
    servers such as sglang and vLLM, parsed from `<think>...</think>` spans in
    the raw completion.

    Attributes:
        text: The model's literal reasoning text.
    """

    text: str
    type: Literal["reasoning"] = "reasoning"


@dataclass(frozen=True)
class ContentSummarizedReasoning(
    TaggedDataclass, fxtr_type=behaviors_type("ContentSummarizedReasoning.v1")
):
    """A summarized reasoning trace.

    Some APIs return a summary of the model's reasoning rather than the literal
    trace; this type makes that explicit. Corresponds to a `thinking` content
    block in the Anthropic Messages API, which is summarized on Claude 4+ models
    (https://platform.claude.com/docs/en/build-with-claude/extended-thinking),
    to reasoning summaries in the OpenAI Responses API
    (https://platform.openai.com/docs/guides/reasoning), and to thought summaries
    (parts with `thought: true`) in the Gemini API
    (https://ai.google.dev/gemini-api/docs/thinking).

    A reasoning item may carry no summary text at all (summaries are
    not always generated); that is still this type, with an empty `text`.

    Attributes:
        text: The summarized reasoning text (may be empty; see above).
        signature: Opaque provider blob accompanying the reasoning, echoed
            back verbatim when sending this content to the API in multi-turn
            conversations: the `signature` field of an Anthropic `thinking`
            block, a Gemini `thoughtSignature`, or OpenAI
            `reasoning.encrypted_content` (the encrypted full reasoning,
            returned when requested via `include`). None if the provider did
            not attach one.
    """

    text: str
    signature: str | None
    type: Literal["summarized_reasoning"] = "summarized_reasoning"


@dataclass(frozen=True)
class ContentRedactedReasoning(
    TaggedDataclass, fxtr_type=behaviors_type("ContentRedactedReasoning.v1")
):
    """Reasoning whose content the provider masked, returning only encrypted bytes.

    Corresponds to a `redacted_thinking` content block in the Anthropic Messages
    API (https://platform.claude.com/docs/en/build-with-claude/extended-thinking),
    emitted for the small fraction of responses whose reasoning is flagged as
    sensitive — no readable text or summary exists.

    Attributes:
        data: The encrypted reasoning payload, as returned by the API. Must be
            passed back verbatim in multi-turn conversations.
    """

    data: str
    type: Literal["redacted_reasoning"] = "redacted_reasoning"


@dataclass(frozen=True)
class ContentToolUse(TaggedDataclass, fxtr_type=behaviors_type("ContentToolUse.v1")):
    """A tool invocation requested by the model.

    This is the single representation of a tool call in this schema; provider
    formats that report tool calls outside the message content must be folded
    into `content` by their adapters. Corresponds to:

    - Anthropic Messages API: a `tool_use` content block (`{id, name, input}`,
      https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview),
      kept at its position in the content list.
    - Gemini API: a `functionCall` part (`{id, name, args}`,
      https://ai.google.dev/gemini-api/docs/function-calling), kept at its
      position in the parts list. Gemini 3+ always populates
      `functionCall.id` (as do the Live and Interactions APIs); Gemini 2.5
      and earlier omit it on `generateContent` and pair results with calls
      by function name and position. For those models the adapter must
      compute that pairing at ingest and record it as deterministic,
      visibly-synthetic ids (e.g. `synth-{turn}-{call}`), stripping them
      again when serializing back to the API.
    - OpenAI Responses API: a `function_call` output item
      (`{call_id, name, arguments}`,
      https://platform.openai.com/docs/guides/function-calling), kept at its
      position in the output item list; `id` holds the item's `call_id` (the
      id its `function_call_output` refers to), not the `fc_...` item id.
    - OpenAI Chat Completions API: an entry in the assistant message's
      `tool_calls` array (`tool_calls[].id`, `tool_calls[].function.name`,
      `tool_calls[].function.arguments`). The array has no position within the
      message content, so the adapter appends these blocks after all other
      content blocks, in array order, and reconstructs the array from the
      trailing tool-use blocks when converting back.

    Attributes:
        id: Identifier for the tool use, assigned by the provider and echoed
            verbatim in the corresponding ToolMessage's `tool_call_id`
            (Anthropic `tool_use_id` / OpenAI Chat Completions `tool_call_id`
            / OpenAI Responses `call_id` / Gemini `functionResponse.id`).
            Assigned by all current provider APIs; for legacy Gemini (<= 2.5)
            calls without one, the adapter synthesizes a deterministic id
            encoding the positional pairing (see the Gemini note above).
        name: Name of the tool being invoked.
        input: Arguments to the tool. Both OpenAI APIs deliver arguments as a
            JSON-encoded string (`arguments`); adapters store the parsed
            object here.
        parse_error: Error which occurred parsing the provider's JSON-encoded
            arguments, if any; `input` is empty in that case. Always None for
            providers that deliver arguments as structured objects (Anthropic
            `input`, Gemini `args`).
    """

    id: str
    name: str
    input: JSONObject
    parse_error: str | None
    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True)
class CacheableContentText(ContentText, fxtr_type=behaviors_type("CacheableContentText.v1")):
    """A text block that may carry a prompt-cache breakpoint.

    Attributes:
        cache_control: The breakpoint, or None for a plain text block (the
            subclass with `cache_control=None` behaves exactly like
            ContentText).
    """

    cache_control: CacheControl | None = None


TextContent = ContentText | CacheableContentText


Content = (
    TextContent
    | ContentReasoning
    | ContentSummarizedReasoning
    | ContentRedactedReasoning
    | ContentToolUse
)
