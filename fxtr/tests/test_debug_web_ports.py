"""Automatic viewer ports, socket reservation, and publication of the selected address."""

from __future__ import annotations

import contextlib
import errno
import random
import socket
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import anyio.to_thread
import pytest

from fxtr.project import cli
from fxtr.project.config import Project
from fxtr.project.viewer import find_viewer, project_identity, record_viewer, recorded_viewer

pytest.importorskip("ariadne")

from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from fxtr.runner.postgres.debug_web import ports  # noqa: E402
from fxtr.runner.postgres.debug_web import server as server_module  # noqa: E402
from fxtr.runner.postgres.debug_web.dev_server import DEV_SERVER_PORT_OFFSET  # noqa: E402
from fxtr.runner.postgres.debug_web.ports import ViewerPortError, bind_viewer  # noqa: E402


@pytest.fixture
def base_port(monkeypatch: pytest.MonkeyPatch) -> int:
    """Two available backend/Vite pairs, avoiding fixed ports other tests or viewers may use."""
    for candidate in random.sample(range(20000, 45000), 100):
        try:
            with contextlib.ExitStack() as stack:
                for offset in (0, 1, DEV_SERVER_PORT_OFFSET, DEV_SERVER_PORT_OFFSET + 1):
                    stack.enter_context(socket.create_server(("127.0.0.1", candidate + offset)))
        except OSError:
            continue
        monkeypatch.setattr(ports, "DEFAULT_VIEWER_PORT", candidate)
        return candidate
    raise AssertionError("no adjacent free port pairs")


@pytest.mark.parametrize("occupied_offset", [0, DEV_SERVER_PORT_OFFSET])
def test_automatic_selection_skips_either_occupied_port_and_reserves_the_backend(
    base_port: int, occupied_offset: int
) -> None:
    with socket.create_server(("127.0.0.1", base_port + occupied_offset)):
        with bind_viewer("127.0.0.1", None, dev=True) as listener:
            assert listener.getsockname()[1] == base_port + 1
            with pytest.raises(OSError) as taken:
                socket.create_server(("127.0.0.1", base_port + 1))
            assert taken.value.errno == errno.EADDRINUSE
            # Vite binds its own socket after selection.
            with socket.create_server(("127.0.0.1", base_port + 1 + DEV_SERVER_PORT_OFFSET)):
                pass
        with socket.create_server(("127.0.0.1", base_port + 1)):
            pass


def test_static_viewer_needs_only_the_backend_port(base_port: int) -> None:
    with socket.create_server(("127.0.0.1", base_port + DEV_SERVER_PORT_OFFSET)):
        with bind_viewer("127.0.0.1", None, dev=False) as listener:
            assert listener.getsockname()[1] == base_port


@pytest.mark.parametrize("available_last", [True, False])
def test_automatic_selection_tries_exactly_fifty_additional_ports(
    monkeypatch: pytest.MonkeyPatch, available_last: bool
) -> None:
    tried: list[int] = []
    create = socket.create_server

    def bind(address: tuple[str, int], **kwargs: Any) -> socket.socket:
        tried.append(address[1])
        if available_last and address[1] == 8050:
            return create((address[0], 0), **kwargs)
        raise OSError(errno.EADDRINUSE, "occupied")

    monkeypatch.setattr(ports.socket, "create_server", bind)
    if available_last:
        with bind_viewer("127.0.0.1", None, dev=False):
            pass
    else:
        with pytest.raises(ViewerPortError, match="8000 through 8050"):
            with bind_viewer("127.0.0.1", None, dev=False):
                pytest.fail("started with every candidate occupied")
    assert tried == list(range(8000, 8051))


