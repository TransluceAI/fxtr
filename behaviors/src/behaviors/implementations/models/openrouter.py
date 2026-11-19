"""OpenRouter (https://openrouter.ai), an OpenAI-compatible endpoint that fronts many providers.

``OpenRouterAPI`` is ``OpenAICompatibleAPI`` with what OpenRouter needs fixed: the base URL, the
key (``OPENROUTER_API_KEY`` unless given), and the codec's ``openrouter`` variant, which reads and
echoes the ``reasoning_details`` OpenRouter returns for signed upstream reasoning (Claude's
thinking, OpenAI's encrypted continuations) and its ``usage`` extensions
(``behaviors.implementations.dialects.openai_chat_completions``). Literal reasoning is echoed in
the ``reasoning`` field, OpenRouter's own name for it. Routing options (``provider``, model
variants) go in ``extra_body`` as OpenRouter documents them.
"""

import os
from typing import Any

from openai import AsyncOpenAI

from behaviors.implementations.dialects.openai_chat_completions import (
    MaxTokensParam,
    OpenAIChatCompletionsCodec,
    ReasoningEchoField,
)
from behaviors.implementations.models.openai_compatible import OpenAICompatibleAPI

__all__ = ["OPENROUTER_BASE_URL", "OpenRouterAPI"]

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"


class OpenRouterAPI(OpenAICompatibleAPI):
    """A ``StreamingModelAPI`` serving one model through OpenRouter.

    Args:
        model: The OpenRouter model slug (``anthropic/claude-sonnet-4.5``, ``moonshotai/kimi-k3``,
            ...), with any routing variant suffix OpenRouter accepts.
        client: A ready ``AsyncOpenAI`` client pointed at OpenRouter, instead of ``api_key``.
        api_key: The OpenRouter key; ``OPENROUTER_API_KEY`` when omitted.
        extra_body: OpenRouter request options filled in where a request leaves the key absent
            (``provider`` routing preferences, for example).
        forward_cache_control: Forward prompt-cache breakpoints as ``cache_control`` parts, for
            upstreams that honor them.
        max_tokens_param: Which parameter carries the output-token limit.
        reasoning_echo_field: The field literal reasoning is echoed in; OpenRouter's is
            ``reasoning``.
    """

    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
        api_key: str | None = None,
        extra_body: dict[str, Any] | None = None,
        forward_cache_control: bool = False,
        max_tokens_param: MaxTokensParam = "max_tokens",
        reasoning_echo_field: ReasoningEchoField | None = "reasoning",
    ) -> None:
        codec = OpenAIChatCompletionsCodec(
            forward_cache_control=forward_cache_control,
            max_tokens_param=max_tokens_param,
            reasoning_echo_field=reasoning_echo_field,
            extra_body=extra_body,
            variant="openrouter",
        )
        if client is not None:
            if api_key is not None:
                raise ValueError("pass either a client or an api_key, not both")
            super().__init__(model, client=client, codec=codec)
            return
        key = api_key or os.environ.get(OPENROUTER_API_KEY_ENV)
        if not key:
            raise ValueError(
                f"OpenRouterAPI needs a key: pass api_key or set {OPENROUTER_API_KEY_ENV}"
            )
        super().__init__(model, api_key=key, base_url=OPENROUTER_BASE_URL, codec=codec)
