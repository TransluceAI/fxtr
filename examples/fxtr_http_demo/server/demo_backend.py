"""Stateless HTTP demo backend for `examples/fxtr_http_demo` (fxtr 7a Part 5).

A single stateless backend exercising the whole `@fxtr-ui/connector-http`
+ `@fxtr-ui/panel-viewer` stack end-to-end. Built on the Part 2
`fxtr2.experimental.http.service_*` library (entity-data + discovery +
GET/POST/WS services) plus a **hand-rolled WS dynamic-dispatch service**
(the workstation's `BasicDynamicDispatchImpl` is bound to the workstation
RPC system, so it can't be reused here — but the renderer-compile step
and the dispatch entity/notification types are reused from
`fxtr_workstation.dynamic_dispatch`).

Run via ``uv run python server/demo_backend.py`` from this example's env
(carries ``fxtr2`` + ``fxtr_workstation`` editable). It picks a free
port, serves the combined app, prints ``READY http://127.0.0.1:<port>``
once listening, then shuts down gracefully on SIGTERM/SIGINT. The web/
orchestrator spawns it and reads the READY line.

Served with ``provenanced=True`` so layered pins expose their provenance.

Exposed surface:
  - **entities** — two ``Note``s + two ``Photo``s (bare), plus ``note-prov``
    (a genuinely-provenanced ``Note`` that *links* to a provenanced ``Photo`` —
    its own ``provenance_parent`` surfaces in the metadata panel, and following
    its ``links.source`` ref reaches a Photo that itself carries provenance:
    the nested-object provenance case), all pinned + discoverable.
  - **echo** — GET service: ``echo(x) -> x``, ``$fxtr.type``.
  - **data** — POST service: ``get_note() -> <note entity>``,
    ``make_service() -> <undiscoverable GET descriptor>``,
    ``boom() -> error``, ``$fxtr.type``.
  - **dispatcher** — WS dynamic-dispatch service: ``resolve(typeRef,
    keyUuid) -> {subtype_key, renderer} | None`` (table keyed by the
    entity type's id_string) + ``subscribe(inbox) -> <transient
    unsubscribe>`` (emits a reload-everything notify immediately, so a
    fresh connect / reconnect self-heals stale caches), ``$fxtr.type``.
  - a hidden GET service handed out by ``data.make_service`` —
    ``ping() -> "hidden-pong"``.

Renderers (``note`` / ``photo``) are esbuild-compiled at startup from
``server/renderers/<name>/index.tsx`` into ``RendererEntity`` blobs and
mapped by entity type id. Baseline hot reload: edit a renderer and
**bounce this process** — the ws drop makes the frontend's reload inbox
go ``broken`` (Part 1) → caches wipe → re-resolve → fresh renderers; or
hit the dispatcher-debug "reload everything" button. No file-watcher.
"""

from __future__ import annotations

import dataclasses
import signal
import socket
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import fxtr2.data.system_entities  # noqa: F401 — registers the `ctx` builtin handler
import hypercorn.asyncio
import hypercorn.config
from fxtr2.conversion.converters.mashumaro_entities import ChildFxtrID, DataclassEntity
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.experimental.http.asgi_app import create_fxtr_app
from fxtr2.experimental.http.service_registry import ServiceRegistry
from fxtr_workstation.dynamic_dispatch import ReloadEverythingNotification, RendererEntity
from fxtr_workstation.dynamic_dispatch.basic_dynamic_dispatch.compile import compile_renderer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route

# Demo entity + service type ids. Builtin (not content-addressed) so the
# renderer table can key on a stable id_string; `track_builtin` makes the
# type entity itself loadable via /data (the frontend reads an entity's
# `$fxtr.type` and passes it to `resolve`).
NOTE_TYPE = FxtrID.builtin("demo_http_note")
PHOTO_TYPE = FxtrID.builtin("demo_http_photo")
ECHO_TYPE = FxtrID.builtin("demo_http_echo")
DATA_TYPE = FxtrID.builtin("demo_http_data")
DISPATCHER_TYPE = FxtrID.builtin("demo_http_dispatcher")
STOPWATCH_TYPE = FxtrID.builtin("demo_http_stopwatch")

RENDERERS_DIR = Path(__file__).parent / "renderers"
#: Outer key of the compiled module's 2-level map (`module[subtype_key][keyUuid]`).
SUBTYPE_KEY = "default"

GROUND = FxtrID.from_tag_and_payload(".", b"")


def _make_c_node(scope: FxtrScope) -> FxtrID:
    """Mint a c-node provenance context so a child scope can store a
    genuinely-provenanced (``n``-node) entity."""
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": "demo_http_prov",
    }
    return scope.create_c_node(data)


