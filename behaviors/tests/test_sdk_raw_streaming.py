"""The provider SDKs' request preparation plus their httpx2 client can send a body we rendered
ourselves and hand back the raw response *as a stream*: the call returns before the body is read,
chunks arrive as the server produces them, and closing the response early releases the connection.

This is what lets one bytes-level decoder serve both a normal `ModelAPI` call and the observation of
a forwarded request (`CodecModelAPI.forward_request`), with no second decoder: the SDK keeps
preparing the request (credentials, base URL, default headers, timeout) and its typed event parsing
is bypassed by sending with its httpx2 client directly (`_raw_stream.post_raw`). If an SDK upgrade
breaks any of this, these tests say so before anything built on it does.

The response body is a gated stream rather than canned bytes: a `MockTransport` response built from
`content=` is already consumed, so a test on it would pass even if the SDK read the whole body
before returning, which is exactly the buffering these checks exist to rule out.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
import pytest
from anthropic import AsyncAnthropic
from anthropic._models import FinalRequestOptions as AnthropicOptions
from openai import AsyncOpenAI
from openai._models import FinalRequestOptions as OpenAIOptions

from behaviors.implementations.models._raw_stream import post_raw
from behaviors.types.dialect import EncodedRequest

pytestmark = pytest.mark.anyio

_FIRST = b'event: message_start\ndata: {"type":"message_start"}\n\n'
_SECOND = b'event: ping\ndata: {"type":"ping"}\n\n'


@dataclass
class _GatedStream(httpx2.AsyncByteStream):
    """A response body that yields one chunk, then waits for `release` before ending.

    `started` says the body was iterated at all, `finished` that it was drained, `closed` that
    the transport was told to release it.
    """

    started: bool = False
    finished: bool = False
    closed: bool = False
    release: anyio.Event = field(default_factory=anyio.Event)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started = True
        yield _FIRST
        await self.release.wait()
        yield _SECOND
        self.finished = True

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class _Upstream:
    seen: list[httpx2.Request] = field(default_factory=list[httpx2.Request])
    stream: _GatedStream = field(default_factory=_GatedStream)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": "req_1"},
            stream=self.stream,
        )

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handler)


_ENCODED = EncodedRequest(path="/v1/messages", body={"model": "m", "input": "hi"})


async def _raw_post(client: Any, path: str, options_type: Any) -> httpx2.Response:
    # An SDK that read the whole body before returning would wait on the gate forever; the
    # deadline turns that into a failure rather than a hung test.
    with anyio.fail_after(5):
        return await post_raw(client, _ENCODED, path=path, options_type=options_type)


async def _assert_streams(response: httpx2.Response, upstream: _Upstream) -> None:
    assert upstream.stream.started is False  # the call returned without touching the body
    chunks = response.aiter_bytes()
    assert await anext(chunks) == _FIRST  # the first chunk arrives while the stream is open
    assert upstream.stream.finished is False
    assert response.headers["request-id"] == "req_1"
    await response.aclose()  # giving up early releases the connection
    assert upstream.stream.closed is True
    aclose: Any = getattr(chunks, "aclose")  # httpx2 types the iterator without its aclose
    await aclose()


async def test_anthropic_client_streams_the_raw_response() -> None:
    upstream = _Upstream()
    client = AsyncAnthropic(
        api_key="k", http_client=httpx2.AsyncClient(transport=upstream.transport())
    )
    response = await _raw_post(client, "/v1/messages", AnthropicOptions)
    await _assert_streams(response, upstream)
    (request,) = upstream.seen
    assert {k: v for k, v in request.headers.items() if k in {"x-api-key", "authorization"}} in (
        {"x-api-key": "k"},
        {"authorization": "Bearer k"},
    )
    assert request.headers["anthropic-version"]  # the SDK's own defaults are on the request
    assert request.content == b'{"model":"m","input":"hi"}'  # our body, verbatim


async def test_openai_client_streams_the_raw_response() -> None:
    upstream = _Upstream()
    client = AsyncOpenAI(
        api_key="k", http_client=httpx2.AsyncClient(transport=upstream.transport())
    )
    response = await _raw_post(client, "/responses", OpenAIOptions)
    await _assert_streams(response, upstream)
    (request,) = upstream.seen
    assert request.headers["authorization"] == "Bearer k"
    assert str(request.url) == "https://api.openai.com/v1/responses"
