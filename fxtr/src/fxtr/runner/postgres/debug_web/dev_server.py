"""The viewer's Vite dev server, run by ``fxtr view`` from a checkout of fxtr.

:func:`run_dev_server` starts ``frontend/node_modules/.bin/vite`` on the host the Python server
binds, with ``FXTR_DEV_BACKEND`` pointing its ``/api`` proxy (``vite.config.ts``) back at that
server, waits until it listens, and stops it on exit.

Its port is the Python server's plus :data:`DEV_SERVER_PORT_OFFSET` (:func:`dev_server_port`), so
that a restarted ``fxtr view`` runs it where it was: the Python server redirects a tab to the dev
server, and the script Vite puts in every page, finding its server gone, waits for it to come back
at the same address and then reloads the page. It listens there or not at all (``--strictPort``):
``serve`` selects an available backend/dev-server pair before starting it.

Otherwise it never fails the server: when Vite is not installed, cannot start, or does not listen
in time, it logs why and yields ``None``, and the server serves the built viewer instead, at the
same URL.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from anyio.abc import Process

logger = logging.getLogger(__name__)

DEV_SERVER_PORT_OFFSET = 10000
"""The dev server's port, less the Python server's: large, so it stays clear of the ports people
pick for another viewer (8001, 8080)."""

_WILDCARD_LOOPBACK = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}


def reachable_host(host: str) -> str:
    """A host that reaches a server bound to ``host``: loopback for a wildcard address."""
    return _WILDCARD_LOOPBACK.get(host, host)


@dataclasses.dataclass(frozen=True)
class DevServerConfig:
    """Where the dev server listens and what it forwards ``/api`` to."""

    host: str
    """The host to bind (the Python server's)."""
    backend: str
    """The Python server's URL, which the dev server proxies ``/api`` to."""
    port: int
    """The port to listen on (:func:`dev_server_port`)."""
    startup_timeout: float = 30.0
    """How long to wait for the dev server to listen before giving up on it."""


def dev_server_port(server_port: int) -> int | None:
    """The port the dev server of a Python server on ``server_port`` listens on, ``None`` when
    that is past the last port."""
    port = server_port + DEV_SERVER_PORT_OFFSET
    return port if port <= 65535 else None


def free_port(host: str, port: int = 0) -> int | None:
    """``port`` on ``host`` when nothing listens on it right now (``None`` when something does);
    for ``0``, any such port."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":
            # As Node binds: a port whose last connections are closing (a dev server just
            # stopped, for a restart) is free, one something listens on is not.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return None
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def run_dev_server(frontend: Path, config: DevServerConfig) -> AsyncIterator[int | None]:
    """Run the Vite dev server of ``frontend`` for the duration, yielding the port it listens on
    (``None``, with a logged warning, when it could not be started)."""
    vite = frontend / "node_modules" / ".bin" / "vite"
    if not vite.is_file():
        logger.warning(
            "the viewer's dev server is not installed (%s is missing; run `pnpm install` in the"
            " fxtr checkout): serving the built viewer instead",
            vite,
        )
        yield None
        return
    port = config.port
    # Vite refuses a taken port (--strictPort), but what holds it would answer the check that
    # Vite listens, and the viewer would redirect there.
    if free_port(config.host, port) is None:
        logger.warning(
            "port %d, where the viewer's dev server runs, is in use: serving the built viewer"
            " instead",
            port,
        )
        yield None
        return
    command = [str(vite), "--host", config.host, "--port", str(port), "--strictPort"]
    command += ["--clearScreen", "false", "--logLevel", "warn"]
    try:
        process = await anyio.open_process(
            command,
            cwd=frontend,
            env={**os.environ, "FXTR_DEV_BACKEND": config.backend},
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
        )
    except OSError as exc:
        logger.warning(
            "could not start the viewer's dev server (%s): serving the built viewer instead", exc
        )
        yield None
        return
    try:
        listening = await _wait_listening(process, reachable_host(config.host), port, config)
        if not listening:
            logger.warning(
                "the viewer's dev server did not start (see its output above): serving the"
                " built viewer instead"
            )
            await _stop(process)
        yield port if listening else None
    finally:
        await _stop(process)


async def _wait_listening(process: Process, host: str, port: int, config: DevServerConfig) -> bool:
    with anyio.move_on_after(config.startup_timeout):
        while process.returncode is None:
            try:
                stream = await anyio.connect_tcp(host, port)
            except OSError:
                await anyio.sleep(0.1)
                continue
            await stream.aclose()
            return True
    return False


async def _stop(process: Process) -> None:
    if process.returncode is not None:
        return
    with anyio.CancelScope(shield=True):
        process.terminate()
        with anyio.move_on_after(5):
            await process.wait()
            return
        process.kill()
        await process.wait()
