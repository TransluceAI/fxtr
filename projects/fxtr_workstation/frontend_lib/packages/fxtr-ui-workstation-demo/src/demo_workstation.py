"""Backend for `@fxtr-ui/workstation-demo`.

Boots a :class:`WorkstationServer`, registers entities and
services under a single peer RPC connection, and prints the
type-id strings to stdout so the TS orchestrator can plumb them
through Vite into the dispatch registry.

Exposed entity types:

- ``_DemoNote`` — a short text note.
- ``_DemoListNode`` — a self-referential linked-list node.
- :class:`fxtr_report_builder.RootReport` — a report whose body
  is built via MDX → FxtrCbor through ``build_root_report``.
- :class:`fxtr_report_builder.SubReport` — referenced from the
  root report through ``<RenderFigure>`` and standalone.

Service types:

- ``_EchoServiceImpl`` — echoes its argument.
- ``_CounterServiceImpl`` — increment / reset / value.
- ``_NotifyNServiceImpl`` — calls back N times.

stdout protocol:

    TYPE_IDS {...json...}
    READY ws://...
    DEBUG_UI http://...

The TYPE_IDS line is a JSON dict keyed by an externally-stable
label (``note``, ``list_node``, ``echo``, ``counter``,
``notify_n``, ``report``, ``sub_report``) — the TS dispatch
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
from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_report_builder import (
    ROOT_REPORT_TYPE_ID,
    SUB_REPORT_TYPE_ID,
    build_root_report,
    build_sub_report,
)
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.peer_rpc import open_workstation_rpc
from fxtr_workstation.protocols import WorkstationSessionEventInbox

# Stable namespace so the type-ids are deterministic across runs;
# the TS side reads them through the TYPE_IDS line so absolute
# stability isn't required, but determinism makes debugging
# easier.
_DEMO_NS = uuid.UUID("3c7a9d52-4e1b-4f8a-9d23-1b6e7c4f8a09")

# --- Entity types --------------------------------------------------------

_NOTE_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "fxtr_ui_workstation_demo_note")
_LIST_NODE_TYPE_ID: FxtrID = system_entities.define_type(
    _DEMO_NS, "fxtr_ui_workstation_demo_list_node"
)


@dataclasses.dataclass(frozen=True)
class _DemoNote(DataclassEntity, fxtr_type=_NOTE_TYPE_ID):
    """A short text note. The TS-side static entity dispatcher
    routes it to the demo's note renderer via `$fxtr.type`."""

    title: str
    body: str


@dataclasses.dataclass(frozen=True)
class _DemoListNode(DataclassEntity, fxtr_type=_LIST_NODE_TYPE_ID):
    """A self-referential linked-list node. `next` is `None` at
    the tail; intermediate nodes reference the next node. The
    demo's FullPanel handler renders `next` as a `<RefLink>` so
    walking the chain exercises the inline-ref-via-load path.

    `from __future__ import annotations` (at the top of this
    file) lets us name `_DemoListNode` in the `next` annotation
    without a forward-reference string — the type is resolved
    lazily by mashumaro's serialization machinery."""

    label: str
    next: ChildRef[_DemoListNode] | None = None


# --- Service types -------------------------------------------------------

