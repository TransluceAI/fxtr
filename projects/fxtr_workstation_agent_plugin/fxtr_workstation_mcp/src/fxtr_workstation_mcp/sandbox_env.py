"""Long-lived eryx sandbox environment, lazy-started.

Same supervisor-task pattern as :class:`WorkstationConnection`: a
single task in the lifespan's task group enters and exits the
``DirectSandboxEnvironment`` async context manager. Tools never
touch the env's CMs themselves.

Lifecycle:

1. First ``run_script`` (or any other env consumer) calls
   :meth:`SandboxEnvSupervisor.start(tg)` — idempotent.
2. The supervisor body bootstraps the eryx ``SandboxFactory`` from
   our ``sandbox_runtime`` dep-closure stub, then opens a
   ``DirectSandboxEnvironment`` over it.
3. ``await get_env()`` returns the env once ready.
4. On lifespan exit, ``shutdown()`` is called; the supervisor exits
   its CMs and the env drains.

Bootstrap dominates startup (~3 s, paid once). Per-spawn is fast.
We deliberately don't bootstrap eagerly — stage-1 tests + the
``connect`` / ``open_session`` flow don't need a sandbox; only
``run_script`` does. That keeps the stage-1 test suite snappy and
defers the bootstrap cost until the agent actually needs it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import anyio
import anyio.abc
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr_sandboxing.direct_launcher import (
    DirectSandboxEnvironment,
    create_direct_sandbox_environment,
)

from .sandbox_factory import bootstrap_factory

_log = logging.getLogger(__name__)


class SandboxEnvSupervisor:
    """Owns the eryx ``SandboxFactory`` + ``DirectSandboxEnvironment``
    lifetime. One supervisor task per :class:`AppContext`.

    Tools use the env via :meth:`get_env`, which blocks until the
    bootstrap has completed.
    """

    def __init__(self, cache_dir: Path) -> None:
        """Args:
        cache_dir: Wheel download cache for the bundle materializer.
            Safe to share across MCP server runs.
        """
        self._cache_dir = cache_dir
        self._started = False
        self._ready = anyio.Event()
        self._shutdown = anyio.Event()
        self._exited = anyio.Event()
        self._error: BaseException | None = None
        self._env: DirectSandboxEnvironment | None = None

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set() and self._error is None

    def start(self, tg: anyio.abc.TaskGroup) -> None:
        """Schedule the supervisor on ``tg``. Idempotent."""
        if self._started:
            return
        self._started = True
        tg.start_soon(self._supervisor_loop)

    async def get_env(self) -> DirectSandboxEnvironment:
        """Block until the env is bootstrapped, then return it.
        Raises any startup error."""
        await self._ready.wait()
        if self._error is not None:
            raise self._error
        assert self._env is not None
        return self._env

    async def shutdown(self) -> None:
        """Signal the supervisor to exit. Waits for it to drain
        (with a generous timeout)."""
        self._shutdown.set()
        with anyio.move_on_after(5.0):
            await self._exited.wait()

    async def _supervisor_loop(self) -> None:
        try:
            # Transient bootstrap scope: prepare() stores the bundle
            # entity here, but build_site_packages_for_bundle
            # materializes it to disk so we don't need to keep the
            # entity reachable past this block. We isolate from
            # whatever active scope happens to be in play but
            # explicitly include GLOBAL_SCOPE so type-descriptor
            # entities (which the prepare() machinery references) can
            # lift correctly during ``store_cbor``.
            async with FxtrScope(
                isolated=True,
                include=[GLOBAL_SCOPE],
                name="sandbox_bootstrap",
            ) as bs_scope:
                with bs_scope.as_active_scope():
                    factory = await bootstrap_factory(cache_dir=self._cache_dir)
            async with create_direct_sandbox_environment(factory=factory) as env:
                self._env = env
                self._ready.set()
                await self._shutdown.wait()
        except BaseException as e:  # noqa: BLE001 — recorded + re-raised
            if self._error is None:
                self._error = e
            if not self._ready.is_set():
                self._ready.set()
            raise
        finally:
            self._env = None
            self._exited.set()
