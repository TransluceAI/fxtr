"""Types describing a single model request/response exchange."""

import json
from dataclasses import dataclass, field
from typing import Literal

from behaviors.types.adaptation import DEFAULT_ADAPTATION_POLICY, AdaptationPolicy, RequestLoss
from behaviors.types.dialect import DialectExtras
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
class StopRefusal(TaggedDataclass, fxtr_type=behaviors_type("StopRefusal.v1")):
    """The model or a provider's safety system declined to produce all or part of the answer.

    Any text the model wrote stays in the response's content, so the response may have no
    content, partial content, or the model's own explanation. Covers:

    - Anthropic `stop_reason: "refusal"`, from a model declining under a response format or
      from Anthropic's safety classifiers, which Anthropic does not distinguish
      (https://platform.claude.com/docs/en/build-with-claude/structured-outputs,
      https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/handle-streaming-refusals):
      `complete` is None.
    - OpenAI's `refusal` content part (Responses) or `message.refusal` (Chat Completions), the
      model's explanation in place of the answer in a completed response
      (https://platform.openai.com/docs/guides/structured-outputs#refusals): `complete` is True.
      It comes with a response format, JSON mode included; without one, a model refuses in
      ordinary text, which is a `StopEndTurn` like any other answer.
    - OpenAI `finish_reason: "content_filter"` and a Responses result incomplete for
      `content_filter`, and Gemini `finishReason` or `promptFeedback.blockReason` values
      `SAFETY`, `PROHIBITED_CONTENT` and `IMAGE_SAFETY`
      (https://ai.google.dev/api/generate-content#FinishReason): `complete` is False.

    A refusal is a stop reason rather than content, so a harness of another dialect gets one in
    its own form; what a dialect cannot express of it (OpenAI's streamed `refusal` part, say) is
    recorded as a translation loss. If refusal text ever needs to be told apart from answer text
    in the content itself, a refusal content block is the extension.

    Attributes:
        provider_reason: The provider's stop/finish reason exactly as reported, e.g.
            `"refusal"` (Anthropic and OpenAI's refusal item), `"content_filter"` (OpenAI), or
            `"SAFETY"` (Gemini).
        complete: Whether the response is the model's whole refusal (True), was cut off or
            blocked (False), or the provider does not say which (None).
        detail: What the provider said about the stop beyond the reason, kept as its own JSON:
            Anthropic's `stop_details` (the policy category behind it), Gemini's finish message
            or safety ratings. None when the provider gave nothing more.
    """

    provider_reason: str
    complete: bool | None
    detail: JSONObject | None = None
    type: Literal["refusal"] = "refusal"


@dataclass(frozen=True)
class StopContentFilter(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("StopContentFilter.v2"),
):
    """The provider withheld output for a reason other than a refusal (see `StopRefusal`).

    Gemini `finishReason` values `RECITATION` (output that recites training data), `SPII`
    (personal information) and `BLOCKLIST` (a term list), and a `promptFeedback.blockReason` of
    `BLOCKLIST` or `OTHER` (https://ai.google.dev/api/generate-content). Where
    the line between this and a refusal falls is a judgment call: these stop output because of
    what it contains, not because the model or a safety policy declined the request.

    Attributes:
        provider_reason: The provider's stop/finish reason exactly as reported.
        detail: What the provider said about the stop beyond the reason, kept as its own JSON
            (Gemini's finish message or safety ratings), or None.
    """

    provider_reason: str
    detail: JSONObject | None = None
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
    StopEndTurn
    | StopMaxTokens
    | StopStopSequence
    | StopToolUse
    | StopRefusal
    | StopContentFilter
    | StopOther
)


SpeedTier = Literal["standard", "fast"]
"""A provider's inference speed tier.

`fast` is the premium-priced faster configuration of the same model that both
Anthropic (fast mode: `speed`) and OpenAI (the fast service tier:
`service_tier`, also routed by OpenRouter) offer per request; `standard` is the
regular one.
"""


