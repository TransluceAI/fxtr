"""Backend for `@fxtr-ui/dispatch-demo`.

Boots a :class:`WorkstationServer`, registers two distinct
entity types (notes + photos) and two distinct service types
(echo + counter) under a single peer RPC connection, and prints
the type-id strings to stdout so the TS orchestrator can plumb
them through Vite into the dispatch registry.

stdout protocol:

    TYPE_IDS {...json...}
    READY ws://...
    DEBUG_UI http://...

The TYPE_IDS line is a JSON dict keyed by an externally-stable
label (`note`, `photo`, `echo`, `counter`) — the TS dispatch
registry keys components by these id strings.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import signal
import sys
import uuid
from typing import Any, Protocol

import anyio
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.peer_rpc import open_workstation_rpc

# Stable namespace so the type-ids are deterministic across runs;
# the TS side reads them through the TYPE_IDS line so absolute
# stability isn't required, but determinism makes debugging
# easier.
_DEMO_NS = uuid.UUID("8f3a2c7e-1b4d-4e2a-9c5f-d6e7a8b9c0d1")

# --- Entity types --------------------------------------------------------

_NOTE_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "fxtr_ui_dispatch_demo_note")
_PHOTO_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "fxtr_ui_dispatch_demo_photo")


@dataclasses.dataclass(frozen=True)
class _Note(DataclassEntity, fxtr_type=_NOTE_TYPE_ID):
    """A short text note. Carries the `$fxtr.type` field so the
    TS-side static entity dispatcher can route it to `NoteCard`."""

    title: str
    body: str


@dataclasses.dataclass(frozen=True)
class _Photo(DataclassEntity, fxtr_type=_PHOTO_TYPE_ID):
    """A photo placeholder. Dispatches to `PhotoCard` on the TS
    side via its distinct `$fxtr.type` value."""

    caption: str
    url: str


# --- Service types -------------------------------------------------------

_ECHO_SVC_TYPE_ID: FxtrID = system_entities.define_type(
    _DEMO_NS, "fxtr_ui_dispatch_demo_echo_service"
)
_COUNTER_SVC_TYPE_ID: FxtrID = system_entities.define_type(
    _DEMO_NS, "fxtr_ui_dispatch_demo_counter_service"
)


@service_protocol
class _EchoServiceProto(Protocol):
    """Trivial echo service — returns its argument unchanged."""

    async def echo(self, value: Any) -> Any: ...


class _EchoServiceImpl(TypedServiceImpl[_EchoServiceProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return _EchoServiceProto

    def _service_type_id(self) -> FxtrID:
        return _ECHO_SVC_TYPE_ID

    @service_method
    async def echo(self, value: Any) -> Any:
        return value


@service_protocol
class _CounterServiceProto(Protocol):
    """A counter — `increment` returns the new value, `reset`
    zeroes it. Used to demonstrate cross-component shared state
    via `useDispatchSharedAtom` on the TS side."""

    async def increment(self) -> int: ...

    async def reset(self) -> int: ...

    async def value(self) -> int: ...


class _CounterServiceImpl(TypedServiceImpl[_CounterServiceProto]):
    def __init__(self) -> None:
        super().__init__()
        self._count = 0

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return _CounterServiceProto

    def _service_type_id(self) -> FxtrID:
        return _COUNTER_SVC_TYPE_ID

    @service_method
    async def increment(self) -> int:
        self._count += 1
        return self._count

    @service_method
    async def reset(self) -> int:
        self._count = 0
        return self._count

    @service_method
    async def value(self) -> int:
        return self._count


# --- Main ----------------------------------------------------------------


async def _amain() -> None:
    type_ids = {
        "note": _NOTE_TYPE_ID.id_string(),
        "photo": _PHOTO_TYPE_ID.id_string(),
        "echo": _ECHO_SVC_TYPE_ID.id_string(),
        "counter": _COUNTER_SVC_TYPE_ID.id_string(),
    }

    async with WorkstationServer(
        debug_ui_dist_path=WorkstationServer.bundled_debug_ui_dist_path(),
    ) as ws:
        async with open_workstation_rpc(ws.rpc_url, name="fxtr-ui-dispatch-demo/peer-rpc") as peer:
            # Two entities per type so the demo grid has a few
            # rows. Multiple stubs for the same type share the
            # dispatch registry's resolved component but each
            # `(stub, key)` pair has its own dispatch entry.
            entities = [
                ("demo-note-1", _Note(title="Hello", body="from the dispatch demo")),
                ("demo-note-2", _Note(title="A second note", body="that loads independently")),
                (
                    "demo-photo-1",
                    _Photo(caption="Placeholder pic", url="https://placekitten.com/400/300"),
                ),
                (
                    "demo-photo-2",
                    _Photo(caption="Another placeholder", url="https://placekitten.com/300/300"),
                ),
            ]
            entity_refs = [(name, e.store(peer.scope)) for (name, e) in entities]

            services = [
                ("demo-echo", _EchoServiceImpl()),
                ("demo-counter", _CounterServiceImpl()),
            ]
            service_refs = [(name, await peer.scope.mount(impl)) for (name, impl) in services]

            # Expose all of them under stable external names. Using
            # `expose_entity` / `expose_service` chains via an
            # `AsyncExitStack` to keep the registrations alive for
            # the lifetime of the process.
            async with contextlib.AsyncExitStack() as stack:
                for name, ref in entity_refs:
                    await stack.enter_async_context(
                        expose_entity(peer.remote_service, name, ref.entity_id, peer.scope)
                    )
                for name, ref in service_refs:
                    await stack.enter_async_context(
                        expose_service(peer.remote_service, name, ref, peer.scope)
                    )

                # Emit the type-id-string map BEFORE READY so the
                # orchestrator captures it during the startup wait.
                print(f"TYPE_IDS {json.dumps(type_ids)}", flush=True)
                print(f"READY {ws.bound_url}", flush=True)
                print(f"DEBUG_UI {ws.debug_http_url}", flush=True)

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
