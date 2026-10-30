#!/usr/bin/env python3
"""Interactive demo of the workstation + debug UI session features.

Sibling to ``debug_ui_demo.py``. Boots a workstation with the bundled
debug UI, prints the URLs, then presents a menu of session-themed
scenarios. Each scenario steps through observable workstation
operations one ``prompt(...)`` at a time so the user can watch the
UI tick forward in single steps.

Run:

    cd projects/fxtr_workstation
    uv run python examples/debug_ui_sessions_demo.py

Requires the debug UI to have been built first:

    cd fxtr_workstation/debug_ui
    pnpm install && pnpm build
"""

from __future__ import annotations

import contextlib
import sys
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, Protocol

import anyio
import anyio.to_thread
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.rpc.scope_bridge import RPCSession
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

# Reuse the test-only ClientWS / connect_rpc helpers + fixtures.
_TESTS_PARENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_TESTS_PARENT))
from fxtr_workstation import WorkstationServer, expose_entity, expose_service  # noqa: E402
from fxtr_workstation.client_protocol import (  # noqa: E402
    CallbackEvent,
    CreateSessionRequest,
    EntityMetadataResultEvent,
    ErrorResponse,
    InvokeResultEvent,
    LoadEntityErrorResponse,
    LoadEntityRequest,
    LoadEntityResponse,
    LoadResultEvent,
    LoadResultSilentEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    ServerFrame,
    SessionCreatedResponse,
    SessionEventFrame,
    SessionSnapshotEvent,
    SubscribeSessionRequest,
)
from fxtr_workstation.protocols import WorkstationServiceProto  # noqa: E402
from fxtr_workstation.state import (  # noqa: E402
    _ensure_entity_record_via_track,
    _maybe_release_entity_record,
)
from tests._fixtures import (  # noqa: E402
    CallbackInvokerImpl,
    EchoFactoryImpl,
    EchoImpl,
    Note,
    NoteMakerImpl,
    NoteMakerProto,
    NoteWithChild,
)
from tests._ws_client import ClientWS, connect_client, connect_rpc  # noqa: E402

# --- Inline fixture: a factory that returns NoteMakerProto services
# --- (used by the dynamic-services scenario). ----------------------

_NMF_NS = uuid.UUID("77777777-8888-9999-aaaa-bbbbbbbbbbbb")
NOTEMAKER_FACTORY_TYPE_ID = system_entities.define_type(_NMF_NS, "notemaker_factory")


@service_protocol
class NoteMakerFactoryProto(Protocol):
    async def make_notemaker(self) -> ServiceRef[NoteMakerProto]: ...


