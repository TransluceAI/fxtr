"""Chat message types.

Messages deliberately carry no identifiers: no provider API lets the client
author a message ID, and the IDs providers do return identify the API
response, not a message — those live on ModelResponse. The only IDs in the
message layer are tool-call IDs (ContentToolUse.id / ToolMessage.tool_call_id),
which providers require the client to echo back verbatim.
"""

from dataclasses import dataclass
from typing import Literal

from behaviors.types.content import Content, TextContent
from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class SystemMessage(TaggedDataclass, fxtr_type=behaviors_type("SystemMessage.v1")):
    """System message in a chat conversation.

    Corresponds to the top-level `system` parameter in the Anthropic Messages
    API (https://platform.claude.com/docs/en/api/messages), to a
    `system` role message in the OpenAI Chat Completions API
    (https://platform.openai.com/docs/api-reference/chat/create), and to the
    `system_instruction` parameter in the Gemini API
    (https://ai.google.dev/gemini-api/docs/text-generation).

    Note that the `developer` role messages from OpenAI are unsupported.
    (TODO: should we support this?)

    Attributes:
        content: The message content.
    """

    content: tuple[TextContent, ...]
    role: Literal["system"] = "system"


@dataclass(frozen=True)
class UserMessage(TaggedDataclass, fxtr_type=behaviors_type("UserMessage.v1")):
    """User message in a chat conversation.

    Corresponds to a `user` role message in the Anthropic Messages API
    (https://platform.claude.com/docs/en/api/messages) and the OpenAI Chat
    Completions API (https://platform.openai.com/docs/api-reference/chat/create),
    and to a `contents[]` entry with role `user` in the Gemini API
    (https://ai.google.dev/gemini-api/docs/text-generation).

    Attributes:
        content: The message content.
    """

    content: tuple[TextContent, ...]
    role: Literal["user"] = "user"


@dataclass(frozen=True)
class AssistantMessage(TaggedDataclass, fxtr_type=behaviors_type("AssistantMessage.v1")):
    """Assistant message in a chat conversation.

    Corresponds to an `assistant` role message. Everything the model produced
    in its turn — text, reasoning, and tool calls — lives in `content` as an
    ordered tuple of blocks; there is no separate tool-calls field. Adapters
    map provider formats onto this as follows:

    - Anthropic Messages API: the message's content blocks (text, thinking,
      tool_use — https://platform.claude.com/docs/en/api/messages), in order.
    - Gemini API: the role `model` parts (text, thought, functionCall —
      https://ai.google.dev/gemini-api/docs/text-generation), in order.
    - OpenAI Responses API: one message per assistant turn, flattening the
      turn's consecutive output items (`reasoning`, `message`, and
      `function_call` — https://platform.openai.com/docs/api-reference/responses)
      into content blocks in item order; the reverse mapping splits the blocks
      back into items.
    - OpenAI Chat Completions API: the message `content` followed by its
      `tool_calls` array entries as trailing ContentToolUse blocks (see
      ContentToolUse for the ordering contract —
      https://platform.openai.com/docs/api-reference/chat/object).

    Attributes:
        content: The message content (text, reasoning, and tool-use blocks).
    """

    content: tuple[Content, ...]
    role: Literal["assistant"] = "assistant"


@dataclass(frozen=True)
class ToolMessage(TaggedDataclass, fxtr_type=behaviors_type("ToolMessage.v1")):
    """Tool result message in a chat conversation.

    The normalized representation of one tool result: each ToolMessage answers
    exactly one prior ContentToolUse, matched by `tool_call_id`. Adapters map
    provider formats onto this as follows:

    - OpenAI Chat Completions API: a `tool` role message
      (`{role: "tool", content, tool_call_id}`,
      https://platform.openai.com/docs/guides/function-calling); a single
      ContentText.
    - OpenAI Responses API: a `function_call_output` item
      (`{call_id, output}`,
      https://platform.openai.com/docs/api-reference/responses); a single
      ContentText.
    - Anthropic Messages API: a `tool_result` content block (with
      `tool_use_id`) inside a `user` message; each block becomes its own
      ToolMessage
      (https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview).
      A string `content` becomes a single ContentText; list `content` becomes
      one block per entry, in order.
    - Gemini API: a `functionResponse` part in a `user` role content entry,
      likewise one ToolMessage per part
      (https://ai.google.dev/gemini-api/docs/function-calling); the adapter
      JSON-encodes the `response` object into a single ContentText.

    Attributes:
        content: The tool result content, in order.
        tool_call_id: ID of the tool call this message is responding to,
            echoed verbatim from the corresponding ContentToolUse `id`
            (Anthropic `tool_use_id` / OpenAI Chat Completions `tool_call_id`
            / OpenAI Responses `call_id` / Gemini `functionResponse.id`).
            Every ToolMessage answers exactly one prior ContentToolUse whose
            `id` matches this value.
        is_error: Whether the tool execution failed (Anthropic `is_error`),
            or None if the provider format has no error flag (both OpenAI
            APIs and Gemini convey failures inside the result content
            itself).
    """

    content: tuple[TextContent, ...]
    tool_call_id: str
    is_error: bool | None
    role: Literal["tool"] = "tool"


ChatMessage = SystemMessage | UserMessage | AssistantMessage | ToolMessage
