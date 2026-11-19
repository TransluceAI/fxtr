"""The OpenAI Chat Completions dialect (`/v1/chat/completions`), model side only.

No harness we run speaks chat completions, so this codec has no harness-facing half; it serves
`OpenAICompatibleAPI`, which covers every endpoint speaking the dialect: OpenAI itself, OpenRouter,
Anthropic's and Gemini's OpenAI-compatible endpoints, and self-hosted vLLM/sglang servers.

The codec comes in two variants, chosen at construction (`variant`), each one coherent set of
decoding, assembly and encoding rules for a wire contract:

- `standard`: the dialect as OpenAI documents it, plus the literal-reasoning extension the
  compatible servers share (the `reasoning_content` / `reasoning` string, see the echo field
  below). The default; what vLLM, sglang, Moonshot, DeepSeek, Z.ai and OpenAI itself speak.
  OpenRouter's `reasoning_details` are not read here and reasoning state is never rendered.
- `openrouter`: OpenRouter's extension on top of that: the `reasoning_details` array (signed
  thinking, summaries with encrypted continuations, plain text) assembled, carried and echoed as
  described below. `OpenRouterAPI` selects it.

Rendering notes:

- Literal reasoning (`ContentReasoning`) in assistant context is sent back on the assistant
  message under `reasoning_echo_field`: `reasoning_content` by default (the field vLLM, sglang,
  Moonshot, DeepSeek, Z.ai and OpenRouter accept; OpenRouter also accepts `reasoning`), or
  removed by adaptation when None. Adaptation also clears `ToolMessage.is_error` (failures are
  conveyed in the result text).
- Reasoning arrives either as the `reasoning_content` (vLLM/sglang) or `reasoning` (OpenRouter)
  string, surfaced as `full` `ContentReasoning` with no state, or, in the `openrouter` variant,
  as OpenRouter's `reasoning_details` entries, which carry what the upstream provider needs echoed
  back: a signed thinking text (`reasoning.text` with `signature`, format `anthropic-claude-v1`),
  a summary followed by an encrypted item (`reasoning.summary` + `reasoning.encrypted`, format
  `openai-responses-v1`), or plain text with no signature (format `unknown`). Plain text is
  literal reasoning. The rest becomes one `ContentReasoning` per run of entries sealed by a
  signature or encrypted data, its text the entries' text and its state the entries themselves
  with the model that produced them (`OpenRouterReasoning`); its form is `full` when the run
  begins with `reasoning.text` and `summary` otherwise. The native array is assembled first and
  kept whole (`_ReasoningDetails`: streamed fragments merged by type and `index`, or `id`, a
  buffered array taken as it is). Entries complete in order: the upstream providers stream
  reasoning as sequential blocks and OpenRouter relays them so, which means the fragments of one
  entry arrive together and the next entry, content, a tool call, the finish or the end of the
  stream completes the entries before it. That is when a sealed run's state is emitted
  (`StreamReasoningState`); a fragment for an entry already complete breaks the contract and is a
  `CodecError`, since what was passed on could no longer be made to match. When a chunk carries
  `reasoning_details`, the `reasoning` string beside it is the same text and is not read again.
- A `ContentReasoning` whose state is `OpenRouterReasoning` is sent back, in the `openrouter`
  variant, as the assistant message's `reasoning_details`, unmodified. That variant accepts only
  such state, and only from the model the request names (`accepts_reasoning_state`); the
  standard variant accepts none. Adaptation removes the rest before rendering
  (`adaptation.drop_foreign_reasoning`); a block without state is sent as literal reasoning when
  its form is `full`, and otherwise removed by adaptation too, since this dialect cannot carry a
  summary or a redaction.
- Cache breakpoints (`CacheableContentText`) are forwarded as Anthropic-style `cache_control`
  on content parts only when `forward_cache_control=True` (e.g. OpenRouter serving Anthropic
  models); breakpoints on tool definitions have no compatible representation and are always
  dropped. Forwarded TTLs are fitted and ordered as the Anthropic codec's are.
- `SamplingParams.reasoning` maps to `reasoning_effort` (see `_openai_common.reasoning_effort`).
- Usage: OpenRouter's `cost` is `Usage.billed_cost_usd`; `prompt_tokens_details.cache_write_tokens`
  is a subset of `prompt_tokens` like `cached_tokens`, so both come off the non-cached input.
  Both are read in either variant, where present.
- `SamplingParams.extra` keys are forwarded verbatim in the request body for the provider to
  validate; the codec's own `extra_body` (instance-level provider parameters, e.g. OpenRouter
  routing) fills in only where the request left a key absent.
"""

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from behaviors.implementations.dialects._common import (
    ReasoningStateFilter,
    ToolNames,
    apply_declared_defaults,
    apply_dialect_extras,
    flattened_description,
    merge_provider_params,
    same_openrouter_model,
    unadapted,
)
from behaviors.implementations.dialects._openai_common import (
    budget_as_effort,
    decode_failure_body,
    failure_from_error_object,
    reasoning_effort,
    response_format_flat,
    service_tier_param,
    service_tier_speed,
)
from behaviors.implementations.dialects.adaptation import (
    AdaptedRequestBuilder,
    clear_tool_error_flags,
    drop_assistant_blocks,
    drop_breakpoints,
    drop_foreign_extras,
    drop_foreign_reasoning,
    fit_cache_ttls,
    note_flattened_tools,
    order_cache_ttls,
    tool_results_first,
)
from behaviors.implementations.dialects.anthropic_messages import (
    CACHE_TTLS as ANTHROPIC_CACHE_TTLS,
)
from behaviors.implementations.dialects.anthropic_messages import (
    DEFAULT_CACHE_TTL as ANTHROPIC_DEFAULT_CACHE_TTL,
)
from behaviors.implementations.dialects.codec_utils import (
    SseFrameParser,
    decode_json_object,
    own_reasoning_state,
)
from behaviors.implementations.dialects.contract_adaptation import (
    drop_non_object_response_format,
    drop_output_schemas,
    move_rejected_keywords,
    move_strict_rejected_keywords,
    resolve_automatic_strictness,
    strict_problems,
    unenforce_incompatible_response_format,
    unenforce_incompatible_strict_tools,
)
from behaviors.implementations.dialects.schema_support import openai_rejects
from behaviors.interfaces.dialect_codecs import AdaptedRequest, CodecError, SseDecoder
from behaviors.types.content import (
    CacheableContentText,
    CacheableContentToolUse,
    Content,
    ContentReasoning,
    ContentText,
    ContentToolUse,
    ReasoningForm,
)
from behaviors.types.dialect import Dialect, EncodedRequest
from behaviors.types.failure import ModelCallFailure
from behaviors.types.json import JSONObject
from behaviors.types.message import (
    AssistantMessage,
    CacheableToolMessage,
    ChatMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from behaviors.types.namespace import behaviors_type
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ResponseFormat,
    ResponseFormatJsonSchema,
    StopEndTurn,
    StopMaxTokens,
    StopOther,
    StopReason,
    StopRefusal,
    StopToolUse,
    Usage,
)
from behaviors.types.streaming import (
    StreamEvent,
    StreamReasoningDelta,
    StreamReasoningStart,
    StreamReasoningState,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
)
from behaviors.types.tool import (
    CacheableToolParam,
    ToolChoice,
    ToolChoiceAuto,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolParam,
)
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData


