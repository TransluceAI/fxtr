"""Anthropic Messages API implementation of ``StreamingModelAPI``.

Requests always stream under the hood (Anthropic recommends streaming for
long-running requests); ``generate`` is ``generate_with_events`` with no
callback. Failures are returned as ``ModelCallFailure`` data, never raised,
and there are no internal retries (the SDK's own retries are disabled,
including on a client passed in).

Rendering notes:

- Cache breakpoints are taken from ``CacheableContentText`` /
  ``CacheableToolParam`` blocks (see ``behaviors.types.content`` and
  ``behaviors.types.tool``); a breakpoint on an empty text block is dropped
  because the API rejects it.
  With ``final_block_cache_control`` set, the request's final content block
  also gets that breakpoint if it is a non-empty text or tool-result block
  the caller left unmarked — one trailing breakpoint per request, so each
  turn of a conversation reads the prefix the previous turn wrote. Adding it
  raises ``ModelError`` if the request would exceed Anthropic's breakpoint
  limit or put a 1-hour breakpoint after a 5-minute one.
- ``SystemMessage`` content is hoisted into the top-level ``system``
  parameter regardless of its position in the context.
- Consecutive user-role messages (``UserMessage`` / ``ToolMessage``) merge
  into one API message, the inverse of how tool results fan out into
  individual ``ToolMessage``s on ingest.
- Assistant reasoning is sent back only as ``ContentSummarizedReasoning``
  with a signature (an Anthropic ``thinking`` block) or
  ``ContentRedactedReasoning``; the API has no representation for
  signature-less or literal reasoning, so those blocks are omitted.
- ``SamplingParams.reasoning`` maps to the ``thinking`` configuration
  (disabled / enabled-with-budget / adaptive) and, when an effort level is
  given, to ``output_config.effort`` (``minimal`` clamps to ``low``, the rest
  of the neutral vocabulary is Anthropic's own). Adaptive thinking (enabled
  with no budget) exists only on newer models — older ones 400 on it, so give
  those a budget. Enabled thinking always carries ``thinking_display``
  (default ``"summarized"``): newer models default to ``"omitted"``, which
  streams no thinking text.
- ``temperature``, ``top_p``, and ``SamplingParams.extra`` are sent unchanged
  in the request body for Anthropic to validate; ``extra`` keys take precedence
  over the typed fields. Unset typed fields are omitted; ``extra`` values are
  preserved verbatim, including explicit JSON nulls. The one exception is
  ``extra["top_k"]``, which must be an int (``ModelError`` otherwise) and is
  omitted when None.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import anthropic
import httpx2
from anthropic import AsyncAnthropic, Omit, omit
from anthropic.types import (
    CacheControlEphemeralParam,
    MessageParam,
    OutputConfigParam,
    RawMessageStreamEvent,
    TextBlockParam,
    ThinkingConfigParam,
    ToolChoiceParam,
)
from anthropic.types import ToolParam as AnthropicToolParam

from behaviors.implementations.models._client_misuse import raise_if_client_misused
from behaviors.interfaces.model_apis import ModelError
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import (
    CacheableContentText,
    Content,
    ContentRedactedReasoning,
    ContentSummarizedReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.failure import FailureCategory, ModelCallFailure
from behaviors.types.json import JSONObject
from behaviors.types.message import (
    AssistantMessage,
    ChatMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ReasoningParams,
    SamplingParams,
    StopContentFilter,
    StopEndTurn,
    StopMaxTokens,
    StopOther,
    StopReason,
    StopStopSequence,
    StopToolUse,
    Usage,
)
from behaviors.types.streaming import (
    StreamEvent,
    StreamEventCallback,
    StreamSignatureDelta,
    StreamSummarizedReasoningDelta,
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
    ToolChoiceTool,
    ToolParam,
)

logger = logging.getLogger(__name__)

_MAX_FAILURE_DESCRIPTION = 500

FAST_MODE_BETA = "fast-mode-2026-02-01"


ThinkingDisplay = Literal["summarized", "omitted"]
"""Whether enabled thinking streams its (summarized) text or only signed, empty blocks."""


def _sampling_body(sampling: SamplingParams) -> JSONObject:
    """Set typed sampling fields overlaid by verbatim provider-specific ``extra`` values.

    They travel in ``extra_body`` because SDK 1.x does not take
    ``temperature``, ``top_p``, or ``top_k`` as keyword arguments to
    ``messages.create``.
    """
    extra = dict(sampling.extra)
    top_k = extra.pop("top_k", None)
    if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int)):
        raise ModelError(f"SamplingParams.extra['top_k'] must be an int, got {top_k!r}")
    typed: JSONObject = {
        "temperature": sampling.temperature,
        "top_p": sampling.top_p,
        "top_k": top_k,
    }
    return {name: value for name, value in typed.items() if value is not None} | extra


def _thinking_param(
    reasoning: ReasoningParams | None, display: ThinkingDisplay
) -> ThinkingConfigParam | Omit:
    """The `thinking` request parameter for a neutral reasoning config."""
    if reasoning is None:
        return omit
    if not reasoning.enabled:
        return {"type": "disabled"}
    if reasoning.budget_tokens is not None:
        return {"type": "enabled", "budget_tokens": reasoning.budget_tokens, "display": display}
    return {"type": "adaptive", "display": display}


def _output_config_param(reasoning: ReasoningParams | None) -> OutputConfigParam | Omit:
    """The `output_config` request parameter carrying the effort level, if one was asked for."""
    if reasoning is None or not reasoning.enabled or reasoning.effort is None:
        return omit
    # "minimal" is the one neutral level Anthropic doesn't offer; "low" is its nearest.
    effort = "low" if reasoning.effort == "minimal" else reasoning.effort
    return {"effort": effort}


def _cache_control_param(cache_control: CacheControl) -> CacheControlEphemeralParam:
    param: dict[str, Any] = {"type": cache_control.type}
    if cache_control.ttl is not None:
        param["ttl"] = cache_control.ttl
    return cast(CacheControlEphemeralParam, param)


def _text_block_param(block: ContentText) -> TextBlockParam:
    param: dict[str, Any] = {"type": "text", "text": block.text}
    if (
        isinstance(block, CacheableContentText)
        and block.cache_control is not None
        # The API rejects cache_control on an empty text block.
        and block.text
    ):
        param["cache_control"] = _cache_control_param(block.cache_control)
    return cast(TextBlockParam, param)


def _assistant_block_params(message: AssistantMessage) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ContentText):
            blocks.append(dict(_text_block_param(block)))
        elif isinstance(block, ContentSummarizedReasoning):
            if block.signature is not None:
                blocks.append(
                    {"type": "thinking", "thinking": block.text, "signature": block.signature}
                )
        elif isinstance(block, ContentRedactedReasoning):
            blocks.append({"type": "redacted_thinking", "data": block.data})
        elif isinstance(block, ContentToolUse):
            blocks.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                }
            )
        # ContentReasoning (literal reasoning) has no Anthropic representation
        # and is omitted; see the module docstring.
    return blocks


def _tool_result_block_param(message: ToolMessage) -> dict[str, Any]:
    param: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id,
        "content": [dict(_text_block_param(block)) for block in message.content],
    }
    if message.is_error is not None:
        param["is_error"] = message.is_error
    return param


def _render_context(
    context: tuple[ChatMessage, ...],
) -> tuple[list[TextBlockParam] | Omit, list[MessageParam]]:
    """Render messages into the top-level ``system`` parameter and the messages list."""
    system_blocks: list[TextBlockParam] = []
    messages: list[MessageParam] = []
    # Blocks of the current run of user-role messages, merged into one API
    # message when the run ends.
    user_blocks: list[dict[str, Any]] = []

    def flush_user_blocks() -> None:
        if user_blocks:
            messages.append(cast(MessageParam, {"role": "user", "content": list(user_blocks)}))
            user_blocks.clear()

    for message in context:
        if isinstance(message, SystemMessage):
            system_blocks.extend(_text_block_param(block) for block in message.content)
        elif isinstance(message, UserMessage):
            user_blocks.extend(dict(_text_block_param(block)) for block in message.content)
        elif isinstance(message, ToolMessage):
            user_blocks.append(_tool_result_block_param(message))
        else:
            flush_user_blocks()
            messages.append(
                cast(
                    MessageParam,
                    {"role": "assistant", "content": _assistant_block_params(message)},
                )
            )
    flush_user_blocks()

    return (system_blocks if system_blocks else omit), messages


# Anthropic's breakpoint rules, from
# https://platform.claude.com/docs/en/build-with-claude/prompt-caching: at most four breakpoints
# per request, and a longer TTL may not follow a shorter one in render order (tools, system,
# messages); a breakpoint without a TTL is 5 minutes. They are checked only for the breakpoint
# `_add_final_block_cache_control` adds, and must be updated here if Anthropic changes them.
_MAX_CACHE_BREAKPOINTS = 4
_CACHE_TTL_RANKS = {"5m": 0, "1h": 1}
_DEFAULT_CACHE_TTL = "5m"


def _breakpoint_ttls(blocks: list[dict[str, Any]]) -> list[str]:
    """The TTL of each breakpoint in ``blocks``, in order, including tool-result contents."""
    ttls: list[str] = []
    for block in blocks:
        cache_control = block.get("cache_control")
        if cache_control is not None:
            ttls.append(cast(dict[str, Any], cache_control).get("ttl", _DEFAULT_CACHE_TTL))
        if block.get("type") == "tool_result":
            ttls.extend(_breakpoint_ttls(cast(list[dict[str, Any]], block["content"])))
    return ttls


def _add_final_block_cache_control(
    tools: list[AnthropicToolParam],
    system: list[TextBlockParam] | Omit,
    messages: list[MessageParam],
    cache_control: CacheControl,
) -> None:
    """Put ``cache_control`` on the request's final block, if it is eligible and unmarked.

    Eligible blocks are non-empty text and tool results. Raises ``ModelError`` if the added
    breakpoint would break Anthropic's breakpoint rules (listed above ``_MAX_CACHE_BREAKPOINTS``),
    since the resulting 400 would repeat on every request built the same way.
    """
    if not messages:
        return
    final_content = cast(list[dict[str, Any]], messages[-1]["content"])
    if not final_content:
        return
    final_block = final_content[-1]
    eligible = final_block["type"] == "tool_result" or (
        final_block["type"] == "text" and final_block["text"]
    )
    if not eligible or "cache_control" in final_block:
        return

    rendered_blocks = [
        *cast(list[dict[str, Any]], tools),
        *(cast(list[dict[str, Any]], system) if not isinstance(system, Omit) else []),
        *(
            block
            for message in messages
            for block in cast(list[dict[str, Any]], message["content"])
        ),
    ]
    existing_ttls = _breakpoint_ttls(rendered_blocks)
    if len(existing_ttls) >= _MAX_CACHE_BREAKPOINTS:
        raise ModelError(
            f"final_block_cache_control would add a breakpoint to a request that already has "
            f"{len(existing_ttls)}; Anthropic allows at most {_MAX_CACHE_BREAKPOINTS}"
        )
    ttl = cache_control.ttl or _DEFAULT_CACHE_TTL
    rank = _CACHE_TTL_RANKS.get(ttl)
    for existing in existing_ttls:
        existing_rank = _CACHE_TTL_RANKS.get(existing)
        # TTLs outside the known ranks are left for the API to judge.
        if rank is not None and existing_rank is not None and existing_rank < rank:
            raise ModelError(
                f"final_block_cache_control has TTL {ttl!r} but an earlier breakpoint has TTL "
                f"{existing!r}; Anthropic requires longer TTLs to precede shorter ones"
            )
    final_block["cache_control"] = _cache_control_param(cache_control)


def _tool_param(tool: ToolParam) -> AnthropicToolParam:
    param: dict[str, Any] = {"name": tool.name, "input_schema": dict(tool.input_schema)}
    if tool.description is not None:
        param["description"] = tool.description
    if tool.strict is not None:
        param["strict"] = tool.strict
    if tool.input_examples is not None:
        param["input_examples"] = [dict(example) for example in tool.input_examples]
    if isinstance(tool, CacheableToolParam) and tool.cache_control is not None:
        param["cache_control"] = _cache_control_param(tool.cache_control)
    return cast(AnthropicToolParam, param)


def _tool_choice_param(
    tool_choice: ToolChoice | None, parallel_tool_calls: bool | None, has_tools: bool
) -> ToolChoiceParam | Omit:
    param: dict[str, Any] | None = None
    if isinstance(tool_choice, ToolChoiceAuto):
        param = {"type": "auto"}
    elif isinstance(tool_choice, ToolChoiceRequired):
        param = {"type": "any"}
    elif isinstance(tool_choice, ToolChoiceTool):
        param = {"type": "tool", "name": tool_choice.name}
    elif isinstance(tool_choice, ToolChoiceNone):
        param = {"type": "none"}
    if parallel_tool_calls is not None and has_tools:
        # Anthropic expresses parallel tool use as an inverted flag on the
        # tool_choice object (not valid on "none").
        if param is None:
            param = {"type": "auto"}
        if param["type"] != "none":
            param["disable_parallel_tool_use"] = not parallel_tool_calls
    if param is None:
        return omit
    return cast(ToolChoiceParam, param)


@dataclass
class _BlockState:
    """Accumulator for one content block of the streamed response."""

    kind: str
    text_parts: list[str] = field(default_factory=list[str])
    signature_parts: list[str] = field(default_factory=list[str])
    json_parts: list[str] = field(default_factory=list[str])
    redacted_data: str = ""
    tool_id: str = ""
    tool_name: str = ""


_warned_delta_types: set[str] = set()


class _StreamAssembler:
    """Turns raw Anthropic stream events into StreamEvents and a ModelResponse."""

    def __init__(self) -> None:
        self._message_id: str | None = None
        self._usage: dict[str, int] = {}
        self._stop_reason: str | None = None
        self._stop_sequence: str | None = None
        self._blocks: dict[int, _BlockState] = {}
        self._block_order: list[int] = []
        self._tool_ordinals: dict[int, int] = {}

    def _add_block(self, index: int, state: _BlockState) -> None:
        self._blocks[index] = state
        self._block_order.append(index)

    def _overwrite_usage(self, usage_fields: dict[str, Any]) -> None:
        # Anthropic streams usage as successive snapshots, not increments.
        for key, value in usage_fields.items():
            if isinstance(value, int):
                self._usage[key] = value
        details = usage_fields.get("output_tokens_details")
        if isinstance(details, dict):
            thinking_tokens = cast(dict[str, Any], details).get("thinking_tokens")
            if isinstance(thinking_tokens, int):
                self._usage["thinking_tokens"] = thinking_tokens

    def handle(self, event: RawMessageStreamEvent) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        if event.type == "message_start":
            self._message_id = event.message.id
            self._overwrite_usage(event.message.usage.model_dump())
        elif event.type == "content_block_start":
            block = event.content_block
            if block.type == "text":
                self._add_block(event.index, _BlockState(kind="text", text_parts=[block.text]))
                if block.text:
                    out.append(StreamTextDelta(text=block.text))
            elif block.type == "thinking":
                self._add_block(
                    event.index,
                    _BlockState(
                        kind="thinking",
                        text_parts=[block.thinking],
                        signature_parts=[block.signature] if block.signature else [],
                    ),
                )
                if block.thinking:
                    out.append(StreamSummarizedReasoningDelta(text=block.thinking))
            elif block.type == "redacted_thinking":
                self._add_block(
                    event.index, _BlockState(kind="redacted_thinking", redacted_data=block.data)
                )
            elif block.type == "tool_use":
                ordinal = len(self._tool_ordinals)
                self._tool_ordinals[event.index] = ordinal
                self._add_block(
                    event.index,
                    _BlockState(kind="tool_use", tool_id=block.id, tool_name=block.name),
                )
                out.append(StreamToolUseStart(index=ordinal, id=block.id, name=block.name))
            else:
                logger.warning("Ignoring unsupported Anthropic content block type %r", block.type)
                self._add_block(event.index, _BlockState(kind="ignored"))
        elif event.type == "content_block_delta":
            delta = event.delta
            state = self._blocks.get(event.index)
            if state is None:
                logger.warning("Ignoring delta for unknown content block index %d", event.index)
            elif delta.type == "text_delta":
                state.text_parts.append(delta.text)
                out.append(StreamTextDelta(text=delta.text))
            elif delta.type == "thinking_delta":
                state.text_parts.append(delta.thinking)
                out.append(StreamSummarizedReasoningDelta(text=delta.thinking))
            elif delta.type == "signature_delta":
                state.signature_parts.append(delta.signature)
                out.append(StreamSignatureDelta(signature=delta.signature))
            elif delta.type == "input_json_delta":
                state.json_parts.append(delta.partial_json)
                out.append(
                    StreamToolUseInputDelta(
                        index=self._tool_ordinals.get(event.index, 0),
                        partial_json=delta.partial_json,
                    )
                )
            elif delta.type not in _warned_delta_types:
                # Once per type: progress-style deltas (e.g. thinking token counts under a beta)
                # arrive many times per response.
                _warned_delta_types.add(delta.type)
                logger.warning("Ignoring unsupported Anthropic delta type %r", delta.type)
        elif event.type == "message_delta":
            if event.delta.stop_reason is not None:
                self._stop_reason = event.delta.stop_reason
            if event.delta.stop_sequence is not None:
                self._stop_sequence = event.delta.stop_sequence
            self._overwrite_usage(event.usage.model_dump())
        return out

    def _stop(self) -> StopReason:
        if self._stop_reason == "end_turn":
            return StopEndTurn()
        if self._stop_reason == "max_tokens":
            return StopMaxTokens()
        if self._stop_reason == "stop_sequence":
            return StopStopSequence(stop_sequence=self._stop_sequence or "")
        if self._stop_reason == "tool_use":
            return StopToolUse()
        if self._stop_reason == "refusal":
            return StopContentFilter(provider_reason="refusal")
        return StopOther(provider_reason=self._stop_reason or "no stop_reason reported")

    def _content(self, state: _BlockState) -> Content | None:
        if state.kind == "text":
            return ContentText(text="".join(state.text_parts))
        if state.kind == "thinking":
            return ContentSummarizedReasoning(
                text="".join(state.text_parts),
                signature="".join(state.signature_parts) or None,
            )
        if state.kind == "redacted_thinking":
            return ContentRedactedReasoning(data=state.redacted_data)
        if state.kind == "tool_use":
            raw = "".join(state.json_parts) or "{}"
            parse_error: str | None = None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                parse_error = str(exc)
                parsed = {}
            if not isinstance(parsed, dict):
                parse_error = "tool call arguments decoded to a non-object JSON value"
                parsed = {}
            return ContentToolUse(
                id=state.tool_id,
                name=state.tool_name,
                input=cast(dict[str, Any], parsed),
                parse_error=parse_error,
            )
        return None

    def finish(self) -> ModelResponse:
        if self._message_id is None:
            raise ModelError("Anthropic stream ended before a message_start event")
        content: list[Content] = []
        for index in self._block_order:
            block = self._content(self._blocks[index])
            if block is not None:
                content.append(block)
        usage = Usage(
            input_tokens=self._usage.get("input_tokens", 0),
            cache_read_input_tokens=self._usage.get("cache_read_input_tokens"),
            cache_write_input_tokens=self._usage.get("cache_creation_input_tokens"),
            output_tokens=self._usage.get("output_tokens", 0),
            reasoning_output_tokens=self._usage.get("thinking_tokens"),
        )
        return ModelResponse(
            id=self._message_id,
            output=(AssistantMessage(content=tuple(content)),),
            stop_reason=self._stop(),
            usage=usage,
        )


def _failure_from_exception(exc: anthropic.APIError | httpx2.HTTPError) -> ModelCallFailure:
    description = str(exc)[:_MAX_FAILURE_DESCRIPTION]
    if not isinstance(exc, anthropic.APIStatusError):
        if isinstance(exc, anthropic.APIConnectionError):
            raise_if_client_misused(exc)
        # Connection failures and timeouts (anthropic.APIConnectionError, raw
        # httpx2 transport errors escaping the SDK mid-stream): the provider is
        # unreachable or unresponsive, which consumers treat like an overload.
        return ModelCallFailure(
            category="overloaded", description=description, provider_http_status=None
        )
    status = exc.status_code
    lower = description.lower()
    body: object = exc.body
    if isinstance(body, dict):
        error = cast(dict[str, Any], body).get("error")
        if isinstance(error, dict):
            lower += " " + str(cast(dict[str, Any], error).get("message") or "").lower()
    category: FailureCategory
    if status == 400 and ("prompt is too long" in lower or "too many tokens" in lower):
        category = "context_length"
    elif status == 400 and "content filtering" in lower:
        category = "content_filter"
    elif status in (401, 403):
        category = "authentication"
    elif status == 429:
        category = "rate_limited"
    # The async client surfaces 529 as InternalServerError, so the status code
    # (not the exception class) is what identifies an overload.
    elif status == 529 or "overloaded" in lower:
        category = "overloaded"
    elif 400 <= status < 500:
        category = "invalid_request"
    else:
        category = "server_error"
    return ModelCallFailure(category=category, description=description, provider_http_status=status)


async def _noop_callback(event: StreamEvent) -> None:
    pass


class AnthropicMessagesAPI:
    """A ``StreamingModelAPI`` serving one Anthropic model.

    Attributes are fixed at construction: the model, the client (or the
    credentials to build one), the ``max_tokens`` used when a request's
    sampling params don't specify one (the API requires a value), the
    breakpoint placed on each request's final block, if any, the display
    mode sent with enabled thinking (see the module docstring for both), and
    whether to ask for fast mode.
    """

    def __init__(
        self,
        model: str,
        *,
        client: AsyncAnthropic | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        default_max_tokens: int = 16384,
        final_block_cache_control: CacheControl | None = None,
        thinking_display: ThinkingDisplay = "summarized",
        fast_mode: bool = False,
    ):
        """``fast_mode`` asks for fast-mode inference (``speed: "fast"``, under its beta) on every
        request; a model that doesn't offer it rejects the request."""
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self._model = model
        self._owns_client = client is None
        self._client = (
            client.with_options(max_retries=0)
            if client is not None
            else AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=0)
        )
        self._default_max_tokens = default_max_tokens
        self._final_block_cache_control = final_block_cache_control
        self._thinking_display: ThinkingDisplay = thinking_display
        self._fast_mode = fast_mode

    async def aclose(self) -> None:
        """Release the underlying client, if this instance constructed it."""
        if self._owns_client:
            await self._client.close()

    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        return await self.generate_with_events(request, _noop_callback)

    async def generate_with_events(
        self, request: ModelRequest, callback: StreamEventCallback
    ) -> ModelResponse | ModelCallFailure:
        system, messages = _render_context(request.context)
        tools = [_tool_param(tool) for tool in request.tools]
        if self._final_block_cache_control is not None:
            _add_final_block_cache_control(tools, system, messages, self._final_block_cache_control)
        tool_choice = _tool_choice_param(
            request.tool_choice, request.parallel_tool_calls, bool(request.tools)
        )

        sampling = request.sampling
        extra_body = _sampling_body(sampling)
        if self._fast_mode:
            extra_body["speed"] = "fast"

        assembler = _StreamAssembler()
        try:
            stream = await self._client.messages.create(
                model=self._model,
                max_tokens=sampling.max_tokens or self._default_max_tokens,
                messages=messages,
                system=system,
                tools=tools if tools else omit,
                tool_choice=tool_choice,
                stop_sequences=(list(sampling.stop_sequences) if sampling.stop_sequences else omit),
                thinking=_thinking_param(sampling.reasoning, self._thinking_display),
                output_config=_output_config_param(sampling.reasoning),
                extra_body=extra_body or None,
                extra_headers={"anthropic-beta": FAST_MODE_BETA} if self._fast_mode else None,
                stream=True,
            )
            async with stream:
                async for event in stream:
                    for out in assembler.handle(event):
                        await callback(out)
        except (anthropic.APIError, httpx2.HTTPError) as exc:
            return _failure_from_exception(exc)
        return assembler.finish()


