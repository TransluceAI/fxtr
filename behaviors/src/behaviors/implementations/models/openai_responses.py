"""OpenAI Responses API implementation of ``StreamingModelAPI``.

The Responses surface (``/v1/responses``) is OpenAI's stateful-by-default API;
this implementation always sends ``store: false`` and the full context, so it
stays interchangeable with the other ``ModelAPI`` implementations. Requests
always stream; ``generate`` is ``generate_with_events`` with no callback.
Failures are returned as ``ModelCallFailure`` data, never raised, and there
are no internal retries (the SDK's own retries are disabled, including on a
client passed in).

Rendering notes:

- ``SystemMessage`` content is hoisted into the top-level ``instructions``
  parameter regardless of its position in the context.
- Signed reasoning round-trips: ``ContentSummarizedReasoning`` with a
  signature renders as a ``reasoning`` input item
  (``include: ["reasoning.encrypted_content"]`` is always requested, so
  responses carry the payload back). The API verifies that the encrypted
  payload's embedded item id matches the wrapping item's ``id``, so the
  signature this implementation emits packs both — ``{item_id}:{payload}``
  (see ``behaviors.types.reasoning_signature``) — and is unpacked when
  rendering the item back. Signature-less summarized reasoning, literal reasoning, and
  redacted reasoning have no representation and are dropped.
- ``SamplingParams.reasoning`` maps to ``reasoning.effort`` (see
  ``openai_common.reasoning_effort``); whenever reasoning is on,
  ``reasoning.summary: "auto"`` is requested so summaries stream back.
- ``stop_sequences`` have no Responses API parameter and are dropped with a
  warning; ``ToolMessage.is_error`` and cache breakpoints are likewise
  dropped (prompt caching is implicit on this surface), as are **images**.
- ``SamplingParams.extra`` keys are forwarded verbatim in the request body
  (merged over the instance-level ``extra_body``) for the provider to
  validate.
"""

import json
import logging
from typing import Any, cast

import httpx2
import openai
from openai import AsyncOpenAI
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseReasoningItem,
    ResponseStreamEvent,
)

from behaviors.implementations.models.openai_common import (
    ProviderReportedError,
    failure_from_exception,
    reasoning_effort,
)
from behaviors.interfaces.model_apis import ModelError
from behaviors.types.content import Content, ContentSummarizedReasoning, ContentText, ContentToolUse
from behaviors.types.failure import ModelCallFailure
from behaviors.types.message import (
    AssistantMessage,
    ChatMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from behaviors.types.reasoning_signature import (
    SYNTHETIC_REASONING_ID_PREFIX,
    decode_reasoning_signature,
    encode_reasoning_signature,
)
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ReasoningParams,
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
    StreamSignatureDelta,
    StreamSummarizedReasoningDelta,
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


# --- request rendering ----------------------------------------------------------------------


def _text_of(content: tuple[ContentText, ...]) -> str:
    return "".join(block.text for block in content)


def _assistant_items(message: AssistantMessage) -> list[dict[str, Any]]:
    """One input item per content block, preserving block order."""
    items: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ContentText):
            if block.text:
                items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": block.text}],
                    }
                )
        elif isinstance(block, ContentSummarizedReasoning):
            if block.signature is not None:
                item_id, payload = decode_reasoning_signature(block.signature)
                items.append(
                    {
                        "type": "reasoning",
                        "id": item_id or f"{SYNTHETIC_REASONING_ID_PREFIX}{len(items)}",
                        "summary": (
                            [{"type": "summary_text", "text": block.text}] if block.text else []
                        ),
                        "encrypted_content": payload,
                    }
                )
        elif isinstance(block, ContentToolUse):
            items.append(
                {
                    "type": "function_call",
                    "call_id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input),
                }
            )
        # Signature-less, literal, and redacted reasoning are dropped; see the
        # module docstring.
    return items


def _render_context(context: tuple[ChatMessage, ...]) -> tuple[str | None, list[dict[str, Any]]]:
    """Render messages into the top-level ``instructions`` and the ``input`` item list."""
    instruction_parts: list[str] = []
    items: list[dict[str, Any]] = []
    for message in context:
        if isinstance(message, SystemMessage):
            instruction_parts.append(_text_of(message.content))
        elif isinstance(message, UserMessage):
            items.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": block.text} for block in message.content
                    ],
                }
            )
        elif isinstance(message, ToolMessage):
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": _text_of(message.content),
                }
            )
        else:
            items.extend(_assistant_items(message))
    instructions = "\n\n".join(part for part in instruction_parts if part)
    return (instructions or None), items


