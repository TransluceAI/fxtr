"""Anthropic Messages API implementation of ``StreamingModelAPI``.

A thin transport over the ``anthropic`` SDK: the request body comes from
``AnthropicMessagesCodec.encode_request`` and the streamed response is parsed by its SSE decoder,
so what this module owns is credentials, the base URL and the HTTP round trip. The SDK's own
request shaping and typed event parsing are bypassed (its base client ``post`` under the
raw-response header returns the untouched ``httpx2.Response``), and its retries are off per
request (retry policy belongs to the caller, see ``generate_with_retries``).

Requests always stream; ``generate`` is ``generate_with_events`` with no callback. Failures are
returned as ``ModelCallFailure`` data, never raised. Rendering and parsing rules are documented
on the codec (``behaviors.implementations.dialects.anthropic_messages``).

One request option of the API itself, applied to every generated request (forwarded requests go
out as the harness sent them): ``final_block_cache_control`` puts that breakpoint on the request's
final block (``overrides.with_final_block_breakpoint``, which the provider-neutral wrapper
``overrides.FinalBlockCacheControl`` also applies, for any model API). What Anthropic does not
accept of the request's breakpoints (a fifth, or a longer TTL after a shorter one) the codec's
adaptation changes, recording each change. Everything else about a request is the request's own:
fast mode is ``SamplingParams.speed`` and the thinking display is ``ReasoningParams.summary``
(``behaviors.implementations.models.overrides.SamplingOverrides`` pins either for every request).
"""

import dataclasses
import logging
import re

import httpx2
from anthropic import AsyncAnthropic
from anthropic._models import FinalRequestOptions

from behaviors.implementations.dialects.anthropic_messages import AnthropicMessagesCodec
from behaviors.implementations.models._raw_stream import (
    call_and_decode,
    forward_raw,
    require_supported_client,
)
from behaviors.implementations.models._workarounds import RejectionWorkaround, WorkaroundLedger
from behaviors.implementations.models.overrides import with_final_block_breakpoint
from behaviors.interfaces.dialect_codecs import ModelSideCodec
from behaviors.interfaces.model_apis import ForwardTransportError
from behaviors.types.cache_control import CacheControl
from behaviors.types.dialect import Dialect, EncodedRequest, Headers, RawHttpResponse
from behaviors.types.failure import ModelCallFailure
from behaviors.types.response import ModelRequest, ModelResponse
from behaviors.types.streaming import StreamEvent, StreamEventCallback


async def _noop_callback(event: StreamEvent) -> None:
    pass


_logger = logging.getLogger(__name__)

# The API's own instruction, on the models that spell thinking off `between_tools`:
#   To turn thinking off on this model, send "thinking": {"type": "between_tools"} instead of
#   {"type": "disabled"}. The model does not think before responding. The short updates it
#   writes between tool calls come back as thinking blocks.
# Only this substitution is followed: it keeps what was asked (no thinking before the
# response). A model with no off switch recommends `adaptive` instead, which changes the
# behavior asked for and so stays the failure it is.
_BETWEEN_TOOLS_INSTRUCTION = re.compile(
    r'send\s+"thinking":\s*\{\s*"type":\s*"between_tools"\s*\}\s+instead\s+of\s+'
    r'\{\s*"type":\s*"disabled"\s*\}'
)
_THINKING_OFF_BETWEEN_TOOLS = "between_tools"

# Model ids this process has already reported the spelling for, so the diagnostic shows once.
_reported_thinking_off_spelling: set[str] = set()


def _asks_between_tools(result: ModelResponse | ModelCallFailure) -> bool:
    """Whether `result` is the API's rejection carrying the instruction. Only a rejected request
    (HTTP 400) qualifies: an in-stream `error` event carries no HTTP status and arrives after the
    stream has started, by which time events may have reached the callback, so a retry would
    deliver a response on top of them."""
    return (
        isinstance(result, ModelCallFailure)
        and result.provider_http_status == 400
        and result.category == "invalid_request"
        and _BETWEEN_TOOLS_INSTRUCTION.search(result.description) is not None
    )


def _spell_thinking_off(encoded: EncodedRequest, spelling: str) -> EncodedRequest:
    """The rendered request with thinking off respelled, when it carries the canonical
    `disabled`; any other request goes as rendered."""
    thinking = encoded.body.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "disabled":
        return encoded
    body = {**encoded.body, "thinking": {**thinking, "type": spelling}}
    return dataclasses.replace(encoded, body=body)