_ECHO_SVC_TYPE_ID: FxtrID = system_entities.define_type(
    _DEMO_NS, "fxtr_ui_workstation_demo_echo_service"
)
_COUNTER_SVC_TYPE_ID: FxtrID = system_entities.define_type(
    _DEMO_NS, "fxtr_ui_workstation_demo_counter_service"
)
_NOTIFY_N_SVC_TYPE_ID: FxtrID = system_entities.define_type(
    _DEMO_NS, "fxtr_ui_workstation_demo_notify_n_service"
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
    zeroes it. Demonstrates that service-typed refs surface
    through the workstation registry even when the TS side has
    no registered renderer (falls back to the service-fallback
    components shipped by `@fxtr-ui/workstation`)."""

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


@service_protocol
class _NotifyNServiceProto(Protocol):
    """Callback-stream demo service.

    `notify_n(cb, n, prefix)` calls ``cb.notify(f"{prefix}-{i}")``
    ``n`` times in sequence, in-call (no background tasks), and
    returns ``n`` (direct push is fire-and-forget; a closed inbox
    silently drops). Mirrors the integration-test pattern in
    ``fxtr-ui-connector-workstation/tests/_demo_workstation.py``.
    """

    async def notify_n(
        self, cb: ServiceRef[WorkstationSessionEventInbox], n: int, prefix: str
    ) -> int: ...


class _NotifyNServiceImpl(TypedServiceImpl[_NotifyNServiceProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return _NotifyNServiceProto

    def _service_type_id(self) -> FxtrID:
        return _NOTIFY_N_SVC_TYPE_ID

    @service_method
    async def notify_n(
        self, cb: ServiceRef[WorkstationSessionEventInbox], n: int, prefix: str
    ) -> int:
        call_scope = get_active_scope()
        proxy = await cb.resolve(call_scope)
        for i in range(n):
            await proxy.notify(f"{prefix}-{i}")
        return n


# --- Peer RPC plumbing (copied from the sibling demo) --------------------


# --- Main ----------------------------------------------------------------


async def _amain() -> None:
    type_ids = {
        "note": _NOTE_TYPE_ID.id_string(),
        "list_node": _LIST_NODE_TYPE_ID.id_string(),
        "echo": _ECHO_SVC_TYPE_ID.id_string(),
        "counter": _COUNTER_SVC_TYPE_ID.id_string(),
        "notify_n": _NOTIFY_N_SVC_TYPE_ID.id_string(),
        "report": ROOT_REPORT_TYPE_ID.id_string(),
        "sub_report": SUB_REPORT_TYPE_ID.id_string(),
    }

    async with WorkstationServer(
        debug_ui_dist_path=WorkstationServer.bundled_debug_ui_dist_path(),
    ) as ws:
        async with open_workstation_rpc(
            ws.rpc_url, name="fxtr-ui-workstation-demo/peer-rpc"
        ) as peer:
            # ----- Note entities -------------------------------------
            notes = [
                (
                    "demo/note-1",
                    _DemoNote(
                        title="Hello, workstation!",
                        body="A short demo note rendered through the FullPanel dispatch.",
                    ),
                ),
                (
                    "demo/note-2",
                    _DemoNote(
                        title="Iteration",
                        body=("The plan says we may iterate post-demo. Welcome to the iteration."),
                    ),
                ),
            ]
            note_refs = [(name, e.store(peer.scope)) for (name, e) in notes]

            # ----- Linked-list chain ---------------------------------
            # Build a 5-node chain `#0 → #1 → ... → #4`. Construct
            # the tail first and prepend each node so each `next`
            # is already-stored when the parent is built.
            tail = _DemoListNode(label="#4", next=None).store(peer.scope)
            node_3 = _DemoListNode(label="#3", next=tail).store(peer.scope)
            node_2 = _DemoListNode(label="#2", next=node_3).store(peer.scope)
            node_1 = _DemoListNode(label="#1", next=node_2).store(peer.scope)
            chain_head = _DemoListNode(label="#0", next=node_1).store(peer.scope)

            # ----- Report + sub-report -------------------------------
            # Demonstrates the @fxtr-ui/reports rendering path:
            #   - SubReport renders as a `Figure` (overview-only) inside
            #     the RootReport's FullPanel body.
            #   - DemoNote is referenced through `<RenderLink>` (inline
            #     reflink) AND through `<RenderFigure>` (which has no
            #     registered renderer in the demo's dispatch registry,
            #     so it falls back to `FigureNoMatchFallback` —
            #     `DemoNoteFullPanel` wrapped in a shaded container with
            #     a reflink header).
            methodology_sub_ref = (
                await build_sub_report(
                    label="Methodology Details",
                    overview_source=(
                        "A short overview of how the demo data was put" " together.\n"
                    ),
                    detail_source=(
                        "## Approach\n\n"
                        "Demo data was hand-built for illustrative"
                        " purposes — there's no real analysis here.\n\n"
                        "## Limitations\n\n"
                        "Everything is fake.\n"
                    ),
                )
            ).store(peer.scope)

            demo_report_ref = (
                await build_root_report(
                    title="Q4 Demo Report",
                    summary=(
                        "A short demo report demonstrating embedded"
                        " sub-reports, figures, and inline ref-links."
                    ),
                    source=(
                        "Welcome to the demo report. It exercises the\n"
                        "`@fxtr-ui/reports` rendering path end-to-end.\n\n"
                        "## Methodology\n\n"
                        "<RenderFigure entity={props.fxtr_inputs.sub} />\n\n"
                        "## Cross-references\n\n"
                        "For more context, see"
                        ' <RenderLink entity={props.fxtr_inputs.note} linkText="the welcome note" />.\n\n'
                        "## Without a Figure renderer\n\n"
                        "The next embed shows what happens when an"
                        " entity type has no `Figure` renderer registered"
                        " — the default fallback wraps its FullPanel.\n\n"
                        "<RenderFigure entity={props.fxtr_inputs.note} />\n"
                    ),
                    inputs={
                        "sub": methodology_sub_ref,
                        "note": note_refs[0][1],
                    },
                )
            ).store(peer.scope)

            entity_refs = [
                *note_refs,
                ("demo/chain-head", chain_head),
                ("demo/sub-report", methodology_sub_ref),
                ("demo/report", demo_report_ref),
            ]

            # ----- Services ------------------------------------------
            services = [
                ("demo/echo", _EchoServiceImpl()),
                ("demo/counter", _CounterServiceImpl()),
                ("demo/notify-n", _NotifyNServiceImpl()),
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