class NoteMakerFactoryImpl(TypedServiceImpl[NoteMakerFactoryProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return NoteMakerFactoryProto

    def _service_type_id(self) -> FxtrID:
        return NOTEMAKER_FACTORY_TYPE_ID

    @service_method
    async def make_notemaker(self) -> ServiceRef[NoteMakerProto]:
        scope = get_active_scope()
        return (await scope.mount(NoteMakerImpl())).replace_service_type(NoteMakerProto)


# --- prompt + pretty-print helpers ---------------------------------


async def prompt(message: str) -> None:
    print()
    print(f">>> {message}")

    def _wait() -> None:
        try:
            input("    [enter to continue, ctrl-C to abort] ")
        except EOFError:
            pass

    await anyio.to_thread.run_sync(_wait)


def _short_repr(value: Any, max_len: int = 200) -> str:
    s = repr(value)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _print_recv(label: str, frame: ServerFrame) -> None:
    print(f"  [{label}] <- {_short_repr(frame)}")


def _print_send(label: str, req: Any) -> None:
    print(f"  [{label}] -> {_short_repr(req)}")


# --- Send/recv helpers ---------------------------------------------


async def _send(client: ClientWS, label: str, req: Any) -> None:
    _print_send(label, req)
    await client.send(req)


async def _recv(client: ClientWS, label: str) -> ServerFrame:
    frame = await client.receive()
    _print_recv(label, frame)
    return frame


@contextlib.asynccontextmanager
async def background_drain(label: str, client: ClientWS) -> AsyncGenerator[None]:
    """Run a task that prints every frame ``client`` receives until
    the context exits. Used for foreign subscribers in a scenario,
    where we don't synchronously consume their queue."""

    async def loop() -> None:
        with contextlib.suppress(Exception):
            while True:
                frame = await client.receive()
                _print_recv(label, frame)

    async with anyio.create_task_group() as tg:
        tg.start_soon(loop)
        try:
            yield
        finally:
            tg.cancel_scope.cancel()


# --- Session operation helpers (driver side) -----------------------


async def create_session(
    client: ClientWS, label: str, *, request_id: int, metadata: dict[str, Any]
) -> str:
    await _send(client, label, CreateSessionRequest(request_id=request_id, metadata=metadata))
    while True:
        frame = await _recv(client, label)
        if isinstance(frame, SessionCreatedResponse) and frame.request_id == request_id:
            return frame.session_id


async def subscribe_session(client: ClientWS, label: str, sid: str, *, request_id: int) -> int:
    await _send(client, label, SubscribeSessionRequest(request_id=request_id, session_id=sid))
    while True:
        frame = await _recv(client, label)
        if isinstance(frame, SessionSnapshotEvent) and frame.subscription_id == request_id:
            return request_id


async def mutate(
    client: ClientWS,
    label: str,
    *,
    request_id: int,
    sub_id: int,
    ack_seq: int,
    update_id: int,
    patch: list[dict[str, Any]] | None = None,
    ops: list[dict[str, Any]] | None = None,
) -> ServerFrame:
    await _send(
        client,
        label,
        MutateSessionRequest(
            request_id=request_id,
            subscription_id=sub_id,
            ack_seq=ack_seq,
            update_id=update_id,
            patch=patch or [],
            ops=ops or [],  # type: ignore[arg-type]
        ),
    )
    while True:
        frame = await _recv(client, label)
        if (
            isinstance(frame, (MutationCommittedResponse, ErrorResponse))
            and frame.request_id == request_id
        ):
            return frame
        # Ignore foreign events / acks for other requests.


async def close_session_via(
    client: ClientWS, label: str, sub_id: int, ack_seq: int, *, request_id: int, update_id: int
) -> None:
    """Send a close mutation. Tolerates a missing ack if the session
    is already gone (best-effort cleanup)."""
    with contextlib.suppress(Exception):
        await mutate(
            client,
            label,
            request_id=request_id,
            sub_id=sub_id,
            ack_seq=ack_seq,
            update_id=update_id,
            ops=[{"kind": "close"}],
        )


# --- RPC peer setup helper -----------------------------------------


@contextlib.asynccontextmanager
async def rpc_peer(
    rpc_url: str, name: str
) -> AsyncGenerator[tuple[RPCSession[UnknownService], Any]]:
    """Open an RPC session, name it, and yield ``(session, ws_ref)``
    where ws_ref is the workstation service ref."""
    async with connect_rpc(rpc_url) as session:
        ws_ref = session.remote_service.replace_service_type(WorkstationServiceProto)
        ws_proxy = await ws_ref.resolve(session.scope)
        await ws_proxy.name_connection(name)
        yield session, ws_ref


# --- Scenario 1: happy path ----------------------------------------


async def scenario_happy_path(ws: WorkstationServer) -> None:
    """RPC peer exposes echo + Note. Alice creates a session, mutates;
    Bob (foreign subscriber) sees the state_update."""
    async with rpc_peer(ws.rpc_url, "alice-rpc") as (rpc, ws_ref):
        echo_ref = await rpc.scope.mount(EchoImpl())
        note = Note(title="hello", body="world").store(rpc.scope)

        async with (
            expose_service(ws_ref, "echo-svc", echo_ref, rpc.scope) as svc_id,
            expose_entity(ws_ref, "demo-note", note.entity_id, rpc.scope) as ent_id,
        ):
            print(f"  [rpc] exposed echo as {svc_id}, note as {ent_id}")
            await prompt("RPC peer exposed echo + note. Tables show new rows.")

            async with connect_client(ws.client_url) as alice:
                async with connect_client(ws.client_url) as bob:
                    sid = await create_session(
                        alice, "alice", request_id=1, metadata={"scenario": "happy"}
                    )
                    await prompt(f"alice created session {sid[:8]}…")

                    sub_alice = await subscribe_session(alice, "alice", sid, request_id=2)
                    await prompt("alice subscribed; her sub-panel can be checked in the UI")

                    await subscribe_session(bob, "bob", sid, request_id=2)
                    await prompt("bob subscribed too")

                    async with background_drain("bob-bg", bob):
                        await prompt('alice mutates: track entity + add /note="hello"')
                        await mutate(
                            alice,
                            "alice",
                            request_id=10,
                            sub_id=sub_alice,
                            ack_seq=0,
                            update_id=1,
                            patch=[{"op": "add", "path": "/note", "value": "hello"}],
                            ops=[{"kind": "track_entity", "short_id": ent_id}],
                        )
                        await anyio.sleep(0.1)  # let bob's drain print the broadcast
                        await prompt("bob's foreign-broadcast state_update should print above")

                        await prompt("alice closes the session")
                        await close_session_via(
                            alice, "alice", sub_alice, ack_seq=1, request_id=11, update_id=2
                        )
                        await anyio.sleep(0.1)


# --- Scenario 2: slot delivery -------------------------------------


async def scenario_slots(ws: WorkstationServer) -> None:
    """Load + invoke. Watch the session-debug stream surface entity /
    service refcounts as result events arrive."""
    async with rpc_peer(ws.rpc_url, "alice-rpc") as (rpc, ws_ref):
        echo_factory = await rpc.scope.mount(EchoFactoryImpl())
        note = Note(title="solo", body="solo").store(rpc.scope)

        async with (
            expose_service(ws_ref, "factory", echo_factory, rpc.scope) as svc_id,
            expose_entity(ws_ref, "the-note", note.entity_id, rpc.scope) as ent_id,
        ):
            print(f"  [rpc] factory={svc_id}, note={ent_id}")
            await prompt("RPC peer exposed an EchoFactory + a Note")

            async with connect_client(ws.client_url) as client:
                sid = await create_session(
                    client, "client", request_id=1, metadata={"scenario": "slots"}
                )
                sub = await subscribe_session(client, "client", sid, request_id=2)
                await prompt(f"check session {sid[:8]}… in the UI; sub-panel opens")

                await prompt(f"load_entity slot=1 against {ent_id}")
                await mutate(
                    client,
                    "client",
                    request_id=10,
                    sub_id=sub,
                    ack_seq=0,
                    update_id=1,
                    ops=[{"kind": "load_entity", "slot": 1, "short_id": ent_id}],
                )
                # The LoadResultEvent will follow on the wire; print it.
                await _recv(client, "client")
                await prompt("LoadResultEvent should appear in the event log")

                await prompt(f"invoke factory.make_pair (slot=2 on service {svc_id})")
                await mutate(
                    client,
                    "client",
                    request_id=11,
                    sub_id=sub,
                    ack_seq=2,
                    update_id=2,
                    ops=[
                        {
                            "kind": "invoke",
                            "slot": 2,
                            "service_short_id": svc_id,
                            "method_name": "make_pair",
                            "args": [],
                        }
                    ],
                )
                await _recv(client, "client")
                await prompt(
                    "InvokeResultEvent with embedded entity + service "
                    "refs; new tracked rows in the UI tables, refcount "
                    "badges in the session pins view"
                )

                await close_session_via(
                    client, "client", sub, ack_seq=4, request_id=12, update_id=3
                )


# --- Scenario 3: session tracker keeps things alive ----------------


async def scenario_tracker(ws: WorkstationServer) -> None:
    """Session-pin keeps an entity alive after the RPC peer drops, and
    keeps a service alive after its explicit registration is released."""
    async with connect_client(ws.client_url) as client:
        sid = await create_session(client, "client", request_id=1, metadata={"scenario": "tracker"})
        sub = await subscribe_session(client, "client", sid, request_id=2)
        await prompt(f"session {sid[:8]}… created + subscribed; check it in the UI")

        # Phase A: entity-pin survives RPC disconnect.
        async with rpc_peer(ws.rpc_url, "scratch-rpc") as (rpc, ws_ref):
            note = Note(title="lives-on", body="!").store(rpc.scope)
            async with expose_entity(ws_ref, "scratch-note", note.entity_id, rpc.scope) as ent_id:
                await prompt(
                    f"RPC peer exposed entity {ent_id}; " f"the entity row shows 1 RPC source"
                )
                await mutate(
                    client,
                    "client",
                    request_id=10,
                    sub_id=sub,
                    ack_seq=0,
                    update_id=1,
                    ops=[{"kind": "track_entity", "short_id": ent_id}],
                )
                await prompt(
                    f"session pinned the entity; the entity row's "
                    f"pinned_by_sessions now includes {sid[:8]}…"
                )
            # expose_entity exited — explicit registration released.
            await prompt(
                "expose_entity released the explicit registration. "
                "Entity stays alive via session pin (RPC source gone, "
                "session pin remains)."
            )
        await prompt(
            "RPC connection dropped. The entity is still tracked "
            "(session pin keeps it; entities are independent of the "
            "RPC connection's scope)."
        )

        # Phase B: service-pin keeps a service alive past explicit
        # release. We DON'T drop the RPC connection here because a
        # service-pin blocks the RPC's graceful drain — we'd deadlock.
        async with rpc_peer(ws.rpc_url, "svc-rpc") as (rpc2, ws_ref2):
            echo_ref = await rpc2.scope.mount(EchoImpl())
            async with expose_service(ws_ref2, "echo-svc", echo_ref, rpc2.scope) as svc_id:
                await prompt(f"RPC peer exposed service {svc_id} (1 RPC source)")
                await mutate(
                    client,
                    "client",
                    request_id=11,
                    sub_id=sub,
                    ack_seq=1,
                    update_id=2,
                    ops=[{"kind": "track_service", "short_id": svc_id}],
                )
                await prompt(
                    "session pinned the service; service row now shows "
                    "the session in pinned_by_sessions"
                )
            # expose_service exited — RPC source released. Service
            # survives via session pin.
            await prompt(
                "expose_service released; service survives via the "
                "session pin (registering_connection_ids empty, "
                "pinned_by_sessions still has us). NOTE: we leave the "
                "RPC connection up; releasing it now would block on "
                "the session pin (graceful-drain semantics)."
            )

            # Untrack to let the RPC drain on context exit.
            await mutate(
                client,
                "client",
                request_id=12,
                sub_id=sub,
                ack_seq=2,
                update_id=3,
                ops=[
                    {"kind": "untrack_entity", "short_id": ent_id},
                    {"kind": "untrack_service", "short_id": svc_id},
                ],
            )
            await prompt(
                "session untracked both — entity drops (no holders), "
                "service drops, RPC scope can drain on connection exit"
            )

        await close_session_via(client, "client", sub, ack_seq=3, request_id=20, update_id=4)


# --- Scenario 4: stale-ack rejection + same-mutator skip-ahead -----


async def scenario_stale_ack(ws: WorkstationServer) -> None:
    """Two clients on one session. Stale ack from the foreign client
    is rejected; same-mutator skip-ahead is allowed."""
    async with connect_client(ws.client_url) as alice:
        async with connect_client(ws.client_url) as bob:
            sid = await create_session(
                alice, "alice", request_id=1, metadata={"scenario": "stale-ack"}
            )
            await prompt("alice created session")

            sub_alice = await subscribe_session(alice, "alice", sid, request_id=2)
            sub_bob = await subscribe_session(bob, "bob", sid, request_id=2)
            await prompt("alice + bob both subscribed; check the session in the UI")

            await prompt("alice mutates first: ack=0, /foo=1")
            await mutate(
                alice,
                "alice",
                request_id=10,
                sub_id=sub_alice,
                ack_seq=0,
                update_id=1,
                patch=[{"op": "add", "path": "/foo", "value": 1}],
            )
            # bob receives the foreign state_update.
            await _recv(bob, "bob")
            await prompt("bob received alice's state_update (still ack=0 from his side)")

            await prompt("bob mutates with stale ack=0 — expect rejection")
            await mutate(
                bob,
                "bob",
                request_id=20,
                sub_id=sub_bob,
                ack_seq=0,
                update_id=1,
                patch=[{"op": "add", "path": "/bar", "value": 2}],
            )
            await prompt("alice mutates again at ack=0 — same-mutator skip-ahead allowed")
            await mutate(
                alice,
                "alice",
                request_id=11,
                sub_id=sub_alice,
                ack_seq=0,
                update_id=2,
                patch=[{"op": "add", "path": "/baz", "value": 3}],
            )
            await _recv(bob, "bob")  # foreign state_update for alice's second commit
            await prompt("bob now acks up to seq=2 and retries — succeeds")
            await mutate(
                bob,
                "bob",
                request_id=21,
                sub_id=sub_bob,
                ack_seq=2,
                update_id=2,
                patch=[{"op": "add", "path": "/qux", "value": 4}],
            )
            await _recv(alice, "alice")  # foreign state_update for bob's commit

            await close_session_via(
                alice, "alice", sub_alice, ack_seq=3, request_id=12, update_id=3
            )


# --- Scenario 5: retention horizon ---------------------------------


async def scenario_retention(ws: WorkstationServer) -> None:
    """Load a NoteWithChild → child appears in entity_refcounts. A
    later mutation that acks past the load result drops the child
    refcount."""
    async with rpc_peer(ws.rpc_url, "alice-rpc") as (rpc, ws_ref):
        child = Note(title="child", body="b").store(rpc.scope)
        parent = NoteWithChild(title="parent", child=child).store(rpc.scope)
        async with expose_entity(ws_ref, "parent", parent.entity_id, rpc.scope) as parent_id:
            await prompt(f"RPC peer exposed parent={parent_id} (with embedded child)")

            async with connect_client(ws.client_url) as client:
                sid = await create_session(
                    client, "client", request_id=1, metadata={"scenario": "retention"}
                )
                sub = await subscribe_session(client, "client", sid, request_id=2)
                await prompt(f"check session {sid[:8]}… in the UI (open the sub-panel)")

                await prompt(f"load_entity slot=1 on parent {parent_id}")
                await mutate(
                    client,
                    "client",
                    request_id=10,
                    sub_id=sub,
                    ack_seq=0,
                    update_id=1,
                    ops=[{"kind": "load_entity", "slot": 1, "short_id": parent_id}],
                )
                await _recv(client, "client")  # the LoadResultEvent
                await prompt(
                    "LoadResultEvent delivered. The child entity is now "
                    "tracked in the workstation; the session-debug pane "
                    "shows entity_refcounts incremented for the child"
                )

                await prompt(
                    "empty mutation with ack_seq=2 — past the load result "
                    "(at seq=2). Retention prune should drop the child's "
                    "session refcount."
                )
                await mutate(
                    client,
                    "client",
                    request_id=11,
                    sub_id=sub,
                    ack_seq=2,
                    update_id=2,
                )
                await prompt(
                    "the child's refcount badge should now be 0 / absent; "
                    "if no other holders, the entity row drops."
                )

                await close_session_via(
                    client, "client", sub, ack_seq=3, request_id=12, update_id=3
                )


# --- Scenario 6: close as a mutation -------------------------------


async def scenario_close(ws: WorkstationServer) -> None:
    """Close-mutation produces a terminal state_update with closed=True
    that foreign subscribers see; the session leaves the registry."""
    async with connect_client(ws.client_url) as alice:
        async with connect_client(ws.client_url) as bob:
            sid = await create_session(alice, "alice", request_id=1, metadata={"scenario": "close"})
            sub_alice = await subscribe_session(alice, "alice", sid, request_id=2)
            await subscribe_session(bob, "bob", sid, request_id=2)
            await prompt(f"both subscribed to {sid[:8]}…; check it in the UI")

            await prompt('alice mutates: /closing_because="demo"')
            await mutate(
                alice,
                "alice",
                request_id=10,
                sub_id=sub_alice,
                ack_seq=0,
                update_id=1,
                patch=[{"op": "add", "path": "/closing_because", "value": "demo"}],
            )
            await _recv(bob, "bob")  # foreign state_update

            await prompt("alice sends close mutation")
            await mutate(
                alice,
                "alice",
                request_id=11,
                sub_id=sub_alice,
                ack_seq=1,
                update_id=2,
                ops=[{"kind": "close"}],
            )
            # bob receives the terminal state_update with closed=True.
            await _recv(bob, "bob")
            await prompt(
                "bob just received the terminal state_update (closed=true). "
                "The session row drops from the sessions list and the "
                "checkbox should clear in the UI."
            )


# --- Scenario 7: dynamic services (factory → service → entities) ---


def _expect_tag(payload: Any, tag: str) -> str:
    """Assert that ``payload`` is a list-based-escape tag of ``tag``
    kind and return the wrapped short_id."""
    if not (isinstance(payload, list) and len(payload) == 2 and payload[0] == tag):
        raise RuntimeError(f"expected {tag!r} tag in payload, got {payload!r}")
    return str(payload[1])


async def scenario_dynamic_services(ws: WorkstationServer) -> None:
    """Invoke a factory that returns a fresh NoteMaker service. The
    session tracks the returned service, calls a couple of methods on
    it that produce entities, tracks each entity (saving its short_id
    into state), then untracks the service and closes."""
    async with rpc_peer(ws.rpc_url, "alice-rpc") as (rpc, ws_ref):
        factory_ref = await rpc.scope.mount(NoteMakerFactoryImpl())
        async with expose_service(ws_ref, "nm-factory", factory_ref, rpc.scope) as factory_id:
            print(f"  [rpc] factory={factory_id}")
            await prompt(
                "RPC peer exposed a NoteMakerFactory. The services " "table should show one row."
            )

            async with connect_client(ws.client_url) as client:
                sid = await create_session(
                    client,
                    "client",
                    request_id=1,
                    metadata={"scenario": "dynamic-services"},
                )
                sub = await subscribe_session(client, "client", sid, request_id=2)
                await prompt(f"check session {sid[:8]}… in the UI")

                # m1: invoke factory.make_notemaker → fresh service ref.
                await prompt(f"invoke {factory_id}.make_notemaker (slot=1)")
                await mutate(
                    client,
                    "client",
                    request_id=10,
                    sub_id=sub,
                    ack_seq=0,
                    update_id=1,
                    ops=[
                        {
                            "kind": "invoke",
                            "slot": 1,
                            "service_short_id": factory_id,
                            "method_name": "make_notemaker",
                            "args": [],
                        }
                    ],
                )
                frame = await _recv(client, "client")
                assert isinstance(frame, SessionEventFrame)
                assert isinstance(frame.event, InvokeResultEvent)
                nm_id = _expect_tag(frame.event.payload, "service")
                await prompt(
                    f"InvokeResultEvent surfaced new service {nm_id}; "
                    f"the services table should now show 2 rows. The "
                    f"new service is workstation-tracked but not yet "
                    f"explicitly pinned by the session — the refcount "
                    f"comes from the result-event ref, which will drop "
                    f"once we ack past it."
                )

                # m2: track the new service + record its id in state.
                await prompt(
                    f"track_service {nm_id} + state.notemaker = "
                    f"{nm_id!r}; ack_seq=2 retires the result event"
                )
                await mutate(
                    client,
                    "client",
                    request_id=11,
                    sub_id=sub,
                    ack_seq=2,
                    update_id=2,
                    patch=[
                        {"op": "add", "path": "/notemaker", "value": nm_id},
                        {"op": "add", "path": "/notes", "value": []},
                    ],
                    ops=[{"kind": "track_service", "short_id": nm_id}],
                )
                await prompt(
                    "session pins view should now show the new service "
                    "as `explicit pin` with refcount 1; state tree shows "
                    "/notemaker and an empty /notes list"
                )

                # m3: invoke nm.make_note → entity result.
                await prompt(f"invoke {nm_id}.make_note('first', '...') slot=2")
                await mutate(
                    client,
                    "client",
                    request_id=12,
                    sub_id=sub,
                    ack_seq=3,
                    update_id=3,
                    ops=[
                        {
                            "kind": "invoke",
                            "slot": 2,
                            "service_short_id": nm_id,
                            "method_name": "make_note",
                            "args": ["first", "body of first"],
                        }
                    ],
                )
                frame = await _recv(client, "client")
                assert isinstance(frame, SessionEventFrame)
                assert isinstance(frame.event, InvokeResultEvent)
                note1_id = _expect_tag(frame.event.payload, "entity")

                # m4: track + append to /notes.
                await prompt(f"track_entity {note1_id} + state.notes ← [{note1_id!r}]")
                await mutate(
                    client,
                    "client",
                    request_id=13,
                    sub_id=sub,
                    ack_seq=5,
                    update_id=4,
                    patch=[{"op": "replace", "path": "/notes", "value": [note1_id]}],
                    ops=[{"kind": "track_entity", "short_id": note1_id}],
                )

                # m5: invoke nm.make_note again → entity result.
                await prompt(f"invoke {nm_id}.make_note('second', '...') slot=3")
                await mutate(
                    client,
                    "client",
                    request_id=14,
                    sub_id=sub,
                    ack_seq=6,
                    update_id=5,
                    ops=[
                        {
                            "kind": "invoke",
                            "slot": 3,
                            "service_short_id": nm_id,
                            "method_name": "make_note",
                            "args": ["second", "body of second"],
                        }
                    ],
                )
                frame = await _recv(client, "client")
                assert isinstance(frame, SessionEventFrame)
                assert isinstance(frame.event, InvokeResultEvent)
                note2_id = _expect_tag(frame.event.payload, "entity")

                # m6: track + append to /notes.
                await prompt(
                    f"track_entity {note2_id} + state.notes ← " f"[{note1_id!r}, {note2_id!r}]"
                )
                await mutate(
                    client,
                    "client",
                    request_id=15,
                    sub_id=sub,
                    ack_seq=8,
                    update_id=6,
                    patch=[{"op": "add", "path": "/notes/1", "value": note2_id}],
                    ops=[{"kind": "track_entity", "short_id": note2_id}],
                )
                await prompt(
                    "pins view: 1 service + 2 entities, all 'explicit pin'. "
                    "state tree shows /notemaker and /notes = [<n1>, <n2>]."
                )

                # m7: untrack the service. Entities stay (still pinned).
                await prompt(f"untrack_service {nm_id}")
                await mutate(
                    client,
                    "client",
                    request_id=16,
                    sub_id=sub,
                    ack_seq=9,
                    update_id=7,
                    ops=[{"kind": "untrack_service", "short_id": nm_id}],
                )
                await prompt(
                    "service pin gone; the dynamic NoteMaker should "
                    "drop from the services table (no other holders). "
                    "Tracked entities remain pinned by the session."
                )

                await prompt("close the session")
                await close_session_via(
                    client, "client", sub, ack_seq=10, request_id=17, update_id=8
                )


# --- Scenario 8: callback round-trip + close ----------------------


async def scenario_callback_lifecycle(ws: WorkstationServer) -> None:
    """One client creates a workstation-minted callback, watches it
    appear in ``open_callbacks`` on the debug snapshot, fires it
    through an RPC peer, sees the resulting :class:`CallbackEvent`
    land on the session event log, then closes the callback and
    watches it disappear from the open-slots panel.

    What the UI shows over the course of the scenario:

    - **Open slots → callbacks** populates the moment the callback
      is created (its send-side service short_id appears).
    - **Open slots → invokes** flickers in for the duration of the
      ``fire`` invoke; once ``notify`` commits a ``CallbackEvent``
      and ``fire`` returns ``True``, the row clears.
    - **Event log** shows the ``CallbackEvent`` (collapsible,
      payload="hello") committed *before* the ``InvokeResultEvent``
      for ``fire`` — ``fire`` runs ``notify`` synchronously inside
      its own dispatch, so the cb event lands first.
    - **Session pins → services** picks up an ``explicit pin`` row
      for the cb's send-side once a later mutation
      ``track_service``s it (so it survives event-retention drops).
    - After ``close_callback``, the open-callbacks row drops; the
      cb's impl is marked ``_closed = True`` (visible via
      ``listening()`` returning ``False`` on the next invoke).
    """
    async with rpc_peer(ws.rpc_url, "alice-rpc") as (rpc, ws_ref):
        invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
        async with expose_service(ws_ref, "cb-invoker", invoker_ref, rpc.scope) as invoker_id:
            print(f"  [rpc] cb-invoker={invoker_id}")
            await prompt(
                "RPC peer exposed CallbackInvokerImpl. The services table "
                "shows the new row. The invoker's `fire(cb, payload)` "
                "method calls `notify(payload)` on a workstation-minted "
                "callback the session will create."
            )

            async with connect_client(ws.client_url) as client:
                sid = await create_session(
                    client,
                    "client",
                    request_id=1,
                    metadata={"scenario": "callback-lifecycle"},
                )
                sub = await subscribe_session(client, "client", sid, request_id=2)
                await prompt(f"check session {sid[:8]}… in the UI; sub-panel opens")

                # m1: create the callback at slot 1.
                await prompt(
                    "create_callback at slot=1. Watch Open slots → "
                    "callbacks populate with the new entry; the services "
                    "table also gains a fresh `s{N}` row for the cb's "
                    "send-side."
                )
                await mutate(
                    client,
                    "client",
                    request_id=10,
                    sub_id=sub,
                    ack_seq=0,
                    update_id=1,
                    ops=[{"kind": "create_callback", "slot": 1}],
                )
                await prompt(
                    "Open slots → callbacks should now show slot 1 → "
                    "<some s{N}>. The cb's send-side is workstation-"
                    "tracked but not yet explicitly pinned by the session."
                )

                # m2: invoke fire(["callback", 1], "hello"). The cb event
                # commits first, then the invoke result.
                await prompt(
                    "invoke cb-invoker.fire(['callback', 1], 'hello') "
                    "at slot=2. fire calls notify synchronously inside "
                    "its dispatch, so we'll see CallbackEvent commit "
                    "first, then the InvokeResultEvent."
                )
                await mutate(
                    client,
                    "client",
                    request_id=11,
                    sub_id=sub,
                    ack_seq=1,
                    update_id=2,
                    ops=[
                        {
                            "kind": "invoke",
                            "slot": 2,
                            "service_short_id": invoker_id,
                            "method_name": "fire",
                            "args": [["callback", 1], "hello"],
                        }
                    ],
                )
                cb_frame = await _recv(client, "client")
                assert isinstance(cb_frame, SessionEventFrame)
                assert isinstance(cb_frame.event, CallbackEvent)
                inv_frame = await _recv(client, "client")
                assert isinstance(inv_frame, SessionEventFrame)
                assert isinstance(inv_frame.event, InvokeResultEvent)
                await prompt(
                    "Event log: a `callback` row at slot 1 (payload='hello') "
                    "followed by an `invoke_result` row at slot 2 with "
                    "payload=true. Both expand to show their bodies."
                )

                # m3: invoke is_listening(["callback", 1]) — expect True.
                await prompt(
                    "invoke is_listening(['callback', 1]) at slot=3 — "
                    "the cb is still open, so the invoke result should "
                    "be `payload=true`."
                )
                await mutate(
                    client,
                    "client",
                    request_id=12,
                    sub_id=sub,
                    ack_seq=4,
                    update_id=3,
                    ops=[
                        {
                            "kind": "invoke",
                            "slot": 3,
                            "service_short_id": invoker_id,
                            "method_name": "is_listening",
                            "args": [["callback", 1]],
                        }
                    ],
                )
                await _recv(client, "client")  # InvokeResultEvent(True)
                await prompt(
                    "is_listening returned true. Note that no "
                    "CallbackEvent fired this time — `listening()` is a "
                    "side-effect-free read, not a `notify`."
                )

                # m4: close_callback at slot=1.
                await prompt(
                    "close_callback at slot=1. The cb's impl flips "
                    "`_closed = True` synchronously in commit; the "
                    "Open slots → callbacks row clears."
                )
                await mutate(
                    client,
                    "client",
                    request_id=13,
                    sub_id=sub,
                    ack_seq=6,
                    update_id=4,
                    ops=[{"kind": "close_callback", "slot": 1}],
                )
                await prompt(
                    "Open slots → callbacks is empty. The cb's send-"
                    "side service may also drop from the services "
                    "table now (no remaining holders)."
                )

                await close_session_via(
                    client, "client", sub, ack_seq=7, request_id=14, update_id=5
                )


# --- Scenario 9: cross-session callback routing -------------------


async def scenario_callback_cross_session(ws: WorkstationServer) -> None:
    """Two sessions share one workstation. Alice creates a callback;
    Bob fires it. The :class:`CallbackEvent` lands on **Alice**'s
    event log (because the callback's impl carries her session_id),
    not Bob's. Demonstrates that callbacks behave like regular
    workstation-tracked services for routing purposes — the cb's
    send-side has its own ``s{N}`` short_id that any session can
    track and invoke against — but the receive side is bound to its
    originator.

    What the UI shows:

    - Alice's session pins → services gains the cb's send-side as
      a retained-event reference (after she invokes ``return_ref``
      that surfaced its short_id).
    - Bob's session pins → services shows the cb as ``explicit pin``
      after he ``track_service``s it.
    - Bob's event log shows only the InvokeResultEvent for ``fire``
      (payload=true). His event log never sees a CallbackEvent.
    - Alice's event log shows the CallbackEvent (payload='from-bob')
      — the routing target is the callback's owning session, not
      whichever session's invoke called ``notify``.
    """
    async with rpc_peer(ws.rpc_url, "rpc") as (rpc, ws_ref):
        invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
        async with expose_service(ws_ref, "cb-invoker", invoker_ref, rpc.scope) as invoker_id:
            print(f"  [rpc] cb-invoker={invoker_id}")

            async with connect_client(ws.client_url) as alice:
                async with connect_client(ws.client_url) as bob:
                    a_sid = await create_session(
                        alice,
                        "alice",
                        request_id=1,
                        metadata={"scenario": "callback-cross-session", "who": "alice"},
                    )
                    a_sub = await subscribe_session(alice, "alice", a_sid, request_id=2)
                    b_sid = await create_session(
                        bob,
                        "bob",
                        request_id=1,
                        metadata={"scenario": "callback-cross-session", "who": "bob"},
                    )
                    b_sub = await subscribe_session(bob, "bob", b_sid, request_id=2)
                    await prompt(
                        f"both sessions up: alice={a_sid[:8]}… bob={b_sid[:8]}…. "
                        "Check both in the UI."
                    )

                    # m1 (alice): create cb at slot 1; surface its
                    # short_id via return_ref(['callback', 1]).
                    await prompt(
                        "alice creates callback at slot=1 + invokes "
                        "cb-invoker.return_ref(['callback', 1]) at slot=2 "
                        "to surface the cb's send-side short_id."
                    )
                    await mutate(
                        alice,
                        "alice",
                        request_id=10,
                        sub_id=a_sub,
                        ack_seq=0,
                        update_id=1,
                        ops=[
                            {"kind": "create_callback", "slot": 1},
                            {
                                "kind": "invoke",
                                "slot": 2,
                                "service_short_id": invoker_id,
                                "method_name": "return_ref",
                                "args": [["callback", 1]],
                            },
                        ],
                    )
                    frame = await _recv(alice, "alice")
                    assert isinstance(frame, SessionEventFrame)
                    assert isinstance(frame.event, InvokeResultEvent)
                    cb_short_id = _expect_tag(frame.event.payload, "service")
                    await prompt(
                        f"alice's invoke returned cb send-side {cb_short_id}. "
                        "The cb is now visible to bob too — any session "
                        "can pin it."
                    )

                    # m2 (bob): track the cb + fire it with a payload
                    # that names alice's identity.
                    await prompt(
                        f"bob: track_service {cb_short_id} + invoke "
                        f"cb-invoker.fire(['service', {cb_short_id}], "
                        f"'from-bob') at slot=1."
                    )
                    await mutate(
                        bob,
                        "bob",
                        request_id=10,
                        sub_id=b_sub,
                        ack_seq=0,
                        update_id=1,
                        ops=[
                            {"kind": "track_service", "short_id": cb_short_id},
                            {
                                "kind": "invoke",
                                "slot": 1,
                                "service_short_id": invoker_id,
                                "method_name": "fire",
                                "args": [
                                    ["service", cb_short_id],
                                    "from-bob",
                                ],
                            },
                        ],
                    )
                    bob_inv = await _recv(bob, "bob")
                    assert isinstance(bob_inv, SessionEventFrame)
                    assert isinstance(bob_inv.event, InvokeResultEvent)
                    await prompt(
                        "bob just received an InvokeResultEvent (payload="
                        "true). Check bob's event log — only the invoke "
                        "result appears, no CallbackEvent."
                    )

                    # Drain alice's CallbackEvent.
                    alice_cb = await _recv(alice, "alice")
                    assert isinstance(alice_cb, SessionEventFrame)
                    assert isinstance(alice_cb.event, CallbackEvent)
                    await prompt(
                        "alice's event log just gained a CallbackEvent "
                        "(slot=1, payload='from-bob'). Routing followed "
                        "the callback's originating session, not the "
                        "invoker."
                    )

                    await close_session_via(
                        bob, "bob", b_sub, ack_seq=2, request_id=20, update_id=2
                    )
                    await close_session_via(
                        alice, "alice", a_sub, ack_seq=4, request_id=11, update_id=2
                    )


# --- Scenario 10: provenance metadata + layered loads --------------


_PROV_GROUND = FxtrID.from_tag_and_payload(".", b"")


def _make_c_node_for_demo(scope: FxtrScope, ctx_key: str) -> FxtrID:
    """Mint a c-node in ``scope``'s arena that unwraps to ``"."``.

    The c-node's ``ctx_key`` field is the only thing that varies
    between the two contexts in the provenance scenario, so two
    distinct ``ctx_key`` values produce two structurally-distinct
    c-nodes (and therefore two distinct n-nodes when we store an
    entity under each context).
    """
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    cbor: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=_PROV_GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=_PROV_GROUND),
        "ctx_key": ctx_key,
    }
    return scope.create_c_node(cbor)


