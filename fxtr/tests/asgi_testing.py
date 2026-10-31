"""Driving the viewer's app without a server: a request, and a WebSocket as a page holds one."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream
from starlette.types import ASGIApp, Message

HOST = "127.0.0.1:8000"
"""The host every request here is sent to."""

PAGE_ORIGIN = f"http://{HOST}"
"""The origin of the viewer's own pages."""


async def a_request(
    app: ASGIApp,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One request through ``app`` to :data:`HOST`: the status, headers, and body."""
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": body}

    async def send(message: Message) -> None:
        messages.append(message)

    sent = {"host": HOST, **(headers or {})}
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in sent.items()],
    }
    await app(scope, receive, send)
    start = messages[0]
    response_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], response_headers, b"".join(m.get("body", b"") for m in messages[1:])


class PageSocket:
    """A WebSocket to the app as a page holds it: whether the app accepted it, and what it sends."""

    def __init__(self, accepted: bool, sent: MemoryObjectReceiveStream[Message]) -> None:
        self.accepted = accepted
        self._sent = sent

    async def next_event(self, timeout: float = 5) -> dict[str, Any]:
        """The next JSON text message the app sends."""
        with anyio.fail_after(timeout):
            message = await self._sent.receive()
        assert message["type"] == "websocket.send", message
        return json.loads(message["text"])

    async def quiet_for(self, seconds: float) -> bool:
        """Whether the app sends nothing for ``seconds``."""
        with anyio.move_on_after(seconds):
            await self._sent.receive()
            return False
        return True


@contextlib.asynccontextmanager
async def a_page_socket(
    app: ASGIApp, path: str = "/api/events", *, origin: str | None = PAGE_ORIGIN
) -> AsyncIterator[PageSocket]:
    """A WebSocket to ``path`` opened by a page of ``origin`` (by no page when ``None``), which the
    page closes on leaving the block."""
    to_app_send, to_app = anyio.create_memory_object_stream[Message](8)
    from_app, sent = anyio.create_memory_object_stream[Message](64)
    headers = [(b"host", HOST.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {
        "type": "websocket",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "subprotocols": [],
    }

    async def run() -> None:
        async with from_app:
            await app(scope, to_app.receive, from_app.send)

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await to_app_send.send({"type": "websocket.connect"})
        with anyio.fail_after(10):
            first = await sent.receive()
        socket = PageSocket(first["type"] == "websocket.accept", sent)
        try:
            yield socket
        finally:
            if socket.accepted:
                await to_app_send.send({"type": "websocket.disconnect", "code": 1001})
