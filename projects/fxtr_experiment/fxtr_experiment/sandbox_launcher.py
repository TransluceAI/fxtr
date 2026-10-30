"""The sandbox launcher: the real impl behind the launcher seam.

`bindings.BindingHost` serves sandboxed bindings through the protocol
pair defined beside it (`bindings.SandboxLauncherProto` — defn triple →
a launched, connectable process — and `bindings.LaunchedSandboxProto`,
connect-per-use + shutdown), so the whole serving funnel is testable
against a fake with no sandbox machinery. This module is the **sole
importer of `fxtr_sandboxing`** in the package, and the bootstrap
(`user_service.open_local`) imports it lazily, only when a
``sandbox_repo`` is configured — the core never pays the import.

The real impl (:class:`FxtrSandboxLauncher`) owns the runner's one
sandbox environment and every process's lifetime, via the holder-task
pattern (`fxtr_sandboxing`'s ``_serve_spawn_rpc`` precedent): each async
CM whose lifetime is dynamic — the environment, each launched process's
holder scope — lives inside a dedicated task on the launcher's task
group, parked on a teardown event. That yields the decided lifecycle:

- **Lazy environment**: `create_sandbox_environment` opens on the first
  launch (an env with no sandbox spawn touches nothing), configured with
  the runner's ``sandbox_repo`` and ``cache_dir``.
- **Kind-agnostic spawn**: the stored defn triple spawns as
  `fxtr_sandbox_types.NixServiceDefn` — the spawner evals the attr's
  ``kind`` and dispatches (the auto arm; no nix knowledge here).
- **Release-and-forget shutdown**: :meth:`LaunchedSandboxProto.shutdown`
  just sets the holder's teardown event and returns; the holder scope
  then *drains* in the background — already-connected guests stay
  connected (their run scopes hold independent leases on the holder
  scope, taken at connect), and the process exits when the last one
  releases. Nothing force-kills in the normal path.
- **Teardown ordering** at runner close (:meth:`FxtrSandboxLauncher.
  aclose`, on `user_service.open_local`'s exit stack): guests first
  (shutdown + wait each holder), then the environment, then the caller's
  task group and runner scope unwind.

Scope discipline (`v2/notes/scope_discipline.md`) notes: every ref is
stored with the scope it resolves in (the spawn ref with its holder
scope; the spawner ref with the env host scope), and membership
rationales are at each resolve/bridge site.

Failure classification: anything that fails while launching — nix
eval/build (missing commit, broken flake, unknown attr kind), delivery,
spawn — surfaces as `errors.SandboxLaunchError` (fault-class, full
underlying message) from :meth:`FxtrSandboxLauncher.launch`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import anyio
import anyio.abc

from fxtr2.common.task_utils import start_with_scope
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.service_cap_common import UnknownService
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_sandbox_types import (
    BindingConnectRequest,
    NixServiceDefn,
    SandboxConnectProto,
    SandboxSpawnerProto,
)

from fxtr_experiment.bindings import LaunchedSandboxProto, SandboxLauncherProto
from fxtr_experiment_task.errors import SandboxLaunchError
from fxtr_experiment_task.model import SandboxDefn

__all__ = ["FxtrSandboxLauncher", "LaunchedSandboxProto", "SandboxLauncherProto"]

_logger = logging.getLogger(__name__)


class _LaunchedSandbox:
    """The real launched process: a holder task's scope + the spawn ref.

    Attributes:
        _sandbox_ref: The ``SandboxConnectProto`` ref returned by
            ``spawn``, resolvable in ``_holder_scope`` (the holder task
            resolved the spawner there and the framework chain-bridged
            the return into the resolving scope).
        _holder_scope: The per-process holder scope (open until
            ``_teardown`` is set and every outside lease releases).
    """

    def __init__(
        self,
        *,
        sandbox_ref: ServiceRef[UnknownService],
        holder_scope: FxtrScope,
        teardown: anyio.Event,
        done: anyio.Event,
    ) -> None:
        self._sandbox_ref = sandbox_ref
        self._holder_scope = holder_scope
        self._teardown = teardown
        self._done = done

    async def connect(
        self, request: BindingConnectRequest, *, into_scope: FxtrScope
    ) -> ServiceRef[UnknownService]:
        """Contract on `LaunchedSandboxProto.connect`."""
        # The lease that makes release-and-forget sound: `into_scope`
        # (the run/step scope) keeps the holder scope alive until it
        # closes, so shutdown's holder drain waits for in-flight users.
        # Membership: `_sandbox_ref` is tracked in `_holder_scope` (the
        # attribute contract above), so the bridge source is valid; after
        # it, the ref resolves in `into_scope`.
        if not into_scope.is_tracked(self._sandbox_ref):
            await into_scope.bridge_service_from_scope(
                self._sandbox_ref, self._holder_scope, keepalive_via="independent_lease"
            )
        typed = self._sandbox_ref.replace_service_type(
            SandboxConnectProto[BindingConnectRequest, UnknownService]
        )
        proxy = await typed.resolve(into_scope)
        return await proxy.connect(request)

    def shutdown(self) -> None:
        """Contract on `LaunchedSandboxProto.shutdown`."""
        self._teardown.set()

    async def wait_closed(self) -> None:
        """Contract on `LaunchedSandboxProto.wait_closed`."""
        await self._done.wait()


class FxtrSandboxLauncher:
    """The `fxtr_sandboxing`-backed :class:`SandboxLauncherProto` impl.

    Attributes:
        _runner_scope: The runner scope (holder scopes chain to the env
            host scope, not this — kept for naming/diagnostics only).
        _spawner_ref: The env's spawner ref, resolvable in
            ``_env_host_scope`` (captured inside the env holder task,
            where the env CM's body made its host scope active). Both are
            set together when the env holder reports started.
    """

    def __init__(
        self,
        runner_scope: FxtrScope,
        *,
        sandbox_repo: Path,
        cache_dir: Path,
        task_group: anyio.abc.TaskGroup,
    ) -> None:
        """A launcher for one runner.

        Args:
            runner_scope: The runner scope (see class doc).
            sandbox_repo: The runner's configured sandbox repo — the one
                git repo every bound ``git_commit`` resolves against.
            cache_dir: The sandbox environment's cache directory
                (machine-local; wheel/bundle caching).
            task_group: The task group holder tasks run on. Must outlive
                the launcher and be closed *after* :meth:`aclose` (the
                bootstrap's exit-stack ordering guarantees this).
        """
        self._runner_scope = runner_scope
        self._sandbox_repo = sandbox_repo
        self._cache_dir = cache_dir
        self._tg = task_group
        self._env_lock = anyio.Lock()
        self._env_teardown = anyio.Event()
        self._env_done = anyio.Event()
        self._env_started = False
        self._spawner_ref: ServiceRef[SandboxSpawnerProto] | None = None
        self._env_host_scope: FxtrScope | None = None
        self._launched: list[_LaunchedSandbox] = []
        self._closed = False

    async def _env_holder(
        self,
        *,
        task_status: anyio.abc.TaskStatus[tuple[ServiceRef[SandboxSpawnerProto], FxtrScope]],
    ) -> None:
        """Holder task for the one sandbox environment: open, report, park.

        A failure before ``started`` rides back to :meth:`_ensure_env` as
        the ``tg.start`` error; after ``started`` the body only parks,
        and teardown failures are contained (blast-radius rule).

        Started scope-poisoned (`start_with_scope(scope=None)`): holder
        tasks provide NO ambient scope — everything here manages scopes
        explicitly, and any implicit active-scope use (in this body or
        any library it calls) raises ``NoActiveScopeError`` at the
        offending line instead of silently adopting the demanding
        dispatch's ephemeral context (the deadlock class in
        plans/phase6_sandboxing/chunk3_scope_map.md).
        """
        started = False
        try:
            async with create_sandbox_environment(
                cache_dir=self._cache_dir, git_repo_path=self._sandbox_repo
            ) as env:
                # `spawner_ref` resolves in the env's host scope
                # (`create_sandbox_environment`'s documented contract).
                task_status.started((env.spawner_ref, env.host_scope))
                started = True
                await self._env_teardown.wait()
        except Exception:
            if not started:
                raise
            _logger.exception("sandbox environment teardown failed")
        finally:
            self._env_done.set()

    async def _ensure_env(self) -> tuple[ServiceRef[SandboxSpawnerProto], FxtrScope]:
        """Open the environment on first use (one holder task, ever)."""
        async with self._env_lock:
            if self._closed:
                raise SandboxLaunchError("the sandbox launcher is closed")
            if not self._env_started:
                self._spawner_ref, self._env_host_scope = await start_with_scope(
                    self._tg, self._env_holder, scope=None
                )
                self._env_started = True
        assert self._spawner_ref is not None and self._env_host_scope is not None
        return self._spawner_ref, self._env_host_scope

    async def _launch_holder(
        self,
        defn: SandboxDefn,
        spawner_ref: ServiceRef[SandboxSpawnerProto],
        env_host_scope: FxtrScope,
        teardown: anyio.Event,
        done: anyio.Event,
        *,
        task_status: anyio.abc.TaskStatus[tuple[ServiceRef[UnknownService], FxtrScope]],
    ) -> None:
        """Holder task for one launched process: spawn, report, park.

        A failure before ``started`` rides back to :meth:`launch` as the
        ``tg.start`` error; after ``started`` the body only parks and
        drains, and teardown failures are contained to this holder (the
        blast-radius rule — one process's teardown must not kill the
        runner's task group).

        Started scope-poisoned like :meth:`_env_holder` (see there).
        """
        started = False
        try:
            # The holder scope chains to the env host scope (where the
            # spawner and the spawn's return chain live), so it — and
            # every run-scope lease on it — transitively keeps the env
            # serving this process. Closed (drained) when `teardown` is
            # set and the last outside lease releases.
            async with FxtrScope(
                name=f"wf3-sandbox:{defn.flake_attr}",
                isolated=True,
                include=[env_host_scope],
            ) as holder_scope:
                # The transient defn entity, minted purely for interop
                # with `spawn` (bindings are not identity — the entity is
                # never recorded anywhere).
                defn_ref = NixServiceDefn(
                    git_commit=defn.git_commit,
                    flake_attr=defn.flake_attr,
                    flake_path=defn.flake_path,
                ).store(holder_scope)
                # Membership: `spawner_ref` is resolvable in the env host
                # scope (`_ensure_env`'s contract), which `holder_scope`
                # includes.
                spawner = await spawner_ref.resolve(holder_scope)
                sandbox_ref = await spawner.spawn(defn_ref)
                task_status.started((sandbox_ref, holder_scope))
                started = True
                await teardown.wait()
        except Exception:
            if not started:
                raise
            # Post-started teardown failure: contained (see docstring).
            _logger.exception(
                "sandbox holder teardown failed (%s @ %s)", defn.flake_attr, defn.git_commit
            )
        finally:
            done.set()

    async def launch(self, defn: SandboxDefn) -> LaunchedSandboxProto:
        """Contract on `SandboxLauncherProto.launch`."""
        spawner_ref, env_host_scope = await self._ensure_env()
        teardown = anyio.Event()
        done = anyio.Event()
        try:
            sandbox_ref, holder_scope = await start_with_scope(
                self._tg,
                self._launch_holder,
                defn,
                spawner_ref,
                env_host_scope,
                teardown,
                done,
                scope=None,
            )
        except Exception as exc:
            raise SandboxLaunchError(
                f"launching sandbox for {defn.flake_attr!r} at commit "
                f"{defn.git_commit} failed: {exc}"
            ) from exc
        launched = _LaunchedSandbox(
            sandbox_ref=sandbox_ref, holder_scope=holder_scope, teardown=teardown, done=done
        )
        self._launched.append(launched)
        return launched

    async def aclose(self) -> None:
        """Contract on `SandboxLauncherProto.aclose`: guests, then env."""
        async with self._env_lock:
            self._closed = True
        for launched in self._launched:
            launched.shutdown()
        for launched in self._launched:
            await launched.wait_closed()
        self._launched.clear()
        if self._env_started:
            self._env_teardown.set()
            await self._env_done.wait()
