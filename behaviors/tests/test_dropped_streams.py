"""A provider stream that dies partway through is a ``ModelCallFailure``, not an exception."""

import json
from collections.abc import AsyncIterator, Callable

import httpx2
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from behaviors.implementations.models.anthropic import AnthropicMessagesAPI
from behaviors.implementations.models.openai_compatible import OpenAICompatibleAPI
from behaviors.implementations.models.openai_responses import OpenAIResponsesAPI
from behaviors.interfaces.model_apis import ModelAPI
from behaviors.types.content import ContentText
from behaviors.types.failure import ModelCallFailure
from behaviors.types.message import UserMessage
from behaviors.types.response import ModelRequest, SamplingParams

pytestmark = pytest.mark.anyio

_REQUEST = ModelRequest(
    context=(UserMessage(content=(ContentText(text="hi"),)),),
    tools=(),
    tool_choice=None,
    parallel_tool_calls=None,
    sampling=SamplingParams(max_tokens=16),
)


def _event(name: str | None, data: dict[str, object]) -> bytes:
    prefix = f"event: {name}\n" if name is not None else ""
    return f"{prefix}data: {json.dumps(data)}\n\n".encode()


_FIRST_EVENTS = {
    "anthropic": _event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        },
    ),
    "chat": _event(
        None,
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "m",
            "choices": [{"index": 0, "delta": {"content": "he"}, "finish_reason": None}],
        },
    ),
    "responses": _event(
        "response.created",
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {
                "id": "r1",
                "object": "response",
                "created_at": 0,
                "model": "m",
                "output": [],
                "status": "in_progress",
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
            },
        },
    ),
}


class _DyingStream(httpx2.AsyncByteStream):
    """One valid event, then the connection drops."""

    def __init__(self, first: bytes):
        self._first = first

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        raise httpx2.RemoteProtocolError("peer closed connection without sending complete body")


def _client(kind: str) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_DyingStream(_FIRST_EVENTS[kind]),
        )

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


_APIS: dict[str, Callable[[], ModelAPI]] = {
    "anthropic": lambda: AnthropicMessagesAPI(
        "m", client=AsyncAnthropic(api_key="k", max_retries=0, http_client=_client("anthropic"))
    ),
    "chat": lambda: OpenAICompatibleAPI(
        "m",
        client=AsyncOpenAI(
            api_key="k", base_url="http://x.test/v1", max_retries=0, http_client=_client("chat")
        ),
    ),
    "responses": lambda: OpenAIResponsesAPI(
        "m",
        client=AsyncOpenAI(
            api_key="k",
            base_url="http://x.test/v1",
            max_retries=0,
            http_client=_client("responses"),
        ),
    ),
}


@pytest.mark.parametrize("kind", sorted(_APIS))
async def test_dropped_stream_is_an_overload_failure(kind: str) -> None:
    result = await _APIS[kind]().generate(_REQUEST)
    assert isinstance(result, ModelCallFailure)
    assert result.category == "overloaded"
