"""Serve a published flake over the stateless HTTP backend (manager tier).

The flake-agnostic glue between the :class:`~fxtr_nix_runner.manager.FlakeManager`
read adapters and the ``fxtr2.experimental.http`` serving substrate: it builds
everything a publishing flake's ``fxtr`` attrset declares and serves it with
**provenance exposed**:

- ``fxtr.entities.*`` — layered ``(data, prov)`` ids on the wire for provenanced
  layers;
- ``fxtr.namespaces.*`` — bare ``"<ns>:<key>"`` pins (the tsv flow is
  provenance-stripping);
- ``fxtr.uiPresets.<name>`` — a flake-built dynamic-dispatch service
  (``resolve`` / ``subscribe``) whose renderers are built from the preset, so a
  frontend can resolve a renderer per entity type.

Nothing here is demo-specific, so it lives in ``fxtr_nix_runner`` rather than in
any one example. This module is fxtr2-aware (manager tier) and pulls in the http
substrate; like ``rehost`` / ``manager`` it is imported explicitly and **not**
re-exported from ``__init__`` (so ``import fxtr_nix_runner`` stays light)::

    from fxtr_nix_runner.serve import published_serving_context

    async with published_serving_context(repo, port=8080) as server:
        ...  # hit server.base_url
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, cast

import anyio
import hypercorn.asyncio
import hypercorn.config
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.experimental.dynamic_dispatch import (
    WORKSTATION_DISPATCHER_TYPE_ID,
    ReloadEverythingNotification,
)
from fxtr2.experimental.http.asgi_app import create_fxtr_app
from fxtr2.experimental.http.service_registry import ServiceRegistry
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr_nix_runner.manager import FlakeManager
from fxtr_nix_runner.namespaces import build_namespace_manifest
from fxtr_nix_runner.renderers import RendererTable, build_preset_table
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from fxtr_nix_runner._backend import Backend
    from fxtr_nix_runner._config import NixRuntimeConfig


@dataclass
class PublishedServer:
    """The handle :func:`published_serving_context` yields once listening.

    Attributes:
        base_url: The ``http://host:port`` the server is bound to.
        port: The bound port.
        scope: The serving scope.
        registry: The :class:`ServiceRegistry` backing ``/discovery``.
        entity_ids: The declared ``fxtr.entities.*`` ids (layered for a
            provenanced layer).
    """

    base_url: str
    port: int
    scope: FxtrScope
    registry: ServiceRegistry
    entity_ids: dict[str, FxtrID]


def _pick_preset(declared: list[str], requested: str | None) -> str | None:
    """Choose which renderer preset to serve.

    An explicit ``requested`` must be declared (else error). Otherwise: prefer one
    named ``default``, then a sole declared preset; with none declared — or several
    and no ``default`` — return ``None`` (serve no dispatcher; pass ``--preset`` to
    disambiguate).
    """
    if requested is not None:
        if requested not in declared:
            raise ValueError(f"--preset {requested!r} not declared; available: {declared}")
        return requested
    if "default" in declared:
        return "default"
    if len(declared) == 1:
        return declared[0]
    return None


def _register_dispatcher(reg: ServiceRegistry, scope: FxtrScope, table: RendererTable) -> None:
    """Register the flake-driven dynamic-dispatch ws service over ``table``.

    A method-map ws service named ``dispatcher`` on ``/ws``: ``resolve(type_ref,
    key_uuid)`` is a table lookup returning ``{subtype_key, renderer}`` (the
    renderer ref escapes to an entity tag the frontend loads via ``/data``) or
    ``None``; ``subscribe(inbox)`` emits a ``ReloadEverythingNotification`` then
    returns a transient unsubscribe (static serve — reload fires on connect).
    """
    reload_ref = ReloadEverythingNotification().store(scope)

    async def _resolve(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
        type_ref: FxtrID = args[0]
        key_uuid: str = args[1]
        by_key = table.get(type_ref.id_string())
        if by_key is None:
            return None
        entry = by_key.get(key_uuid)
        if entry is None:
            return None
        return {"subtype_key": entry.subtype_key, "renderer": entry.renderer}

    async def _unsubscribe(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
        return None

    async def _subscribe(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
        inbox = args[0]
        ctx.notify(inbox, reload_ref)
        return ctx.transient({"unsubscribe": _unsubscribe})

    async def _fxtr_type(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
        return WORKSTATION_DISPATCHER_TYPE_ID

    reg.add_ws_service(
        "dispatcher",
        ws_path="/ws",
        impl={"resolve": _resolve, "subscribe": _subscribe, "$fxtr.type": _fxtr_type},
        type_id=WORKSTATION_DISPATCHER_TYPE_ID,
        discovery_name="dispatcher",
    )


@contextlib.asynccontextmanager
async def published_serving_context(
    repo: Path | str,
    *,
    port: int,
    rev: str | None = None,
    host: str = "127.0.0.1",
    preset: str | None = None,
    web_dist: Path | str | None = None,
    backend: Backend | None = None,
    config: NixRuntimeConfig | None = None,
) -> AsyncIterator[PublishedServer]:
    """Build a publishing flake and serve it over HTTP, provenance exposed.

    Drives a :class:`FlakeManager` to retrieve every ``fxtr.entities.*`` (layered
    ids for provenanced layers), build every ``fxtr.namespaces.*`` manifest, and —
    if the flake declares a renderer preset — build it into a dispatcher table.
    Then assembles + serves the 7a stateless backend (``create_fxtr_app`` +
    ``ServiceRegistry``, both ``provenanced=True``, plus the dispatcher ws service)
    in a background task. Mirrors ``fxtr2.experimental.http.server.serving_context``:
    yields once listening and shuts the server down on exit.

    The serving scope ``include=[mgr.scope, GLOBAL_SCOPE]`` and the repo store is
    registered as a deep-fetch source, so ``/data`` resolves an entity by walking
    up to the manager scope (entities mounted by ``retrieve``), the serving scope
    itself (renderer blobs minted there), or the store (namespace pins +
    remote-pointer closures) — no eager closure copy. It is opened *around* the
    build span so renderer entities mint into it during the build; the manager
    scope + store outlive the span, so reads stay resolvable for the serve.

    Args:
        repo: Any path inside the publishing flake repo.
        port: The (free) TCP port to bind.
        rev: A committed rev to read the flake at, or ``None`` for the live workdir.
        host: The interface to bind (default loopback).
        preset: The renderer preset name to serve; ``None`` picks the sole / a
            ``default`` preset (see :func:`_pick_preset`). No dispatcher is served
            if the flake declares none.
        web_dist: Optional directory of a built static SPA (the shared
            ``@fxtr-ui/published-app`` viewer's ``dist/``). Mounted at ``/`` as an
            SPA fallback **after** the API routes, so a bare ``fxtr_nix serve
            --web-dist DIR`` brings up the UI on the same origin as the API. The
            mount is flake-agnostic — it serves any built SPA.
        backend: A pre-resolved backend (mostly for tests); resolved from config
            when omitted.
        config: Optional ``NixRuntimeConfig`` for backend resolution.
    """
    async with FlakeManager(repo, rev=rev, backend=backend, config=config) as mgr:
        # Serving scope: entities resolve up through mgr.scope (mounted by
        # retrieve); renderer blobs are minted here; GLOBAL_SCOPE makes the
        # RendererEntity / notification type descriptors reachable for minting; the
        # repo store resolves namespace pins + remote-pointer closures. Opened
        # around the build span so renderers mint into it during the build.
        async with FxtrScope(
            name="published-serving", isolated=True, include=[mgr.scope, GLOBAL_SCOPE]
        ) as serving_scope:
            store, lease = mgr.repo_store
            serving_scope.register_deep_fetch_source(store, IndependentLocalBridge(lease=lease))

            # Build phase: retrieve entities (lazy), build namespace manifests, and
            # build the chosen renderer preset into a dispatcher table.
            async with mgr.build_span() as span:
                ents = mgr.entities(span)
                entity_ids: dict[str, FxtrID] = {
                    name: cast(FxtrID, await ents.retrieve(name))
                    for name in await ents.list_entity_names()
                }
                ns_manifests: dict[str, dict[str, FxtrID]] = {
                    ns: await build_namespace_manifest(span, rev, ns)
                    for ns in await mgr.list_namespace_names(span)
                }
                chosen = _pick_preset(await mgr.list_preset_names(span), preset)
                table = (
                    await build_preset_table(span, serving_scope, rev, chosen)
                    if chosen is not None
                    else None
                )

            pins: dict[str, FxtrID] = dict(entity_ids)  # entities: layered
            for ns, manifest in ns_manifests.items():
                for key, fid in manifest.items():
                    pins[f"{ns}:{key}"] = fid  # namespace pins: bare, "<ns>:<key>"

            base_url = f"http://{host}:{port}"
            done = anyio.Event()
            started = anyio.Event()

            @contextlib.asynccontextmanager
            async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
                started.set()
                yield

            async with anyio.create_task_group() as tg:
                # Relative discovery URLs (no base_url): the client resolves them
                # against whatever origin it actually reached — robust behind a
                # proxy / a non-loopback hostname.
                reg = ServiceRegistry(serving_scope, provenanced=True)
                for name, fid in entity_ids.items():
                    reg.add_entity(name, fid)
                if table is not None:
                    _register_dispatcher(reg, serving_scope, table)

                entity_app = create_fxtr_app(serving_scope, pins, provenanced=True)
                routes = [
                    *entity_app.routes,
                    *reg.routes(),
                    Route("/discovery", reg.discovery_handler),
                ]
                # Optional static SPA mount. Appended LAST so it only catches
                # paths the API routes didn't (Starlette tries routes in order, and
                # a Mount on "/" matches everything). `html=True` serves index.html
                # for directory / unknown-path requests (SPA fallback). The served
                # app reaches the API on the same origin (relative discovery URLs +
                # /data renderer blobs), so no base-url plumbing is needed.
                if web_dist is not None:
                    routes.append(Mount("/", app=StaticFiles(directory=str(web_dist), html=True)))

                app = Starlette(
                    routes=routes,
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

                hcfg = hypercorn.config.Config()
                hcfg.bind = [f"{host}:{port}"]
                hcfg.loglevel = "WARNING"

                async def _serve() -> None:
                    await hypercorn.asyncio.serve(
                        app,  # type: ignore[arg-type]
                        hcfg,
                        shutdown_trigger=done.wait,  # type: ignore[arg-type]
                    )

                tg.start_soon(_serve)
                await started.wait()
                try:
                    yield PublishedServer(
                        base_url=base_url,
                        port=port,
                        scope=serving_scope,
                        registry=reg,
                        entity_ids=entity_ids,
                    )
                finally:
                    # Trigger hypercorn's graceful shutdown and let _serve return
                    # (the task group then exits). Graceful by default — a consumer
                    # that spawns its own background work owns stopping it; we don't
                    # force-cancel here.
                    done.set()


async def run_published_server(
    repo: Path | str,
    *,
    port: int,
    rev: str | None = None,
    host: str = "127.0.0.1",
    preset: str | None = None,
    web_dist: Path | str | None = None,
) -> None:
    """Serve a publishing flake until SIGINT/SIGTERM (the ``fxtr_nix serve`` CLI).

    Enters :func:`published_serving_context`, prints the bound URL once listening,
    and parks on the first SIGINT/SIGTERM.
    """
    import signal

    async with published_serving_context(
        repo, port=port, rev=rev, host=host, preset=preset, web_dist=web_dist
    ) as server:
        print(f"serving {Path(repo)} at {server.base_url}", flush=True)
        with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
            async for _ in signals:
                break
