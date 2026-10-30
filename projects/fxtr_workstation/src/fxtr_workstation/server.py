"""Workstation server lifecycle.

Stage 1 walking skeleton: spins hypercorn up around the Starlette app
in :mod:`fxtr_workstation.ws_app`, owning a workstation-private
``root_scope`` for the duration of the context.

**Scope discipline.** ``WorkstationServer`` is a user-facing class, so
the ``__init__`` boundary captures an active scope when the caller
doesn't pass one. After that, every internal scope is
``isolated=True`` with an explicit ``include=`` — no reliance on the
caller's active scope flows through the route or impl code. See
``v2/notes/scope_discipline.md``.

Usage::

    async with WorkstationServer() as ws:
        ...
        # ws.bound_url -> ws://127.0.0.1:<port>
        # ws.rpc_url   -> ws://127.0.0.1:<port>/rpc
        # ws.state     -> WorkstationState (root_scope is ws.state.root_scope)
"""

from __future__ import annotations

import logging
import pathlib
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Self

import anyio
import hypercorn.asyncio
import hypercorn.config
from anyio import AsyncContextManagerMixin
from fxtr2.data.entity_ref_scope import FxtrScope

from .external_services import ExternalServiceConfig, attach_external_service
from .persistence import PersistenceNamespaceConfig, persistence_cap_worker
from .state import (
    SystemSource,
    WorkstationState,
    _close_session_drain_trackers,
    release_registration,
)
from .ws_app import build_app

_log = logging.getLogger(__name__)


def _reserve_free_port(host: str) -> int:
    """Bind a throwaway socket to ``host:0``, capture the port, release.

    Small race between releasing the socket here and hypercorn
    rebinding to the same port; reliable for local test usage. We can
    move to a no-race approach (e.g. passing a pre-bound socket via
    ``fd://``) if we ever hit flakiness.
    """
    s = socket.socket()
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


async def _wait_until_listening(host: str, port: int, *, timeout: float = 5.0) -> None:
    """Poll ``host:port`` until a TCP connect succeeds (or timeout)."""
    deadline = anyio.current_time() + timeout
    while True:
        try:
            stream = await anyio.connect_tcp(host, port)
        except OSError:
            if anyio.current_time() >= deadline:
                raise RuntimeError(
                    f"workstation server did not bind {host}:{port} within {timeout}s"
                )
            await anyio.sleep(0.02)
            continue
        await stream.aclose()
        return