def _store_provenance_demo_entity(scope: FxtrScope) -> FxtrID:
    """Store a tiny entity in ``scope``. If ``scope`` carries a
    provenance context, the returned FxtrID is layered ``(d_id,
    n_id)`` where ``n_id`` is the scope's per-context n-node."""
    bt = scope.track_builtin(FxtrID.builtin("provenance_demo_entity"))
    cbor: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
        "$fxtr.type": RawWeakFxtrID(inner_id=bt),
        "name": "shared",
    }
    return scope.store_cbor(cbor)


async def scenario_provenance(ws: WorkstationServer) -> None:
    """6c metadata + provenance: same data ID with two distinct
    provenance parents → two records with distinct short IDs.
    Walk through ``GetEntityMetadata`` for both, then load each form
    (the two layered records, the bare ``(data, None)`` form, and the
    n-prov node *itself* as a non-layered entity)."""
    # Build the two layered FxtrIDs inside ``ws.state.root_scope`` so
    # the workstation sees them as same-store. Mint records via the
    # internal ``_ensure_entity_record_via_track`` helper — RPC isn't
    # involved here because we want the demo to focus on the
    # workstation's metadata behavior, not the RPC layer.
    async with FxtrScope(
        isolated=True,
        include=[ws.state.root_scope],
        name="prov-demo/builder",
    ) as builder:
        c_a = _make_c_node_for_demo(builder, ctx_key="trace-a")
        c_b = _make_c_node_for_demo(builder, ctx_key="trace-b")
        async with (
            FxtrScope(
                isolated=True,
                include=[builder],
                provenance_context=c_a,
                provenance_context_from_scope=builder,
                name="prov-demo/scope-a",
            ) as scope_a,
            FxtrScope(
                isolated=True,
                include=[builder],
                provenance_context=c_b,
                provenance_context_from_scope=builder,
                name="prov-demo/scope-b",
            ) as scope_b,
        ):
            fid_a = _store_provenance_demo_entity(scope_a)
            fid_b = _store_provenance_demo_entity(scope_b)
            # Same data half (FxtrID equality ignores prov), distinct
            # provenance parents.
            assert fid_a == fid_b
            assert fid_a._provenance_parent != fid_b._provenance_parent

            with ws.state.mutation_context():
                rec_a = _ensure_entity_record_via_track(ws.state, fid_a, scope_a)
                rec_b = _ensure_entity_record_via_track(ws.state, fid_b, scope_b)

            short_a = rec_a.short_id
            short_b = rec_b.short_id
            print(f"  [setup] minted (data, prov_a) record: short_id={short_a!r}")
            print(f"  [setup] minted (data, prov_b) record: short_id={short_b!r}")
            print(
                "  [setup] same underlying data ID, distinct provenance parents — "
                "the second record's short ID gets the @N suffix"
            )

            try:
                await prompt(
                    "Two layered records minted. The entities table now "
                    "shows two rows whose short IDs share a prefix and "
                    "differ by the @N suffix."
                )

                async with connect_client(ws.client_url) as client:
                    sid = await create_session(
                        client,
                        "client",
                        request_id=1,
                        metadata={"scenario": "provenance"},
                    )
                    sub = await subscribe_session(client, "client", sid, request_id=2)
                    await prompt(f"check session {sid[:8]}… in the UI; sub-panel opens")

                    # We keep ``ack_seq=0`` throughout this scenario so
                    # the retention horizon never advances. The metadata
                    # events surface refs to the ``unprovenanced`` and
                    # ``provenance_parent`` records, and those refs are
                    # the *only* thing keeping those records alive — if
                    # we ack'd past the metadata event the prov_a /
                    # unprov records would be released and the later
                    # load_entity ops would fail validation. Same
                    # mutator (single client, single subscription) so
                    # the no-retention pattern is allowed by the
                    # stale-ack rule.

                    # --- Metadata for record A -------------------------
                    await prompt(
                        f"GetEntityMetadata slot=1 for {short_a!r} " "(the (data, prov_a) record)"
                    )
                    await mutate(
                        client,
                        "client",
                        request_id=10,
                        sub_id=sub,
                        ack_seq=0,
                        update_id=1,
                        ops=[
                            {
                                "kind": "get_entity_metadata",
                                "slot": 1,
                                "short_id": short_a,
                            }
                        ],
                    )
                    meta_a_frame = await _recv(client, "client")
                    assert isinstance(meta_a_frame, SessionEventFrame)
                    assert isinstance(meta_a_frame.event, EntityMetadataResultEvent)
                    md_a = meta_a_frame.event.metadata
                    unprov_short = md_a["unprovenanced"]
                    prov_a_short = md_a["provenance_parent"]
                    print(f"  [client] metadata.unprovenanced = {unprov_short!r}")
                    print(f"  [client] metadata.provenance_parent = {prov_a_short!r}")
                    print(f"  [client] metadata.id_string = {md_a['id_string']!r}")
                    await prompt(
                        "metadata for record A. ``unprovenanced`` short ID "
                        "is for the bare (data, None) form — the "
                        "workstation just minted that record on demand. "
                        "``provenance_parent`` short ID is for the n-node "
                        "as a non-layered entity."
                    )

                    # --- Metadata for record B -------------------------
                    await prompt(f"GetEntityMetadata slot=2 for {short_b!r} (record B)")
                    await mutate(
                        client,
                        "client",
                        request_id=11,
                        sub_id=sub,
                        ack_seq=0,
                        update_id=2,
                        ops=[
                            {
                                "kind": "get_entity_metadata",
                                "slot": 2,
                                "short_id": short_b,
                            }
                        ],
                    )
                    meta_b_frame = await _recv(client, "client")
                    assert isinstance(meta_b_frame, SessionEventFrame)
                    assert isinstance(meta_b_frame.event, EntityMetadataResultEvent)
                    md_b = meta_b_frame.event.metadata
                    prov_b_short = md_b["provenance_parent"]
                    assert md_b["unprovenanced"] == unprov_short  # same data half
                    assert md_b["provenance_parent"] != prov_a_short  # distinct prov
                    print(
                        f"  [client] metadata.unprovenanced = {md_b['unprovenanced']!r} "
                        "(same as A)"
                    )
                    print(
                        f"  [client] metadata.provenance_parent = {prov_b_short!r} "
                        "(distinct from A)"
                    )
                    await prompt(
                        "metadata for record B. ``unprovenanced`` matches "
                        "A — both layered records share the same data ID, "
                        "so the bare form is the same single record. "
                        "``provenance_parent`` differs."
                    )

                    # --- Load the layered form (record A) --------------
                    await prompt(f"load_entity slot=3 against {short_a!r} (layered (data, prov_a))")
                    await mutate(
                        client,
                        "client",
                        request_id=12,
                        sub_id=sub,
                        ack_seq=0,
                        update_id=3,
                        ops=[{"kind": "load_entity", "slot": 3, "short_id": short_a}],
                    )
                    load_layered_frame = await _recv(client, "client")
                    assert isinstance(load_layered_frame, SessionEventFrame)
                    assert isinstance(load_layered_frame.event, LoadResultEvent)
                    await prompt(
                        "Load of the layered form. The CBOR payload's "
                        "child IDs are themselves layered — for example "
                        "``$fxtr.ctx`` is the bare ``.`` ground node "
                        "wrapped under prov_a's c-node, and ``$fxtr.type`` "
                        "is the type ID layered under a u-node. Loads "
                        "via the layered form propagate provenance "
                        "transparently."
                    )

                    # --- Load the unprovenanced form ------------------
                    await prompt(f"load_entity slot=4 against {unprov_short!r} (bare (data, None))")
                    await mutate(
                        client,
                        "client",
                        request_id=13,
                        sub_id=sub,
                        ack_seq=0,
                        update_id=4,
                        ops=[
                            {
                                "kind": "load_entity",
                                "slot": 4,
                                "short_id": unprov_short,
                            }
                        ],
                    )
                    load_bare_frame = await _recv(client, "client")
                    assert isinstance(load_bare_frame, SessionEventFrame)
                    assert isinstance(load_bare_frame.event, LoadResultEvent)
                    await prompt(
                        "Load of the bare (data, None) form. Same payload "
                        "structure but the child IDs are bare — no layered "
                        "provenance attached. This is what fxtr-v1 callers "
                        "would see if they ignored provenance."
                    )

                    # --- Load the prov_parent as a non-layered entity --
                    await prompt(
                        f"load_entity slot=5 against {prov_a_short!r} "
                        "(prov_a's n-node itself, as a non-layered entity)"
                    )
                    await mutate(
                        client,
                        "client",
                        request_id=14,
                        sub_id=sub,
                        ack_seq=0,
                        update_id=5,
                        ops=[
                            {
                                "kind": "load_entity",
                                "slot": 5,
                                "short_id": prov_a_short,
                            }
                        ],
                    )
                    load_prov_frame = await _recv(client, "client")
                    assert isinstance(load_prov_frame, SessionEventFrame)
                    assert isinstance(load_prov_frame.event, LoadResultEvent)
                    await prompt(
                        "Load of the n-node itself, as data. The CBOR "
                        "payload here is the *provenance form* of the "
                        "stored entity — every child ID is a provenance "
                        "node (c-, n-, p-, or u-tagged) rather than a "
                        "ground d-node. ``$fxtr.ctx`` here is prov_a's "
                        "c-node (the trace-a context), confirming the "
                        "n-node was minted under that context."
                    )

                    await close_session_via(
                        client,
                        "client",
                        sub,
                        ack_seq=0,
                        request_id=99,
                        update_id=6,
                    )
            finally:
                # Release the synthesized records so server shutdown
                # sees an empty registry.
                with ws.state.mutation_context():
                    _maybe_release_entity_record(ws.state, rec_a)
                    _maybe_release_entity_record(ws.state, rec_b)