@dataclass(frozen=True)
class OpenRouterReasoning(TaggedDataclass, fxtr_type=behaviors_type("OpenRouterReasoning.v1")):
    """An OpenRouter assistant message's `reasoning_details`, which OpenRouter asks to get back
    unmodified.

    Attributes:
        model: The model that produced them: a model given another model's entries may reject or
            misread them.
        details: The entries, whole, text included.
    """

    model: str
    details: tuple[JSONObject, ...]


logger = logging.getLogger(__name__)

ChatCompletionsVariant = Literal["standard", "openrouter"]
"""Which wire contract the codec speaks: the standard dialect with the literal-reasoning
extension, or OpenRouter's extensions on top of it (see the module docstring)."""

ReasoningEchoField = Literal["reasoning_content", "reasoning"]
"""The assistant-message field literal reasoning is echoed back in (see the module docstring)."""

MaxTokensParam = Literal["max_tokens", "max_completion_tokens"]
"""Which parameter carries the output-token limit: `max_tokens` is the widely compatible
spelling; OpenAI's reasoning models accept only `max_completion_tokens`."""

_ENDPOINT = "/v1/chat/completions"


# --- request: ModelRequest -> chat completions body ------------------------------------------


def _text_content_param(
    content: tuple[ContentText, ...], forward_cache_control: bool
) -> str | list[dict[str, Any]]:
    has_breakpoint = any(
        isinstance(block, CacheableContentText) and block.cache_control is not None
        for block in content
    )
    if has_breakpoint and not forward_cache_control:
        raise unadapted("a cache breakpoint (this endpoint is not configured to forward them)")
    if len(content) == 1 and not has_breakpoint:
        return content[0].text
    parts: list[dict[str, Any]] = []
    for block in content:
        part: dict[str, Any] = {"type": "text", "text": block.text}
        if isinstance(block, CacheableContentText) and block.cache_control is not None:
            if not block.text:
                raise unadapted("a cache breakpoint on an empty text block")
            cache: dict[str, Any] = {"type": block.cache_control.type}
            if block.cache_control.ttl is not None:
                cache["ttl"] = block.cache_control.ttl
            part["cache_control"] = cache
        parts.append(part)
    return parts


# Dropped reasoning is reported once per process per reason, like the unsupported delta types
# below: it recurs on every turn of an affected run.
_warned_dropped_reasoning: set[str] = set()


def _warn_dropped_reasoning(reason: str, message: str) -> None:
    if reason not in _warned_dropped_reasoning:
        _warned_dropped_reasoning.add(reason)
        logger.warning(message)


def _assistant_message_param(
    message: AssistantMessage,
    reasoning_echo_field: ReasoningEchoField | None,
    variant: ChatCompletionsVariant,
    names: ToolNames | None = None,
) -> dict[str, Any]:
    names = names if names is not None else ToolNames()
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_details: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ContentText):
            if isinstance(block, CacheableContentText) and block.cache_control is not None:
                raise unadapted("a cache breakpoint on assistant text")
            text_parts.append(block.text)
        elif isinstance(block, ContentReasoning):
            details = own_reasoning_state(block.reasoning_state, OpenRouterReasoning)
            if details is not None and variant == "openrouter":
                reasoning_details.extend(copy.deepcopy(dict(entry)) for entry in details.details)
            elif block.reasoning_state is not None:
                raise unadapted(
                    "reasoning state other than OpenRouter's, or in the standard variant"
                )
            elif block.form != "full":
                raise unadapted(f"a reasoning {block.form} without state")
            elif reasoning_echo_field is None:
                raise unadapted("reasoning text (this endpoint has no field to echo it in)")
            else:
                reasoning_parts.append(block.text)
        elif isinstance(block, ContentToolUse):  # pyright: ignore[reportUnnecessaryIsInstance]
            if isinstance(block, CacheableContentToolUse) and block.cache_control is not None:
                raise unadapted("a cache breakpoint on a tool call")
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": names.wire_name(block.name, block.namespace),
                        "arguments": json.dumps(block.input),
                    },
                }
            )
    param: dict[str, Any] = {"role": "assistant"}
    if text_parts:
        param["content"] = "".join(text_parts)
    if reasoning_parts and reasoning_echo_field is not None:
        param[reasoning_echo_field] = "".join(reasoning_parts)
    if reasoning_details:
        param["reasoning_details"] = reasoning_details
    if tool_calls:
        param["tool_calls"] = tool_calls
    if not text_parts and not tool_calls:
        # A reasoning-only turn has no text/tool content. Some providers (e.g. Moonshot) reject an
        # assistant message whose `content` is empty, so fall back to a single space rather than "".
        param["content"] = " " if reasoning_parts or reasoning_details else ""
    return param