@dataclass(frozen=True)
class Usage(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("Usage.v2"),
):
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
            (Anthropic `cache_creation_input_tokens` / OpenAI and OpenRouter
            `cache_write_tokens`), or None if the provider does not report
            them.
        output_tokens: Total output tokens generated, including reasoning
            tokens. This matches Anthropic `output_tokens` and OpenAI
            `completion_tokens`; Gemini's `candidatesTokenCount` excludes
            thoughts, so Gemini implementations must add `thoughtsTokenCount`
            to it when populating this field.
        reasoning_output_tokens: The subset of output_tokens spent on
            reasoning (OpenAI `reasoning_tokens` / Gemini
            `thoughtsTokenCount`), or None if the provider does not report
            them separately.
        speed: The speed tier the provider reports having served (Anthropic
            `usage.speed`; OpenAI's and OpenRouter's `service_tier` on the
            response, where `priority` is `fast` and `default` is `standard`),
            or None when it reports none. Billing follows what was served, so
            a `fast` request answered `standard` was not charged the premium.
        billed_cost_usd: What the provider says the call cost, in US dollars
            (OpenRouter `usage.cost`), or None when it reports no cost.
    """

    input_tokens: int
    cache_read_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int
    reasoning_output_tokens: int | None
    speed: SpeedTier | None = None
    billed_cost_usd: float | None = None


ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
"""Provider-neutral reasoning effort level, the union of the providers' vocabularies.

A level is sent to a provider as asked, and whether the model accepts it is the
provider's to validate (OpenAI's 5.6 family takes `max`; GPT 5.5 tops out at
`xhigh`), except where a dialect has a documented nearest level (Anthropic
renders `minimal` as `low`).
"""


@dataclass(frozen=True)
class ReasoningParams(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("ReasoningParams.v2"),
):
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
        summary: Whether readable reasoning summaries should come back with
            the response, separate from reasoning being on and from its
            budget or effort. True asks for them (Anthropic
            `thinking.display: "summarized"`, Responses
            `reasoning.summary: "detailed"`), False asks for none (Anthropic
            `"omitted"`, Responses no `summary`), None leaves each provider's
            declared default (`summarized` / `auto`). The exact detail level a
            dialect offers (Responses `auto`, `concise`) is a dialect extra
            beside this field, so a same-dialect subject gets it verbatim.
            Gemini has no equivalent and ignores it. This is about what the
            response shows, not about the opaque reasoning state a
            continuation needs, which is carried regardless.
    """

    enabled: bool = True
    budget_tokens: int | None = None
    effort: ReasoningEffort | None = None
    summary: bool | None = None


@dataclass(frozen=True)
class SamplingParams(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("SamplingParams.v2"),
):
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
        speed: The speed tier to ask for (Anthropic `speed` with its beta
            header / OpenAI and OpenRouter `service_tier`), or None to send
            nothing. Rendered as asked: a provider or model without the tier
            answers with its own error, or serves the standard tier and says
            so in `Usage.speed`.
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
    speed: SpeedTier | None = None
    extra: JSONObject = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True)
class ResponseFormatText(TaggedDataclass, fxtr_type=behaviors_type("ResponseFormatText.v1")):
    """The reply is plain text, asked for explicitly.

    Corresponds to OpenAI Responses `text.format: {"type": "text"}`, Chat Completions
    `response_format: {"type": "text"}`, and Gemini `responseMimeType: "text/plain"`. Every
    provider's default; kept distinct from no response format so a request that spells it out
    is rendered back the same way.
    """

    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ResponseFormatJsonObject(
    TaggedDataclass, fxtr_type=behaviors_type("ResponseFormatJsonObject.v1")
):
    """The reply is one syntactically valid JSON value, of no particular shape (JSON mode).

    Corresponds to OpenAI `{"type": "json_object"}` (Responses `text.format`, Chat Completions
    `response_format`) and Gemini `responseMimeType: "application/json"` without a schema.
    Anthropic has no equivalent.
    """

    type: Literal["json_object"] = "json_object"


@dataclass(frozen=True)
class ResponseFormatJsonSchema(
    TaggedDataclass, fxtr_type=behaviors_type("ResponseFormatJsonSchema.v1")
):
    """The reply is JSON conforming to a schema (structured output).

    Corresponds to Anthropic `output_config.format: {"type": "json_schema", "schema"}`
    (https://platform.claude.com/docs/en/build-with-claude/structured-outputs), OpenAI
    `{"type": "json_schema", ...}` (Responses `text.format`, Chat Completions
    `response_format.json_schema`;
    https://platform.openai.com/docs/guides/structured-outputs), and Gemini
    `responseMimeType: "application/json"` with `responseJsonSchema`
    (https://ai.google.dev/gemini-api/docs/structured-output).

    Attributes:
        schema: The JSON Schema the reply must conform to: draft 2020-12, the providers'
            format, unless its `$schema` names another draft. Codecs pass it through as given
            (Gemini's OpenAPI `Schema` is converted on decode).
        strict: Whether the provider is to constrain generation to the schema. Anthropic and
            Gemini always do, so their requests decode with True; OpenAI does only when asked
            (`strict: true`) and otherwise treats the schema as guidance.
        name: The schema's name (OpenAI requires one), or None.
        description: What the reply is for (OpenAI), or None.
    """

    schema: JSONObject
    strict: bool
    name: str | None = None
    description: str | None = None
    type: Literal["json_schema"] = "json_schema"