# --- Scenario 11: silent + side-effect-only load (8b/8c) -----------


async def _request_direct_load(
    client: ClientWS, label: str, *, request_id: int, short_id: str
) -> LoadEntityResponse:
    """Send a top-level :class:`LoadEntityRequest` and wait for the
    matching response. Filters out unrelated frames so it's safe to
    call mid-session."""
    await _send(
        client,
        label,
        LoadEntityRequest(request_id=request_id, short_id=short_id),
    )
    while True:
        frame = await _recv(client, label)
        if isinstance(frame, LoadEntityResponse) and frame.request_id == request_id:
            return frame
        if isinstance(frame, LoadEntityErrorResponse) and frame.request_id == request_id:
            raise RuntimeError(f"direct load failed: {frame.message}")


async def scenario_silent_and_direct_load(ws: WorkstationServer) -> None:
    """Demonstrate the 8b + 8c caching primitives.

    The flow models a smart caching client:

    1. The session does a *normal* :class:`LoadEntityOp` first so we
       have a baseline event in the event log to compare against.
    2. The client uses the out-of-band :class:`LoadEntityRequest`
       (stage 8b) to fetch the bytes again — this time without
       touching session state. The reply lands as
       :class:`LoadEntityResponse`, *not* a session event; the session
       sub-panel stays unchanged.
    3. The client fires a :class:`LoadEntitySilentOp` (stage 8c)
       against the same entity. The session takes refcounts on the
       same children as the original load, but the result event is
       a payload-less :class:`LoadResultSilentEvent` — the debug UI
       renders it as a one-line entry in the event log.

    What the UI shows:

    - The first ``load_entity`` puts a :class:`LoadResultEvent` in
      the event log, with the full CBOR payload available behind a
      ``▸ load_result`` expander.
    - The :class:`LoadEntityRequest` does **not** add a row to the
      event log — it's a per-connection RPC-style request, not a
      session mutation. The session pins / refcounts panels are
      unchanged.
    - The :class:`LoadEntitySilentOp` adds a compact
      ``seq=N load_result_silent slot=M`` row (no expander) to the
      event log. The session-debug pane shows the same
      ``entity_refcounts`` increase as a normal load — the silent
      flavor only changes the wire payload, not the side effects.
    """
    async with rpc_peer(ws.rpc_url, "alice-rpc") as (rpc, ws_ref):
        child = Note(title="child", body="b").store(rpc.scope)
        parent = NoteWithChild(title="cache-me", child=child).store(rpc.scope)
        async with expose_entity(ws_ref, "parent", parent.entity_id, rpc.scope) as parent_id:
            print(f"  [rpc] parent={parent_id} (with embedded child Note)")
            await prompt(
                "RPC peer exposed a NoteWithChild. We'll do three "
                "kinds of loads against it: a regular session-bound "
                "load, an out-of-band direct load (8b), and a silent "
                "session-bound load (8c)."
            )

            async with connect_client(ws.client_url) as client:
                sid = await create_session(
                    client,
                    "client",
                    request_id=1,
                    metadata={"scenario": "silent-and-direct-load"},
                )
                sub = await subscribe_session(client, "client", sid, request_id=2)
                await prompt(f"check session {sid[:8]}… in the UI")

                # --- 1: regular load ------------------------------
                await prompt(
                    f"baseline: load_entity slot=1 against {parent_id}. "
                    "Normal LoadResultEvent with the full payload tree."
                )
                await mutate(
                    client,
                    "client",
                    request_id=10,
                    sub_id=sub,
                    ack_seq=0,
                    update_id=1,
                    ops=[{"kind": "load_entity", "slot": 1, "short_id": parent_id}],
                )
                load_frame = await _recv(client, "client")
                assert isinstance(load_frame, SessionEventFrame)
                assert isinstance(load_frame.event, LoadResultEvent)
                await prompt(
                    "LoadResultEvent in the event log; the session-debug "
                    "pane shows entity_refcounts incremented for the "
                    "child entity surfaced inside the CBOR. Note the "
                    "current refcount values — we'll compare against "
                    "the silent load's values in a moment."
                )

                # --- 2: out-of-band direct load -------------------
                await prompt(
                    f"out-of-band: LoadEntityRequest({parent_id}). "
                    "This is a per-connection request, not a session "
                    "op. No subscription needed, no event log entry, "
                    "no session refcount change — just bytes back to "
                    "this one client."
                )
                direct = await _request_direct_load(
                    client, "client", request_id=100, short_id=parent_id
                )
                await prompt(
                    "LoadEntityResponse delivered. Confirm in the UI: "
                    "the session sub-panel is unchanged — no new event "
                    "in the log, no refcount delta. The bytes match "
                    "what the previous load_entity returned (same "
                    "content-addressed entity)."
                )
                # Sanity: payload tag-shape matches the session-bound
                # load's payload structure (same CBOR underneath).
                assert isinstance(load_frame.event.payload, dict)
                assert isinstance(direct.payload, dict)

                # --- 3: silent load -------------------------------
                await prompt(
                    f"silent: load_entity_silent slot=2 against "
                    f"{parent_id}. Same workstation-side machinery as "
                    "load_entity (children tracked, session refcount "
                    "incremented), but the result event has no payload."
                )
                await mutate(
                    client,
                    "client",
                    request_id=11,
                    sub_id=sub,
                    ack_seq=2,
                    update_id=2,
                    ops=[
                        {
                            "kind": "load_entity_silent",
                            "slot": 2,
                            "short_id": parent_id,
                        }
                    ],
                )
                silent_frame = await _recv(client, "client")
                assert isinstance(silent_frame, SessionEventFrame)
                assert isinstance(silent_frame.event, LoadResultSilentEvent)
                await prompt(
                    "LoadResultSilentEvent in the event log — rendered "
                    "as a one-liner with no ▸ expander (no payload to "
                    "expand). The session-debug pane's entity_refcounts "
                    "should have ticked up by the same amount as the "
                    "first load: the smart-caching pattern. The client "
                    "would have already cached the bytes from the "
                    "out-of-band request, so there's nothing to "
                    "retransmit."
                )

                await close_session_via(
                    client, "client", sub, ack_seq=4, request_id=12, update_id=3
                )