def _tool_param(tool: ToolParam) -> dict[str, Any]:
    param: dict[str, Any] = {
        "type": "function",
        "name": tool.name,
        "parameters": dict(tool.input_schema),
    }
    if tool.description is not None:
        param["description"] = tool.description
    if tool.strict is not None:
        param["strict"] = tool.strict
    return param


def _tool_choice_param(tool_choice: ToolChoice) -> Any:
    if isinstance(tool_choice, ToolChoiceAuto):
        return "auto"
    if isinstance(tool_choice, ToolChoiceRequired):
        return "required"
    if isinstance(tool_choice, ToolChoiceNone):
        return "none"
    return {"type": "function", "name": tool_choice.name}


def _reasoning_param(reasoning: ReasoningParams | None) -> dict[str, str] | None:
    """The ``reasoning`` request object, or None to omit it."""
    if reasoning is None:
        return None
    if not reasoning.enabled:
        return {"effort": "none"}
    param: dict[str, str] = {"summary": "auto"}
    effort = reasoning_effort(reasoning)
    if effort is not None:
        param["effort"] = effort
    return param


# --- response assembly ----------------------------------------------------------------------


class _StreamState:
    """Relays Responses stream events as StreamEvents and captures the terminal Response.

    The terminal ``response.completed`` / ``response.incomplete`` event carries
    the fully assembled ``Response``, so assembly reads that; delta events are
    only relayed. Tool-call ordinals are assigned in arrival order and keyed by
    the ``fc_...`` item id so argument deltas can be correlated.
    """

    def __init__(self) -> None:
        self._tool_ordinals: dict[str, int] = {}
        self.response: Response | None = None

    def handle(self, event: ResponseStreamEvent) -> list[StreamEvent]:
        if event.type == "response.output_text.delta":
            return [StreamTextDelta(text=event.delta)] if event.delta else []
        if event.type == "response.reasoning_summary_text.delta":
            return [StreamSummarizedReasoningDelta(text=event.delta)] if event.delta else []
        if event.type == "response.reasoning_text.delta":
            return [StreamReasoningDelta(text=event.delta)] if event.delta else []
        if event.type == "response.function_call_arguments.delta":
            ordinal = self._tool_ordinals.get(event.item_id)
            if ordinal is None or not event.delta:
                return []
            return [StreamToolUseInputDelta(index=ordinal, partial_json=event.delta)]
        if event.type == "response.output_item.added":
            item = event.item
            if item.type == "function_call":
                ordinal = len(self._tool_ordinals)
                self._tool_ordinals[item.id or f"fc_ordinal{ordinal}"] = ordinal
                return [StreamToolUseStart(index=ordinal, id=item.call_id, name=item.name)]
            return []
        if event.type == "response.output_item.done":
            item = event.item
            if item.type == "reasoning" and item.encrypted_content:
                signature = encode_reasoning_signature(item.id, item.encrypted_content)
                return [StreamSignatureDelta(signature=signature)]
            return []
        if event.type == "response.completed":
            self.response = event.response
            return []
        if event.type == "response.incomplete":
            self.response = event.response
            return []
        if event.type == "response.failed":
            error = event.response.error
            message = error.message if error is not None else "response.failed with no error"
            code = error.code if error is not None else None
            raise ProviderReportedError(_status_for_error_code(code), message)
        if event.type == "error":
            raise ProviderReportedError(None, event.message)
        return []


def _status_for_error_code(code: str | None) -> int | None:
    """A pseudo HTTP status for a `response.failed` error code, for category mapping."""
    if code is None:
        return None
    if "rate_limit" in code:
        return 429
    if code in ("invalid_prompt", "invalid_image", "invalid_file"):
        return 400
    return 500


def _reasoning_content(item: ResponseReasoningItem) -> ContentSummarizedReasoning:
    summary = "".join(part.text for part in item.summary)
    if not summary:
        # Some servings put literal reasoning text on the item instead of a summary.
        summary = "".join(part.text for part in item.content or [])
    signature = (
        encode_reasoning_signature(item.id, item.encrypted_content)
        if item.encrypted_content
        else None
    )
    return ContentSummarizedReasoning(text=summary, signature=signature)


def _tool_use_content(item: ResponseFunctionToolCall) -> ContentToolUse:
    raw = item.arguments or "{}"
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
        id=item.call_id,
        name=item.name,
        input=cast(dict[str, Any], parsed),
        parse_error=parse_error,
    )