@dataclasses.dataclass(frozen=True)
class Note(DataclassEntity, fxtr_type=NOTE_TYPE):
    title: str
    body: str
    # Named entity references (strong links). A provenanced note keeps its
    # links provenance-carrying, so following one surfaces the linked
    # entity's own provenance — the nested-object provenance case.
    links: dict[str, ChildFxtrID] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Photo(DataclassEntity, fxtr_type=PHOTO_TYPE):
    caption: str
    url: str


def _type_method(type_id: FxtrID) -> Callable[..., Any]:
    """A ``$fxtr.type`` method returning the service's type entity id."""

    async def fxtr_type(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
        return type_id

    return fxtr_type


async def _echo(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
    return args[0]


async def _hidden_ping(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
    return "hidden-pong"


async def _boom(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
    raise ValueError("boom")


async def _unsubscribe(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
    return None


async def _amain() -> None:
    # include=[GLOBAL_SCOPE] so the `define_type`-registered RendererEntity
    # / ReloadEverythingNotification type descriptors are reachable for the
    # store lift pass (see BasicDynamicDispatchImpl's scope-discipline note).
    async with FxtrScope(name="fxtr_http_demo_backend", include=[GLOBAL_SCOPE]) as scope:
        for tid in (NOTE_TYPE, PHOTO_TYPE, ECHO_TYPE, DATA_TYPE, DISPATCHER_TYPE, STOPWATCH_TYPE):
            scope.track_builtin(tid)

        # Task group the stopwatch ticker tasks run on; assigned when the
        # serve task group opens below (read at request time, so the
        # late binding is fine).
        bg_tg = None

        # --- Entities -------------------------------------------------
        note1 = scope.store(Note(title="Welcome", body="The first note."))
        note2 = scope.store(Note(title="Second", body="Another note."))
        photo1 = scope.store(Photo(caption="A photo", url="https://example.invalid/a.png"))
        photo2 = scope.store(Photo(caption="Another", url="https://example.invalid/b.png"))

        # A genuinely-provenanced Note that *links* to a provenanced Photo,
        # stored under a c-node provenance context (so both get `n`-node
        # provenance parents), then tracked back into the serving scope.
        # Served with provenance exposed (``provenanced=True`` below): the
        # note's own `provenance_parent` surfaces in the metadata panel, and
        # following its `links.source` ref reaches a Photo that itself carries
        # provenance — the nested-object provenance case, navigable in the UI.
        c_node = _make_c_node(scope)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=scope,
            include=[scope],
            name="fxtr_http_demo_backend_prov",
        ) as prov_scope:
            prov_photo_id = prov_scope.store(
                Photo(caption="Source photo", url="https://example.invalid/source.png")
            ).entity_id
            note_prov_id = prov_scope.store(
                Note(
                    title="Provenanced",
                    body="Carries provenance, and links to a provenanced photo.",
                    links={"source": prov_photo_id},
                )
            ).entity_id
            assert note_prov_id._provenance_parent is not None
            scope.track_from_scope(note_prov_id, prov_scope)

        pins = {
            "note-1": note1.entity_id,
            "note-2": note2.entity_id,
            "note-prov": note_prov_id,
            "photo-1": photo1.entity_id,
            "photo-2": photo2.entity_id,
        }

        # --- Renderers (esbuild-compiled at startup) ------------------
        # Map entity-type id_string -> renderer entity ref. The frontend
        # resolves an entity's type (its `$fxtr.type`) against this table.
        renderer_table: dict[str, Any] = {}
        for type_id, subdir in (
            (NOTE_TYPE, "note"),
            (PHOTO_TYPE, "photo"),
            (ECHO_TYPE, "echo"),
            (STOPWATCH_TYPE, "stopwatch"),
        ):
            module_bytes = await compile_renderer(RENDERERS_DIR / subdir)
            renderer_ref = RendererEntity(module=module_bytes).store(scope)
            renderer_table[type_id.id_string()] = renderer_ref

        # The reload-everything payload; content-hash-deduped, /data-loadable.
        reload_ref = ReloadEverythingNotification().store(scope)

        # --- Services -------------------------------------------------
        reg = ServiceRegistry(scope, provenanced=True)
        for name, ref in (
            ("note-1", note1),
            ("note-2", note2),
            ("photo-1", photo1),
            ("photo-2", photo2),
        ):
            reg.add_entity(name, ref.entity_id)
        reg.add_entity("note-prov", note_prov_id)

        # Undiscoverable GET service handed out by `data.make_service`.
        hidden_desc = reg.add_rest_service(
            path="/svc/hidden", impl={"ping": _hidden_ping}, method="GET"
        )

        async def _get_note(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            return note1.entity_id

        async def _make_service(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            return hidden_desc

        async def _resolve(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            # args = [type_ref (FxtrID), dispatch_key_uuid (str)]. We map
            # every dispatch key for a type to the same renderer module
            # (the module itself is keyed by dispatch-key uuid), so the
            # second arg is ignored.
            type_id: FxtrID = args[0]
            renderer_ref = renderer_table.get(type_id.id_string())
            if renderer_ref is None:
                return None  # NoMatch — surfaces the dispatch key's fallback
            return {"subtype_key": SUBTYPE_KEY, "renderer": renderer_ref}

        async def _subscribe(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            inbox = args[0]  # CallbackAddress
            # Emit a reload immediately: a fresh page load / reconnect
            # re-subscribes and this wipes any stale persisted caches.
            ctx.notify(inbox, reload_ref)
            return ctx.transient({"unsubscribe": _unsubscribe})

        async def _stopwatch_subscribe(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            # Push an incrementing count to the inbox once a second until
            # the subscriber unsubscribes (or the connection drops, where
            # `notify` starts failing and the ticker bails).
            inbox = args[0]
            stop = anyio.Event()

            async def _tick() -> None:
                count = 0
                while not stop.is_set():
                    try:
                        ctx.notify(inbox, count)
                    except Exception:
                        break
                    count += 1
                    with anyio.move_on_after(1.0):
                        await stop.wait()

            assert bg_tg is not None
            bg_tg.start_soon(_tick)

            async def _stopwatch_unsub(a: list[Any], k: dict[str, Any], c: Any) -> Any:
                stop.set()
                return None

            return ctx.transient({"unsubscribe": _stopwatch_unsub})

        reg.add_rest_service(
            path="/svc/echo",
            impl={"echo": _echo, "$fxtr.type": _type_method(ECHO_TYPE)},
            method="GET",
            type_id=ECHO_TYPE,
            discovery_name="echo",
        )
        reg.add_rest_service(
            path="/svc/data",
            impl={
                "get_note": _get_note,
                "make_service": _make_service,
                "boom": _boom,
                "$fxtr.type": _type_method(DATA_TYPE),
            },
            method="POST",
            type_id=DATA_TYPE,
            discovery_name="data",
        )
        reg.add_ws_service(
            "dispatcher",
            ws_path="/ws",
            impl={
                "resolve": _resolve,
                "subscribe": _subscribe,
                "$fxtr.type": _type_method(DISPATCHER_TYPE),
            },
            type_id=DISPATCHER_TYPE,
            discovery_name="dispatcher",
        )
        reg.add_ws_service(
            "stopwatch",
            ws_path="/ws",
            impl={
                "subscribe": _stopwatch_subscribe,
                "$fxtr.type": _type_method(STOPWATCH_TYPE),
            },
            type_id=STOPWATCH_TYPE,
            discovery_name="stopwatch",
        )

        entity_app = create_fxtr_app(scope, pins, provenanced=True)
        port = _free_port()
        done = anyio.Event()

        @asynccontextmanager
        async def _lifespan(_app: Starlette) -> AsyncGenerator[None, None]:
            print(f"READY http://127.0.0.1:{port}", flush=True)
            yield

        app = Starlette(
            routes=[
                *entity_app.routes,
                *reg.routes(),
                Route("/discovery", reg.discovery_handler),
            ],
            middleware=[
                Middleware(
                    CORSMiddleware,
                    allow_origins=["*"],
                    allow_methods=["*"],
                    allow_headers=["*"],
                )
            ],
            lifespan=_lifespan,
        )

        config = hypercorn.config.Config()
        config.bind = [f"127.0.0.1:{port}"]
        config.loglevel = "WARNING"

        async with anyio.create_task_group() as tg:
            bg_tg = tg  # stopwatch tickers run here

            async def _serve() -> None:
                await hypercorn.asyncio.serve(
                    app,  # type: ignore[arg-type]
                    config,
                    shutdown_trigger=done.wait,  # type: ignore[arg-type]
                )

            tg.start_soon(_serve)
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                async for _ in signals:
                    break
            done.set()
            # Cancel any still-ticking stopwatch tasks so the group unwinds.
            tg.cancel_scope.cancel()


def _free_port() -> int:
    """Pick a currently-free localhost TCP port (small bind race window)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def main() -> None:
    try:
        anyio.run(_amain)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