class AnthropicMessagesProvider:
    """A ``ModelProvider`` serving any Anthropic model over one shared client.

    Builds an ``AnthropicMessagesAPI`` per requested model id, with this
    provider's request options, on first lookup and caches it; every API shares
    this provider's client, so ``aclose`` here is the one lifecycle call.
    """

    def __init__(
        self,
        *,
        client: AsyncAnthropic | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        default_max_tokens: int = 16384,
        final_block_cache_control: CacheControl | None = None,
        thinking_display: ThinkingDisplay = "summarized",
        fast_mode: bool = False,
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self._owns_client = client is None
        self._client = client or AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=0)
        self._default_max_tokens = default_max_tokens
        self._final_block_cache_control = final_block_cache_control
        self._thinking_display: ThinkingDisplay = thinking_display
        self._fast_mode = fast_mode
        self._apis: dict[str, AnthropicMessagesAPI] = {}

    @property
    def client(self) -> AsyncAnthropic:
        """The client every API from this provider sends requests through."""
        return self._client

    def get_model_api(self, model_id: str) -> AnthropicMessagesAPI:
        api = self._apis.get(model_id)
        if api is None:
            api = AnthropicMessagesAPI(
                model_id,
                client=self._client,
                default_max_tokens=self._default_max_tokens,
                final_block_cache_control=self._final_block_cache_control,
                thinking_display=self._thinking_display,
                fast_mode=self._fast_mode,
            )
            self._apis[model_id] = api
        return api

    async def aclose(self) -> None:
        """Release the shared client, if this provider constructed it."""
        if self._owns_client:
            await self._client.close()
