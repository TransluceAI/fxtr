"""Kitchen-sink stateless backend for the connector-http integration tests.

Built on the Part 2 ``fxtr2.experimental.http.service_*`` library (a
pinned entity graph + GET/POST/WS services returning entities,
descriptors, callbacks/transients, and a ``$fxtr.type`` method). Run via
``uv run python _demo_backend.py`` from the workstation env (which carries
``fxtr2`` editable). It picks a free port, serves the combined entity-data
+ discovery + service + ws app, prints ``READY http://127.0.0.1:<port>``
to stdout once listening, then shuts down gracefully on SIGTERM/SIGINT.
The TS harness (`_integration_harness.ts`) spawns it and reads the READY
line.

Served with ``provenanced=True`` so layered pins expose their provenance.

Exposed surface:
  - entities: ``root`` (a Parent referencing a ``child`` Child) + ``child``
    (both bare), and ``prov_root`` — a genuinely-provenanced Parent → Child
    (layered ids) for the provenance acceptance case.
  - ``echo`` — GET service: ``echo(x) -> x``, ``$fxtr.type``.
  - ``data`` — POST service: ``get_root() -> <root entity>``,
    ``make_service() -> <undiscoverable GET descriptor>``,
    ``boom() -> error``, ``$fxtr.type``.
  - ``feed`` — WS service: ``subscribe(inbox) -> <transient unsubscribe>``
    (emits 3 ordered notifies ``n0``/``n1``/``n2`` first), ``boom()`` →
    error, ``$fxtr.type``.
  - a hidden GET service (no discovery name) handed out by
    ``data.make_service`` — ``ping() -> "hidden-pong"``.
"""

from __future__ import annotations

import dataclasses
import signal
import socket
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import fxtr2.data.system_entities  # noqa: F401 — registers the `ctx` builtin handler
import hypercorn.asyncio
import hypercorn.config
from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.experimental.http.asgi_app import create_fxtr_app
from fxtr2.experimental.http.service_registry import ServiceRegistry
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route

GROUND = FxtrID.from_tag_and_payload(".", b"")

CHILD_TYPE = FxtrID.builtin("demo_http_child")
PARENT_TYPE = FxtrID.builtin("demo_http_parent")
ECHO_TYPE = FxtrID.builtin("demo_http_echo")
DATA_TYPE = FxtrID.builtin("demo_http_data")
FEED_TYPE = FxtrID.builtin("demo_http_feed")


@dataclasses.dataclass(frozen=True)
class Child(DataclassEntity, fxtr_type=CHILD_TYPE):
    label: str
    value: int


@dataclasses.dataclass(frozen=True)
class Parent(DataclassEntity, fxtr_type=PARENT_TYPE):
    title: str
    child: ChildRef[Child]


def _make_c_node(scope: FxtrScope) -> FxtrID:
    """Mint a c-node provenance context so a child scope can store
    genuinely-provenanced (``n``-node) entities."""
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": "demo_http_prov",
    }
    return scope.create_c_node(data)


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
    async with FxtrScope(name="demo_http_backend") as scope:
        for tid in (CHILD_TYPE, PARENT_TYPE, ECHO_TYPE, DATA_TYPE, FEED_TYPE):
            scope.track_builtin(tid)

        child_ref = scope.store(Child(label="kid", value=42))
        parent_ref = scope.store(Parent(title="root", child=child_ref))
        pins = {"child": child_ref.entity_id, "root": parent_ref.entity_id}

        # A genuinely-provenanced Parent → Child: stored in a child scope under
        # a c-node provenance context (so the store takes its prov path and the
        # parent gets an `n`-node provenance parent), then tracked back into the
        # serving scope so `/data` can fetch it. Served with provenance exposed
        # (``provenanced=True`` below), this is the layered-entity acceptance
        # case — its `provenance_parent` surfaces in metadata, and loading it
        # yields a layered child.
        c_node = _make_c_node(scope)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=scope,
            include=[scope],
            name="demo_http_backend_prov",
        ) as prov_scope:
            prov_child = prov_scope.store(Child(label="prov-kid", value=7))
            prov_parent = prov_scope.store(Parent(title="prov-root", child=prov_child))
            prov_root_id = prov_parent.entity_id
            assert prov_root_id._provenance_parent is not None
            scope.track_from_scope(prov_root_id, prov_scope)
        pins["prov_root"] = prov_root_id

        reg = ServiceRegistry(scope, provenanced=True)
        reg.add_entity("root", parent_ref.entity_id)
        reg.add_entity("child", child_ref.entity_id)
        reg.add_entity("prov_root", prov_root_id)

        # An undiscoverable GET service — mounted but not listed; handed
        # out by `data.make_service` as a returned descriptor.
        hidden_desc = reg.add_rest_service(
            path="/svc/hidden", impl={"ping": _hidden_ping}, method="GET"
        )

        async def _get_root(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            return parent_ref.entity_id

        async def _make_service(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            return hidden_desc

        async def _subscribe(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            inbox = args[0]  # a CallbackAddress
            for i in range(3):
                ctx.notify(inbox, f"n{i}")  # ws FIFO: delivered before the result
            return ctx.transient({"unsubscribe": _unsubscribe})

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
                "get_root": _get_root,
                "make_service": _make_service,
                "boom": _boom,
                "$fxtr.type": _type_method(DATA_TYPE),
            },
            method="POST",
            type_id=DATA_TYPE,
            discovery_name="data",
        )
        reg.add_ws_service(
            "feed",
            ws_path="/ws",
            impl={
                "subscribe": _subscribe,
                "boom": _boom,
                "$fxtr.type": _type_method(FEED_TYPE),
            },
            type_id=FEED_TYPE,
            discovery_name="feed",
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
            # Trigger hypercorn's graceful shutdown and let ``serve()`` return,
            # then the task group + scope unwind cleanly. This is prompt
            # because the harness closes every connector first (which closes
            # the client ws sockets), so no server read loop is left parked —
            # an open ws would otherwise block the graceful drain. The
            # harness's SIGKILL fallback remains as a backstop.
            done.set()


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
