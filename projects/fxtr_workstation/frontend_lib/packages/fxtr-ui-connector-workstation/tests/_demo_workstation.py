"""Demo workstation server for the JS integration tests.

Boots a :class:`WorkstationServer`, registers a tiny entity and a
tiny service via a peer RPC connection, prints ``READY <port>``
to stdout, and parks until SIGTERM/SIGINT.

Run via ``uv run`` from the workstation package (the JS test
harness handles this); ``uv`` walks up the directory tree to
find ``projects/fxtr_workstation/pyproject.toml`` and uses that
venv.

Trimmed model of
``projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/run_demo_workstation.py``
+
``projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/tests/test_mcp_in_memory.py::_running_workstation``
— no sandbox, no authorizer, no callback-tester. Just enough
state for the JS tests to:

  - find an entity short_id via ``listEntities`` and write a ref
    into session state;
  - find a service short_id via ``listServices`` and same;
  - verify Track/Untrack ops fired by subscribing to
    ``session_debug``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import signal
import sys
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Protocol

import anyio
import anyio.abc
import httpx_ws
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.rpc.json_serdes import JSONTypes, wrap_json_stream
from fxtr2.rpc.scope_bridge import RPCSession, open_rpc_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types.json_object_stream import JSONObjectStream
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.protocols import WorkstationServiceProto, WorkstationSessionEventInbox

ENTITY_NAME = "e-test"
SERVICE_NAME = "s-test"

_DEMO_NS = uuid.UUID("3a8e1b22-7c4f-4e91-a5d3-1b6c9e0a4f7d")
_NOTE_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "frontend_lib_note")
_TEST_SVC_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "frontend_lib_test_svc")


@dataclasses.dataclass(frozen=True)
class _Note(DataclassEntity, fxtr_type=_NOTE_TYPE_ID):
    """Trivial entity used as a Track/Untrack target. The JS tests
    don't load it; they just write its short_id ref into session
    state and verify the workstation records a refcount."""

    body: str


@service_protocol
class _TestServiceProto(Protocol):
    """Methods exercised by the JS-side invoke + callback integration tests.

    - ``echo(value)`` returns its argument unchanged so we can
      verify arbitrary structured payloads (primitives, dicts,
      lists, entity / service refs) round-trip through the wire.
    - ``combine(value, *, name="default")`` exercises the kwargs
      path: a positional ``value`` plus a keyword-only ``name``.
      Returns a dict bundling both so the JS test can introspect
      what the Python side received.
    - ``fail(message)`` raises so we can test the
      ``invoke_error`` path end-to-end.
    - ``notify_n(cb, n, prefix)`` calls ``cb.notify(f"{prefix}-{i}")``
      ``n`` times in sequence, in-call (no background tasks), and
      returns the number that succeeded before the callback closed.
      Used by the stage-6 callback-inbox integration tests.
    """

    async def echo(self, value: Any) -> Any: ...

    async def combine(self, value: Any, *, name: str = "default") -> dict[str, Any]: ...

    async def fail(self, message: str) -> Any: ...

    async def notify_n(
        self, cb: ServiceRef[WorkstationSessionEventInbox], n: int, prefix: str
    ) -> int: ...


class _TestServiceImpl(TypedServiceImpl[_TestServiceProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return _TestServiceProto

    def _service_type_id(self) -> FxtrID:
        return _TEST_SVC_TYPE_ID

    @service_method
    async def echo(self, value: Any) -> Any:
        return value

    @service_method
    async def combine(self, value: Any, *, name: str = "default") -> dict[str, Any]:
        return {"value": value, "name": name}

    @service_method
    async def fail(self, message: str) -> Any:
        raise RuntimeError(message)

    @service_method
    async def notify_n(
        self, cb: ServiceRef[WorkstationSessionEventInbox], n: int, prefix: str
    ) -> int:
        """Call ``cb.notify("<prefix>-<i>")`` ``n`` times in sequence.

        Resolves the callback ref against the active call scope
        (the dispatch scope) and awaits each notify in-line, so
        all CallbackEvents fire before the invoke returns. Direct
        push is fire-and-forget — a closed inbox silently drops, so
        the loop always runs ``n`` times.

        Returns ``n``. The JS test verifies both the return value
        (via the invoke result) and the inbox.events atom (one
        entry per notify, in order).
        """
        call_scope = get_active_scope()
        proxy = await cb.resolve(call_scope)
        for i in range(n):
            await proxy.notify(f"{prefix}-{i}")
        return n


# --- Peer RPC plumbing (lifted from the workstation/MCP test helpers) ---


@contextlib.asynccontextmanager
async def _httpx_ws_object_stream(
    ws: httpx_ws.AsyncWebSocketSession,
) -> AsyncGenerator[anyio.abc.ObjectStream[JSONTypes]]:
    async def _send(item: JSONTypes) -> None:
        try:
            await ws.send_text(json.dumps(item))
        except httpx_ws.HTTPXWSException as e:
            raise anyio.BrokenResourceError from e

    async def _receive() -> JSONTypes:
        try:
            text = await ws.receive_text()
        except httpx_ws.HTTPXWSException:
            raise anyio.EndOfStream from None
        return json.loads(text)

    stream = JSONObjectStream(_send, _receive)
    try:
        yield stream
    finally:
        await stream.aclose()


@contextlib.asynccontextmanager
async def _connect_peer_rpc(rpc_url: str) -> AsyncGenerator[RPCSession[WorkstationServiceProto]]:
    """Open a workstation ``/rpc`` peer connection. Returns an
    :class:`RPCSession` typed as :class:`WorkstationServiceProto`.
    """
    async with httpx_ws.aconnect_ws(rpc_url) as ws:
        async with _httpx_ws_object_stream(ws) as raw:
            transport = wrap_json_stream(raw)
            async with open_rpc_scope(
                transport,
                our_main_service=None,
                remote_service_type=WorkstationServiceProto,
                name="demo-workstation/peer-rpc",
            ) as session:
                yield session


# --- Main ---------------------------------------------------------------


async def _amain() -> None:
    async with WorkstationServer() as ws:
        async with _connect_peer_rpc(ws.rpc_url) as peer:
            note_ref = _Note(body="frontend_lib integration test note").store(peer.scope)
            svc_ref = await peer.scope.mount(_TestServiceImpl())
            async with (
                expose_entity(peer.remote_service, ENTITY_NAME, note_ref.entity_id, peer.scope),
                expose_service(peer.remote_service, SERVICE_NAME, svc_ref, peer.scope),
            ):
                # `ws.bound_url` is `ws://127.0.0.1:<port>`. The JS
                # harness reads this line from stdout and pulls the
                # port out.
                print(f"READY {ws.bound_url}", flush=True)
                # Park until SIGTERM/SIGINT. anyio's signal handling
                # turns the signal into a clean cancellation that
                # unwinds the async-context stack.
                with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                    async for _ in signals:
                        break


def main() -> None:
    try:
        anyio.run(_amain)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
