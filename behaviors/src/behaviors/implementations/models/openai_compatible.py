"""OpenAI-compatible Chat Completions implementation of ``StreamingModelAPI``.

One class covers every endpoint speaking the OpenAI Chat Completions dialect: OpenAI itself,
OpenRouter, Anthropic's and Gemini's OpenAI-compatible endpoints, and self-hosted vLLM/sglang
servers (``base_url`` + ``api_key="dummy"``).

A thin transport over the ``openai`` SDK: the request body comes from
``OpenAIChatCompletionsCodec.encode_request`` and the streamed response is parsed by its SSE
decoder, so what this module owns is credentials, the base URL and the HTTP round trip. The SDK's
own request shaping and typed chunk parsing are bypassed (its base client ``post`` under the
raw-response header returns the untouched ``httpx2.Response``), and its retries are off per request
(retry policy belongs to the caller, see ``generate_with_retries``).

Requests always stream; ``generate`` is ``generate_with_events`` with no callback. Failures are
returned as ``ModelCallFailure`` data, never raised. Rendering and parsing rules (reasoning echo
field, cache-control forwarding, the output-token parameter name, the codec's ``variant``) are
documented on the codec (``behaviors.implementations.dialects.openai_chat_completions``); the
OpenRouter variant has its own convenience class, ``OpenRouterAPI`` (``.openrouter``).
"""

from typing import Any

from openai import AsyncOpenAI

from behaviors.implementations.dialects.openai_chat_completions import (
    ChatCompletionsVariant,
    MaxTokensParam,
    OpenAIChatCompletionsCodec,
    ReasoningEchoField,
)
from behaviors.implementations.models._openai_options import ModelEndpointOptions
from behaviors.implementations.models._raw_stream import (
    call_and_decode,
    require_supported_client,
    sdk_relative_path,
)
from behaviors.interfaces.dialect_codecs import ModelSideCodec
from behaviors.types.dialect import Dialect
from behaviors.types.failure import ModelCallFailure
from behaviors.types.response import ModelRequest, ModelResponse
from behaviors.types.streaming import StreamEvent, StreamEventCallback

__all__ = [
    "ChatCompletionsVariant",
    "MaxTokensParam",
    "OpenAICompatibleAPI",
    "OpenAICompatibleProvider",
    "ReasoningEchoField",
]


async def _noop_callback(event: StreamEvent) -> None:
    pass


class OpenAICompatibleAPI:
    """A ``StreamingModelAPI`` serving one model behind an OpenAI-compatible endpoint.

    Attributes are fixed at construction: the model, the client (or the base URL / credentials to
    build one), and the codec, given whole (``codec``) or by its options: instance-level
    ``extra_body`` filled into every request where it leaves the key absent (e.g. OpenRouter
    provider routing), whether to forward prompt-cache breakpoints, which parameter name carries
    the output-token limit, which assistant-message field carries echoed literal reasoning, and
    the wire ``variant`` (see the codec module).
    """

    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        codec: OpenAIChatCompletionsCodec | None = None,
        extra_body: dict[str, Any] | None = None,
        forward_cache_control: bool = False,
        max_tokens_param: MaxTokensParam = "max_tokens",
        reasoning_echo_field: ReasoningEchoField | None = "reasoning_content",
        variant: ChatCompletionsVariant = "standard",
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        if codec is not None and (
            extra_body is not None
            or forward_cache_control
            or max_tokens_param != "max_tokens"
            or reasoning_echo_field != "reasoning_content"
            or variant != "standard"
        ):
            raise ValueError("pass either a codec or the codec's options, not both")
        self._model = model
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        require_supported_client(self._client, AsyncOpenAI)
        self._codec = codec or OpenAIChatCompletionsCodec(
            forward_cache_control=forward_cache_control,
            max_tokens_param=max_tokens_param,
            reasoning_echo_field=reasoning_echo_field,
            extra_body=extra_body,
            variant=variant,
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
        )


class OpenAICompatibleProvider:
    """A ``ModelProvider`` serving any model behind an OpenAI-compatible endpoint.

    Builds an ``OpenAICompatibleAPI`` per requested model id on first lookup and caches it; every
    API shares this provider's client, so ``aclose`` here is the one lifecycle call.
    """

    def __init__(
        self,
        *,
        client: AsyncOpenAI | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        extra_body: dict[str, Any] | None = None,
        forward_cache_control: bool = False,
        max_tokens_param: MaxTokensParam = "max_tokens",
        reasoning_echo_field: ReasoningEchoField | None = "reasoning_content",
        variant: ChatCompletionsVariant = "standard",
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        require_supported_client(self._client, AsyncOpenAI)
        self._extra_body = dict(extra_body) if extra_body else {}
        self._forward_cache_control = forward_cache_control
        self._max_tokens_param: MaxTokensParam = max_tokens_param
        self._reasoning_echo_field: ReasoningEchoField | None = reasoning_echo_field
        self._variant: ChatCompletionsVariant = variant
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
                reasoning_echo_field=self._reasoning_echo_field,
                variant=self._variant,
            )
            self._apis[model_id] = api
        return api

    async def aclose(self) -> None:
        """Release the shared client, if this provider constructed it."""
        if self._owns_client:
            await self._client.close()