# --- Scenario 12: preserved events, non-contiguous subsets (8a) ----


async def scenario_preserve_events(ws: WorkstationServer) -> None:
    """Generate many session events via a single ``fire_many`` invoke,
    then preserve a non-contiguous subset of the resulting
    :class:`CallbackEvent` seqs while acking past all of them. A
    follow-up mutation unpreserves *some* of those, leaving a different
    non-contiguous subset still preserved.

    What the UI shows:

    - The Open slots → callbacks row appears once create_callback
      commits.
    - The Open slots → invokes row flickers in for the duration of
      ``fire_many``; six CallbackEvents land in the event log as the
      loop iterates, and one InvokeResultEvent at the end.
    - After the preserve mutation: the event log retains exactly the
      preserved CallbackEvents (rendered in **orange** with a
      ``preserved`` chip) and the most recent state_update. Every
      other event past the new ack horizon is gone.
    - After the unpreserve mutation: the orange rows shrink to the
      remaining preserved subset; the unpreserved ones disappear.
    - The debug snapshot's ``preserved_events`` field tracks the live
      set throughout.
    """
    async with rpc_peer(ws.rpc_url, "alice-rpc") as (rpc, ws_ref):
        invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
        async with expose_service(ws_ref, "cb-invoker", invoker_ref, rpc.scope) as invoker_id:
            print(f"  [rpc] cb-invoker={invoker_id}")
            await prompt(
                "RPC peer exposed CallbackInvokerImpl. Its `fire_many` "
                "method takes a list of payloads and calls notify(p) "
                "for each one — we'll use it to generate a small "
                "burst of CallbackEvents on the session."
            )

            async with connect_client(ws.client_url) as client:
                sid = await create_session(
                    client,
                    "client",
                    request_id=1,
                    metadata={"scenario": "preserve-events"},
                )
                sub = await subscribe_session(client, "client", sid, request_id=2)
                await prompt(f"check session {sid[:8]}… in the UI; sub-panel opens")

                # m1: create the callback at slot 1.
                await prompt(
                    "create_callback at slot=1. Open slots → callbacks "
                    "should populate; the cb's send-side gets a fresh "
                    "s{N} short_id in the services table."
                )
                await mutate(
                    client,
                    "client",
                    request_id=10,
                    sub_id=sub,
                    ack_seq=0,
                    update_id=1,
                    ops=[{"kind": "create_callback", "slot": 1}],
                )

                # m2: invoke fire_many with six tagged payloads.
                # Each notify call commits a CallbackEvent; the
                # invoke_result lands at the end with [true, …, true].
                payloads = [f"hello-{i}" for i in range(6)]
                await prompt(
                    f"invoke cb-invoker.fire_many(['callback', 1], "
                    f"{payloads!r}) at slot=2. The loop fires notify() "
                    "six times, so we'll see six CallbackEvents land "
                    "in the event log followed by one InvokeResultEvent."
                )
                # Wire shape: ``payloads`` is a literal list, so under
                # the list-based escape codec it has to be wrapped as a
                # single-element list-of-list (``[[...]]``). Tagged
                # values like ``["callback", 1]`` are *not* wrapped —
                # the codec recognizes the 2-element-with-reserved-tag
                # form and decodes them in place.
                await mutate(
                    client,
                    "client",
                    request_id=11,
                    sub_id=sub,
                    ack_seq=1,
                    update_id=2,
                    ops=[
                        {
                            "kind": "invoke",
                            "slot": 2,
                            "service_short_id": invoker_id,
                            "method_name": "fire_many",
                            "args": [["callback", 1], [payloads]],
                        }
                    ],
                )
                cb_seqs: list[int] = []
                while len(cb_seqs) < len(payloads):
                    frame = await _recv(client, "client")
                    if isinstance(frame, SessionEventFrame) and isinstance(
                        frame.event, CallbackEvent
                    ):
                        cb_seqs.append(frame.event.seq)
                # Drain the trailing InvokeResultEvent.
                while True:
                    frame = await _recv(client, "client")
                    if isinstance(frame, SessionEventFrame) and isinstance(
                        frame.event, InvokeResultEvent
                    ):
                        invoke_result_seq = frame.event.seq
                        break
                print(f"  [client] callback seqs: {cb_seqs}")
                print(f"  [client] invoke_result seq: {invoke_result_seq}")
                await prompt(
                    "Six CallbackEvents in the event log, each "
                    "expandable to show its payload. Pick a non-"
                    "contiguous subset to preserve in the next step."
                )

                # m3: pick alternate seqs. cb_seqs is in order; preserve
                # indices 0, 2, 4 (every other one) so the remaining
                # event log has gaps once retention drops the rest.
                to_preserve = [cb_seqs[0], cb_seqs[2], cb_seqs[4]]
                will_drop = [cb_seqs[1], cb_seqs[3], cb_seqs[5]]
                await prompt(
                    f"preserve_events seqs={to_preserve} (the 1st, 3rd, "
                    f"5th of the burst); ack_seq={invoke_result_seq} "
                    f"so the entire fire_many's worth of events plus "
                    "the prior state_updates fall past the horizon. "
                    f"Seqs {will_drop} will be dropped by retention; "
                    f"seqs {to_preserve} are moved into the "
                    "preserved-events sidecar and rendered orange."
                )
                await mutate(
                    client,
                    "client",
                    request_id=12,
                    sub_id=sub,
                    ack_seq=invoke_result_seq,
                    update_id=3,
                    ops=[{"kind": "preserve_events", "seqs": to_preserve}],
                )
                await prompt(
                    "Event log now shows the three preserved "
                    "CallbackEvents in **orange** with a `preserved` "
                    "chip — the others are gone. The session-debug "
                    "pane's `preserved_events` field lists the seqs."
                )

                # m4: unpreserve a subset of what we just preserved.
                # Leave one CallbackEvent still preserved.
                still_preserved = [to_preserve[1]]  # the middle one
                to_unpreserve = [to_preserve[0], to_preserve[2]]
                await prompt(
                    f"unpreserve_events seqs={to_unpreserve}; the middle "
                    f"event (seq={still_preserved[0]}) stays preserved. "
                    "After this commit, the event log shows just one "
                    "orange row plus the latest state_update."
                )
                await mutate(
                    client,
                    "client",
                    request_id=13,
                    sub_id=sub,
                    ack_seq=invoke_result_seq + 1,
                    update_id=4,
                    ops=[{"kind": "unpreserve_events", "seqs": to_unpreserve}],
                )
                await prompt(
                    "Event log: a single orange `callback` row at "
                    f"seq={still_preserved[0]}, plus the most recent "
                    "state_update. `preserved_events` in the debug "
                    "snapshot is now a one-element list."
                )

                # m5: finish by unpreserving the last one + closing.
                await prompt(
                    f"unpreserve the last one (seq={still_preserved[0]}) "
                    "and close. After commit, no preserved events; "
                    "session is gone."
                )
                await mutate(
                    client,
                    "client",
                    request_id=14,
                    sub_id=sub,
                    ack_seq=invoke_result_seq + 2,
                    update_id=5,
                    ops=[{"kind": "unpreserve_events", "seqs": still_preserved}],
                )
                await close_session_via(
                    client,
                    "client",
                    sub,
                    ack_seq=invoke_result_seq + 3,
                    request_id=15,
                    update_id=6,
                )


