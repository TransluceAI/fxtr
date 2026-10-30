"""Starlette application wiring for the workstation server.

Stage 1 currently mounts a single ``/rpc`` route which:

1. Reads the :class:`WorkstationState` off ``app.state``.
2. Accepts the WebSocket and registers an :class:`RPCConnectionRecord`
   on the state via :meth:`WorkstationState.add_rpc_connection`. The
   CM also opens a per-connection isolated mountable
   :class:`FxtrScope` (``record.scope``) under
   ``state.root_scope``.
3. Wraps the WS as an ``ObjectStream[JSONTypes]`` (in-band EOF via
   :class:`JSONObjectStream`) and lifts it to
   ``ObjectStream[RPCMessage]`` via :func:`wrap_json_stream`.
4. Mounts a fresh :class:`WorkstationServiceImpl` for this record on
   ``record.scope`` and opens an :func:`open_rpc_scope` against that
   ref. The ``open_rpc_scope`` is itself ``isolated=True`` with
   ``include=[record.scope]`` so the precondition lookup finds our
   mount via the explicit include rather than the active scope.
5. The body is a no-op — the connection stays alive as long as the
   peer keeps our main bridge alive.

**Scope discipline.** No code in this route reads or relies on the
active scope. Every mount and ``open_rpc_scope`` parent is threaded
explicitly through ``record.scope`` (which itself was constructed
explicitly off ``state.root_scope``). See
``v2/notes/scope_discipline.md``.
"""

from __future__ import annotations

import logging
import pathlib

from fxtr2.rpc.json_serdes import wrap_json_stream
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr2.services.service_cap_common import UnknownService
from starlette.applications import Starlette
from starlette.routing import Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from ._ws_transport import asgi_websocket_object_stream
from .client_app import client_endpoint
from .service_impl import WorkstationServiceImpl
from .state import WorkstationState

_log = logging.getLogger(__name__)


def _peer_addr(websocket: WebSocket) -> str | None:
    client = websocket.client
    if client is None:
        return None
    return f"{client.host}:{client.port}"


async def rpc_endpoint(websocket: WebSocket) -> None:
    state: WorkstationState = websocket.app.state.workstation_state
    await websocket.accept()
    async with state.add_rpc_connection(_peer_addr(websocket)) as record:
        # ``record.scope`` is loadable for anything mounted here on this
        # connection's behalf — guaranteed by ``add_rpc_connection``'s
        # CM (see RPCConnectionRecord.scope docstring).
        impl = WorkstationServiceImpl(state, record)
        workstation_ref = await record.scope.mount(impl)
        async with asgi_websocket_object_stream(websocket) as raw_stream:
            transport = wrap_json_stream(raw_stream)
            async with open_rpc_scope(
                transport,
                workstation_ref,
                isolated=True,
                include=[record.scope],
                remote_service_type=UnknownService,
                name=f"workstation-rpc/{record.connection_id}",
            ):
                # Body is a no-op: peer holds our main bridge alive, so
                # ``open_rpc_scope`` parks here until the peer
                # disconnects or shutdown fires.
                pass


def build_app(
    state: WorkstationState,
    *,
    debug_ui_dist_path: pathlib.Path | None = None,
) -> Starlette:
    """Build the workstation's Starlette ASGI app, attaching ``state``.

    Routes:

    - ``/rpc`` — fxtr-RPC connections (see :func:`rpc_endpoint`).
    - ``/client`` — workstation observer protocol (see
      :func:`client_endpoint` in ``client_app.py``).
    - ``/debug`` — only mounted if ``debug_ui_dist_path`` is provided
      and the directory exists. Serves the prebuilt debug UI via
      Starlette ``StaticFiles`` (with ``html=True`` so ``/debug/``
      serves ``index.html``). If the path is provided but doesn't
      exist, we log a warning and skip the mount — useful when the
      caller passes :meth:`WorkstationServer.bundled_debug_ui_dist_path`
      without having built the UI.
    """
    routes: list = [
        WebSocketRoute("/rpc", rpc_endpoint),
        WebSocketRoute("/client", client_endpoint),
    ]
    if debug_ui_dist_path is not None:
        if debug_ui_dist_path.is_dir():
            routes.append(
                Mount(
                    "/debug",
                    app=StaticFiles(directory=str(debug_ui_dist_path), html=True),
                )
            )
        else:
            _log.warning(
                "debug_ui_dist_path %s does not exist; /debug not mounted. "
                "Run `pnpm install && pnpm build` in fxtr_workstation/debug_ui/.",
                debug_ui_dist_path,
            )
    app = Starlette(routes=routes)
    app.state.workstation_state = state
    return app
