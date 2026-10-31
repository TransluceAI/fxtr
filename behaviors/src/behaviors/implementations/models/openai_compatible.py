"""OpenAI-compatible Chat Completions implementation of ``StreamingModelAPI``.

One class covers every endpoint speaking the OpenAI Chat Completions dialect:
OpenAI itself, OpenRouter, Anthropic's and Gemini's OpenAI-compatible
endpoints, and self-hosted vLLM/sglang servers (``base_url`` +
``api_key="dummy"``). Requests always stream; ``generate`` is
``generate_with_events`` with no callback. Failures are returned as
``ModelCallFailure`` data, never raised, and there are no internal retries
(the SDK's own retries are disabled, including on a client passed in).

Rendering notes:

- Assistant reasoning blocks have no representation in the request dialect
  and are omitted when sending context back; ``ToolMessage.is_error`` is
  likewise dropped (failures are conveyed in the result text).
- Streamed reasoning arrives via the ``reasoning_content`` (vLLM/sglang) or
  ``reasoning`` (OpenRouter) delta extensions and is surfaced as literal
  ``ContentReasoning``; OpenRouter's ``reasoning_details`` duplicates
  ``reasoning`` and is ignored.
- Cache breakpoints (``CacheableContentText``) are forwarded as
  Anthropic-style ``cache_control`` on content parts only when
  ``forward_cache_control=True`` (e.g. OpenRouter serving Anthropic models);
  breakpoints on tool definitions have no compatible representation and are
  always dropped.
- ``SamplingParams.reasoning`` maps to ``reasoning_effort``: the effort level
  verbatim when one is given (including ``xhigh`` and ``max``), a level derived from
  ``budget_tokens`` otherwise (this dialect has no budget parameter), ``none``
  when reasoning is explicitly disabled, and no parameter at all when the
  config only says "enabled" (the provider's default). Whether the model
  accepts the parameter (or the ``none`` level) is the provider's to validate.
- ``SamplingParams.extra`` keys are forwarded verbatim in the request body
  (merged over the instance-level ``extra_body``) for the provider to
  validate.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import httpx2
import openai
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionChunk,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

from behaviors.implementations.models.openai_common import (
    ProviderReportedError,
    error_status_and_message,
    failure_from_exception,
    reasoning_effort,
)
from behaviors.interfaces.model_apis import ModelError
from behaviors.types.content import (
    CacheableContentText,
    Content,
    ContentReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.failure import ModelCallFailure
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
    StopContentFilter,
    StopEndTurn,
    StopMaxTokens,
    StopOther,
    StopReason,
    StopToolUse,
    Usage,
)
from behaviors.types.streaming import (
    StreamEvent,
    StreamEventCallback,
    StreamReasoningDelta,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
)
from behaviors.types.tool import (
    ToolChoice,
    ToolChoiceAuto,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolParam,
)

logger = logging.getLogger(__name__)


def _text_content_param(
    content: tuple[ContentText, ...], forward_cache_control: bool
) -> str | list[dict[str, Any]]:
    has_breakpoint = forward_cache_control and any(
        isinstance(block, CacheableContentText) and block.cache_control is not None
        for block in content
    )
    if len(content) == 1 and not has_breakpoint:
        return content[0].text
    parts: list[dict[str, Any]] = []
    for block in content:
        part: dict[str, Any] = {"type": "text", "text": block.text}
        if (
            forward_cache_control
            and isinstance(block, CacheableContentText)
            and block.cache_control is not None
            and block.text
        ):
            cache: dict[str, Any] = {"type": block.cache_control.type}
            if block.cache_control.ttl is not None:
                cache["ttl"] = block.cache_control.ttl
            part["cache_control"] = cache
        parts.append(part)
    return parts


def _assistant_message_param(message: AssistantMessage) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ContentText):
            text_parts.append(block.text)
        elif isinstance(block, ContentToolUse):
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.input)},
                }
            )
        # Reasoning blocks have no request representation in this dialect;
        # see the module docstring.
    param: dict[str, Any] = {"role": "assistant"}
    if text_parts:
        param["content"] = "".join(text_parts)
    if tool_calls:
        param["tool_calls"] = tool_calls
    if not text_parts and not tool_calls:
        param["content"] = ""
    return param


def _render_context(
    context: tuple[ChatMessage, ...], forward_cache_control: bool
) -> list[ChatCompletionMessageParam]:
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
            messages.append(
                {
                    "role": "tool",
                    "content": "".join(block.text for block in message.content),
                    "tool_call_id": message.tool_call_id,
                }
            )
        else:
            messages.append(_assistant_message_param(message))
    return cast(list[ChatCompletionMessageParam], messages)


def _tool_param(tool: ToolParam) -> ChatCompletionToolParam:
    function: dict[str, Any] = {"name": tool.name, "parameters": dict(tool.input_schema)}
    if tool.description is not None:
        function["description"] = tool.description
    if tool.strict is not None:
        function["strict"] = tool.strict
    return cast(ChatCompletionToolParam, {"type": "function", "function": function})


def _tool_choice_param(tool_choice: ToolChoice) -> Any:
    if isinstance(tool_choice, ToolChoiceAuto):
        return "auto"
    if isinstance(tool_choice, ToolChoiceRequired):
        return "required"
    if isinstance(tool_choice, ToolChoiceNone):
        return "none"
    return {"type": "function", "function": {"name": tool_choice.name}}


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


class _StreamAssembler:
    """Turns OpenAI chat completion chunks into StreamEvents and a ModelResponse."""

    def __init__(self) -> None:
        self._response_id: str | None = None
        self._text_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_calls: dict[int, _ToolCallState] = {}
        self._finish_reason: str | None = None
        self._usage: Any = None

    def _tool_call_events(self, raw_call: Any) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        index = getattr(raw_call, "index", 0)
        index = index if isinstance(index, int) else 0
        state = self._tool_calls.get(index)
        if state is None:
            state = _ToolCallState(ordinal=len(self._tool_calls))
            self._tool_calls[index] = state

        call_id = getattr(raw_call, "id", None)
        if isinstance(call_id, str) and call_id:
            state.id = state.id or call_id
        function = getattr(raw_call, "function", None)
        name = getattr(function, "name", None) if function is not None else None
        if isinstance(name, str) and name:
            state.name = (state.name or "") + name
        arguments = getattr(function, "arguments", None) if function is not None else None

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

    def handle(self, chunk: ChatCompletionChunk) -> list[StreamEvent]:
        extra = chunk.model_extra or {}
        error = extra.get("error")
        if isinstance(error, dict):
            status, message = error_status_and_message(cast(dict[str, Any], error))
            raise ProviderReportedError(status, message)

        out: list[StreamEvent] = []
        if chunk.id and self._response_id is None:
            self._response_id = chunk.id
        if chunk.usage is not None:
            self._usage = chunk.usage
        for choice in chunk.choices:
            delta = choice.delta
            delta_extra = delta.model_extra or {}
            reasoning = delta_extra.get("reasoning_content") or delta_extra.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                self._reasoning_parts.append(reasoning)
                out.append(StreamReasoningDelta(text=reasoning))
            if delta.content:
                self._text_parts.append(delta.content)
                out.append(StreamTextDelta(text=delta.content))
            for raw_call in delta.tool_calls or ():
                out.extend(self._tool_call_events(raw_call))
            if choice.finish_reason is not None:
                self._finish_reason = choice.finish_reason
        return out

    def _stop(self) -> StopReason:
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
            return StopContentFilter(provider_reason="content_filter")
        return StopOther(provider_reason=self._finish_reason or "no finish_reason reported")

    def _assembled_usage(self) -> Usage:
        usage: Any = self._usage
        if usage is None:
            return Usage(
                input_tokens=0,
                cache_read_input_tokens=None,
                cache_write_input_tokens=None,
                output_tokens=0,
                reasoning_output_tokens=None,
            )
        cache_read = _int_or_none(getattr(usage, "cache_read_input_tokens", None))
        if cache_read is None:
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cache_read = _int_or_none(getattr(details, "cached_tokens", None))
        reasoning_tokens: int | None = None
        completion_details = getattr(usage, "completion_tokens_details", None)
        if completion_details is not None:
            reasoning_tokens = _int_or_none(getattr(completion_details, "reasoning_tokens", None))
        prompt_tokens = _int_or_none(getattr(usage, "prompt_tokens", None)) or 0
        # OpenAI's prompt_tokens is the total including cached tokens, while
        # Usage.input_tokens is non-cached only.
        input_tokens = max(prompt_tokens - (cache_read or 0), 0)
        return Usage(
            input_tokens=input_tokens,
            cache_read_input_tokens=cache_read,
            cache_write_input_tokens=_int_or_none(
                getattr(usage, "cache_creation_input_tokens", None)
            ),
            output_tokens=_int_or_none(getattr(usage, "completion_tokens", None)) or 0,
            reasoning_output_tokens=reasoning_tokens,
        )

    def finish(self) -> ModelResponse:
        if self._response_id is None:
            raise ModelError("OpenAI-compatible stream ended without producing any chunks")
        content: list[Content] = []
        if self._reasoning_parts:
            content.append(ContentReasoning(text="".join(self._reasoning_parts)))
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


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


async def _noop_callback(event: StreamEvent) -> None:
    pass


class OpenAICompatibleAPI:
    """A ``StreamingModelAPI`` serving one model behind an OpenAI-compatible endpoint.

    Attributes are fixed at construction: the model, the client (or the
    base URL / credentials to build one), instance-level ``extra_body``
    merged into every request (e.g. OpenRouter provider routing), whether to
    forward prompt-cache breakpoints, and which parameter name carries the
    output-token limit (``max_tokens`` is the widely compatible spelling;
    OpenAI's reasoning models accept only ``max_completion_tokens``).
    """

    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        extra_body: dict[str, Any] | None = None,
        forward_cache_control: bool = False,
        max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self._model = model
        self._owns_client = client is None
        self._client = (
            client.with_options(max_retries=0)
            if client is not None
            else AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        )
        self._extra_body = dict(extra_body) if extra_body else {}
        self._forward_cache_control = forward_cache_control
        self._max_tokens_param = max_tokens_param

    async def aclose(self) -> None:
        """Release the underlying client, if this instance constructed it."""
        if self._owns_client:
            await self._client.close()

    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        return await self.generate_with_events(request, _noop_callback)

    async def generate_with_events(
        self, request: ModelRequest, callback: StreamEventCallback
    ) -> ModelResponse | ModelCallFailure:
        sampling = request.sampling
        # extra_body entries are serialized into the request body verbatim, so
        # sampling extras and provider params need no SDK support.
        extra_body: dict[str, Any] = dict(self._extra_body)
        if sampling.max_tokens is not None:
            extra_body[self._max_tokens_param] = sampling.max_tokens
        extra_body.update(sampling.extra)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": _render_context(request.context, self._forward_cache_control),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            kwargs["tools"] = [_tool_param(tool) for tool in request.tools]
        if request.tool_choice is not None:
            kwargs["tool_choice"] = _tool_choice_param(request.tool_choice)
        if request.parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = request.parallel_tool_calls
        if sampling.temperature is not None:
            kwargs["temperature"] = sampling.temperature
        if sampling.top_p is not None:
            kwargs["top_p"] = sampling.top_p
        if sampling.stop_sequences:
            kwargs["stop"] = list(sampling.stop_sequences)
        effort = reasoning_effort(sampling.reasoning)
        if effort is not None:
            kwargs["reasoning_effort"] = effort
        if extra_body:
            kwargs["extra_body"] = extra_body

        assembler = _StreamAssembler()
        create = cast(Any, self._client.chat.completions.create)
        try:
            stream = await create(**kwargs)
            async with stream:
                async for chunk in stream:
                    for out in assembler.handle(chunk):
                        await callback(out)
        except (openai.APIError, httpx2.HTTPError, ProviderReportedError) as exc:
            return failure_from_exception(exc)
        return assembler.finish()


class OpenAICompatibleProvider:
    """A ``ModelProvider`` serving any model behind an OpenAI-compatible endpoint.

    Builds an ``OpenAICompatibleAPI`` per requested model id on first lookup and
    caches it; every API shares this provider's client, so ``aclose`` here is the
    one lifecycle call.
    """

    def __init__(
        self,
        *,
        client: AsyncOpenAI | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        extra_body: dict[str, Any] | None = None,
        forward_cache_control: bool = False,
        max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self._extra_body = dict(extra_body) if extra_body else {}
        self._forward_cache_control = forward_cache_control
        self._max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = max_tokens_param
        self._apis: dict[str, OpenAICompatibleAPI] = {}

    @property
    def client(self) -> AsyncOpenAI:
        """The client every API from this provider sends requests through.

        Also usable for endpoints this dialect doesn't cover, such as ``/v1/completions``.
        """
        return self._client

    def get_model_api(self, model_id: str) -> OpenAICompatibleAPI:
        api = self._apis.get(model_id)
        if api is None:
            api = OpenAICompatibleAPI(
                model_id,
                client=self._client,
                extra_body=self._extra_body,
                forward_cache_control=self._forward_cache_control,
                max_tokens_param=self._max_tokens_param,
            )
            self._apis[model_id] = api
        return api

    async def aclose(self) -> None:
        """Release the shared client, if this provider constructed it."""
        if self._owns_client:
            await self._client.close()
