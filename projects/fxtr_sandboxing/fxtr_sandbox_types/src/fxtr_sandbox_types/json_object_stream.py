"""JSON ObjectStream over async send/receive callables, with escape-based EOF.

For runtimes whose host channel is a JSON-typed callback pair (rather than a
raw byte stream) — eryx is the canonical case: ``_send_to_host`` /
``_receive_from_host`` round-trip JSON-typed values, with no underlying
transport-level half-close to exploit. To layer the fxtr2 RPC bridge on top,
we need an `anyio.abc.ObjectStream[JSONTypes]` that
`fxtr2.rpc.json_serdes.wrap_json_stream` (and friends) can consume — this
adapter does exactly that.

For runtimes whose channel *is* a raw byte stream (UDS, TCP), prefer
`SocketJSONStream` from `fxtr_sandbox_types.socket_json_stream`, which uses
the underlying transport's native half-close instead of an in-band escape.

Framing for JSON lists whose first element is the string `"!"` avoids collision
with control messages:

- Payload lists that naturally start with ``"!"`` are sent as
  ``["!", "escape", <payload>]`` via `send`; `receive` unwraps them.

- Graceful EOF is signaled with ``["!", "eof"]`` from `send_eof`; the peer's
  first `receive` after that raises `anyio.EndOfStream`, as do all later receives
  until `aclose`.

Invalid ``"!"`` lists on the wire raise `ValueError`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Mapping, Sequence

import anyio
import anyio.abc

type JSONTypes = (
    None | bool | int | float | str | Sequence["JSONTypes"] | Mapping[str, "JSONTypes"]
)


class JSONObjectStream(anyio.abc.ObjectStream[JSONTypes]):
    """Anyio-compatible ObjectStream[JSONTypes] backed by async send/receive callables.

    Duck-types the `ObjectStream[JSONTypes]` interface — `send`, `receive`,
    `__aiter__`/`__anext__`, `aclose`, `send_eof` — that downstream stream
    transports expect. `__aiter__`/`__anext__` are required because
    `SimpleJSONTransportContext._read_loop` iterates the stream with
    `async for`. `aclose()` flips an internal flag so subsequent ops raise
    `ClosedResourceError`. After an EOF control is received from the peer,
    `receive()` raises `anyio.EndOfStream` permanently (until superseded only
    by closing semantics upstream). `send_eof()` emits the ``["!", "eof"]``
    sentinel on the underlying send callable.
    """

    def __init__(
        self,
        sender: Callable[[Any], Awaitable[None]],
        receiver: Callable[[], Awaitable[Any]],
    ) -> None:
        self._send = sender
        self._receive = receiver
        self._closed = False
        self._eof = False

    async def send(self, item: JSONTypes) -> None:
        if self._closed:
            raise anyio.ClosedResourceError
        if isinstance(item, list) and len(item) > 0 and item[0] == "!":
            await self._send(["!", "escape", item])
        else:
            await self._send(item)

    async def receive(self) -> JSONTypes:
        if self._closed:
            raise anyio.ClosedResourceError
        if self._eof:
            raise anyio.EndOfStream
        raw = await self._receive()
        if isinstance(raw, list) and len(raw) > 0 and raw[0] == "!":
            if len(raw) == 2 and raw[1] == "eof":
                self._eof = True
                raise anyio.EndOfStream
            if len(raw) == 3 and raw[1] == "escape":
                return raw[2]
            raise ValueError(f"invalid object stream control frame: {raw!r}")
        return raw

    def __aiter__(self) -> "JSONObjectStream":
        return self

    async def __anext__(self) -> JSONTypes:
        try:
            return await self.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            raise StopAsyncIteration

    async def aclose(self) -> None:
        self._closed = True

    async def send_eof(self) -> None:
        if self._closed:
            raise anyio.ClosedResourceError
        await self._send(["!", "eof"])