class WorkstationServer(AsyncContextManagerMixin):
    """Async context manager that runs the workstation HTTP/WS server.

    Args:
        host: Interface to bind. Default ``"127.0.0.1"`` (loopback only).
        port: TCP port. ``0`` (default) reserves an ephemeral port at
            ``__aenter__`` time; the resolved port is exposed via
            :attr:`bound_url`.
        parent_scope: Optional parent :class:`FxtrScope` the
            workstation's ``root_scope`` should ``include``. When
            ``None`` (default), the active scope at construction time
            is captured. The workstation's ``root_scope`` is always
            opened ``isolated=True``; this argument controls *which*
            outer scope its ``include`` points at.
        name: Used in scope / lifetime / logging names.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        parent_scope: FxtrScope | None = None,
        name: str = "workstation",
        debug_ui_dist_path: pathlib.Path | None = None,
        persistence_namespaces: list[PersistenceNamespaceConfig] | None = None,
        external_services: list[ExternalServiceConfig] | None = None,
    ) -> None:
        """Args:
        debug_ui_dist_path: Filesystem path to a built debug UI
            ``dist/`` directory (must contain ``index.html``).
            When provided, mounted at ``/debug`` via Starlette
            ``StaticFiles``. Default ``None`` — no ``/debug``
            route. ``WorkstationServer.bundled_debug_ui_dist_path()``
            returns the path to the in-package ``debug_ui/dist/``
            if you want to use it explicitly.
        persistence_namespaces: Optional list of stage-7
            :class:`PersistenceNamespaceConfig`s. The user is
            responsible for opening each ``FxtrNamespace`` (e.g.
            ``async with SQLiteFxtrNamespace(...) as ns``) before
            constructing the server and keeping it open for the
            server's lifetime. Each namespace's scope is added to
            ``root_scope.include`` so retrieved entities can be
            lazily loaded; one persistence-cap service is mounted
            per config (visible to clients via the standard
            ``get_service_by_external_name`` path). External names
            must be unique across this list and the
            ``external_services`` list combined.
        external_services: Optional list of stage-7-post-1
            :class:`ExternalServiceConfig`s. Each entry pairs a
            user-supplied :class:`ServiceRef` with the
            :class:`FxtrScope` that owns it; the workstation
            bridges the ref into ``root_scope`` at startup,
            mounting it under ``external_name``. The user retains
            scope ownership — the bridge pins their scope to
            ``root_scope``'s lifetime, so the user's outer scope
            CM can't drain until workstation shutdown completes.
            **Each user scope must be opened outside (i.e.
            enclosing) the ``async with WorkstationServer(...)``
            block.** Nesting the other way deadlocks on context
            exit: the user scope can't drain (it's still pinned
            by the workstation's bridge) but the workstation
            won't release the bridge until *its* context exits.
            External names must be unique across this list and the
            ``persistence_namespaces`` list combined.
        """
        self.host = host
        self._requested_port = port
        self.name = name
        self._debug_ui_dist_path = debug_ui_dist_path
        # User-boundary capture: see scope_discipline.md ("user boundary"
        # case). After this point the parent_scope flows explicitly via
        # the workstation's root_scope's ``include``.
        if parent_scope is None:
            parent_scope = FxtrScope.get_active()
        self._parent_scope = parent_scope
        self._persistence_configs = list(persistence_namespaces or ())
        self._external_service_configs = list(external_services or ())
        self._validate_system_service_configs()
        self._bound_port: int | None = None
        self._state: WorkstationState | None = None

    def _validate_system_service_configs(self) -> None:
        """Reject duplicate external names (across persistence +
        external-services lists) and unknown persistence modes early.

        Bring-up errors are easier to diagnose at construction time
        than from inside the per-config bring-up's propagation, so
        we validate the static config here. Persistence mode is
        also checked dynamically inside
        :func:`persistence._build_persistence_impl` for safety.

        The persistence and external-services lists share the
        workstation's external-name namespace at runtime
        (``state.service_external_names``), so the uniqueness check
        spans both.
        """
        seen: set[str] = set()
        for cfg in self._persistence_configs:
            if cfg.external_name in seen:
                raise ValueError(
                    f"persistence_namespaces: duplicate external name " f"{cfg.external_name!r}"
                )
            seen.add(cfg.external_name)
            if cfg.mode not in ("no_provenance", "preserve_provenance"):
                raise ValueError(
                    f"persistence_namespaces: unknown mode {cfg.mode!r} "
                    f"for {cfg.external_name!r}"
                )
        for cfg in self._external_service_configs:
            if cfg.external_name in seen:
                raise ValueError(
                    f"external_services: duplicate external name "
                    f"{cfg.external_name!r} (already used by another "
                    "external_services or persistence_namespaces config)"
                )
            seen.add(cfg.external_name)

    @staticmethod
    def bundled_debug_ui_dist_path() -> pathlib.Path:
        """Path to the bundled ``debug_ui/dist/`` directory inside this
        package. Doesn't check that the directory exists — pass it to
        the ``debug_ui_dist_path=`` constructor arg if you've built
        the UI (``cd fxtr_workstation/debug_ui && pnpm install &&
        pnpm build``)."""
        return pathlib.Path(__file__).parent / "debug_ui" / "dist"

    @property
    def bound_port(self) -> int:
        if self._bound_port is None:
            raise RuntimeError("WorkstationServer is not running")
        return self._bound_port

    @property
    def bound_url(self) -> str:
        return f"ws://{self.host}:{self.bound_port}"

    @property
    def rpc_url(self) -> str:
        return f"{self.bound_url}/rpc"

    @property
    def client_url(self) -> str:
        return f"{self.bound_url}/client"

    @property
    def debug_http_url(self) -> str:
        """HTTP URL for the ``/debug`` route. Only useful if the
        server was constructed with ``debug_ui_dist_path``."""
        return f"http://{self.host}:{self.bound_port}/debug"

    @property
    def state(self) -> WorkstationState:
        if self._state is None:
            raise RuntimeError("WorkstationServer is not running")
        return self._state

    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        port = self._requested_port or _reserve_free_port(self.host)
        shutdown_event = anyio.Event()

        config = hypercorn.config.Config()
        config.bind = [f"{self.host}:{port}"]
        config.accesslog = None
        config.errorlog = "-"

        # Workstation's primary scope. Isolated from the active scope at
        # __aenter__ time (we already captured the parent at __init__);
        # only inherits from the explicit parent. After stage 6a.2 each
        # tracked entity owns its own per-record :class:`FxtrScope`
        # (child of ``root_scope``) — there is no global entities
        # umbrella scope.
        #
        # Stage 7b: every configured persistence namespace's
        # ``ns.scope`` joins ``root_scope.include`` so retrieved
        # entities can be lazily loaded through the standard
        # inheritance chain (call_scope → service_scope → root_scope
        # → ns.scope).
        include_scopes = [self._parent_scope] + [
            cfg.namespace.scope for cfg in self._persistence_configs
        ]
        async with FxtrScope(
            isolated=True,
            include=include_scopes,
            name=f"{self.name}/root",
        ) as root_scope:
            # Background TG owns long-lived workstation worker tasks
            # (per-service-record scope owners). Outlives the serve TG
            # so service-record drains can complete after hypercorn has
            # finished (releases trigger shutdown_event for each record).
            async with anyio.create_task_group() as background_tg:
                state = WorkstationState(
                    root_scope=root_scope,
                    background_tg=background_tg,
                )
                self._state = state

                # Bring up persistence caps (stage 7b). Each
                # ``await background_tg.start(...)`` blocks until
                # the cap's bridge is ready and its system-sourced
                # registration is installed under
                # ``cfg.external_name``. Bring-up failures (bridge
                # error, name conflict) propagate out as a startup
                # error before we hand control to ``yield self``.
                for cfg in self._persistence_configs:
                    await background_tg.start(persistence_cap_worker, state, cfg)

                # Bring up user-supplied external services (stage 7
                # post-1). Inline async work — no per-config worker,
                # since there's no per-config scope to own. Each call
                # bridges the user's ref into ``root_scope``, mints a
                # ``TrackedServiceRecord`` (which spawns the standard
                # ``service_record_worker`` on ``background_tg``),
                # and installs a SystemSource registration. Bring-up
                # failures propagate out as a startup error.
                for cfg in self._external_service_configs:
                    await attach_external_service(state, cfg)

                app = build_app(state, debug_ui_dist_path=self._debug_ui_dist_path)

                async def _serve() -> None:
                    await hypercorn.asyncio.serve(
                        app,  # type: ignore[arg-type]
                        config,
                        shutdown_trigger=shutdown_event.wait,
                    )

                self._bound_port = port
                try:
                    async with anyio.create_task_group() as serve_tg:
                        serve_tg.start_soon(_serve)
                        try:
                            await _wait_until_listening(self.host, port)
                            yield self
                        finally:
                            # Aggressively close every active /client
                            # connection so hypercorn doesn't sit
                            # waiting for browser pages to disconnect.
                            # /rpc connections still drain naturally —
                            # they carry data we don't want to drop on
                            # the floor.
                            for record in list(state.client_connections.values()):
                                record.cancel()
                            shutdown_event.set()
                    # Once ``serve_tg`` has exited, all incoming traffic
                    # has stopped. Close every session's mutation queue
                    # so the per-session ``session_worker`` tasks see
                    # ``EndOfStream`` and exit — otherwise
                    # ``background_tg.__aexit__`` would hang waiting on
                    # them.
                    for send_stream in list(state.session_mutation_queues.values()):
                        send_stream.close()
                    state.session_mutation_queues.clear()
                    # Auto-close every still-open session: drain its
                    # tracker contributions through the normal
                    # release path (refcount → 0 →
                    # ``_maybe_release_entity_record`` →
                    # ``record.scope.force_close()``). Sessions don't
                    # auto-close on server shutdown, so any client
                    # that disconnected without sending ``CloseOp``
                    # (browser tab close, network drop, SIGTERM mid-
                    # session) would otherwise leave its pinned
                    # records alive and block ``root_scope``'s
                    # drain — each per-record scope is a
                    # ``include=[root_scope]`` child.
                    with state.mutation_context():
                        for session in list(state.sessions.values()):
                            _close_session_drain_trackers(state, session)
                    state.sessions.clear()
                    # Defensive backstop. After the auto-close above,
                    # ``entity_records`` should be empty in any
                    # well-behaved program. Anything still here is
                    # an orphan: a record minted outside the normal
                    # registration / session-pin paths (typically a
                    # test helper that bypasses lifecycle) or a bug
                    # that left a record stranded. Force-close so
                    # the root drain can complete; log loudly so the
                    # anomaly is visible.
                    if state.entity_records:
                        _log.warning(
                            "Workstation shutdown: %d entity record(s) survived "
                            "session-close drain — force-closing their per-record "
                            "scopes. This usually means a test helper minted "
                            "records without registering them, or a bug stranded "
                            "a record. Records: %s",
                            len(state.entity_records),
                            sorted(r.short_id for r in state.entity_records.values()),
                        )
                        for record in list(state.entity_records.values()):
                            record.scope.force_close()
                        state.entity_records.clear()
                    # Stage 7b: release every system-sourced
                    # registration (currently just persistence caps).
                    # Each release fires the underlying tracked
                    # record's ``shutdown_event``, which lets the
                    # corresponding ``persistence_cap_worker`` wake
                    # from its park on ``shutdown_event.wait()`` and
                    # drain ``impl_scope``. Without this step,
                    # ``background_tg.__aexit__`` would hang
                    # forever waiting on those parked workers.
                    with state.mutation_context():
                        for tracked in list(state.service_records.values()):
                            for reg in list(tracked.registrations):
                                if isinstance(reg.source, SystemSource):
                                    release_registration(state, reg)
                finally:
                    self._bound_port = None
                    self._state = None
