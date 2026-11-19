"""Which SDK clients the Anthropic and OpenAI model APIs accept, and the credentials they send.

The model APIs build requests with the SDK but send them through the shared transport
(`_raw_stream`), which does not run the SDK's send-time steps. These tests pin what follows: the
OpenAI adapters authenticate as the SDK's own model methods do, and a client that relies on a
send-time step is refused when the model API is constructed.
"""

import json

import httpx2
import pytest
from anthropic import AsyncAnthropic
from anthropic.lib.credentials import StaticToken
from openai import AsyncOpenAI

from behaviors.implementations.models.anthropic import (
    AnthropicMessagesAPI,
    AnthropicMessagesProvider,
)
from behaviors.implementations.models.openai_compatible import OpenAICompatibleAPI
from behaviors.implementations.models.openai_responses import OpenAIResponsesAPI
from behaviors.types.content import ContentText
from behaviors.types.message import UserMessage
from behaviors.types.response import ModelRequest, SamplingParams

pytestmark = pytest.mark.anyio

_REQUEST = ModelRequest(
    context=(UserMessage(content=(ContentText(text="question"),)),),
    tools=(),
    tool_choice=None,
    parallel_tool_calls=None,
    sampling=SamplingParams(),
)


def _capturing_client() -> tuple[httpx2.AsyncClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(401, json={"error": {"message": "mock endpoint"}})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), seen


@pytest.mark.parametrize("api_type", [OpenAICompatibleAPI, OpenAIResponsesAPI])
async def test_an_openai_admin_key_never_reaches_a_model_endpoint(
    api_type: type[OpenAICompatibleAPI] | type[OpenAIResponsesAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    http, seen = _capturing_client()
    client = AsyncOpenAI(
        admin_api_key="fixture-admin", base_url="https://model.invalid/v1", http_client=http
    )
    # As with the SDK's own model methods, the admin key is not a credential for this endpoint,
    # so the SDK refuses the request before anything is sent.
    with pytest.raises(TypeError, match="Could not resolve authentication method"):
        await api_type("m", client=client).generate(_REQUEST)
    assert seen == []


async def test_an_openai_model_key_is_sent_alongside_a_configured_admin_key() -> None:
    http, seen = _capturing_client()
    client = AsyncOpenAI(
        api_key="fixture-model",
        admin_api_key="fixture-admin",
        base_url="https://model.invalid/v1",
        http_client=http,
    )
    await OpenAICompatibleAPI("m", client=client).generate(_REQUEST)
    assert [request.headers["authorization"] for request in seen] == ["Bearer fixture-model"]
    assert json.loads(seen[0].content)["model"] == "m"


@pytest.mark.parametrize("api_type", [AnthropicMessagesAPI, AnthropicMessagesProvider])
def test_an_anthropic_credential_provider_is_refused(
    api_type: type[AnthropicMessagesAPI] | type[AnthropicMessagesProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    client = AsyncAnthropic(credentials=StaticToken("fixture-token"))
    with pytest.raises(ValueError, match="authenticates at send time"):
        if api_type is AnthropicMessagesAPI:
            AnthropicMessagesAPI("m", client=client)
        else:
            AnthropicMessagesProvider(client=client)


def test_a_client_that_changes_requests_at_send_time_is_refused() -> None:
    class SigningClient(AsyncAnthropic):
        async def _prepare_request(self, request: httpx2.Request) -> None:
            request.headers["x-signature"] = "signed"

    with pytest.raises(ValueError, match="changes requests at send time"):
        AnthropicMessagesAPI("m", client=SigningClient(api_key="fixture"))


def test_a_static_key_client_is_accepted() -> None:
    AnthropicMessagesAPI("m", client=AsyncAnthropic(api_key="fixture"))
    OpenAICompatibleAPI("m", client=AsyncOpenAI(api_key="fixture"))
