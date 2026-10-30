"""The experiment demo's workflow runner mount.

:func:`mount_experiment_runner` assembles a persistent
:mod:`fxtr_experiment` runner — the two-file durable session, the
runner scope, the binding registry, the executor, and the mounted user
capability — and auto-binds the demo's standard task fns. Pair the
yielded handle's ``(capability_ref, scope)`` with an
``ExternalServiceConfig`` under the name ``experiment-workflow``; the
driver (:mod:`experiment_demo_helper_service`) drives the same
capability through the yielded ``workflow`` wrapper.

Scope topology (mirrors the fxtr_experiment test suite's, and both
properties are load-bearing): the **host scope** — home of the task-fn
adapter + connector mounts — is a plain **non-isolated** scope
*enclosing* the session + runner scope. Enclosing, because the binding
registry bridges connectors into the runner scope with independent
leases, and an independent lease requires its source scope to close
*after* its holder (``v2/notes/scope_service_lifetimes.md``'s
keep-alive contract — nesting the host inside the runner deadlocks the
host's drain). Non-isolated (the ambient-parented user-boundary style,
the suite's ``test-host``), because service-ref arg lowering on the
capability proxy needs the connector's key reachable — an isolated
host's mounts fail the lowering's registration walk. Entities passed
as capability-call args (handle keys, input notes) are stored in the
**runner scope** (the proxy's home scope, so ids are coverable on
calls).

The two sqlite files live under ``data_dir`` (the demo puts this inside
the scaffolded runtime workdir, so workflow state shares the workdir's
lifetime: ``--auto-workdir`` re-scaffolds — and wipes — by design;
point ``--workdir`` at a workdir you scaffolded yourself for state that
survives relaunches).

Auto-bind: handle keys are content-addressed
(:func:`demo_task_fns.fn_key_note`), so every relaunch binds the same
key ids that earlier sessions wrote into handle grids — which is what
makes re-demand-after-relaunch (the resume arc) work out of the box.
Bindings themselves are ephemeral runner config and are re-created
fresh on every mount. The binding release capabilities are held on the
yielded handle for the mount's lifetime.
"""

from __future__ import annotations

import contextlib
import dataclasses
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

import anyio
import anyio.abc
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceRef
from fxtr2.services.service_cap_common import UnknownService
from fxtr_experiment.executor import Executor
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.bindings import BindingHost, wrap_singleton
from fxtr_experiment_task.task_adapter import TaskFnAdapter
from fxtr_experiment.user_api import UserCapabilityProto
from fxtr_experiment.user_service import UserCapabilityImpl, Workflow
from fxtr_workstation.protocols import WorkstationServiceProto, expose_service

from demo_task_fns import STANDARD_FNS, fn_key_note


@dataclasses.dataclass
class ExperimentRunner:
    """One mounted experiment runner (see the module docstring).

    Attributes:
        capability_ref: The mounted user capability — mounted on (thus
            resolvable in) ``scope``; pair the two in an
            ``ExternalServiceConfig``.
        scope: The runner scope (includes the session's entity
            namespace + ``GLOBAL_SCOPE``); also where entities passed
            as capability-call args should be stored (the ``workflow``
            proxy's home scope, so such ids are coverable — the same
            membership the test suite's ambient-active-scope stores
            rely on).
        host_scope: The enclosing host scope that owns the task-fn
            adapter + connector mounts (closes after ``scope`` — see
            the module docstring's topology note).
        workflow: Host-side wrapper over the same capability the
            browser drives (proxy resolved in ``scope``).
        session: The open durable session (for smoke tests /
            diagnostics; the capability owns all normal traffic).
        fn_keys: Handle-key entity id per auto-bound standard fn name
            (stored in ``scope``).
        fn_instances: The live auto-bound fn instances (demo plumbing:
            the driver re-arms the flaky fn's planted failure).
        releases: The held binding release capabilities (auto-bound
            fns); dropping them would auto-release the bindings, so
            the handle keeps them for the mount's lifetime.
    """

    capability_ref: ServiceRef[UnknownService]
    scope: FxtrScope
    host_scope: FxtrScope
    workflow: Workflow
    session: WorkflowSession
    fn_keys: dict[str, FxtrID]
    fn_instances: dict[str, object]
    releases: list[ServiceRef]


