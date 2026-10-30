"""Demo workstation backend for `@fxtr-ui/demo`.

Boots a :class:`WorkstationServer`, registers a tiny entity and a
tiny service via a peer RPC connection (mirroring the integration
tests' harness), prints ``READY <ws-url>`` to stdout, and parks
until SIGTERM/SIGINT. The demo's `server.ts` spawns this via
``uv run python src/demo_workstation.py``, reads the URL from the
READY line, and points Vite at it.

Adapted from
``packages/fxtr-ui-connector-workstation/tests/_demo_workstation.py``.
For chunk 6 (mutate-only) the entity + service aren't exercised by
the UI yet; they're left in place so chunks 7 (load) and 8 (invoke)
have a target to hit without re-touching the backend.
"""

from __future__ import annotations

import dataclasses
import random
import signal
import sys
import uuid
from typing import Any, Protocol

import anyio
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.peer_rpc import open_workstation_rpc
from fxtr_workstation.protocols import WorkstationSessionEventInbox

ENTITY_NAME = "demo-note"
SERVICE_NAME = "demo-service"

_DEMO_NS = uuid.UUID("c1f4b8b2-3a1e-4e5d-9c8f-2b3d4e5a6f78")
_NOTE_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "fxtr_ui_demo_note")
_DEMO_SVC_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "fxtr_ui_demo_service")


@dataclasses.dataclass(frozen=True)
class _Note(DataclassEntity, fxtr_type=_NOTE_TYPE_ID):
    """A trivial entity for the demo to load + display."""

    body: str


@service_protocol
class _DemoServiceProto(Protocol):
    """Demo service exercised by the invoke + inbox UI.

    - ``echo(value)`` returns its argument unchanged.
    - ``combine(value, *, name="default")`` exercises kwargs.
    - ``fail(message)`` raises so the UI can show the error path.
    - ``notify_random(cb)`` picks a random int in ``[0, 99]`` and
      calls ``cb.notify(value)`` once, in-call. Returns the value
      sent (or ``-1`` if the callback was closed). Used by the
      "notify random" button on the demo's inbox cards.
    """

    async def echo(self, value: Any) -> Any: ...

    async def combine(self, value: Any, *, name: str = "default") -> dict[str, Any]: ...

    async def fail(self, message: str) -> Any: ...

    async def notify_random(self, cb: ServiceRef[WorkstationSessionEventInbox]) -> int: ...


class _DemoServiceImpl(TypedServiceImpl[_DemoServiceProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return _DemoServiceProto

    def _service_type_id(self) -> FxtrID:
        return _DEMO_SVC_TYPE_ID

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
    async def notify_random(self, cb: ServiceRef[WorkstationSessionEventInbox]) -> int:
        """Pick a random int and notify the callback with it.

        Resolves ``cb`` against the active call scope (the dispatch
        scope) and awaits the notify in-line so the resulting
        :class:`CallbackEvent` fires before this invoke returns.
        Direct push is fire-and-forget (a closed inbox silently
        drops), so the picked value is returned unconditionally.
        """
        call_scope = get_active_scope()
        proxy = await cb.resolve(call_scope)
        value = random.randint(0, 99)
        await proxy.notify(value)
        return value


# --- Main ---------------------------------------------------------------


async def _amain() -> None:
    async with WorkstationServer(
        # Enable the bundled debug UI at `/debug` on the HTTP side of
        # the workstation. Useful for poking at session refcounts,
        # preserved events, in-flight invokes, etc. while exercising
        # the demo. URL is logged after READY below.
        debug_ui_dist_path=WorkstationServer.bundled_debug_ui_dist_path(),
    ) as ws:
        async with open_workstation_rpc(ws.rpc_url, name="fxtr-ui-demo/peer-rpc") as peer:
            note_ref = _Note(body="hello from the demo workstation").store(peer.scope)
            svc_ref = await peer.scope.mount(_DemoServiceImpl())
            async with (
                expose_entity(peer.remote_service, ENTITY_NAME, note_ref.entity_id, peer.scope),
                expose_service(peer.remote_service, SERVICE_NAME, svc_ref, peer.scope),
            ):
                # `ws.bound_url` is `ws://127.0.0.1:<port>`. The TS
                # orchestrator reads this line from stdout.
                print(f"READY {ws.bound_url}", flush=True)
                # Debug UI URL is a separate line so the orchestrator
                # can pull it out without affecting the READY parser.
                print(f"DEBUG_UI {ws.debug_http_url}", flush=True)
                # Park until SIGTERM/SIGINT. anyio's signal handling
                # turns the signal into a clean cancellation.
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
