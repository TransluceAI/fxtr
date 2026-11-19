"""Gemini API (Generative Language `generateContent`) implementation of ``StreamingModelAPI``.

A thin transport: the request body comes from ``GeminiGenerateContentCodec.encode_request`` and
the streamed response is parsed by its SSE decoder, so what this module owns is credentials, the
base URL and the HTTP round trip. Google's SDK (``google-genai``) builds its own requests from
its own types, so instead of wrapping it the module carries a small native client
(``GeminiClient``) with the slice of a provider SDK client the shared transport uses
(``_raw_stream.SdkClient``): the API key as ``x-goog-api-key``, the base URL, and an httpx2 client.

Requests always stream (``streamGenerateContent?alt=sse``); ``generate`` is
``generate_with_events`` with no callback. Failures are returned as ``ModelCallFailure`` data,
never raised. Rendering and parsing rules are documented on the codec
(``behaviors.implementations.dialects.gemini_generate_content``).

Two things are learned from the provider, as `RejectionWorkaround`s (`_workarounds`): each is
recognized from the model's refusal of the codec's rendering, retried, and kept for every later
request from the instance once a request with it succeeds. Each records what it changes on the
result (`adaptation_losses`):

* **Thought signatures on calls it did not make.** Gemini 3 models refuse a history whose
  function calls carry no `thoughtSignature` (calls another model made, or whose signature a
  harness dropped); Google's Gemini 3 guide gives a placeholder that skips the check
  (`SKIP_SIGNATURE_VALIDATION`), sent on every unsigned call.
* **The model's spelling of thinking off.** The codec renders off as `thinkingBudget: 0`, which
  some models take (`gemini-3.8-flash`, which refuses the `MINIMAL` level) and others refuse with
  a bare "invalid argument" (the flash-lite models, which take `MINIMAL`); for those it is sent as
  effort `minimal` (`thinkingLevel: MINIMAL`), the least thinking they offer (Google's docs say it
  does not guarantee none): a change in meaning, refused under `AdaptationPolicy("refuse")`.
"""

import dataclasses
import logging
import os
import re
from typing import Any, cast

import httpx2

from behaviors.implementations.dialects.gemini_generate_content import GeminiGenerateContentCodec
from behaviors.implementations.models._raw_stream import call_and_decode, forward_raw
from behaviors.implementations.models._workarounds import RejectionWorkaround, WorkaroundLedger
from behaviors.interfaces.dialect_codecs import AdaptedRequest, ModelSideCodec
from behaviors.interfaces.model_apis import ForwardTransportError
from behaviors.types.adaptation import RequestLoss
from behaviors.types.dialect import Dialect, EncodedRequest, Headers, RawHttpResponse
from behaviors.types.failure import ModelCallFailure
from behaviors.types.json import JSONObject
from behaviors.types.response import ModelRequest, ModelResponse, ReasoningParams
from behaviors.types.streaming import StreamEvent, StreamEventCallback

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

# The placeholder signature the API accepts on a function call it did not sign.
SKIP_SIGNATURE_VALIDATION = "skip_thought_signature_validator"

_MODEL_IN_PATH = re.compile(r"/models/[^:/]+:")

# (adaptation, model id) pairs this process has already reported, so each diagnostic shows once.
_reported: set[tuple[str, str]] = set()

_PLACEHOLDERS = "placeholder signatures"
_MINIMAL_OFF = "thinking off as MINIMAL"


async def _noop_callback(event: StreamEvent) -> None:
    pass


@dataclasses.dataclass
class _RequestOptions:
    """What the shared transport hands the client for one request (an SDK's options object)."""

    method: str
    url: str
    json_data: JSONObject
    headers: dict[str, str]
    max_retries: int = 0

    @classmethod
    def construct(cls, **fields: Any) -> "_RequestOptions":
        known = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in fields.items() if key in known})