_RESPELL_THINKING_OFF = RejectionWorkaround(
    name="thinking off as between_tools",
    recognizes=lambda _prepared, failure: _asks_between_tools(failure),
    # Both spellings mean no thinking before the response: nothing is lost.
    apply_to_wire=lambda encoded: (_spell_thinking_off(encoded, _THINKING_OFF_BETWEEN_TOOLS), ()),
)


def _report_thinking_off_spelling(model: str, spelling: str) -> None:
    if model in _reported_thinking_off_spelling:
        return
    _reported_thinking_off_spelling.add(model)
    _logger.warning(
        "%r takes thinking off as `%s` (its API said so on a request rendered `disabled`): "
        "requests to it with reasoning off are sent with that spelling from now on. The model "
        "writes no thinking before responding; its short updates between tool calls come back "
        "as thinking blocks. Reported once per process per model.",
        model,
        spelling,
    )


class AnthropicMessagesAPI:
    """A ``StreamingModelAPI`` serving one Anthropic model.

    Attributes are fixed at construction: the model, the client (or the credentials to build
    one), the ``max_tokens`` used when a request's sampling params don't specify one (the API
    requires a value), and the request option described in the module docstring.

    One thing is learned from the provider: the spelling of thinking off the model takes. The
    codec renders the canonical `disabled`; if the API answers with its instruction to send
    `between_tools` instead, the call is retried once with that spelling, and on success every
    later request with reasoning off from this instance is rendered that way (a provider caches
    one instance per model id, so the learning is per client and model). The adaptation applies
    to requests this instance renders, never to a raw forwarded request.
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
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self._model = model
        self._owns_client = client is None
        self._client = client or AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=0)
        require_supported_client(self._client, AsyncAnthropic)
        self._codec = AnthropicMessagesCodec(default_max_tokens=default_max_tokens)
        self._final_block_cache_control = final_block_cache_control
        self._workarounds = WorkaroundLedger(
            (_RESPELL_THINKING_OFF,),
            on_confirm=lambda _: _report_thinking_off_spelling(model, _THINKING_OFF_BETWEEN_TOOLS),
        )

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dialect(self) -> Dialect:
        return self._codec.dialect

    @property
    def codec(self) -> ModelSideCodec:
        return self._codec

    async def aclose(self) -> None:
        """Release the underlying client, if this instance constructed it."""
        if self._owns_client:
            await self._client.close()

    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        """See `ModelAPI.generate`. The request's `adaptation_policy` decides what adapting it to
        the dialect may change."""
        return await self.generate_with_events(request, _noop_callback)

    async def generate_with_events(
        self,
        request: ModelRequest,
        callback: StreamEventCallback,
    ) -> ModelResponse | ModelCallFailure:
        """See `StreamingModelAPI.generate_with_events`."""
        if self._final_block_cache_control is not None:
            request = with_final_block_breakpoint(request, self._final_block_cache_control)
        return await call_and_decode(
            self._client,
            codec=self._codec,
            model=self._model,
            request=request,
            options_type=FinalRequestOptions,
            callback=callback,
            workarounds=self._workarounds,
        )

    async def forward_request(self, target: str, headers: Headers, body: bytes) -> RawHttpResponse:
        try:
            return await forward_raw(
                self._client,
                target,
                headers,
                body,
                model=self._model,
                options_type=FinalRequestOptions,
            )
        except httpx2.HTTPError as exc:
            raise ForwardTransportError(f"Anthropic request failed: {exc}") from exc


class AnthropicMessagesProvider:
    """A ``ModelProvider`` serving any Anthropic model over one shared client.

    Builds an ``AnthropicMessagesAPI`` per requested model id, with this provider's request
    options, on first lookup and caches it; every API shares this provider's client, so
    ``aclose`` here is the one lifecycle call.
    """

    def __init__(
        self,
        *,
        client: AsyncAnthropic | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        default_max_tokens: int = 16384,
        final_block_cache_control: CacheControl | None = None,
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self._owns_client = client is None
        self._client = client or AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=0)
        require_supported_client(self._client, AsyncAnthropic)
        self._default_max_tokens = default_max_tokens
        self._final_block_cache_control = final_block_cache_control
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
            )
            self._apis[model_id] = api
        return api

    async def aclose(self) -> None:
        """Release the shared client, if this provider constructed it."""
        if self._owns_client:
            await self._client.close()
