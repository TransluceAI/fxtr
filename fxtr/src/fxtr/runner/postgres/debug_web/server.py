"""Serve the experiment viewer for ``fxtr view``.

Serves the project ``--project`` (a path in it; the working directory by default) belongs to,
through its local configuration (``FXTR_DATABASE_URL`` overrides the database URL). ``--host`` /
``--port`` set the bind address. Without ``--port``, it prefers the last successful port and then
tries 8000 through 8050 (see :mod:`.ports`). Once startup succeeds it prints
the viewer's URL and opens it in a browser (``--no-open``: only prints it), and records the URL
(:func:`~fxtr.project.viewer.record_viewer`), so a launch of the project opens its
job there. When the project's viewer already runs (:func:`~fxtr.project.viewer.find_viewer`), it
opens that one instead and exits, so running ``fxtr view`` again is harmless. From a checkout of
fxtr, that URL redirects to the viewer's Vite dev server, which it runs alongside, at its port plus
:data:`~.dev_server.DEV_SERVER_PORT_OFFSET`. When
something else holds either port, automatic selection tries another pair; an explicit port
refuses to start (:class:`.ports.ViewerPortError`). Before serving, it rebuilds the project's
view bundle when stale, and while serving whenever its sources change
(see :mod:`.views_watch`). It also rebuilds a checkout's built viewer when that is what it
serves and it is stale (see :func:`~.viewer_build.refresh_viewer`).
Renderer build failures keep the last valid bundle while watching for source fixes.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

import anyio
import anyio.to_thread
import uvicorn

from fxtr.project.viewer import DEFAULT_VIEWER_PORT, find_viewer, record_viewer, recorded_viewer
from fxtr.runner.postgres.client import LocalProjectClient
from fxtr.runner.postgres.debug_web.app import checkout_frontend, create_app
from fxtr.runner.postgres.debug_web.dev_server import (
    DevServerConfig,
    dev_server_port,
    reachable_host,
)
from fxtr.runner.postgres.debug_web.ports import bind_viewer, viewer_url

GRACEFUL_SHUTDOWN_SECONDS = 2
"""How long a stopping server waits for requests still open before cancelling them."""


async def serve(
    project: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    """Serve the project ``project`` is in (the working directory's when ``None``) until the
    server is stopped, announcing the viewer's URL once it is listening (and opening it in a
    browser when ``open_browser``; a browser that fails to open leaves the server running).
    From a checkout, the viewer is its dev server (see
    :func:`~fxtr.runner.postgres.debug_web.app.create_app`). The project's view bundle is rebuilt
    before serving if stale, and while serving as its sources change. Build failures keep the
    last valid bundle, or the default renderers until a build succeeds. A checkout's built
    viewer is rebuilt if stale when it is served instead of the dev server. When the project's
    viewer runs already, it opens that one
    (when ``open_browser``) instead, and returns.

    ``port=None`` selects automatically through :func:`.ports.bind_viewer`; an explicit port
    must be available. The selected backend socket is reserved throughout application startup.

    Raises:
        ViewerPortError: If an explicit port is unavailable, automatic selection exhausts its
            candidates, or the bind address is invalid.
    """
    client = LocalProjectClient.from_project(project, handle_sigint=False)
    running = await anyio.to_thread.run_sync(find_viewer, client.project)
    if running is not None:
        print(f"the project's viewer is already running at {running}", flush=True)
        if open_browser:
            await _open_browser(running)
        return
    use_dev = checkout_frontend() is not None
    remembered = await anyio.to_thread.run_sync(recorded_viewer, client.project)
    with bind_viewer(host, port, dev=use_dev, remembered=remembered) as listener:
        selected = listener.getsockname()[1]
        if port is None and selected != DEFAULT_VIEWER_PORT:
            print(f"automatically selected viewer port {selected}", flush=True)
        dev_server = None
        if use_dev:
            dev_port = dev_server_port(selected)
            assert dev_port is not None  # bind_viewer validated and checked this pair.
            dev_server = DevServerConfig(
                host=host, backend=viewer_url(reachable_host(host), selected), port=dev_port
            )
        app = create_app(client, dev_server=dev_server)
        server = uvicorn.Server(
            uvicorn.Config(
                app, host=host, port=selected, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS
            )
        )

        async def announce() -> None:
            while not server.started:
                if server.should_exit:
                    return
                await anyio.sleep(0.05)
            url = viewer_url(host, selected)
            print(f"the viewer is at {url}", flush=True)
            await anyio.to_thread.run_sync(
                record_viewer, client.project, viewer_url(reachable_host(host), selected)
            )
            if open_browser:
                await _open_browser(url)

        exit_error: SystemExit | None = None
        async with anyio.create_task_group() as tg:
            tg.start_soon(announce)
            try:
                await server.serve(sockets=[listener])
            except SystemExit as exc:
                exit_error = exc  # Raise outside the task group to avoid an exception group.
            tg.cancel_scope.cancel()
    if exit_error is not None:
        raise exit_error
    if not server.started:
        raise SystemExit(3)  # Uvicorn's startup-failure exit code, also for versions that return.


async def _open_browser(url: str) -> None:
    try:
        await anyio.to_thread.run_sync(webbrowser.open, url)
    except Exception as exc:  # noqa: BLE001 - the server outlives a missing browser
        print(f"could not open a browser: {exc}", flush=True)