@asynccontextmanager
async def mount_experiment_runner(data_dir: Path) -> AsyncIterator[ExperimentRunner]:
    """Open the persistent runner over ``data_dir`` (see module docstring).

    Args:
        data_dir: Directory for the two sqlite files (created if
            missing): ``state.sqlite`` + ``entities.sqlite``.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    # Non-isolated on purpose — see the module docstring's topology note.
    async with FxtrScope(name="experiment-demo-host") as host_scope:
        async with WorkflowSession.open(
            str(data_dir / "state.sqlite"), str(data_dir / "entities.sqlite")
        ) as session:
            async with FxtrScope(
                name="experiment-runner",
                isolated=True,
                # GLOBAL_SCOPE: service type entities (define_type) live there.
                include=[session.entity_scope, GLOBAL_SCOPE],
            ) as runner_scope:
                bindings = BindingHost(runner_scope, session)
                executor = Executor(session, bindings, runner_scope)
                impl = UserCapabilityImpl(session, executor, bindings, runner_scope)
                cap_ref = await runner_scope.mount(impl)
                # Membership: mounted on (thus resolvable in) the runner scope.
                proxy = await cap_ref.replace_service_type(UserCapabilityProto).resolve(
                    runner_scope
                )
                wf = await Workflow.connect(proxy)

                # Auto-bind the standard fns (fresh instances per mount;
                # ephemeral bindings over stable content-addressed keys).
                # Adapters + connectors mount on the enclosing host scope;
                # key entities store into the runner scope (call-arg
                # coverage — see the class docstring).
                fn_keys: dict[str, FxtrID] = {}
                fn_instances: dict[str, object] = {}
                releases: list[ServiceRef] = []
                for name, factory in STANDARD_FNS.items():
                    key = fn_key_note(name).store(runner_scope).entity_id
                    fn_keys[name] = key
                    instance = factory()
                    fn_instances[name] = instance
                    fn_ref = await host_scope.mount(TaskFnAdapter(instance))
                    connector = await wrap_singleton(host_scope, fn_ref)
                    releases.append(await wf.bind_handle(key, connector))

                yield ExperimentRunner(
                    capability_ref=cap_ref,
                    scope=runner_scope,
                    host_scope=host_scope,
                    workflow=wf,
                    session=session,
                    fn_keys=fn_keys,
                    fn_instances=fn_instances,
                    releases=releases,
                )


class RunnerSupervisor:
    """Owns the runner's lifetime so it can be **crashed and revived**
    in-process (the browser-drivable interruption arc).

    Each live runner is one supervised task on the supervisor's task
    group: the task opens :func:`mount_experiment_runner` (and, when a
    registrar is set, registers the capability with the workstation
    under ``experiment-workflow`` via ``expose_service``) and parks
    inside its own cancel scope. :meth:`crash` cancels that scope —
    the whole mount stack (runs mid-step, executor, session) unwinds
    under cancellation, exactly the arc the fxtr_experiment executor
    suite proves resumable (the engine's raise-time poisoning marks
    running generations interrupted; durable checkpoints survive).
    :meth:`revive` starts a fresh supervised task over the same files.

    The demo driver (:mod:`experiment_demo_helper_service`) stays
    *stable* across crashes — it holds this supervisor, looks up
    :attr:`current` per call, and exposes crash/revive as buttons.
    A crash-then-revive also re-registers ``experiment-workflow``
    under a fresh service ref: dashboard-style clients must treat the
    stub as re-derivable (the chunk-4 renderer's re-root contract).

    Use as an async CM (``async with RunnerSupervisor(data_dir) as
    sup:``); exit cancels any live runner. Set the registrar (a
    resolved workstation ref + the scope it lives in) before the first
    :meth:`revive` that should register externally; both must outlive
    every revival (the orchestrator crashes the runner before closing
    its peer-RPC session).

    The supervisor is mount-agnostic: ``mount`` is the async-CM factory
    each revival opens over ``data_dir`` (default: this module's
    :func:`mount_experiment_runner`), and ``external_name`` is the
    workstation name each revival registers under — the behavior
    scenario runs a second supervisor with its own mount + name.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        mount: "Callable[[Path], AbstractAsyncContextManager[ExperimentRunner]] | None" = None,
        external_name: str = "experiment-workflow",
    ) -> None:
        self._data_dir = data_dir
        self._mount = mount if mount is not None else mount_experiment_runner
        self._external_name = external_name
        self._tg: anyio.abc.TaskGroup | None = None
        self._current: ExperimentRunner | None = None
        self._crash_scope: anyio.CancelScope | None = None
        self._down = anyio.Event()
        self._down.set()
        self._epoch = 0
        # The registrar pair: a ServiceRef[WorkstationServiceProto] +
        # the scope it is resolvable in (the orchestrator's peer-RPC
        # session scope; outlives every revival per the class doc).
        self._workstation_ref: ServiceRef[WorkstationServiceProto] | None = None
        self._workstation_scope: FxtrScope | None = None

    @property
    def current(self) -> ExperimentRunner | None:
        """The live runner, or ``None`` while crashed."""
        return self._current

    @property
    def epoch(self) -> int:
        """Bumps on every revival (per-runner cache invalidation)."""
        return self._epoch

    def set_registrar(
        self, workstation_ref: ServiceRef[WorkstationServiceProto], scope: FxtrScope
    ) -> None:
        """Register future revivals with the workstation (see class doc)."""
        self._workstation_ref = workstation_ref
        self._workstation_scope = scope

    async def __aenter__(self) -> "RunnerSupervisor":
        self._tg_cm = anyio.create_task_group()
        self._tg = await self._tg_cm.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> bool | None:
        assert self._tg is not None
        await self.crash()
        self._tg.cancel_scope.cancel()
        return await self._tg_cm.__aexit__(*exc)  # type: ignore[arg-type]

    async def revive(self) -> bool:
        """Start a runner if none is live. Returns True if one started."""
        assert self._tg is not None, "supervisor not entered"
        if self._current is not None:
            return False
        await self._tg.start(self._run_one)
        return True

    async def crash(self) -> bool:
        """Forcibly tear down the live runner (cancellation mid-step).

        Returns True if a runner was live. Waits for the teardown to
        finish (the supervised task's mount stack fully unwound).
        """
        if self._crash_scope is None:
            return False
        self._crash_scope.cancel()
        await self._down.wait()
        return True

    async def _run_one(
        self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED
    ) -> None:
        """One runner's lifetime. The crash cancel scope wraps the WHOLE
        mount: a crash cancels everything under the supervised task —
        in-flight runs get real cancellation (which the executor records
        as fault-class ``RunCancelled`` → interrupted), and the mount
        stack unwinds under it."""
        try:
            with anyio.CancelScope() as crash_scope:
                async with self._mount(self._data_dir) as runner:
                    async with contextlib.AsyncExitStack() as stack:
                        if self._workstation_ref is not None:
                            assert self._workstation_scope is not None
                            # Register under the stable external name for this
                            # revival; the CM releases the name on teardown
                            # (crash included), freeing it for the next one.
                            # The workstation proxy is bridged into (and
                            # resolved in) runner.scope so the capability arg
                            # lowers there — the capability is mounted on
                            # runner.scope, and register_service's arg
                            # lowering needs its key reachable from the
                            # proxy's home scope. Lease ordering: the peer-RPC
                            # session (the bridge source) outlives every
                            # revival, per RunnerSupervisor's class doc.
                            await runner.scope.bridge_service_from_scope(
                                self._workstation_ref,
                                self._workstation_scope,
                                keepalive_via="independent_lease",
                            )
                            await stack.enter_async_context(
                                expose_service(
                                    self._workstation_ref,
                                    self._external_name,
                                    runner.capability_ref,
                                    runner.scope,
                                )
                            )
                        self._current = runner
                        self._crash_scope = crash_scope
                        self._epoch += 1
                        self._down = anyio.Event()
                        try:
                            task_status.started()
                            await anyio.sleep_forever()
                        finally:
                            self._current = None
                            self._crash_scope = None
        finally:
            self._down.set()
