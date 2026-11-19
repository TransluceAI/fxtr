"""OpenAI Responses API implementation of ``StreamingModelAPI``.

A thin transport over the ``openai`` SDK: the request body comes from
``OpenAIResponsesCodec.encode_request`` and the streamed response is parsed by its SSE decoder,
so what this module owns is credentials, the base URL and the HTTP round trip. The SDK's own
request shaping and typed event parsing are bypassed (its base client ``post`` under the
raw-response header returns the untouched ``httpx2.Response``), and its retries are off per request
(retry policy belongs to the caller, see ``generate_with_retries``).

Requests always stream; ``generate`` is ``generate_with_events`` with no callback. Failures are
returned as ``ModelCallFailure`` data, never raised. Rendering and parsing rules (``store: false``
and ``reasoning.summary`` as declared defaults, signed-reasoning round-tripping, what is
dropped) are documented on the codec (``behaviors.implementations.dialects.openai_responses``).
"""

import dataclasses
import logging
from typing import Any, cast

import httpx2
from openai import AsyncOpenAI

from behaviors.implementations.dialects.openai_responses import OpenAIResponsesCodec
from behaviors.implementations.models._openai_options import ModelEndpointOptions
from behaviors.implementations.models._raw_stream import (
    call_and_decode,
    forward_raw,
    require_supported_client,
    sdk_relative_path,
)
from behaviors.implementations.models._workarounds import RejectionWorkaround, WorkaroundLedger
from behaviors.interfaces.dialect_codecs import ModelSideCodec
from behaviors.interfaces.model_apis import ForwardTransportError
from behaviors.types.adaptation import RequestLoss
from behaviors.types.dialect import Dialect, EncodedRequest, Headers, RawHttpResponse
from behaviors.types.failure import ModelCallFailure
from behaviors.types.response import ModelRequest, ModelResponse
from behaviors.types.streaming import StreamEvent, StreamEventCallback

_logger = logging.getLogger(__name__)


async def _noop_callback(event: StreamEvent) -> None:
    pass


# Model ids this process has already reported, so the diagnostic shows once.
_reported_without_breakpoints: set[str] = set()


def _rejects_breakpoints(result: ModelResponse | ModelCallFailure) -> bool:
    """Whether `result` is the API's rejection of explicit prompt caching, which models before
    gpt-5.6 answer with for either field (`prompt_cache_breakpoint is not supported on this
    model`, and the same for `prompt_cache_options`, in either mode). Only a rejected request
    (HTTP 400) qualifies: nothing has streamed, so a retry is a fresh call."""
    return (
        isinstance(result, ModelCallFailure)
        and result.provider_http_status == 400
        and any(field in result.description for field in _CACHE_FIELDS)
    )


_CACHE_FIELDS = ("prompt_cache_breakpoint", "prompt_cache_options")


def _without_breakpoints(encoded: EncodedRequest) -> EncodedRequest:
    """The rendered request with every `prompt_cache_breakpoint` removed from its input parts
    (message content and tool outputs), and without `prompt_cache_options`: such a model takes
    neither, and without them the API caches in its implicit mode."""
    items: list[Any] = []
    for raw in cast("list[Any]", encoded.body.get("input") or []):
        item = dict(cast("dict[str, Any]", raw)) if isinstance(raw, dict) else raw
        if isinstance(item, dict):
            for key in ("content", "output"):
                parts = item.get(key)
                if isinstance(parts, list):
                    item[key] = [
                        {
                            k: v
                            for k, v in cast("dict[str, Any]", part).items()
                            if k != "prompt_cache_breakpoint"
                        }
                        if isinstance(part, dict)
                        else part
                        for part in cast("list[Any]", parts)
                    ]
        items.append(item)
    body = {key: value for key, value in encoded.body.items() if key != "prompt_cache_options"}
    if "input" in body:
        body["input"] = items
    return dataclasses.replace(encoded, body=body)


def _no_explicit_caching(
    encoded: EncodedRequest,
) -> tuple[EncodedRequest, tuple[RequestLoss, ...]]:
    stripped = _without_breakpoints(encoded)
    if stripped.body == encoded.body:
        return encoded, ()
    loss = RequestLoss(
        "cache_breakpoint",
        "explicit prompt caching (breakpoints and prompt_cache_options) not sent: this model "
        "rejects it, and caches only at the API's implicit breakpoint",
        semantic=False,
    )
    return stripped, (loss,)


_NO_EXPLICIT_CACHING = RejectionWorkaround(
    name="no explicit prompt caching",
    recognizes=lambda _prepared, failure: _rejects_breakpoints(failure),
    apply_to_wire=_no_explicit_caching,
)


def _report_without_breakpoints(model: str) -> None:
    if model in _reported_without_breakpoints:
        return
    _reported_without_breakpoints.add(model)
    _logger.warning(
        "%r rejects explicit prompt caching (its API said so): requests to it are sent without "
        "prompt_cache_breakpoint or prompt_cache_options from now on, and are cached only at the "
        "API's own implicit breakpoint. Reported once per process per model.",
        model,
    )


class OpenAIResponsesAPI:
    """A ``StreamingModelAPI`` serving one model over the OpenAI Responses API.

    Attributes are fixed at construction: the model, the client (or the credentials to build
    one), and instance-level ``extra_body`` filled into every request where it leaves the key
    absent.

    One thing is learned from the provider: whether the model takes explicit prompt caching
    (`prompt_cache_breakpoint` and `prompt_cache_options`, from gpt-5.6). The codec renders the
    breakpoints and options a request carries; if the API rejects either, the call is retried
    once without both, and on success every later request from this instance is sent without
    them (a provider caches one instance per model id, so the learning is per client and
    model). The adaptation applies to requests this instance renders, never to a raw forwarded
    request.
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
        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        require_supported_client(self._client, AsyncOpenAI)
        self._codec = OpenAIResponsesCodec(extra_body=extra_body)
        self._workarounds = WorkaroundLedger(
            (_NO_EXPLICIT_CACHING,), on_confirm=lambda _: _report_without_breakpoints(model)
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
        return await call_and_decode(
            self._client,
            codec=self._codec,
            model=self._model,
            request=request,
            options_type=ModelEndpointOptions,
            callback=callback,
            relative_path=sdk_relative_path,
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
                options_type=ModelEndpointOptions,
                relative_path=sdk_relative_path,
            )
        except httpx2.HTTPError as exc:
            raise ForwardTransportError(f"OpenAI request failed: {exc}") from exc


class OpenAIResponsesProvider:
    """A ``ModelProvider`` serving any model over the OpenAI Responses API.

    Builds an ``OpenAIResponsesAPI`` per requested model id on first lookup and caches it; every
    API shares this provider's client, so ``aclose`` here is the one lifecycle call.
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
        require_supported_client(self._client, AsyncOpenAI)
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