@pytest.mark.parametrize("failure", [errno.EACCES, errno.EADDRNOTAVAIL])
@pytest.mark.parametrize("dev", [True, False])
def test_other_bind_errors_abort_without_walking_and_release_sockets(
    base_port: int, monkeypatch: pytest.MonkeyPatch, failure: int, dev: bool
) -> None:
    create = socket.create_server
    tried: list[int] = []

    def bind(address: tuple[str, int], **kwargs: Any) -> socket.socket:
        tried.append(address[1])
        if dev and address[1] == base_port:
            return create(address, **kwargs)
        raise OSError(failure, "cannot bind")

    monkeypatch.setattr(ports.socket, "create_server", bind)
    with pytest.raises(ViewerPortError, match="could not bind.*cannot bind"):
        with bind_viewer("127.0.0.1", None, dev=dev):
            pytest.fail("started despite bind failure")
    assert tried == ([base_port, base_port + DEV_SERVER_PORT_OFFSET] if dev else [base_port])
    with create(("127.0.0.1", base_port)):
        pass


def test_an_explicit_default_port_is_strict(base_port: int) -> None:
    with socket.create_server(("127.0.0.1", base_port)):
        with pytest.raises(ViewerPortError, match="is in use"):
            with bind_viewer("127.0.0.1", base_port, dev=False):
                pytest.fail("an explicit port moved")


def test_a_remembered_port_is_preferred_but_an_explicit_port_overrides_it(base_port: int) -> None:
    remembered = f"http://127.0.0.1:{base_port + 1}/"
    with bind_viewer("127.0.0.1", None, dev=True, remembered=remembered) as listener:
        assert listener.getsockname()[1] == base_port + 1
    with bind_viewer("127.0.0.1", base_port, dev=True, remembered=remembered) as listener:
        assert listener.getsockname()[1] == base_port
    with socket.create_server(("127.0.0.1", base_port + 1)):
        with bind_viewer("127.0.0.1", None, dev=True, remembered=remembered) as listener:
            assert listener.getsockname()[1] == base_port


@pytest.mark.parametrize(
    "remembered", ["http://[", "http://127.0.0.1:bad/", "http://elsewhere:8001/"]
)
def test_unusable_remembered_addresses_are_ignored(base_port: int, remembered: str) -> None:
    with bind_viewer("127.0.0.1", None, dev=False, remembered=remembered) as listener:
        assert listener.getsockname()[1] == base_port


@pytest.mark.parametrize("arguments, expected", [([], None), (["--port", "8000"], 8000)])
def test_fxtr_view_distinguishes_automatic_and_explicit_ports(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], expected: int | None
) -> None:
    received: list[int | None] = []

    async def serve(*args: Any, **kwargs: Any) -> None:
        received.append(kwargs["port"])

    monkeypatch.setattr(server_module, "serve", serve)
    assert anyio.run(cli.main, ["view", *arguments]) == 0
    assert received == [expected]