# --- Menu / main loop ---------------------------------------------


SCENARIOS: list[tuple[str, str, Any]] = [
    (
        "1",
        "Basic happy path (RPC peer exposes; client mutates; foreign subscriber sees state_update)",
        scenario_happy_path,
    ),
    ("2", "Slot delivery (load + invoke; entity & service refs surfaced)", scenario_slots),
    (
        "3",
        "Session tracker keeps things alive (entity past RPC disconnect, service past explicit release)",
        scenario_tracker,
    ),
    ("4", "Stale-ack rejection + same-mutator skip-ahead (two clients)", scenario_stale_ack),
    (
        "5",
        "Retention horizon (load surfaces child; ack past it; child released)",
        scenario_retention,
    ),
    ("6", "Close as a mutation", scenario_close),
    (
        "7",
        "Dynamic services (factory → service → entity results, all tracked into state)",
        scenario_dynamic_services,
    ),
    (
        "8",
        "Callback lifecycle (create → fire → listening → close; watch open_callbacks)",
        scenario_callback_lifecycle,
    ),
    (
        "9",
        "Callback cross-session routing (alice's cb fired by bob; CallbackEvent lands on alice)",
        scenario_callback_cross_session,
    ),
    (
        "10",
        "Provenance metadata + layered loads "
        "(same data, two prov → distinct short IDs; metadata + loads "
        "of layered / bare / prov-node-itself)",
        scenario_provenance,
    ),
    (
        "11",
        "Silent + side-effect-only load (8b/8c: out-of-band "
        "LoadEntityRequest + LoadEntitySilentOp for client-side caching)",
        scenario_silent_and_direct_load,
    ),
    (
        "12",
        "Preserved events with non-contiguous subsets "
        "(8a: fire_many → preserve every-other CallbackEvent → "
        "unpreserve some, watch orange chips in the event log)",
        scenario_preserve_events,
    ),
]


