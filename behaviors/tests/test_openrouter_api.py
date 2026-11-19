"""`OpenRouterAPI`: the chat-completions transport with OpenRouter's contract fixed."""

from typing import Any, cast

import httpx2
import pytest
from openai import AsyncOpenAI

from behaviors.implementations.dialects.openai_chat_completions import OpenAIChatCompletionsCodec
from behaviors.implementations.models.openai_compatible import OpenAICompatibleAPI
from behaviors.implementations.models.openrouter import OPENROUTER_BASE_URL, OpenRouterAPI
from behaviors.types.content import ContentReasoning, ContentText
from behaviors.types.message import AssistantMessage, UserMessage
from behaviors.types.response import ModelRequest, SamplingParams

_MODEL = "moonshotai/kimi-k3"


def test_openrouter_api_fixes_the_endpoint_the_key_and_the_codec_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    api = OpenRouterAPI(_MODEL)
    assert api.model_id == _MODEL
    codec = api.codec
    assert isinstance(codec, OpenAIChatCompletionsCodec) and codec.variant == "openrouter"
    client = api._client  # pyright: ignore[reportPrivateUsage]
    assert str(client.base_url).rstrip("/") == OPENROUTER_BASE_URL
    assert client.api_key == "or-key"
    # Literal reasoning goes back in OpenRouter's own field.
    request = ModelRequest(
        context=(
            UserMessage(content=(ContentText(text="hi"),)),
            AssistantMessage(
                content=(ContentReasoning(text="hm", form="full"), ContentText(text="ok"))
            ),
        ),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(),
    )
    body = codec.encode_request(_MODEL, request, streaming=True).body
    assert cast("list[dict[str, Any]]", body["messages"])[1]["reasoning"] == "hm"


def test_openrouter_api_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterAPI(_MODEL)
    assert OpenRouterAPI(_MODEL, api_key="k").model_id == _MODEL


def test_openrouter_api_takes_a_ready_client() -> None:
    client = AsyncOpenAI(
        api_key="k", base_url="https://openrouter.test/api/v1", http_client=httpx2.AsyncClient()
    )
    api = OpenRouterAPI("m", client=client)
    assert api._client is client  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ValueError, match="either a client or an api_key"):
        OpenRouterAPI("m", client=client, api_key="k")


def test_compatible_api_takes_a_codec_or_its_options_not_both() -> None:
    codec = OpenAIChatCompletionsCodec(variant="openrouter")
    api = OpenAICompatibleAPI("m", api_key="k", codec=codec)
    assert api.codec is codec
    with pytest.raises(ValueError, match="either a codec or the codec"):
        OpenAICompatibleAPI("m", api_key="k", codec=codec, forward_cache_control=True)