@pytest.mark.anyio
async def test_the_selected_address_reaches_servers_browser_and_record_and_survives_restart(
    tmp_path: Path,
    base_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = Project(
        root=tmp_path,
        name="p",
        modules=(),
        stale_after=timedelta(seconds=30),
        database_url="postgresql+asyncpg://unused/db",
        schema="s",
    )
    opened: list[str] = []
    selected: list[int] = []
    configs: list[Any] = []
    monkeypatch.setattr(
        server_module.LocalProjectClient,
        "from_project",
        lambda *a, **kw: SimpleNamespace(project=project),
    )
    monkeypatch.setattr(server_module, "find_viewer", lambda project: None)
    monkeypatch.setattr(server_module, "checkout_frontend", lambda: tmp_path / "frontend")
    monkeypatch.setattr(server_module.webbrowser, "open", opened.append)

    def app(client: Any, **kwargs: Any) -> Starlette:
        configs.append(kwargs["dev_server"])
        return Starlette()

    async def serve(server: Any, sockets: list[socket.socket]) -> None:
        assert sockets[0].getsockname()[1] == server.config.port
        selected.append(server.config.port)
        server.started = True
        with anyio.fail_after(2):
            while len(opened) < len(selected):
                await anyio.sleep(0.01)

    monkeypatch.setattr(server_module, "create_app", app)
    monkeypatch.setattr(server_module.uvicorn.Server, "serve", serve)
    with socket.create_server(("127.0.0.1", base_port)):
        await server_module.serve()
    expected = f"http://127.0.0.1:{base_port + 1}/"
    assert recorded_viewer(project) == expected
    # The default is now free; a restart still prefers the last successful address.
    await server_module.serve()
    assert selected == [base_port + 1, base_port + 1]
    assert opened == [expected, expected]
    assert all(config.backend == expected for config in configs)
    assert all(config.port == base_port + 1 + DEV_SERVER_PORT_OFFSET for config in configs)
    assert f"the viewer is at {expected}" in capsys.readouterr().out
    with socket.create_server(("127.0.0.1", base_port + 1)):
        pass


@pytest.mark.anyio
async def test_startup_failure_preserves_the_last_successful_record_and_releases_the_port(
    tmp_path: Path,
    base_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project(
        root=tmp_path,
        name="p",
        modules=(),
        stale_after=timedelta(seconds=30),
        database_url="postgresql+asyncpg://unused/db",
        schema="s",
    )
    previous = f"http://127.0.0.1:{base_port}/"
    record_viewer(project, previous)
    monkeypatch.setattr(
        server_module.LocalProjectClient,
        "from_project",
        lambda *a, **kw: SimpleNamespace(project=project),
    )
    monkeypatch.setattr(server_module, "find_viewer", lambda project: None)
    monkeypatch.setattr(server_module, "create_app", lambda *a, **kw: Starlette())
    monkeypatch.setattr(server_module, "checkout_frontend", lambda: None)

    async def fail(server: Any, sockets: list[socket.socket]) -> None:
        server.should_exit = True

    monkeypatch.setattr(server_module.uvicorn.Server, "serve", fail)
    with pytest.raises(SystemExit):
        await server_module.serve(port=base_port + 1, open_browser=False)
    assert recorded_viewer(project) == previous
    with socket.create_server(("127.0.0.1", base_port + 1)):
        pass


@pytest.mark.anyio
async def test_uvicorn_serves_the_reserved_socket_and_discovery_finds_the_selected_port(
    tmp_path: Path,
    base_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project(
        root=tmp_path,
        name="p",
        modules=(),
        stale_after=timedelta(seconds=30),
        database_url="postgresql+asyncpg://unused/db",
        schema="s",
    )
    monkeypatch.setattr(
        server_module.LocalProjectClient,
        "from_project",
        lambda *a, **kw: SimpleNamespace(project=project),
    )
    monkeypatch.setattr(server_module, "find_viewer", lambda project: None)

    async def info(_: Request) -> JSONResponse:
        return JSONResponse(project_identity(project))

    monkeypatch.setattr(
        server_module, "create_app", lambda *a, **kw: Starlette(routes=[Route("/api/info", info)])
    )
    servers: list[Any] = []
    server_type = server_module.uvicorn.Server

    def server(config: Any) -> Any:
        instance = server_type(config)
        servers.append(instance)
        return instance

    monkeypatch.setattr(server_module, "checkout_frontend", lambda: None)
    monkeypatch.setattr(server_module.uvicorn, "Server", server)
    with socket.create_server(("127.0.0.1", base_port)):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(lambda: server_module.serve(open_browser=False))
            try:
                with anyio.fail_after(5):
                    while recorded_viewer(project) is None:
                        await anyio.sleep(0.01)
                    url = await anyio.to_thread.run_sync(find_viewer, project)
                    assert url == f"http://127.0.0.1:{base_port + 1}/"
            finally:
                if servers:
                    servers[0].should_exit = True
    assert recorded_viewer(project) == f"http://127.0.0.1:{base_port + 1}/"
    with socket.create_server(("127.0.0.1", base_port + 1)):
        pass