ResponseFormat = ResponseFormatText | ResponseFormatJsonObject | ResponseFormatJsonSchema


@dataclass(frozen=True)
class ModelRequest(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("ModelRequest.v2"),
):
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
        extras: Material from the request's original wire dialect that the vocabulary has no
            slot for (see `behaviors.types.dialect.DialectExtras`), or None. Attached when a
            request is decoded from a dialect; applied only by a codec of the same dialect.
        response_format: The form the model's reply must take, or None if the request did
            not specify one (plain text). A codec-backed model API fits it to its dialect's
            limits before sending, and reports what that changed
            (`ModelResponse.adaptation_losses`). The reply's text conforms to it only when the
            response ended with `StopEndTurn`: a refusal, a content filter or a token limit
            may leave partial, empty or non-conforming text.
        adaptation_policy: What the model API may change about this request in order to send
            it (`behaviors.types.adaptation.AdaptationPolicy`): by default every change is
            made and reported; `semantic_changes="refuse"` fails the call instead of changing
            its meaning.
    """

    context: tuple[ChatMessage, ...]
    tools: tuple[ToolDefinition, ...]
    tool_choice: ToolChoice | None
    parallel_tool_calls: bool | None
    sampling: SamplingParams
    extras: DialectExtras | None = None
    response_format: ResponseFormat | None = None
    adaptation_policy: AdaptationPolicy = DEFAULT_ADAPTATION_POLICY


@dataclass(frozen=True)
class ModelResponse(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("ModelResponse.v2"),
):
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
        adaptation_losses: How the request was changed to fit the dialect and model before
            it was sent (see `behaviors.types.adaptation`); empty when it was sent as
            asked.
    """

    id: str
    output: tuple[ChatMessage, ...]
    stop_reason: StopReason
    usage: Usage
    adaptation_losses: tuple[RequestLoss, ...] = ()
    kind: Literal["model_response"] = "model_response"

    def raise_for_stop_reason(
        self, allow: tuple[type[StopReason], ...] = (StopEndTurn, StopStopSequence)
    ) -> None:
        """Raise `UnexpectedStopError` unless the response stopped for one of `allow`, as an HTTP
        client's `raise_for_status` does for a status.

        By default a response is accepted only when the model finished its answer: a refusal,
        a content filter, a token limit, tool calls or another stop may leave partial, empty or
        non-conforming text, which a caller reading the answer should not take for one. A caller
        that expects tool calls passes `StopToolUse` in `allow`. Check before reading the text.
        """
        if not isinstance(self.stop_reason, allow):
            raise UnexpectedStopError(self.stop_reason)


class UnexpectedStopError(Exception):
    """A response stopped for a reason its caller did not accept
    (`ModelResponse.raise_for_stop_reason`).

    An expected outcome, not a library error: a refusal, a content filter or a token limit. The
    message names the stop and what the provider said about it (a refusal's policy category,
    say).

    Attributes:
        stop_reason: Why the response stopped.
    """

    def __init__(self, stop_reason: StopReason) -> None:
        self.stop_reason = stop_reason
        super().__init__(_describe_stop(stop_reason))


def _describe_stop(stop: StopReason) -> str:
    if isinstance(stop, StopRefusal | StopContentFilter):
        kind = "refused" if isinstance(stop, StopRefusal) else "filtered"
        detail = f": {json.dumps(dict(stop.detail))}" if stop.detail else ""
        return f"the response was {kind} ({stop.provider_reason}){detail}"
    if isinstance(stop, StopMaxTokens):
        return "the response was cut off at the token limit"
    if isinstance(stop, StopOther):
        return f"the response stopped for another reason ({stop.provider_reason})"
    return f"the response stopped with {stop.type}"
