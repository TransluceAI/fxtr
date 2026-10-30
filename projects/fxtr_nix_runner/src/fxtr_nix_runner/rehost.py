"""The build-time rehost: a token-authed HTTP view of a ``RepoEntityStore``.

This is the **manager tier** of ``fxtr_nix_runner`` — unlike the transport
modules (``_backend`` / ``_host`` / ``_podman``, which stay anyio-only and
fxtr2-free), this module depends on ``fxtr2``. Import it explicitly
(``from fxtr_nix_runner.rehost import rehost_context``); it is deliberately
**not** re-exported from the package ``__init__`` so a bare
``import fxtr_nix_runner`` doesn't drag in fxtr2 + the HTTP serving stack.

What it does: serve a repo entity store's by-id entities over HTTP so a nix
build's fixed-output derivations can fetch entity bytes (the
``FXTR_REMOTE_BASE_URL`` / ``FXTR_REMOTE_BEARER_TOKEN`` contract). It reuses
fxtr2's :func:`create_fxtr_app` over a fresh scope with the store registered as
the scope's deep-fetch source, so ``/data/<id>`` resolves any id the store
holds. Only ``/data`` is needed for FOD byte-fetches (preload writers resolve
pin *names* from TSVs themselves), but the other routes are harmless.

Binding is ephemeral (port 0) and the real port is reported back. The manager
chooses ``host``: ``127.0.0.1`` for the host backend, ``0.0.0.0`` for podman
(so ``host.containers.internal`` can route to it — the bearer token is then the
only access control, which is why it's a fresh per-rehost secret).
"""

from __future__ import annotations

import secrets
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

import anyio
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.experimental.http.asgi_app import create_fxtr_app
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import Response

#: The env-var names carrying the rehost contract into a build.
REMOTE_BASE_URL_ENV = "FXTR_REMOTE_BASE_URL"
REMOTE_BEARER_TOKEN_ENV = "FXTR_REMOTE_BEARER_TOKEN"


@dataclass(frozen=True)
class RehostInfo:
    """A running rehost's coordinates.

    Attributes:
        base_url: The host-local URL of the bound server (with the real
            ephemeral port), e.g. ``http://127.0.0.1:54321``. Reachable from the
            host; for the host backend it's also the build-visible URL.
        token: The fresh bearer token required on every request
            (``Authorization: Bearer <token>``).
        port: The bound host port — feed it to ``Handle.expose_host_endpoint``
            to get the URL valid *inside builds* on a given backend (which may
            differ from ``base_url``, e.g. ``host.containers.internal`` under
            podman).
    """

    base_url: str
    token: str
    port: int

    def build_env(self, base_url: str | None = None) -> dict[str, str]:
        """The ``FXTR_REMOTE_*`` env to deliver to a build.

        Pass ``base_url`` = the backend's build-visible URL (from
        ``Handle.expose_host_endpoint(self.port).url_for_builds``) when it
        differs from the host-local one; defaults to :attr:`base_url` (correct
        for the host backend / in-process tests).
        """
        return {
            REMOTE_BASE_URL_ENV: base_url if base_url is not None else self.base_url,
            REMOTE_BEARER_TOKEN_ENV: self.token,
        }


class _BearerAuthMiddleware:
    """Pure-ASGI middleware: reject any request without the exact bearer token.

    Constant-time compare (the token is the only access control when the rehost
    is bound to a non-loopback interface for podman).
    """

    def __init__(self, app: Callable[..., Any], *, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization")
        if auth is None or not secrets.compare_digest(auth.decode("latin-1"), self._expected):
            await Response("Unauthorized", status_code=401)(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _pick_free_port(host: str) -> int:
    """Find a free ephemeral port on ``host`` by binding then releasing it.

    A bound-but-never-listened socket frees its port immediately on close (no
    TIME_WAIT), so the port is available to rebind right away. There's a tiny
    race (another process could grab it in the gap), negligible for a localhost
    rehost stood up per build span — and far simpler than the ``fd://`` handoff,
    which has murky fd-ownership/double-close semantics across hypercorn.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def rehost_context(
    store: RepoEntityStore[Any], *, host: str = "127.0.0.1"
) -> AsyncIterator[RehostInfo]:
    """Run a token-authed HTTP rehost of ``store`` for the block's duration.

    Args:
        store: The (already-open) repo entity store to serve by id. Its session
            lifetime must outlive this rehost — the rehost mounts it as a
            deep-fetch source via an ``IndependentLocalBridge`` over a lease
            clone (case 1: the store outlives the rehost scope).
        host: Interface to bind. ``127.0.0.1`` (host backend) or ``0.0.0.0``
            (podman, so ``host.containers.internal`` routes in).

    Yields:
        A :class:`RehostInfo` with the bound port, base URL, and bearer token.
    """
    import hypercorn.asyncio
    import hypercorn.config

    token = secrets.token_hex(32)
    port = _pick_free_port(host)

    async with FxtrScope(name="fxtr-nix-rehost", isolated=True) as scope:
        # The store resolves by-id reads the rehost scope has never seen.
        scope.register_deep_fetch_source(store, IndependentLocalBridge(lease=store.get_lease()))

        started = anyio.Event()
        done = anyio.Event()

        @asynccontextmanager
        async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
            started.set()
            yield

        base_app = create_fxtr_app(scope, {})
        app = Starlette(
            routes=base_app.routes,
            middleware=[Middleware(_BearerAuthMiddleware, token=token)],
            lifespan=_lifespan,
        )

        config = hypercorn.config.Config()
        config.bind = [f"{host}:{port}"]
        config.loglevel = "WARNING"

        async def _serve() -> None:
            await hypercorn.asyncio.serve(app, config, shutdown_trigger=done.wait)  # type: ignore[arg-type]

        async with anyio.create_task_group() as tg:
            tg.start_soon(_serve)
            try:
                await started.wait()
                yield RehostInfo(base_url=f"http://{host}:{port}", token=token, port=port)
            finally:
                done.set()