def _stop_reason(response: Response, saw_tool_call: bool) -> StopReason:
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        if reason == "max_output_tokens":
            return StopMaxTokens()
        if reason == "content_filter":
            return StopContentFilter(provider_reason="content_filter")
        return StopOther(provider_reason=f"incomplete: {reason or 'no reason reported'}")
    if saw_tool_call:
        return StopToolUse()
    return StopEndTurn()


def _usage(response: Response) -> Usage:
    usage = response.usage
    if usage is None:
        return Usage(
            input_tokens=0,
            cache_read_input_tokens=None,
            cache_write_input_tokens=None,
            output_tokens=0,
            reasoning_output_tokens=None,
        )
    cached = usage.input_tokens_details.cached_tokens
    return Usage(
        # Responses `input_tokens` is the total including cached tokens, while
        # Usage.input_tokens is non-cached only.
        input_tokens=max(usage.input_tokens - cached, 0),
        cache_read_input_tokens=cached,
        cache_write_input_tokens=None,
        output_tokens=usage.output_tokens,
        reasoning_output_tokens=usage.output_tokens_details.reasoning_tokens,
    )


def _response_to_model_response(response: Response) -> ModelResponse:
    content: list[Content] = []
    saw_tool_call = False
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    if part.text:
                        content.append(ContentText(text=part.text))
                elif part.type == "refusal":
                    # No separate refusal block exists in the model vocabulary;
                    # the text is kept and the status/stop reason carries the rest.
                    content.append(ContentText(text=part.refusal))
        elif item.type == "reasoning":
            content.append(_reasoning_content(item))
        elif item.type == "function_call":
            saw_tool_call = True
            content.append(_tool_use_content(item))
        else:
            logger.warning("Ignoring unsupported Responses output item type %r", item.type)
    return ModelResponse(
        id=response.id,
        output=(AssistantMessage(content=tuple(content)),),
        stop_reason=_stop_reason(response, saw_tool_call),
        usage=_usage(response),
    )


async def _noop_callback(event: StreamEvent) -> None:
    pass


class OpenAIResponsesAPI:
    """A ``StreamingModelAPI`` serving one model over the OpenAI Responses API.

    Attributes are fixed at construction: the model, the client (or the
    credentials to build one), and instance-level ``extra_body`` merged into
    every request.
    """

    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        extra_body: dict[str, Any] | None = None,
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
        if sampling.stop_sequences:
            logger.warning("The Responses API has no stop-sequences parameter; dropping them")

        instructions, input_items = _render_context(request.context)
        extra_body: dict[str, Any] = dict(self._extra_body)
        extra_body.update(sampling.extra)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "stream": True,
        }
        if instructions is not None:
            kwargs["instructions"] = instructions
        if request.tools:
            kwargs["tools"] = [_tool_param(tool) for tool in request.tools]
        if request.tool_choice is not None:
            kwargs["tool_choice"] = _tool_choice_param(request.tool_choice)
        if request.parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = request.parallel_tool_calls
        if sampling.max_tokens is not None:
            kwargs["max_output_tokens"] = sampling.max_tokens
        if sampling.temperature is not None:
            kwargs["temperature"] = sampling.temperature
        if sampling.top_p is not None:
            kwargs["top_p"] = sampling.top_p
        reasoning = _reasoning_param(sampling.reasoning)
        if reasoning is not None:
            kwargs["reasoning"] = reasoning
        if extra_body:
            kwargs["extra_body"] = extra_body

        state = _StreamState()
        create = cast(Any, self._client.responses.create)
        try:
            stream = await create(**kwargs)
            async with stream:
                async for event in stream:
                    for out in state.handle(event):
                        await callback(out)
        except (openai.APIError, httpx2.HTTPError, ProviderReportedError) as exc:
            return failure_from_exception(exc)
        if state.response is None:
            raise ModelError("Responses stream ended without a terminal response")
        return _response_to_model_response(state.response)


class OpenAIResponsesProvider:
    """A ``ModelProvider`` serving any model over the OpenAI Responses API.

    Builds an ``OpenAIResponsesAPI`` per requested model id on first lookup and
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
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self._extra_body = dict(extra_body) if extra_body else {}
        self._apis: dict[str, OpenAIResponsesAPI] = {}

    @property
    def client(self) -> AsyncOpenAI:
        """The client every API from this provider sends requests through."""
        return self._client

    def get_model_api(self, model_id: str) -> OpenAIResponsesAPI:
        api = self._apis.get(model_id)
        if api is None:
            api = OpenAIResponsesAPI(model_id, client=self._client, extra_body=self._extra_body)
            self._apis[model_id] = api
        return api

    async def aclose(self) -> None:
        """Release the shared client, if this provider constructed it."""
        if self._owns_client:
            await self._client.close()
