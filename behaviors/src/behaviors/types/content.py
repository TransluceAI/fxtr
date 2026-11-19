"""Content blocks that make up chat messages."""

from dataclasses import dataclass
from typing import Literal

from behaviors.types.cache_control import CacheControl
from behaviors.types.json import JSONObject
from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData


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


ReasoningForm = Literal["full", "summary", "redacted"]
"""What a reasoning block's text is.

- `full`: the model's own reasoning, e.g. `reasoning_content` from sglang or vLLM, OpenRouter's
  `reasoning` and `reasoning.text` entries, or a Responses `reasoning` item's `content`.
- `summary`: a provider-written summary of it, e.g. an Anthropic `thinking` block (summarized on
  Claude 4+ models), a Responses reasoning `summary`, or a Gemini thought part (`thought: true`).
  A reasoning item that came without a summary is a summary with empty text.
- `redacted`: the provider withheld the reasoning (an Anthropic `redacted_thinking` block); the
  text is empty.
"""


@dataclass(frozen=True)
class ContentReasoning(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("ContentReasoning.v2"),
):
    """Reasoning the model produced before its answer, and the provider state that continues it.

    Providers that reason ask for some state back on the next request of a conversation: an
    Anthropic thinking block's signature, an OpenAI reasoning item's encrypted content, a Gemini
    thought signature, OpenRouter's `reasoning_details`. That state is `reasoning_state`, a tagged
    value whose type names the provider format, so the vocabulary needs no change for a new one.
    Each payload type is defined with the codec that produces it: behaviors' own in its dialect
    modules, another codebase's in its own namespace (`com.example.SomeReasoningState.v1`).

    Only the codec that owns a payload type reads it (with
    `behaviors.implementations.dialects.codec_utils.own_reasoning_state`); to everything else it
    is opaque. A model codec sends back only the states it recognizes. A harness-side codec
    carries a state raw when its dialect is the state's own, and otherwise in an envelope in its
    dialect's opaque slot, as JSON: a payload whose fields are not all JSON values (bytes, entity
    ids) loses its state at a harness.

    Attributes:
        text: The reasoning text, as `form` describes it (may be empty).
        form: What the text is (see `ReasoningForm`).
        reasoning_state: The provider state to send back with this block, or None if the
            provider attached none.
    """

    text: str
    form: ReasoningForm
    reasoning_state: DynamicData | None = None
    type: Literal["reasoning"] = "reasoning"


@dataclass(frozen=True)
class ContentToolUse(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("ContentToolUse.v2"),
):
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
        namespace: The group the called tool was declared in, when it was
            declared in one (OpenAI Responses `namespace` tools; see
            `ToolParam.namespace`); None otherwise. A dialect without
            namespaces leaves it None, and the group is recovered from the
            request's tool declarations when the call is rendered back.
    """

    id: str
    name: str
    input: JSONObject
    parse_error: str | None
    namespace: str | None = None
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


@dataclass(frozen=True)
class CacheableContentToolUse(
    ContentToolUse, fxtr_type=behaviors_type("CacheableContentToolUse.v1")
):
    """A tool call that may carry a prompt-cache breakpoint.

    In the Anthropic Messages API the breakpoint sits on the `tool_use` block itself. An agentic
    caller that marks the last block of each recent turn (OpenCode, for one) lands it here
    whenever an assistant turn is a bare tool call.

    Attributes:
        cache_control: The breakpoint, or None for a plain tool call.
    """

    cache_control: CacheControl | None = None


TextContent = ContentText | CacheableContentText


Content = TextContent | ContentReasoning | ContentToolUse | CacheableContentToolUse
