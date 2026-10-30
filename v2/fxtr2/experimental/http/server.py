"""Hypercorn-based server wrapper for the fxtr ASGI app.

Provides convenience functions for serving the ASGI app:
- serve_until: Serve until an event is set
- serve_forever: Serve until cancelled
- serving_context: Async context manager that runs a background server
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio

from fxtr2.experimental.http.asgi_app import create_fxtr_app

if TYPE_CHECKING:
    from fxtr2.data.entity_core import FxtrID
    from fxtr2.data.entity_ref_scope import FxtrScope


async def serve_until(
    scope: FxtrScope,
    pins: dict[str, FxtrID],
    done: anyio.Event,
    port: int,
    host: str = "127.0.0.1",
    *,
    started: anyio.Event | None = None,
    provenanced: bool = False,
) -> None:
    """Serve the ASGI app until the event is set.

    Args:
        scope: The FxtrScope to serve entities from.
        pins: Dict of pin names to FxtrIDs.
        done: Event that signals when to shut down.
        port: Port to listen on.
        host: Host to bind to.
        started: Optional event that is set once the server is listening.
        provenanced: When ``True``, layered pins expose provenance (the layered
            ``[data, prov]`` form + prov-node precomp); ``False`` (default) serves
            every pin bare.
    """
    import hypercorn.asyncio
    import hypercorn.config

    app = create_fxtr_app(scope, pins, provenanced=provenanced)

    if started is not None:
        original_app = app

        @asynccontextmanager
        async def _lifespan(app):
            started.set()
            yield

        from starlette.applications import Starlette

        app = Starlette(
            routes=original_app.routes,
            lifespan=_lifespan,
        )

    config = hypercorn.config.Config()
    config.bind = [f"{host}:{port}"]
    config.loglevel = "WARNING"

    await hypercorn.asyncio.serve(
        app,  # type: ignore
        config,
        shutdown_trigger=done.wait,  # type: ignore[arg-type]
    )


async def serve_forever(
    scope: FxtrScope,
    pins: dict[str, FxtrID],
    port: int,
    host: str = "127.0.0.1",
    *,
    provenanced: bool = False,
) -> None:
    """Serve the ASGI app until the task is cancelled.

    Args:
        scope: The FxtrScope to serve entities from.
        pins: Dict of pin names to FxtrIDs.
        port: Port to listen on.
        host: Host to bind to.
        provenanced: When ``True``, layered pins expose provenance; ``False``
            (default) serves every pin bare.
    """
    done = anyio.Event()
    try:
        await serve_until(scope, pins, done, port, host, provenanced=provenanced)
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def serving_context(
    scope: FxtrScope,
    pins: dict[str, FxtrID],
    port: int,
    host: str = "127.0.0.1",
    *,
    provenanced: bool = False,
) -> AsyncIterator[None]:
    """Start a background server in a task group, shut down on context exit.

    Args:
        scope: The FxtrScope to serve entities from.
        pins: Dict of pin names to FxtrIDs.
        port: Port to listen on.
        host: Host to bind to.
        provenanced: When ``True``, layered pins expose provenance; ``False``
            (default) serves every pin bare.
    """
    done = anyio.Event()
    started = anyio.Event()

    async def _run() -> None:
        await serve_until(scope, pins, done, port, host, started=started, provenanced=provenanced)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_run)
        await started.wait()
        try:
            yield
        finally:
            done.set()