class GeminiClient:
    """The Gemini API's credentials, base URL and HTTP client, in the shape the shared transport
    sends through (`_raw_stream.SdkClient`).

    The key is `api_key`, else `GEMINI_API_KEY`, else `GOOGLE_API_KEY`; the base URL is
    `base_url`, else `GOOGLE_GEMINI_BASE_URL`, else the public endpoint. Paths are absolute
    (`/v1beta/models/...`), the API version being the codec's.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
        timeout: float = 600.0,
    ) -> None:
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise ValueError(
                "no Gemini API key: pass api_key or set GEMINI_API_KEY (or GOOGLE_API_KEY)"
            )
        self._key = key
        self._base_url = (
            base_url or os.environ.get("GOOGLE_GEMINI_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._owns_http = http_client is None
        self._http = http_client or httpx2.AsyncClient()
        self._timeout = httpx2.Timeout(timeout, connect=10.0)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def default_headers(self) -> dict[str, Any]:
        return {"x-goog-api-key": self._key, "Content-Type": "application/json"}

    @property
    def _client(self) -> httpx2.AsyncClient:
        return self._http

    async def _prepare_options(self, options: Any) -> Any:
        return options

    def _build_request(self, options: Any) -> httpx2.Request:
        opts = cast("_RequestOptions", options)
        return self._http.build_request(
            opts.method.upper(),
            self._base_url + opts.url,
            json=opts.json_data,
            headers={**self.default_headers, **opts.headers},
            timeout=self._timeout,
        )

    async def close(self) -> None:
        """Release the HTTP client, if this client constructed it."""
        if self._owns_http:
            await self._http.aclose()


def _place_model(path: str, payload: JSONObject, model: str) -> str:
    """The model of a forwarded request is in its path (`/v1beta/models/{model}:{method}`)."""
    return _MODEL_IN_PATH.sub(f"/models/{model}:", path, count=1)


def _rejects_unsigned_calls(result: ModelResponse | ModelCallFailure) -> bool:
    """Whether `result` is the API's refusal of function calls without a thought signature.
    Only a rejected request (HTTP 400) qualifies: nothing has streamed, so a retry is a fresh
    call."""
    if not isinstance(result, ModelCallFailure) or result.provider_http_status != 400:
        return False
    text = result.description.lower()
    return "thought_signature" in text or "thoughtsignature" in text or "thought signature" in text


def _with_placeholder_signatures(encoded: EncodedRequest) -> EncodedRequest:
    """The rendered request with the placeholder signature on every unsigned function call."""
    contents: list[Any] = []
    changed = False
    for raw in cast("list[Any]", encoded.body.get("contents") or []):
        content = raw
        if isinstance(raw, dict) and cast("dict[str, Any]", raw).get("role") == "model":
            parts: list[Any] = []
            for part in cast("list[Any]", cast("dict[str, Any]", raw).get("parts") or []):
                if (
                    isinstance(part, dict)
                    and "functionCall" in part
                    and "thoughtSignature" not in part
                ):
                    part = {
                        **cast("dict[str, Any]", part),
                        "thoughtSignature": SKIP_SIGNATURE_VALIDATION,
                    }
                    changed = True
                parts.append(part)
            content = {**cast("dict[str, Any]", raw), "parts": parts}
        contents.append(content)
    if not changed:
        return encoded
    return dataclasses.replace(encoded, body={**encoded.body, "contents": contents})


def _refuses_thinking_off(request: ModelRequest, result: ModelResponse | ModelCallFailure) -> bool:
    """Whether `result` is a model's refusal of `thinkingBudget: 0`: a 400 the API words only as
    an invalid argument, to a request that turned thinking off."""
    reasoning = request.sampling.reasoning
    return (
        reasoning is not None
        and not reasoning.enabled
        and isinstance(result, ModelCallFailure)
        and result.provider_http_status == 400
        and "invalid argument" in result.description.lower()
    )


def _minimal_thinking(adapted: AdaptedRequest) -> AdaptedRequest:
    """Reasoning off sent as the least the model offers, effort `minimal` (`thinkingLevel:
    MINIMAL`), for a model that refuses `thinkingBudget: 0`: it may still think a little."""
    reasoning = adapted.request.sampling.reasoning
    if reasoning is None or reasoning.enabled:
        return adapted
    minimal = ReasoningParams(enabled=True, effort="minimal", summary=reasoning.summary)
    request = dataclasses.replace(
        adapted.request, sampling=dataclasses.replace(adapted.request.sampling, reasoning=minimal)
    )
    return adapted.with_change(
        request,
        RequestLoss(
            "reasoning_off",
            "reasoning off sent as effort minimal, the least this model takes, which does not "
            "guarantee none",
        ),
    )


def _placeholder_signatures(
    encoded: EncodedRequest,
) -> tuple[EncodedRequest, tuple[RequestLoss, ...]]:
    signed = _with_placeholder_signatures(encoded)
    if signed is encoded:
        return encoded, ()
    loss = RequestLoss(
        "thought_signature_placeholder",
        "function calls without a thought signature sent with the API's placeholder, which "
        "skips its signature check",
        semantic=False,
    )
    return signed, (loss,)


_WORKAROUNDS = (
    RejectionWorkaround(
        name=_PLACEHOLDERS,
        recognizes=lambda _prepared, failure: _rejects_unsigned_calls(failure),
        apply_to_wire=_placeholder_signatures,
    ),
    RejectionWorkaround(
        name=_MINIMAL_OFF,
        # Judged by what the caller asked for (thinking off), which an attempt with this
        # workaround applied no longer sends, so its recurring refusal can still be recognized.
        recognizes=lambda prepared, failure: _refuses_thinking_off(prepared.original, failure),
        apply_to_request=_minimal_thinking,
    ),
)


def _report(adaptation: str, model: str) -> None:
    if (adaptation, model) in _reported:
        return
    _reported.add((adaptation, model))
    if adaptation == _PLACEHOLDERS:
        _logger.warning(
            "%r refuses function calls without a thought signature (its API said so): calls in "
            "the history that carry none (another model's, or one a harness did not echo) are "
            "sent with the placeholder signature from now on. Reported once per process per "
            "model.",
            model,
        )
    else:
        _logger.warning(
            "%r refuses thinkingBudget 0 (its API said so): requests to it with reasoning off "
            "are sent with thinkingLevel MINIMAL from now on, the least thinking it offers, "
            "which does not guarantee none. Reported once per process per model.",
            model,
        )


class GeminiGenerateContentAPI:
    """A ``StreamingModelAPI`` serving one model over the Gemini API.

    Attributes are fixed at construction: the model and the client (or the credentials to build
    one). What is learned from the provider is described in the module docstring; it applies to
    requests this instance renders, never to a raw forwarded request.
    """

    def __init__(
        self,
        model: str,
        *,
        client: GeminiClient | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self._model = model
        self._owns_client = client is None
        self._client = client or GeminiClient(api_key=api_key, base_url=base_url)
        self._codec = GeminiGenerateContentCodec(model=model)
        self._workarounds = WorkaroundLedger(
            _WORKAROUNDS, on_confirm=lambda workaround: _report(workaround.name, model)
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
            options_type=_RequestOptions,
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
                options_type=_RequestOptions,
                place_model=_place_model,
            )
        except httpx2.HTTPError as exc:
            raise ForwardTransportError(f"Gemini request failed: {exc}") from exc


class GeminiGenerateContentProvider:
    """A ``ModelProvider`` serving any model over the Gemini API.

    Builds a ``GeminiGenerateContentAPI`` per requested model id on first lookup and caches it;
    every API shares this provider's client.
    """

    def __init__(
        self,
        *,
        client: GeminiClient | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self._owns_client = client is None
        self._client = client or GeminiClient(api_key=api_key, base_url=base_url)
        self._apis: dict[str, GeminiGenerateContentAPI] = {}

    @property
    def client(self) -> GeminiClient:
        """The client every API from this provider sends requests through."""
        return self._client

    def get_model_api(self, model_id: str) -> GeminiGenerateContentAPI:
        api = self._apis.get(model_id)
        if api is None:
            api = GeminiGenerateContentAPI(model_id, client=self._client)
            self._apis[model_id] = api
        return api

    async def aclose(self) -> None:
        """Release the shared client, if this provider constructed it."""
        if self._owns_client:
            await self._client.close()