def _render_context(
    context: tuple[ChatMessage, ...],
    forward_cache_control: bool,
    reasoning_echo_field: ReasoningEchoField | None,
    variant: ChatCompletionsVariant,
    names: ToolNames | None = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for message in context:
        if isinstance(message, SystemMessage):
            messages.append(
                {
                    "role": "system",
                    "content": _text_content_param(message.content, forward_cache_control),
                }
            )
        elif isinstance(message, UserMessage):
            messages.append(
                {
                    "role": "user",
                    "content": _text_content_param(message.content, forward_cache_control),
                }
            )
        elif isinstance(message, ToolMessage):
            if message.is_error is not None:
                raise unadapted("a tool result's error flag")
            if (
                isinstance(message, CacheableToolMessage) and message.cache_control is not None
            ) or any(
                isinstance(block, CacheableContentText) and block.cache_control is not None
                for block in message.content
            ):
                raise unadapted("a cache breakpoint on a tool result")
            messages.append(
                {
                    "role": "tool",
                    "content": "".join(block.text for block in message.content),
                    "tool_call_id": message.tool_call_id,
                }
            )
        else:
            messages.append(_assistant_message_param(message, reasoning_echo_field, variant, names))
    return messages


def _tool_param(tool: ToolParam, names: ToolNames | None = None) -> dict[str, Any]:
    # A freeform tool arrives with the one-string-field schema the vocabulary gives it, and a
    # grouped tool under its wire name (its own, unless two groups share it) with its group's
    # description folded into its own.
    if isinstance(tool, CacheableToolParam) and tool.cache_control is not None:
        raise unadapted("a cache breakpoint on a tool definition")
    function: dict[str, Any] = {
        "name": names.wire_name(tool.name, tool.namespace) if names is not None else tool.name,
        "parameters": copy.deepcopy(dict(tool.input_schema)),
    }
    description = flattened_description(tool)
    if description is not None:
        function["description"] = description
    if tool.strict == "auto":
        raise unadapted(f"tool {tool.name}'s automatic strictness")
    if tool.strict:
        function["strict"] = True
    if tool.output_schema is not None:
        raise unadapted(f"tool {tool.name}'s output schema")
    return {"type": "function", "function": function}


def _response_format_param(response_format: ResponseFormat) -> dict[str, Any]:
    """`response_format`: the Responses shape, with a schema's fields nested under
    `json_schema`."""
    flat = response_format_flat(response_format)
    if isinstance(response_format, ResponseFormatJsonSchema):
        return {
            "type": "json_schema",
            "json_schema": {k: v for k, v in flat.items() if k != "type"},
        }
    return flat


def _tool_choice_param(tool_choice: ToolChoice, names: ToolNames) -> Any:
    if isinstance(tool_choice, ToolChoiceAuto):
        return "auto"
    if isinstance(tool_choice, ToolChoiceRequired):
        return "required"
    if isinstance(tool_choice, ToolChoiceNone):
        return "none"
    return {"type": "function", "function": {"name": names.choice_wire_name(tool_choice.name)}}


def adapt_chat_completions_request(
    model: str,
    request: ModelRequest,
    accepts: ReasoningStateFilter,
    *,
    forward_cache_control: bool = False,
    reasoning_echo_field: ReasoningEchoField | None = "reasoning_content",
    variant: ChatCompletionsVariant = "standard",
) -> AdaptedRequest:
    """`request` changed so a chat completions endpoint configured this way can carry it, one
    loss per change.

    The dialect has no error flag on a tool result and no form for a reasoning summary or redaction
    without state. The model's own reasoning text is echoed back only under a
    `reasoning_echo_field`; reasoning state only in the `openrouter` variant, and only OpenRouter's
    for `model` (what `accepts`, the codec's `accepts_reasoning_state`, lets through). A reasoning
    budget is sent as the nearest effort level. Breakpoints are forwarded only with
    `forward_cache_control`, and then only on system and user text, with their TTLs fitted and
    ordered as Anthropic requires. A turn's tool results go first, as the API requires.
    """
    adaptation = AdaptedRequestBuilder(request)
    drop_foreign_extras(adaptation, "openai_chat_completions")
    drop_foreign_reasoning(adaptation, accepts)
    clear_tool_error_flags(adaptation)
    if reasoning_echo_field is None:
        drop_assistant_blocks(
            adaptation,
            lambda block: (
                isinstance(block, ContentReasoning)
                and block.reasoning_state is None
                and block.form == "full"
            ),
            "literal_reasoning",
            "reasoning text block(s) with no field to echo them in",
        )
    drop_assistant_blocks(
        adaptation,
        lambda block: (
            isinstance(block, ContentReasoning)
            and block.reasoning_state is None
            and block.form == "summary"
        ),
        "unsigned_reasoning",
        "reasoning summary block(s) with no state",
    )
    drop_assistant_blocks(
        adaptation,
        lambda block: (
            isinstance(block, ContentReasoning)
            and block.reasoning_state is None
            and block.form == "redacted"
        ),
        "redacted_reasoning",
        "redacted reasoning block(s) with no state",
    )
    note_flattened_tools(adaptation)
    budget_as_effort(adaptation)

    def unforwarded(value: object, container: ChatMessage | None) -> bool:
        forwarded = (
            forward_cache_control
            and isinstance(value, CacheableContentText)
            and bool(value.text)
            and isinstance(container, SystemMessage | UserMessage)
        )
        return not forwarded

    drop_breakpoints(
        adaptation,
        unforwarded,
        "outside system and user text"
        if forward_cache_control
        else "(this endpoint is not configured to forward them)",
    )
    fit_cache_ttls(adaptation, ANTHROPIC_CACHE_TTLS)
    order_cache_ttls(adaptation, ANTHROPIC_DEFAULT_CACHE_TTL)
    tool_results_first(adaptation)
    drop_non_object_response_format(adaptation)
    move_rejected_keywords(adaptation, openai_rejects)
    resolve_automatic_strictness(adaptation)
    move_strict_rejected_keywords(adaptation)
    unenforce_incompatible_response_format(adaptation)
    unenforce_incompatible_strict_tools(adaptation, strict_problems(None))
    drop_output_schemas(adaptation, "tool_output_schema", "no slot for it")
    return adaptation.result()


def encode_chat_completions_request(
    model: str,
    request: ModelRequest,
    *,
    streaming: bool,
    forward_cache_control: bool = False,
    max_tokens_param: MaxTokensParam = "max_tokens",
    reasoning_echo_field: ReasoningEchoField | None = "reasoning_content",
    extra_body: dict[str, Any] | None = None,
    variant: ChatCompletionsVariant = "standard",
) -> EncodedRequest:
    """Render a request as a `/v1/chat/completions` body.

    Everything the request asked for is rendered as asked; `extra_body` fills in keys the
    request left absent (a declared default, never an override).
    """
    sampling = request.sampling
    names = ToolNames(request.tools, flattened=True)
    body: dict[str, Any] = {
        "model": model,
        "messages": _render_context(
            request.context, forward_cache_control, reasoning_echo_field, variant, names
        ),
    }
    if request.tools:
        body["tools"] = [_tool_param(tool, names) for tool in request.tools]
    if request.tool_choice is not None:
        body["tool_choice"] = _tool_choice_param(request.tool_choice, names)
    if request.parallel_tool_calls is not None:
        body["parallel_tool_calls"] = request.parallel_tool_calls
    if sampling.max_tokens is not None:
        body[max_tokens_param] = sampling.max_tokens
    if sampling.temperature is not None:
        body["temperature"] = sampling.temperature
    if sampling.top_p is not None:
        body["top_p"] = sampling.top_p
    if sampling.stop_sequences:
        body["stop"] = list(sampling.stop_sequences)
    reasoning = sampling.reasoning
    if (
        reasoning is not None
        and reasoning.enabled
        and reasoning.effort is None
        and reasoning.budget_tokens is not None
    ):
        raise unadapted("a reasoning budget")
    effort = reasoning_effort(reasoning)
    if effort is not None:
        body["reasoning_effort"] = effort
    if sampling.speed is not None:
        body["service_tier"] = service_tier_param(sampling.speed)
    if request.response_format is not None:
        body["response_format"] = _response_format_param(request.response_format)
    if streaming:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    merge_provider_params(body, sampling.extra)
    headers, query = apply_dialect_extras(body, request.extras, "openai_chat_completions")
    apply_declared_defaults(body, extra_body or {})
    return EncodedRequest(
        path=_ENDPOINT, body=cast("JSONObject", body), query=query, headers=headers
    )


# --- response: chat completion chunks / body -> ModelResponse ---------------------------------


@dataclass
class _ToolCallState:
    """Accumulator for one streamed tool call, keyed by its choice index."""

    ordinal: int
    id: str | None = None
    name: str | None = None
    argument_parts: list[str] = field(default_factory=list[str])
    started: bool = False
    # Argument fragments that arrived before id/name were known, replayed
    # right after the deferred StreamToolUseStart.
    pending_argument_parts: list[str] = field(default_factory=list[str])


@dataclass
class _LiteralRun:
    """Consecutive literal reasoning text (the `reasoning`/`reasoning_content` string, or
    OpenRouter `reasoning.text` entries with no signature): one `full` `ContentReasoning` with no
    state."""

    parts: list[str] = field(default_factory=list[str])


@dataclass
class _DetailGroup:
    """A run of `reasoning_details` entries that one signature or encrypted payload seals: a
    signed thinking text on its own, or summaries followed by their encrypted item. One
    `ContentReasoning`, whose state is the entries (`OpenRouterReasoning`); `positions` index the
    native array. Its form follows the entry that begins it: `full` for `reasoning.text`, the
    upstream model's own thinking as OpenRouter relays it, and `summary` otherwise
    (`reasoning.summary`, or an encrypted item with no summary before it)."""

    form: ReasoningForm
    positions: list[int] = field(default_factory=list[int])
    state: DynamicData | None = None  # set once the group is complete, if sealed


def _detail_text(entry: dict[str, Any]) -> str:
    for key in ("text", "summary"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return ""


def _detail_sealed(entry: dict[str, Any]) -> bool:
    return bool(entry.get("signature")) or bool(entry.get("data"))


_DETAIL_STRING_FIELDS = ("text", "summary", "data", "signature")


class _ReasoningDetails:
    """OpenRouter's `reasoning_details` as the model produced them, assembled from a stream.

    The native array is kept whole and in order, which is what the model needs echoed back;
    what the entries mean for the vocabulary is decided by the assembler, which only ever works
    from entries this class has completed. A streamed fragment belongs to the entry with its
    type and `index`; with no `index`, to the entry with its type and `id`; with neither, to the
    open entry when that has its type and either the fragment carries no text (a signature or
    data continuation) or the entry is not yet sealed, and otherwise starts a new entry.
    Fragments of one entry concatenate their text, summary, data and signature.

    Entries complete in order. The providers behind OpenRouter stream reasoning as sequential
    blocks, so a fragment that begins a new entry completes the open one, and `complete_open`
    marks the boundaries the response itself provides (content, a tool call, the finish, the end
    of the stream). A fragment for an entry that is already complete breaks that contract and
    raises `CodecError`: the entry has been passed on, and a silent change would leave what was
    echoed different from what the model produced. A buffered response's array is already
    complete and is taken as it is.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._by_key: dict[tuple[str, str, object], int] = {}
        self._open: int | None = None

    def feed(self, fragment: dict[str, Any]) -> tuple[int, bool]:
        """Merge a streamed fragment; the entry's position, and whether the fragment began it."""
        kind = fragment.get("type")
        kind = kind if isinstance(kind, str) else ""
        index = fragment.get("index")
        ident = fragment.get("id")
        key: tuple[str, str, object] | None = None
        if isinstance(index, int) and not isinstance(index, bool):
            key = ("index", kind, index)
        elif isinstance(ident, str) and ident:
            key = ("id", kind, ident)
        position: int | None = None
        if key is not None:
            position = self._by_key.get(key)
        elif self._open is not None:
            open_entry = self.entries[self._open]
            if open_entry.get("type") == kind and (
                not _detail_text(fragment) or not _detail_sealed(open_entry)
            ):
                position = self._open
        if position is not None and position != self._open:
            entry = self.entries[position]
            raise CodecError(
                "OpenRouter sent a reasoning_details fragment for an entry that was already "
                f"complete (type {entry.get('type')!r}, index {entry.get('index')!r}, id "
                f"{entry.get('id')!r}) after a later entry or the response content had begun; "
                "the fragments of one entry are expected to arrive together"
            )
        new = position is None
        if position is None:
            position = len(self.entries)
            self.entries.append({})
            if key is not None:
                self._by_key[key] = position
            self._open = position
        entry = self.entries[position]
        for name, value in fragment.items():
            if name in _DETAIL_STRING_FIELDS and isinstance(value, str):
                previous = entry.get(name)
                entry[name] = (previous if isinstance(previous, str) else "") + value
            elif value is not None:
                entry[name] = value
        return position, new

    def complete_open(self) -> None:
        """A boundary the response provides (content, a tool call, the finish, the end of the
        stream): the open entry takes no more fragments."""
        self._open = None

    def load(self, entries: list[dict[str, Any]]) -> list[int]:
        """Take a buffered response's complete array as it is; the positions added."""
        first = len(self.entries)
        self.entries.extend(copy.deepcopy(entry) for entry in entries)
        return list(range(first, len(self.entries)))


class _CompletionAssembler:
    """Turns chat completion chunks (parsed JSON) into StreamEvents and a ModelResponse. The
    `openrouter` variant reads `reasoning_details`; the standard one reads only the literal
    reasoning string."""

    def __init__(self, variant: ChatCompletionsVariant = "standard") -> None:
        self._variant: ChatCompletionsVariant = variant
        self._response_id: str | None = None
        self._model = ""
        self._text_parts: list[str] = []
        self._refused = False
        self._details = _ReasoningDetails()
        self._reasoning: list[_LiteralRun | _DetailGroup] = []
        self._group: _DetailGroup | None = None  # the group still taking entries
        self._details_seen = False
        self._literal_positions: set[int] = set()
        self._tool_calls: dict[int, _ToolCallState] = {}
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] | None = None
        self._service_tier: object = None

    def _literal_delta(self, text: str) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        last = self._reasoning[-1] if self._reasoning else None
        if not isinstance(last, _LiteralRun):
            last = _LiteralRun()
            self._reasoning.append(last)
            out.append(StreamReasoningStart(form="full"))
        last.parts.append(text)
        out.append(StreamReasoningDelta(text=text))
        return out

    def _group_sealed(self, group: _DetailGroup) -> bool:
        return any(_detail_sealed(self._details.entries[p]) for p in group.positions)

    def _group_text(self, group: _DetailGroup) -> str:
        return "".join(_detail_text(self._details.entries[p]) for p in group.positions)

    def _finish_group(self) -> list[StreamEvent]:
        """The open group takes no more entries. If a signature or encrypted data sealed it, its
        entries, all complete by now, become its state and the state is emitted; otherwise it
        stays a block without state (summaries whose encrypted item never came)."""
        group, self._group = self._group, None
        if group is None or not self._group_sealed(group):
            return []
        group.state = DynamicData.from_tagged(
            OpenRouterReasoning(
                model=self._model,
                details=tuple(copy.deepcopy(self._details.entries[p]) for p in group.positions),
            )
        )
        return [StreamReasoningState(state=group.state)]

    def _boundary(self) -> list[StreamEvent]:
        """Content, a tool call, the finish, a trailing usage chunk or the end of the stream: the
        reasoning so far is complete, entries and group alike."""
        self._details.complete_open()
        return self._finish_group()

    def _detail_delta(self, fragment: dict[str, Any]) -> list[StreamEvent]:
        """A streamed `reasoning_details` fragment: assembled natively, then placed."""
        position, new = self._details.feed(fragment)
        return self._place(position, new, _detail_text(fragment))

    def _place(self, position: int, new: bool, text: str) -> list[StreamEvent]:
        """Give an assembled entry its place among the response's reasoning blocks: a plain text
        entry is literal reasoning, anything else joins the open group (or starts the next one
        when the open group is already sealed). A new entry is classified by what it holds when
        it begins; its later fragments follow it. A new entry of either kind that cannot join
        the open group finishes it first, since the entries before it are complete."""
        entry = self._details.entries[position]
        if position in self._literal_positions:
            if _detail_sealed(entry):
                _warn_dropped_reasoning(
                    "late-signature",
                    "ignoring a signature on reasoning text that arrived as plain text: the "
                    "text was already relayed as literal reasoning",
                )
            return self._literal_delta(text) if text else []
        out: list[StreamEvent] = []
        if new:
            plain = entry.get("type") == "reasoning.text" and entry.get("format") in (
                None,
                "unknown",
            )
            if plain and not _detail_sealed(entry):
                out.extend(self._finish_group())
                self._literal_positions.add(position)
                if text:
                    out.extend(self._literal_delta(text))
                return out
            if self._group is not None and self._group_sealed(self._group):
                out.extend(self._finish_group())
            if self._group is None:
                form: ReasoningForm = "full" if entry.get("type") == "reasoning.text" else "summary"
                self._group = _DetailGroup(form=form)
                self._reasoning.append(self._group)
                out.append(StreamReasoningStart(form=form))
            self._group.positions.append(position)
        if text:
            out.append(StreamReasoningDelta(text=text))
        return out

    def _tool_call_events(self, raw_call: dict[str, Any]) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        index = raw_call.get("index", 0)
        index = index if isinstance(index, int) and not isinstance(index, bool) else 0
        state = self._tool_calls.get(index)
        if state is None:
            state = _ToolCallState(ordinal=len(self._tool_calls))
            self._tool_calls[index] = state

        call_id = raw_call.get("id")
        if isinstance(call_id, str) and call_id:
            state.id = state.id or call_id
        function = _as_dict(raw_call.get("function"))
        name = function.get("name") if function is not None else None
        if isinstance(name, str) and name:
            state.name = (state.name or "") + name
        arguments = function.get("arguments") if function is not None else None

        if not state.started and state.name:
            # The id is provider-assigned but some compatible servers omit it;
            # synthesize a deterministic one so the start event (and the
            # assembled ContentToolUse) always carries an id.
            state.id = state.id or f"tool_call_{state.ordinal}"
            state.started = True
            out.append(StreamToolUseStart(index=state.ordinal, id=state.id, name=state.name))
            for pending in state.pending_argument_parts:
                out.append(StreamToolUseInputDelta(index=state.ordinal, partial_json=pending))
            state.pending_argument_parts.clear()

        if isinstance(arguments, str) and arguments:
            state.argument_parts.append(arguments)
            if state.started:
                out.append(StreamToolUseInputDelta(index=state.ordinal, partial_json=arguments))
            else:
                state.pending_argument_parts.append(arguments)
        return out

    def handle(self, chunk: dict[str, Any]) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        chunk_id = chunk.get("id")
        if isinstance(chunk_id, str) and chunk_id and self._response_id is None:
            self._response_id = chunk_id
        model = chunk.get("model")
        if isinstance(model, str) and model and not self._model:
            self._model = model
        usage = _as_dict(chunk.get("usage"))
        if usage is not None:
            self._usage = usage
        if chunk.get("service_tier") is not None:
            self._service_tier = chunk.get("service_tier")
        for choice in _dict_list(chunk.get("choices")):
            delta = _as_dict(choice.get("delta")) or {}
            details = (
                _dict_list(delta.get("reasoning_details")) if self._variant == "openrouter" else []
            )
            if details:
                self._details_seen = True
                for detail in details:
                    out.extend(self._detail_delta(detail))
            elif not self._details_seen:
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    out.extend(self._literal_delta(reasoning))
            content = delta.get("content")
            if isinstance(content, str) and content:
                out.extend(self._boundary())
                self._text_parts.append(content)
                out.append(StreamTextDelta(text=content))
            refusal = delta.get("refusal")
            if isinstance(refusal, str) and refusal:
                # The model's refusal in place of the answer: text, which the stop reason marks.
                out.extend(self._boundary())
                self._text_parts.append(refusal)
                self._refused = True
                out.append(StreamTextDelta(text=refusal))
            raw_calls = _dict_list(delta.get("tool_calls"))
            if raw_calls:
                out.extend(self._boundary())
            for raw_call in raw_calls:
                out.extend(self._tool_call_events(raw_call))
            finish_reason = choice.get("finish_reason")
            if isinstance(finish_reason, str):
                out.extend(self._boundary())
                self._finish_reason = finish_reason
        if not chunk.get("choices"):
            out.extend(self._boundary())  # a trailing usage chunk ends the reasoning too
        return out

    def flush(self) -> list[StreamEvent]:
        """The stream has ended: complete whatever reasoning is still open."""
        return self._boundary()

    def handle_message(self, message: dict[str, Any], finish_reason: Any) -> None:
        """Load a complete (non-streamed) choice message."""
        details = (
            _dict_list(message.get("reasoning_details")) if self._variant == "openrouter" else []
        )
        if details:
            for position in self._details.load(details):
                self._place(position, True, _detail_text(self._details.entries[position]))
        else:
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                self._literal_delta(reasoning)
        content = message.get("content")
        if isinstance(content, str) and content:
            self._text_parts.append(content)
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal:
            self._text_parts.append(refusal)
            self._refused = True
        for ordinal, raw_call in enumerate(_dict_list(message.get("tool_calls"))):
            function = _as_dict(raw_call.get("function")) or {}
            call_id = raw_call.get("id")
            name = function.get("name")
            arguments = function.get("arguments")
            self._tool_calls[ordinal] = _ToolCallState(
                ordinal=ordinal,
                id=call_id if isinstance(call_id, str) else None,
                name=name if isinstance(name, str) else None,
                argument_parts=[arguments] if isinstance(arguments, str) else [],
                started=True,
            )
        if isinstance(finish_reason, str):
            self._finish_reason = finish_reason

    def _stop(self) -> StopReason:
        if self._refused and self._finish_reason in ("stop", None):
            return StopRefusal(provider_reason="refusal", complete=True)
        if self._finish_reason == "stop":
            # OpenAI reports finish_reason "stop" (not "tool_calls") when the
            # tool call was forced via tool_choice; a response that called
            # tools stopped to call them regardless.
            if self._tool_calls:
                return StopToolUse()
            return StopEndTurn()
        if self._finish_reason == "length":
            return StopMaxTokens()
        if self._finish_reason in ("tool_calls", "function_call"):
            return StopToolUse()
        if self._finish_reason == "content_filter":
            return StopRefusal(provider_reason="content_filter", complete=False)
        return StopOther(provider_reason=self._finish_reason or "no finish_reason reported")

    def _assembled_usage(self) -> Usage:
        usage = self._usage
        if usage is None:
            return Usage(
                input_tokens=0,
                cache_read_input_tokens=None,
                cache_write_input_tokens=None,
                output_tokens=0,
                reasoning_output_tokens=None,
                speed=service_tier_speed(self._service_tier),
            )
        prompt_details = _as_dict(usage.get("prompt_tokens_details")) or {}
        cache_read = _int_or_none(usage.get("cache_read_input_tokens"))
        if cache_read is None:
            cache_read = _int_or_none(prompt_details.get("cached_tokens"))
        # OpenAI's `prompt_tokens` is the total: the tokens read from and written to the cache
        # are subsets of it, while Usage.input_tokens is what was neither. An Anthropic-style
        # `cache_creation_input_tokens` beside `prompt_tokens` is a separate count.
        cache_write_within = _int_or_none(prompt_details.get("cache_write_tokens"))
        cache_write = _int_or_none(usage.get("cache_creation_input_tokens"))
        if cache_write is None:
            cache_write = cache_write_within
        reasoning_tokens: int | None = None
        completion_details = _as_dict(usage.get("completion_tokens_details"))
        if completion_details is not None:
            reasoning_tokens = _int_or_none(completion_details.get("reasoning_tokens"))
        prompt_tokens = _int_or_none(usage.get("prompt_tokens")) or 0
        return Usage(
            input_tokens=max(prompt_tokens - (cache_read or 0) - (cache_write_within or 0), 0),
            cache_read_input_tokens=cache_read,
            cache_write_input_tokens=cache_write,
            output_tokens=_int_or_none(usage.get("completion_tokens")) or 0,
            reasoning_output_tokens=reasoning_tokens,
            speed=service_tier_speed(self._service_tier),
            billed_cost_usd=_float_or_none(usage.get("cost")),
        )

    def finish(self) -> ModelResponse | ModelCallFailure:
        if self._response_id is None:
            return ModelCallFailure(
                category="server_error",
                description="chat completions stream ended without producing any chunks",
                provider_http_status=None,
            )
        self._boundary()
        content: list[Content] = []
        for unit in self._reasoning:
            if isinstance(unit, _LiteralRun):
                text = "".join(unit.parts)
                if text:
                    content.append(ContentReasoning(text=text, form="full"))
            elif unit.state is not None or self._group_text(unit):
                # Sealed: the entries are the state. Unsealed (summaries whose encrypted item
                # never came): the text alone.
                content.append(
                    ContentReasoning(
                        text=self._group_text(unit), form=unit.form, reasoning_state=unit.state
                    )
                )
        if self._text_parts:
            content.append(ContentText(text="".join(self._text_parts)))
        for state in sorted(self._tool_calls.values(), key=lambda s: s.ordinal):
            raw = "".join(state.pending_argument_parts + state.argument_parts) or "{}"
            parse_error: str | None = None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                parse_error = str(exc)
                parsed = {}
            if not isinstance(parsed, dict):
                parse_error = "tool call arguments decoded to a non-object JSON value"
                parsed = {}
            content.append(
                ContentToolUse(
                    id=state.id or f"tool_call_{state.ordinal}",
                    name=state.name or "",
                    input=cast(dict[str, Any], parsed),
                    parse_error=parse_error,
                )
            )
        return ModelResponse(
            id=self._response_id,
            output=(AssistantMessage(content=tuple(content)),),
            stop_reason=self._stop(),
            usage=self._assembled_usage(),
        )


class ChatCompletionsSseDecoder:
    """Decodes a chat completions SSE stream (`data:` chunks, `[DONE]` sentinel) into stream
    events and the assembled response. An error object delivered in a chunk (OpenRouter's way of
    reporting a failed provider inside a 200) ends the stream as a failure."""

    def __init__(self, variant: ChatCompletionsVariant = "standard") -> None:
        self._frames = SseFrameParser()
        self._assembler = _CompletionAssembler(variant)
        self._done = False
        self._failure: ModelCallFailure | None = None

    def _consume(self, frames: list[tuple[str | None, str]]) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for _event, data in frames:
            if self._failure is not None:
                continue
            if data.strip() == "[DONE]":
                self._done = True
                events.extend(self._assembler.flush())
                continue
            payload = decode_json_object(data, "chat completion chunk")
            error = _as_dict(payload.get("error"))
            if error is not None:
                self._failure = failure_from_error_object(error)
                continue
            events.extend(self._assembler.handle(payload))
        return events

    def feed(self, chunk: bytes) -> list[StreamEvent]:
        return self._consume(self._frames.feed(chunk))

    def complete(self) -> bool:
        return self._done or self._failure is not None

    def finish(self) -> ModelResponse | ModelCallFailure:
        self._consume(self._frames.finish())
        if self._failure is not None:
            return self._failure
        return self._assembler.finish()

    def raw_message(self) -> JSONObject | None:
        # Chat completions has no harness side to compare a raw view against, so none is assembled.
        return None


def decode_chat_completion(
    body: bytes, variant: ChatCompletionsVariant = "standard"
) -> ModelResponse | ModelCallFailure:
    """A complete (non-streamed) chat completion body as a ModelResponse."""
    payload = decode_json_object(body, "chat completion body")
    error = _as_dict(payload.get("error"))
    if error is not None:
        return failure_from_error_object(error)
    assembler = _CompletionAssembler(variant)
    assembler.handle(
        {
            "id": payload.get("id"),
            "model": payload.get("model"),
            "usage": payload.get("usage"),
            "service_tier": payload.get("service_tier"),
        }
    )
    choices = _dict_list(payload.get("choices"))
    if choices:
        message = _as_dict(choices[0].get("message")) or {}
        assembler.handle_message(message, choices[0].get("finish_reason"))
    return assembler.finish()


def decode_chat_completions_failure(status: int, body: bytes) -> ModelCallFailure:
    return decode_failure_body(status, body)


class OpenAIChatCompletionsCodec:
    """The chat completions dialect, model side.

    Args:
        forward_cache_control: Forward `CacheableContentText` breakpoints as `cache_control`
            parts (for endpoints that honor them, e.g. OpenRouter serving Anthropic models).
        max_tokens_param: Which parameter carries the output-token limit.
        reasoning_echo_field: The assistant-message field literal reasoning is echoed in, or
            None to drop it.
        extra_body: Provider parameters filled in where a request leaves the key absent.
        variant: The wire contract: `standard` (the default) or `openrouter` (see the module
            docstring).
    """

    dialect: Dialect = "openai_chat_completions"

    def __init__(
        self,
        *,
        forward_cache_control: bool = False,
        max_tokens_param: MaxTokensParam = "max_tokens",
        reasoning_echo_field: ReasoningEchoField | None = "reasoning_content",
        extra_body: dict[str, Any] | None = None,
        variant: ChatCompletionsVariant = "standard",
    ) -> None:
        self._forward_cache_control = forward_cache_control
        self._max_tokens_param: MaxTokensParam = max_tokens_param
        self._reasoning_echo_field: ReasoningEchoField | None = reasoning_echo_field
        self._extra_body = dict(extra_body) if extra_body else {}
        self._variant: ChatCompletionsVariant = variant

    @property
    def variant(self) -> ChatCompletionsVariant:
        return self._variant

    def adapt_request(self, model: str, request: ModelRequest) -> AdaptedRequest:
        return adapt_chat_completions_request(
            model,
            request,
            lambda state: self.accepts_reasoning_state(state, model),
            forward_cache_control=self._forward_cache_control,
            reasoning_echo_field=self._reasoning_echo_field,
            variant=self._variant,
        )

    def encode_request(
        self, model: str, request: ModelRequest, *, streaming: bool
    ) -> EncodedRequest:
        return encode_chat_completions_request(
            model,
            request,
            streaming=streaming,
            forward_cache_control=self._forward_cache_control,
            max_tokens_param=self._max_tokens_param,
            reasoning_echo_field=self._reasoning_echo_field,
            extra_body=self._extra_body,
            variant=self._variant,
        )

    def sse_decoder(self) -> SseDecoder:
        return ChatCompletionsSseDecoder(self._variant)

    def decode_response(self, body: bytes) -> ModelResponse | ModelCallFailure:
        return decode_chat_completion(body, self._variant)

    def decode_failure(self, status: int, body: bytes) -> ModelCallFailure:
        return decode_chat_completions_failure(status, body)

    def accepts_reasoning_state(self, state: DynamicData, model: str) -> bool:
        """In the `openrouter` variant, OpenRouter `reasoning_details` that `model` produced (a
        routing suffix such as `:nitro` aside); in the standard variant, nothing."""
        if self._variant != "openrouter":
            return False
        details = own_reasoning_state(state, OpenRouterReasoning)
        return details is not None and same_openrouter_model(details.model, model)


# --- helpers -----------------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any] | None:
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [cast(dict[str, Any], item) for item in cast(list[Any], value) if isinstance(item, dict)]


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