async def main() -> None:
    bundled = WorkstationServer.bundled_debug_ui_dist_path()
    if not (bundled / "index.html").is_file():
        print(
            "ERROR: the debug UI hasn't been built yet.\n"
            "Run `cd fxtr_workstation/debug_ui && pnpm install && pnpm build` "
            "and try again."
        )
        return

    async with WorkstationServer(debug_ui_dist_path=bundled) as ws:
        print()
        print("Workstation server up:")
        print(f"  /rpc:    {ws.rpc_url}")
        print(f"  /client: {ws.client_url}")
        print(f"  /debug:  {ws.debug_http_url}/")
        print()
        print("Open the /debug URL in your browser. The page should be empty.")

        while True:
            print()
            print("Pick a scenario:")
            for key, label, _ in SCENARIOS:
                print(f"  {key}. {label}")
            print("  q. Quit")

            def _read() -> str:
                try:
                    return input("> ").strip()
                except EOFError:
                    return "q"

            choice = await anyio.to_thread.run_sync(_read)
            if choice == "q":
                return
            match = next(((k, lbl, fn) for k, lbl, fn in SCENARIOS if k == choice), None)
            if match is None:
                print(f"unknown choice: {choice!r}")
                continue
            _, label, fn = match
            print()
            print(f"=== {label} ===")
            try:
                await fn(ws)
            except KeyboardInterrupt:
                print("\nscenario aborted by Ctrl-C")
                raise
            except Exception as e:  # noqa: BLE001
                print(f"\nscenario raised: {type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        anyio.run(main)
    except KeyboardInterrupt:
        print("\nAborted.")
