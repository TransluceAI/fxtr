"""Select and reserve the viewer's listening port.

An explicit port is strict. Automatic selection first tries the remembered address when its
host matches, then ports 8000 through 8050. In development mode a candidate must also have its
paired Vite port available. Only address-in-use errors advance to the next candidate.

The backend socket stays bound through startup and is handed to Uvicorn. The Vite probe socket
is released before startup because Vite binds its own socket; Vite's strict-port launch handles
a subsequent conflict without moving independently to another port.
"""

from __future__ import annotations

import contextlib
import errno
import socket
from collections.abc import Iterator
from urllib.parse import urlsplit

from fxtr.project.viewer import DEFAULT_VIEWER_PORT, viewer_info
from fxtr.runner.postgres.debug_web.dev_server import DEV_SERVER_PORT_OFFSET, reachable_host

PORT_RETRIES = 50
"""Additional ports after the default to try during automatic selection."""


class ViewerPortError(Exception):
    """The requested address cannot be bound, or automatic selection exhausted its candidates."""


def viewer_url(host: str, port: int) -> str:
    """The viewer's URL on a server bound to ``host:port`` (an IPv6 host in brackets)."""
    return f"http://{f'[{host}]' if ':' in host else host}:{port}/"


@contextlib.contextmanager
def bind_viewer(
    host: str, port: int | None, *, dev: bool, remembered: str | None = None
) -> Iterator[socket.socket]:
    """Reserve a backend socket, checking its Vite port too when needed. Close it on exit."""
    maximum = 65535 - DEV_SERVER_PORT_OFFSET if dev else 65535
    if port is not None and not 1 <= port <= maximum:
        if dev and maximum < port <= 65535:
            raise ViewerPortError(
                f"the viewer's dev server runs at its port plus {DEV_SERVER_PORT_OFFSET}, past the"
                f" last port for {port}: pass a --port of at most {maximum}"
            )
        raise ViewerPortError(f"--port must be between 1 and {maximum}")
    if port is None:
        candidates = list(range(DEFAULT_VIEWER_PORT, DEFAULT_VIEWER_PORT + PORT_RETRIES + 1))
        try:
            previous = urlsplit(remembered or "")
            preferred = previous.port
            if (
                previous.scheme == "http"
                and previous.hostname == reachable_host(host).lower()
                and preferred is not None
                and 1 <= preferred <= maximum
            ):
                candidates.insert(0, preferred)
        except ValueError:
            pass  # A malformed remembered URL is not a port preference.
    else:
        candidates = [port]
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for candidate in dict.fromkeys(candidates):
        needed = [candidate, candidate + DEV_SERVER_PORT_OFFSET] if dev else [candidate]
        with contextlib.ExitStack() as sockets:
            opened: list[socket.socket] = []
            for number in needed:
                try:
                    opened.append(
                        sockets.enter_context(socket.create_server((host, number), family=family))
                    )
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise ViewerPortError(f"could not bind {host}:{number}: {exc}") from None
                    if port is None:
                        break
                    info = viewer_info(viewer_url(reachable_host(host), number))
                    holder = (
                        f"the viewer of {info.get('root')} (schema {info.get('schema')})"
                        if info is not None
                        else "another program"
                    )
                    what = f"port {number}"
                    remedy = "stop it, or pass --port to serve elsewhere"
                    if number != candidate:
                        what += ", where the viewer's dev server runs"
                    raise ViewerPortError(f"{what} is in use by {holder}: {remedy}") from None
            else:  # Every required port was available; Vite will bind its own checked port.
                for probe in opened[1:]:
                    probe.close()
                yield opened[0]
                return
    raise ViewerPortError(
        f"no available viewer port: tried {DEFAULT_VIEWER_PORT} through"
        f" {DEFAULT_VIEWER_PORT + PORT_RETRIES}"
        + (" and their paired dev-server ports" if dev else "")
        + "; stop another server or pass --port to select a different port"
    )
