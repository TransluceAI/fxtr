"""Unit tests for ``ModelProvider`` implementations: routing, fall-through, and caching."""

import json
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from behaviors.implementations.models.anthropic import FAST_MODE_BETA, AnthropicMessagesProvider
from behaviors.implementations.models.openai_compatible import OpenAICompatibleProvider
from behaviors.implementations.models.openai_responses import OpenAIResponsesProvider
from behaviors.implementations.models.providers import ModelOverrides, SingleModelProvider
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import ContentText
from behaviors.types.failure import ModelCallFailure
from behaviors.types.message import UserMessage
from behaviors.types.response import ModelRequest, ModelResponse, ReasoningParams, SamplingParams

pytestmark = pytest.mark.anyio


@dataclass(frozen=True)
class _StubModel:
    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        raise AssertionError("routing tests never call the model")


def test_single_model_provider_serves_every_id() -> None:
    api = _StubModel()
    provider = SingleModelProvider(api)
    assert provider.get_model_api("claude-opus-5") is api
    assert provider.get_model_api("anything-else") is api


def test_model_overrides_match_exactly_and_fall_through_to_base() -> None:
    rerouted, fallback = _StubModel(), _StubModel()
    provider = ModelOverrides(
        overrides={"claude-haiku-4-5": rerouted}, base=SingleModelProvider(fallback)
    )
    assert provider.get_model_api("claude-haiku-4-5") is rerouted
    assert provider.get_model_api("claude-opus-5") is fallback


def test_model_overrides_without_base_serve_only_listed_ids() -> None:
    provider = ModelOverrides(overrides={"served": _StubModel()})
    assert provider.get_model_api("served") is not None
    assert provider.get_model_api("unlisted") is None


async def test_anthropic_provider_caches_one_api_per_model_id() -> None:
    provider = AnthropicMessagesProvider(api_key="test-key")
    try:
        opus = provider.get_model_api("claude-opus-5")
        assert provider.get_model_api("claude-opus-5") is opus
        assert provider.get_model_api("claude-haiku-4-5") is not opus
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "provider",
    [AnthropicMessagesProvider, OpenAICompatibleProvider, OpenAIResponsesProvider],
)
async def test_providers_build_clients_without_sdk_retries(
    provider: type[AnthropicMessagesProvider | OpenAICompatibleProvider | OpenAIResponsesProvider],
) -> None:
    instance = provider(api_key="test-key")
    try:
        client: AsyncAnthropic | AsyncOpenAI = instance.client
        assert client.max_retries == 0
    finally:
        await instance.aclose()


async def test_openai_compatible_provider_applies_its_settings_to_every_model() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(400, json={"error": {"message": "captured"}})

    client = AsyncOpenAI(
        api_key="test-key",
        base_url="http://endpoint.test/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    provider = OpenAICompatibleProvider(
        client=client,
        extra_body={"provider": {"order": ["a"]}},
        max_tokens_param="max_completion_tokens",
    )
    request = ModelRequest(
        context=(UserMessage(content=(ContentText(text="hi"),)),),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(max_tokens=7),
    )
    try:
        assert provider.client is client
        for model_id in ("model-a", "model-b"):
            result = await provider.get_model_api(model_id).generate(request)
            assert isinstance(result, ModelCallFailure)
    finally:
        await client.close()

    assert [body["model"] for body in bodies] == ["model-a", "model-b"]
    for body in bodies:
        assert body["provider"] == {"order": ["a"]}
        assert body["max_completion_tokens"] == 7
        assert "max_tokens" not in body


async def test_anthropic_provider_applies_its_request_options_to_every_model() -> None:
    sent: list[tuple[dict[str, Any], str | None]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append((json.loads(request.content), request.headers.get("anthropic-beta")))
        return httpx2.Response(400, json={"type": "error", "error": {"message": "captured"}})

    client = AsyncAnthropic(
        api_key="test-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    provider = AnthropicMessagesProvider(
        client=client,
        final_block_cache_control=CacheControl(ttl="1h"),
        thinking_display="omitted",
        fast_mode=True,
    )
    request = ModelRequest(
        context=(UserMessage(content=(ContentText(text="hi"),)),),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(reasoning=ReasoningParams()),
    )
    try:
        for model_id in ("claude-a", "claude-b"):
            result = await provider.get_model_api(model_id).generate(request)
            assert isinstance(result, ModelCallFailure)
    finally:
        await client.close()

    assert [body["model"] for body, _ in sent] == ["claude-a", "claude-b"]
    for body, beta in sent:
        assert body["thinking"] == {"type": "adaptive", "display": "omitted"}
        assert body["messages"][-1]["content"][-1]["cache_control"] == {
            "type": "ephemeral",
            "ttl": "1h",
        }
        assert body["speed"] == "fast"
        assert beta == FAST_MODE_BETA
