"""Types describing a single model request/response exchange."""

from dataclasses import dataclass, field
from typing import Literal

from behaviors.types.json import JSONObject, JSONValue
from behaviors.types.message import ChatMessage
from behaviors.types.namespace import behaviors_type
from behaviors.types.tool import ToolChoice, ToolDefinition
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class StopEndTurn(TaggedDataclass, fxtr_type=behaviors_type("StopEndTurn.v1")):
    """Model finished its turn naturally.

    Corresponds to Anthropic `stop_reason: "end_turn"`
    (https://platform.claude.com/docs/en/api/messages) and OpenAI
    `finish_reason: "stop"`
    (https://platform.openai.com/docs/api-reference/chat/object), and to Gemini
    `finishReason: "STOP"` (https://ai.google.dev/api/generate-content).
    """

    type: Literal["end_turn"] = "end_turn"


@dataclass(frozen=True)
class StopMaxTokens(TaggedDataclass, fxtr_type=behaviors_type("StopMaxTokens.v1")):
    """Output was truncated at the token limit.

    Corresponds to Anthropic `stop_reason: "max_tokens"`, OpenAI
    `finish_reason: "length"`, and Gemini `finishReason: "MAX_TOKENS"`.
    """

    type: Literal["max_tokens"] = "max_tokens"


@dataclass(frozen=True)
class StopStopSequence(TaggedDataclass, fxtr_type=behaviors_type("StopStopSequence.v1")):
    """A stop sequence was hit.

    Corresponds to Anthropic `stop_reason: "stop_sequence"` (with the matched
    sequence in `stop_sequence`) and OpenAI `finish_reason: "stop"` when a
    custom `stop` string was matched.

    Attributes:
        stop_sequence: Which stop sequence was hit.
    """

    stop_sequence: str
    type: Literal["stop_sequence"] = "stop_sequence"


@dataclass(frozen=True)
class StopToolUse(TaggedDataclass, fxtr_type=behaviors_type("StopToolUse.v1")):
    """Model stopped to call tools.

    Corresponds to Anthropic `stop_reason: "tool_use"` and OpenAI
    `finish_reason: "tool_calls"`.
    """

    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True)
class StopContentFilter(TaggedDataclass, fxtr_type=behaviors_type("StopContentFilter.v1")):
    """Generation was stopped or blocked by a provider-side content filter.

    Covers every provider mechanism where a safety/content system (rather than
    the model finishing naturally) ends generation:

    - Anthropic `stop_reason: "refusal"`
      (https://platform.claude.com/docs/en/api/messages)
    - OpenAI `finish_reason: "content_filter"`
      (https://platform.openai.com/docs/api-reference/chat/object)
    - Gemini `finishReason` values `SAFETY`, `PROHIBITED_CONTENT`, `BLOCKLIST`,
      `SPII`, `IMAGE_SAFETY`, and `RECITATION`
      (https://ai.google.dev/api/generate-content#FinishReason)

    Adapters map each of these onto StopContentFilter and record the raw
    provider value verbatim in `provider_reason`.

    Attributes:
        provider_reason: The provider's stop/finish reason exactly as reported,
            e.g. `"refusal"` (Anthropic), `"content_filter"` (OpenAI), or
            `"SAFETY"` / `"RECITATION"` / ... (Gemini).
    """

    provider_reason: str
    type: Literal["content_filter"] = "content_filter"


@dataclass(frozen=True)
class StopOther(TaggedDataclass, fxtr_type=behaviors_type("StopOther.v1")):
    """Generation stopped for a reason not covered by the other variants.

    Attributes:
        provider_reason: The provider's stop/finish reason exactly as reported.
    """

    provider_reason: str
    type: Literal["other"] = "other"


StopReason = (
    StopEndTurn | StopMaxTokens | StopStopSequence | StopToolUse | StopContentFilter | StopOther
)


@dataclass(frozen=True)
class Usage(TaggedDataclass, fxtr_type=behaviors_type("Usage.v1")):
    """Token usage for a single model response.

    Corresponds to the `usage` object in the Anthropic Messages API
    (https://platform.claude.com/docs/en/api/messages) and the OpenAI Chat
    Completions API (https://platform.openai.com/docs/api-reference/chat/object),
    and to `usageMetadata` in the Gemini API
    (https://ai.google.dev/api/generate-content).

    Attributes:
        input_tokens: Non-cached input tokens.
        cache_read_input_tokens: Input tokens served from the prompt cache
            (Anthropic `cache_read_input_tokens` / OpenAI `cached_tokens`), or
            None if the provider does not report them.
        cache_write_input_tokens: Input tokens written to the prompt cache
            (Anthropic `cache_creation_input_tokens`), or None if the provider
            does not report them.
        output_tokens: Total output tokens generated, including reasoning
            tokens. This matches Anthropic `output_tokens` and OpenAI
            `completion_tokens`; Gemini's `candidatesTokenCount` excludes
            thoughts, so Gemini implementations must add `thoughtsTokenCount`
            to it when populating this field.
        reasoning_output_tokens: The subset of output_tokens spent on
            reasoning (OpenAI `reasoning_tokens` / Gemini
            `thoughtsTokenCount`), or None if the provider does not report
            them separately.
    """

    input_tokens: int
    cache_read_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int
    reasoning_output_tokens: int | None


ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
"""Provider-neutral reasoning effort level, the union of the providers' vocabularies.

OpenAI implementations preserve explicit levels and let the API validate model-specific
support. Other implementations may map levels their provider lacks (e.g. `minimal` →
Anthropic `low`).
"""


@dataclass(frozen=True)
class ReasoningParams(TaggedDataclass, fxtr_type=behaviors_type("ReasoningParams.v1")):
    """Provider-neutral reasoning/thinking configuration for a model request.

    Every provider now exposes reasoning in one of two currencies — a token
    budget or an effort level — and each implementation maps these fields to
    its own dialect:

    - Anthropic Messages: `enabled=False` → `thinking: {type: "disabled"}`;
      a budget → `{type: "enabled", budget_tokens}`; enabled with no budget →
      `{type: "adaptive"}`; `effort` → `output_config.effort`
      (https://platform.claude.com/docs/en/build-with-claude/extended-thinking).
    - OpenAI: `effort` → `reasoning_effort` (Chat Completions) /
      `reasoning.effort` (Responses); a budget with no effort is converted to
      the effort level whose typical spend is nearest
      (https://platform.openai.com/docs/guides/reasoning).
    - Gemini: `generationConfig.thinkingConfig.thinkingBudget` — the budget
      verbatim, 0 when disabled, -1 (dynamic) when enabled with no budget
      (https://ai.google.dev/gemini-api/docs/thinking).

    Attributes:
        enabled: Whether the model should reason at all. With `enabled=False`
            the other fields are ignored.
        budget_tokens: Maximum tokens to spend on reasoning, or None to let
            the provider decide (Anthropic adaptive / Gemini dynamic).
        effort: Reasoning effort level, or None for the provider's default.
    """

    enabled: bool = True
    budget_tokens: int | None = None
    effort: ReasoningEffort | None = None


@dataclass(frozen=True)
class SamplingParams(TaggedDataclass, fxtr_type=behaviors_type("SamplingParams.v1")):
    """Sampling/generation parameters for a model request.

    Only the parameters that every major provider supports are first-class
    fields; anything provider-specific goes in `extra`.

    Attributes:
        max_tokens: Maximum number of output tokens to generate (Anthropic
            `max_tokens` / OpenAI `max_completion_tokens` / Gemini
            `maxOutputTokens`), or None to use the provider's default.
        temperature: Sampling temperature, or None for the provider's default.
        top_p: Nucleus sampling threshold, or None for the provider's default.
        stop_sequences: Custom sequences at which generation stops (Anthropic
            `stop_sequences` / OpenAI `stop` / Gemini `stopSequences`).
        reasoning: Reasoning/thinking configuration, or None to leave it to
            the provider's default (no reasoning parameter is sent at all).
        extra: Provider-specific parameters passed through verbatim (e.g.
            `top_k`, `seed`). Keys are interpreted by the model
            implementation; unknown keys are an error rather than silently
            dropped.
    """

    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop_sequences: tuple[str, ...] = ()
    reasoning: ReasoningParams | None = None
    extra: JSONObject = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True)
class ModelRequest(TaggedDataclass, fxtr_type=behaviors_type("ModelRequest.v1")):
    """Exactly what is sent to the model for one request.

    Attributes:
        context: The messages sent to the model.
        tools: The tools offered on the call.
        tool_choice: The tool-use policy sent with the request (see
            behaviors.types.tool), or None if the request did not specify one
            (every provider then defaults to auto).
        parallel_tool_calls: Whether the model was allowed to call multiple
            tools in one response (OpenAI `parallel_tool_calls` / Anthropic
            `tool_choice.disable_parallel_tool_use`, inverted), or None if the
            request did not specify it.
        sampling: The sampling parameters for the call.
    """

    context: tuple[ChatMessage, ...]
    tools: tuple[ToolDefinition, ...]
    tool_choice: ToolChoice | None
    parallel_tool_calls: bool | None
    sampling: SamplingParams


@dataclass(frozen=True)
class ModelResponse(TaggedDataclass, fxtr_type=behaviors_type("ModelResponse.v1")):
    """A model response together with how and why it ended.

    The tag distinguishes a serialized response from a serialized
    ``ModelCallFailure``.

    Attributes:
        id: Provider-assigned identifier for the API response: the Anthropic
            message `id` (`msg_...`,
            https://platform.claude.com/docs/en/api/messages), the OpenAI
            completion `id` (`chatcmpl-...`) or Responses API response `id`
            (`resp_...`), or the Gemini `responseId`. This identifies the
            request/response exchange, not the assistant message — messages
            carry no IDs (see behaviors.types.message). It is never sent back
            to the provider and nothing may assume uniqueness across
            providers.
        output: The messages the model produced. Usually a single assistant
            message; models with backend-executed tools (e.g. server-side web
            search) may produce several, e.g. an assistant message with a
            tool-use block, the tool result, and a final assistant message.
        stop_reason: Why generation stopped.
        usage: Token usage for the response.
    """

    id: str
    output: tuple[ChatMessage, ...]
    stop_reason: StopReason
    usage: Usage
    kind: Literal["model_response"] = "model_response"
