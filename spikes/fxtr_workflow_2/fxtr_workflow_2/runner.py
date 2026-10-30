"""The workflow runner: a durable task/workflow over a realization map + op log.

The runner and run groups are plain async context managers — not services. The
runner owns a child ``FxtrScope`` and an anyio task group (which hosts running
**jobs**). A run group is ``runner.run_group(name, desc)`` (an async CM yielding
a ``RunGroupHandle``); the first *service* a caller touches is the
``TaskSlotProto`` returned by ``RunGroupHandle.get_storage`` (resolve it against
``RunGroupHandle.scope``).

Responsibilities split cleanly:

- **Job** (one per slot path): runs the task fn exactly once in its own isolated
  scope, pins a ``WorkflowTaskComplete`` into the **realization map**, and
  *produces a structured outcome*. It does not touch the op log.
- **Requester** (each ``run`` call): builds the invocation (cache key), then
  joins/starts the job (or cache-hits), and owns the **op log** — it emits a
  ``RunStarted`` before waiting and a ``RunCompleted`` when its wait resolves,
  attributed to its run group, and maps the job's outcome to a single raised
  exception.

**Nesting** (a running task fn launching sub-tasks): the runner re-mints every
slot capability a job ``J`` receives as a ``J``-bound slot — currently
``step_ctx.subslot(key)`` (incoming ``TaskStorage`` args are future work). A run
on a ``J``-bound slot is attributed to ``J``: the child is tracked under ``J``,
and on ``J``'s success the runner waits for all children, poisons ``J`` if any
child failed, else records ``depends_on``. On ``J``'s *failure* the runner
finalizes immediately and does not wait for children (they finish on their own).

Scope discipline (``notes/scope_discipline.md`` / ``notes/scope_service_lifetimes.md``):
the runner scope is isolated with explicit includes; each run group / job has its
own child scope; slots a job creates mount on the job scope; services are
resolved in the scope where they are used.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import socket
import time
import traceback
import uuid
from collections.abc import AsyncGenerator, Mapping
from typing import Any

import anyio
import anyio.abc
from frozendict import frozendict

from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    Entity,
    EntityRef,
    FxtrScope,
    ServiceKey,
    ServiceRef,
    get_active_scope,
)
from fxtr2.data.fxtr_context_api import FxtrNamespace, FxtrQueryableNamespace
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from ._ns import WORKFLOWS_NS
from .cache_key import SlotArgMarker, SlotResolver, bare_value, build_invocation
from .entities import (
    RunCompleted,
    RunGroup,
    RunGroupClosed,
    RunGroupOpened,
    RunStarted,
    WorkflowOutcome,
    WorkflowTaskComplete,
)
from .stateless_service_provider import StatelessServiceSpawnerProto
from .task_types import (
    TaskConflictError,
    TaskFailedError,
    TaskFnProto,
    TaskSlotProto,
    TaskStepContextProto,
    WorkflowCycleError,
)

_log = logging.getLogger(__name__)

TASK_SLOT_SERVICE_TYPE_ID = define_type(WORKFLOWS_NS, "service/task_slot")
TASK_STEP_CONTEXT_SERVICE_TYPE_ID = define_type(WORKFLOWS_NS, "service/task_step_context")

#: Child outcomes that poison a parent (a completed slot may never depend on one).
_FAILURE_OUTCOMES: frozenset[WorkflowOutcome] = frozenset(
    {"failed", "conflict", "child_incomplete", "child_conflict", "interrupted"}
)


def _default_owner() -> str:
    """Return the default ``owner`` for a runner: ``"{host}:{pid}:{nonce}"``."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _validate_slot_key(key: str) -> None:
    """Validate a single slot-path segment (nonempty, no ``/``).

    Raises:
        ValueError: If ``key`` is empty or contains a ``/``.
    """
    if not key:
        raise ValueError("slot key must be nonempty")
    if "/" in key:
        raise ValueError(f"slot key must not contain '/': {key!r}")


def _project_result(scope: FxtrScope, result_id: FxtrID) -> FxtrID:
    """Project a task result to the provenance-layer node stored in
    ``WorkflowTaskComplete.result``.

    A layered id contributes its prov half; otherwise we ``u``-wrap (treating a
    bare provenance-tag node as data too — we key on the ``_provenance_parent``
    sidecar, not the tag). ``result_id`` must be loadable in ``scope``.
    """
    if result_id._provenance_parent is not None:
        return scope.extract_provenance_parent(result_id)
    return scope.create_u_node(result_id)


@dataclasses.dataclass
class _ChildObs:
    """A parent job's observation of one child slot it ran into.

    Registered before the child resolves (so it stays tracked even if the
    parent's await is cancelled), then filled in. ``complete_ref`` (the child's
    ``WorkflowTaskComplete``, loadable in the runner scope) is set on success;
    ``outcome`` records the child's outcome for poisoning.
    """

    path: str
    resolved: anyio.Event
    outcome: WorkflowOutcome | None = None
    complete_ref: EntityRef[Any] | None = None


@dataclasses.dataclass
class _Job:
    """In-memory state for the one running job of a slot.

    ``invocation_ref`` / ``bare_args`` / ``bare_kwargs`` are loadable in the
    runner scope (the starter copied the invocation closure there). ``run_group_ref``
    is the launching requester's group — nested runs the job's task fn makes are
    attributed to it. The job produces an *outcome* (set before ``done`` fires);
    on success ``result_id`` is the projected provenance-layer node and
    ``complete_ref`` is its ``WorkflowTaskComplete`` (both loadable in the runner
    scope). ``children`` tracks the slots this job's task fn ran into. The job
    emits no op-log events — that is the requester's concern.
    """

    slot_path: str
    cache_key: FxtrID
    invocation_ref: EntityRef[Any]
    run_group_ref: EntityRef[RunGroup]
    #: The task-fn args/kwargs, with slot args carried as ``SlotArgMarker``s
    #: (re-minted job-bound before the task fn is invoked).
    bare_args: tuple[object, ...]
    bare_kwargs: dict[str, object]
    done: anyio.Event
    #: The request chain (slot paths from the root down to this job's own path);
    #: used for cycle detection. Empty for a job started by a top-level run.
    chain: tuple[str, ...] = ()
    children: dict[str, _ChildObs] = dataclasses.field(default_factory=dict)
    outcome: WorkflowOutcome | None = None
    result_id: FxtrID | None = None
    complete_ref: EntityRef[Any] | None = None
    reason: str | None = None
    #: The underlying exception for a ``failed`` outcome (for ``raise ... from``);
    #: None otherwise.
    error: BaseException | None = None


class WorkflowRunner(anyio.AsyncContextManagerMixin):
    """Manages a durable workflow over a realization map + op log.

    Args:
        spawner: The spawn-only capability used to resolve task functions. Must
            be resolvable in a scope this runner includes (e.g. mounted on one of
            the namespace scopes); resolved per job in the job scope.
        realization_map: Queryable namespace holding successful results
            (``WorkflowTaskComplete`` pins keyed by slot path).
        op_log: Append-only namespace receiving operation events. To avoid
            duplicating entity data, back ``op_log`` and ``realization_map`` by
            the **same SQLite db file** — two namespaces over one store dedup
            content automatically.
        owner: Identifier stamped on every op-log event (which runner recorded
            it). Defaults to ``"{host}:{pid}:{nonce}"``.
    """

    def __init__(
        self,
        *,
        spawner: ServiceRef[StatelessServiceSpawnerProto],
        realization_map: FxtrQueryableNamespace,
        op_log: FxtrNamespace,
        owner: str | None = None,
    ) -> None:
        self._spawner = spawner
        self._realization_map = realization_map
        self._op_log = op_log
        self._owner = owner if owner is not None else _default_owner()
        self._scope: FxtrScope | None = None
        self._task_group: anyio.abc.TaskGroup | None = None
        self._jobs: dict[str, _Job] = {}
        self._jobs_lock = anyio.Lock()
        self._oplog_seq = 0
        # Registry of every slot this runner has mounted → its root-relative
        # path, for recognizing a task-storage slot passed back as an arg (NQ-2):
        # the arg's ServiceRef keeps the same handle identity (cap-IR
        # bounce-back), so its ServiceKey matches. (Grows for the runner's
        # lifetime — see followups for cleanup.) Run-group consistency
        # (decision 5) needs no explicit check: sibling group scopes are
        # isolated, so a slot from another group isn't reachable to pass as an
        # arg in the first place.
        self._slots: dict[ServiceKey, str] = {}

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[WorkflowRunner]:
        async with anyio.create_task_group() as task_group:
            # Scope discipline: isolated + explicit includes rather than the
            # ambient active scope. We include the namespaces' scopes so entities
            # pinned into / retrieved from them are loadable here (the caller
            # enters the namespaces around the runner, keeping them open).
            scope = FxtrScope(
                isolated=True,
                include=[self._realization_map.scope, self._op_log.scope],
                name="workflow_runner",
            )
            async with scope:
                self._scope = scope
                self._task_group = task_group
                try:
                    yield self
                finally:
                    self._scope = None
                    self._task_group = None

    @property
    def owner(self) -> str:
        """The owner string stamped on this runner's op-log events."""
        return self._owner

    @property
    def scope(self) -> FxtrScope:
        """The runner's child scope (parent of every run-group scope)."""
        if self._scope is None:
            raise RuntimeError("WorkflowRunner must be used as an async context manager")
        return self._scope

    @contextlib.asynccontextmanager
    async def run_group(self, name: str, description: str) -> AsyncGenerator[RunGroupHandle]:
        """Open a run group for the duration of the ``async with`` block.

        Records a ``RunGroupOpened`` op-log event on entry and ``RunGroupClosed``
        on exit. Run groups are an op-log grouping device only — they do not
        partition the realization map, so different groups can run into /
        cache-share the same slots. The group owns its own scope: slots handed
        out by ``get_storage`` mount there and are freed when the group closes.

        Yields:
            A :class:`RunGroupHandle` exposing ``get_storage`` + ``scope``.
        """
        # run_group_ref is loadable in self.scope: we just stored it there.
        run_group_ref = RunGroup(name=name, description=description, created_at=time.time()).store(
            self.scope
        )
        await self._append_oplog(
            RunGroupOpened(run_group=run_group_ref, owner=self._owner, timestamp=time.time())
        )
        try:
            async with FxtrScope(
                isolated=True, include=[self.scope], name=f"workflow_run_group:{name}"
            ) as group_scope:
                yield RunGroupHandle(self, run_group_ref, group_scope)
        finally:
            # group_scope has closed (slots freed); the op log is still open.
            await self._append_oplog(
                RunGroupClosed(run_group=run_group_ref, owner=self._owner, timestamp=time.time())
            )

    # --- op-log helpers -----------------------------------------------------

    async def _append_oplog(self, event: Entity) -> None:
        """Append ``event`` to the op log under a monotonic, time-ordered key."""
        seq = self._oplog_seq
        self._oplog_seq += 1
        ts_us = int(time.time() * 1_000_000)
        key = f"{ts_us:020d}-{seq:08d}"
        await self._op_log.pin(key, event, scope=self.scope)

    async def _emit_started(
        self, run_group_ref: EntityRef[RunGroup], path: str, inv_ref: EntityRef[Any], launched: bool
    ) -> None:
        """Record this request's ``RunStarted`` op-log event (before it waits)."""
        await self._append_oplog(
            RunStarted(
                run_group=run_group_ref,
                slot_path=path,
                invocation=inv_ref,
                launched_job=launched,
                owner=self._owner,
                timestamp=time.time(),
            )
        )

    async def _emit_completed(
        self,
        run_group_ref: EntityRef[RunGroup],
        path: str,
        inv_ref: EntityRef[Any],
        outcome: WorkflowOutcome,
        result_id: FxtrID | None,
        reason: str | None,
    ) -> None:
        """Record this request's ``RunCompleted`` op-log event (its outcome)."""
        await self._append_oplog(
            RunCompleted(
                run_group=run_group_ref,
                slot_path=path,
                invocation=inv_ref,
                outcome=outcome,
                result=result_id,
                reason=reason,
                owner=self._owner,
                timestamp=time.time(),
            )
        )

    # --- slots --------------------------------------------------------------

    async def _mount_slot(
        self, scope: FxtrScope, run_group_ref: EntityRef[RunGroup], path: str, *, job: _Job | None
    ) -> ServiceRef[TaskSlotProto]:
        """Mount a slot capability at ``path`` on ``scope`` and register it so an
        arg referencing it can be recognized later. ``job`` binds the slot to a
        running job (``None`` for a top-level slot)."""
        impl = _SlotImpl(self, run_group_ref, path, scope, job=job)
        ref = await scope.mount(impl)
        self._slots[ServiceKey(ref.handle, ref.service_type_id)] = path
        return ref

    def _make_slot_resolver(self) -> SlotResolver:
        """Build a resolver that maps a task-storage slot ``ServiceRef`` arg to
        its root-relative path, rejecting non-slot ``ServiceRef``s.

        Run-group consistency (decision 5) needs no check here: a slot from
        another run group lives on a sibling group scope and so can't be reached
        to pass as an arg (the service layer rejects encoding it)."""

        def resolve(ref: ServiceRef[object]) -> str:
            path = self._slots.get(ServiceKey(ref.handle, ref.service_type_id))
            if path is None:
                raise TypeError(
                    "a ServiceRef arg is not a task-storage slot of this runner; pass a "
                    "service-handle entity to spawn inside the task instead."
                )
            return path

        return resolve

    async def _materialize_arg(self, value: object, job: _Job, job_scope: FxtrScope) -> object:
        """Replace ``SlotArgMarker``s (from ``bare_value``) with freshly re-minted
        job-bound slots before the task fn is invoked; recurse into collections."""
        if isinstance(value, SlotArgMarker):
            return await self._mount_slot(job_scope, job.run_group_ref, value.path, job=job)
        if isinstance(value, Mapping):
            return {k: await self._materialize_arg(v, job, job_scope) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [await self._materialize_arg(v, job, job_scope) for v in value]
        return value

    # --- the requester ------------------------------------------------------

    async def _request_run(
        self,
        *,
        parent_job: _Job | None,
        run_group_ref: EntityRef[RunGroup],
        path: str,
        inv_ref: EntityRef[Any],
        bare_args: tuple[object, ...],
        bare_kwargs: dict[str, object],
    ) -> EntityRef[UnknownEntity]:
        """Cache-hit / join / start the slot's job, emit the op-log
        started+completed pair, and return the result (loadable in :attr:`scope`)
        or raise. When ``parent_job`` is set this is a nested run: the child is
        tracked under the parent for ``depends_on`` / poisoning."""
        # Cycle detection: running into a slot already in our request chain would
        # deadlock (it's still executing). Checked before any op-log / job work.
        parent_chain = parent_job.chain if parent_job is not None else ()
        if path in parent_chain:
            raise WorkflowCycleError(
                f"slot {path!r} is already running in this request chain {parent_chain!r}"
            )
        cache_key = inv_ref.entity_id
        # Register the observation before resolving so the parent tracks this
        # child even if our await is cancelled (or the parent fails mid-way).
        obs: _ChildObs | None = None
        if parent_job is not None:
            obs = _ChildObs(path=path, resolved=anyio.Event())
            parent_job.children[path] = obs

        conflict_reason: str | None = None
        cache_complete: WorkflowTaskComplete | None = None
        cache_pin: EntityRef[Any] | None = None
        job: _Job | None = None
        launched = False

        async with self._jobs_lock:
            existing = self._jobs.get(path)
            if existing is not None:
                if existing.cache_key != cache_key:
                    conflict_reason = "an in-flight job with a different cache key"
                else:
                    job = existing
            else:
                # pin + its closure are loadable in self.scope (just retrieved).
                pin = await self._realization_map.try_retrieve(
                    path, WorkflowTaskComplete, into_scope=self.scope
                )
                if pin is not None:
                    complete = await pin.load(self.scope)
                    if complete.invocation.entity_id != cache_key:
                        conflict_reason = "an existing result with a different cache key"
                    else:
                        cache_complete = complete
                        cache_pin = pin
                else:
                    job = _Job(
                        slot_path=path,
                        cache_key=cache_key,
                        invocation_ref=inv_ref,
                        run_group_ref=run_group_ref,
                        bare_args=bare_args,
                        bare_kwargs=bare_kwargs,
                        done=anyio.Event(),
                        chain=parent_chain + (path,),
                    )
                    self._jobs[path] = job
                    assert self._task_group is not None
                    self._task_group.start_soon(self._run_job, job)
                    launched = True

        await self._emit_started(run_group_ref, path, inv_ref, launched)

        if conflict_reason is not None:
            await self._emit_completed(
                run_group_ref, path, inv_ref, "conflict", None, conflict_reason
            )
            self._resolve_obs(obs, "conflict", None)
            raise TaskConflictError(f"slot {path!r}: {conflict_reason}")
        if cache_complete is not None:
            result_id = cache_complete.result
            await self._emit_completed(run_group_ref, path, inv_ref, "success", result_id, None)
            self._resolve_obs(obs, "success", cache_pin)
            return EntityRef(entity_id=result_id, target_type=UnknownEntity)

        assert job is not None
        try:
            await job.done.wait()
        except BaseException:
            # Our wait was cancelled; the shared job keeps running for others.
            with anyio.CancelScope(shield=True):
                await self._emit_completed(
                    run_group_ref, path, inv_ref, "interrupted", None, "await cancelled"
                )
            self._resolve_obs(obs, "interrupted", None)
            raise

        outcome = job.outcome
        assert outcome is not None
        self._resolve_obs(obs, outcome, job.complete_ref)
        # The op log keeps the full traceback for a failure (durable diagnostics);
        # the raised error carries a short reason and chains the inner exception.
        op_reason = job.reason
        if outcome == "failed" and job.error is not None:
            op_reason = "".join(traceback.format_exception(job.error))
        await self._emit_completed(run_group_ref, path, inv_ref, outcome, job.result_id, op_reason)
        if outcome == "success":
            assert job.result_id is not None
            return EntityRef(entity_id=job.result_id, target_type=UnknownEntity)
        if outcome == "conflict":
            raise TaskConflictError(f"slot {path!r}: {job.reason}")
        raise TaskFailedError(path, outcome, job.reason or "") from job.error

    @staticmethod
    def _resolve_obs(
        obs: _ChildObs | None, outcome: WorkflowOutcome, complete_ref: EntityRef[Any] | None
    ) -> None:
        """Fill in a parent's child observation and wake anyone waiting on it.

        Called as a nested run resolves: records the child's ``outcome`` (and its
        ``WorkflowTaskComplete`` ref on success) so the parent can later poison /
        build ``depends_on``, then sets ``obs.resolved``. No-op when ``obs`` is
        None (a top-level run, with no parent observing it).
        """
        if obs is None:
            return
        obs.outcome = outcome
        obs.complete_ref = complete_ref if outcome == "success" else None
        obs.resolved.set()

    # --- the job ------------------------------------------------------------

    async def _run_job(self, job: _Job) -> None:
        """Run one job to completion in its own isolated scope, setting the job's
        outcome. Never raises out; the requester owns op-log + error shaping."""
        try:
            # Isolated per job (includes the runner scope so the invocation
            # closure + spawner are loadable). The spawner is resolved *here*
            # (the using scope) rather than on the long-lived runner scope.
            async with FxtrScope(
                isolated=True, include=[self.scope], name=f"workflow_job:{job.slot_path}"
            ) as job_scope:
                try:
                    spawner = await self._spawner.resolve(job_scope)
                    invocation = await job.invocation_ref.load(self.scope)
                    svc_ref = await spawner.spawn(invocation.step_fn)
                    task_fn = await svc_ref.replace_service_type(TaskFnProto).resolve(job_scope)
                    step_ctx_ref = await job_scope.mount(_StepContextImpl(self, job, job_scope))
                    # Re-mint any slot args as job-bound slots (so a run into them
                    # is attributed to this job).
                    fn_args = await self._materialize_arg(list(job.bare_args), job, job_scope)
                    fn_kwargs = await self._materialize_arg(dict(job.bare_kwargs), job, job_scope)
                    assert isinstance(fn_args, list) and isinstance(fn_kwargs, dict)
                    result_ref = await task_fn.run(step_ctx_ref, *fn_args, **fn_kwargs)
                except Exception as exc:  # noqa: BLE001 - task fn / spawn failure
                    # Failure: finalize immediately, do not wait for children
                    # (they finish on their own; a failed job pins nothing).
                    job.outcome = "failed"
                    job.reason = str(exc)
                    job.error = exc
                    await self._finalize(job)
                    return
                # Success: wait for children, then poison or persist.
                await self._complete_success(job, job_scope, result_ref)
                await self._finalize(job)
        finally:
            if not job.done.is_set():
                await self._finalize(job, interrupted=True)

    async def _complete_success(
        self, job: _Job, job_scope: FxtrScope, result_ref: EntityRef[Any]
    ) -> None:
        """The task fn returned: wait for the job's children, then poison ``job``
        if any child failed, else project + pin its result with ``depends_on``."""
        for obs in list(job.children.values()):
            await obs.resolved.wait()

        child_outcomes = [obs.outcome for obs in job.children.values()]
        if any(o in _FAILURE_OUTCOMES for o in child_outcomes):
            job.outcome = "child_conflict" if "conflict" in child_outcomes else "child_incomplete"
            job.reason = "a child task did not complete successfully"
            return

        depends_on = frozendict(
            {p: obs.complete_ref for p, obs in job.children.items() if obs.complete_ref is not None}
        )
        # Project the result to a provenance-layer node and move it into the
        # runner scope (so it outlives the job scope).
        projected_id = _project_result(job_scope, result_ref.entity_id)
        await EntityRef(entity_id=projected_id, target_type=UnknownEntity).copy_to(
            self.scope, from_scope=job_scope
        )
        # Store the Complete in the runner scope first so we can reference it in a
        # parent's depends_on; its child Completes are loadable there too.
        complete_ref = WorkflowTaskComplete(
            invocation=job.invocation_ref, result=projected_id, depends_on=depends_on
        ).store(self.scope)
        try:
            await self._realization_map.pin(job.slot_path, complete_ref, scope=self.scope)
        except KeyError:
            job.outcome = "conflict"
            job.reason = "slot was filled concurrently before this result could be saved"
            return
        job.outcome = "success"
        job.result_id = projected_id
        job.complete_ref = complete_ref

    async def _finalize(self, job: _Job, *, interrupted: bool = False) -> None:
        """Deregister the job and signal its waiters. Shielded so it completes
        even under cancellation. Idempotent via the ``done`` guard."""
        with anyio.CancelScope(shield=True):
            if job.outcome is None and interrupted:
                job.outcome = "interrupted"
                job.reason = "runner cancelled"
            async with self._jobs_lock:
                if self._jobs.get(job.slot_path) is job:
                    del self._jobs[job.slot_path]
            job.done.set()


class RunGroupHandle:
    """Live handle for an open run group (plain python; not a service).

    Hands out slot capabilities via :meth:`get_storage`, mounted on the group's
    own scope (:attr:`scope`). Obtained from :meth:`WorkflowRunner.run_group`.

    ``run_group_ref`` is loadable in ``runner.scope`` (``run_group`` stored it
    there before constructing this handle).
    """

    def __init__(
        self, runner: WorkflowRunner, run_group_ref: EntityRef[RunGroup], group_scope: FxtrScope
    ) -> None:
        self._runner = runner
        self._run_group_ref = run_group_ref
        self._group_scope = group_scope

    @property
    def scope(self) -> FxtrScope:
        """The run group's scope. Resolve slot ``ServiceRef``s from ``get_storage``
        / ``subslot`` against this scope; it closes when the group closes."""
        return self._group_scope

    async def get_storage(self, path: str) -> ServiceRef[TaskSlotProto]:
        """Return a slot capability rooted at ``path`` (nonempty), mounted on
        this run group's scope.

        Returns:
            A ``ServiceRef[TaskSlotProto]`` — resolve it against :attr:`scope`.
        """
        if not path:
            raise ValueError("get_storage path must be nonempty")
        for segment in path.split("/"):
            _validate_slot_key(segment)
        # Top-level slot: not bound to a job (job=None); mounts on the group scope.
        return await self._runner._mount_slot(
            self._group_scope, self._run_group_ref, path, job=None
        )


class _SlotImpl(TypedServiceImpl[TaskSlotProto]):
    """A capability over one realization-map slot subtree.

    ``mount_scope`` is where this slot's subslots mount (the group scope for
    top-level slots, a job scope for a job's own subslots). ``job`` is the job
    this slot is bound to (``None`` for top-level slots) — a nested run is
    attributed to it.
    """

    def __init__(
        self,
        runner: WorkflowRunner,
        run_group_ref: EntityRef[RunGroup],
        path: str,
        mount_scope: FxtrScope,
        *,
        job: _Job | None,
    ) -> None:
        self._runner = runner
        self._run_group_ref = run_group_ref
        self._path = path
        self._mount_scope = mount_scope
        self._job = job

    def _target_type_annotation(self) -> Any:
        return TaskSlotProto

    def _service_type_id(self) -> FxtrID:
        return TASK_SLOT_SERVICE_TYPE_ID

    @service_method
    async def run(
        self,
        step_fn: EntityRef[UnknownEntity],
        *args: FxtrServiceData,
        **kwargs: FxtrServiceData,
    ) -> EntityRef[UnknownEntity]:
        runner = self._runner
        # Scope discipline (service-method case): the call scope holds the
        # decoded step_fn + args.
        call_scope = get_active_scope()
        # Recognizes task-storage slot args (-> their path); rejects non-slot
        # ServiceRefs.
        resolver = runner._make_slot_resolver()
        invocation = build_invocation(
            step_fn, args, kwargs, slot_resolver=resolver, placeholder_scope=call_scope
        )
        # Loadable in call_scope: built from step_fn + args, which live there.
        inv_ref = invocation.store(call_scope)
        # Bring the invocation + arg closure into the runner scope so it (and the
        # job that reads it) outlives this call.
        await inv_ref.copy_to(runner.scope, from_scope=call_scope)
        bare_args = tuple(bare_value(a, slot_resolver=resolver) for a in args)
        bare_kwargs = {k: bare_value(v, slot_resolver=resolver) for k, v in kwargs.items()}
        return await runner._request_run(
            parent_job=self._job,
            run_group_ref=self._run_group_ref,
            path=self._path,
            inv_ref=inv_ref,
            bare_args=bare_args,
            bare_kwargs=bare_kwargs,
        )

    @service_method
    async def subslot(self, key: str) -> ServiceRef[TaskSlotProto]:
        _validate_slot_key(key)
        return await self._runner._mount_slot(
            self._mount_scope, self._run_group_ref, f"{self._path}/{key}", job=self._job
        )


class _StepContextImpl(TypedServiceImpl[TaskStepContextProto]):
    """Per-job step context. ``subslot`` mounts a job-bound slot under the job's
    own slot path; ``log`` forwards to python logging."""

    def __init__(self, runner: WorkflowRunner, job: _Job, job_scope: FxtrScope) -> None:
        self._runner = runner
        self._job = job
        self._job_scope = job_scope

    def _target_type_annotation(self) -> Any:
        return TaskStepContextProto

    def _service_type_id(self) -> FxtrID:
        return TASK_STEP_CONTEXT_SERVICE_TYPE_ID

    @service_method
    async def subslot(self, key: str) -> ServiceRef[TaskSlotProto]:
        _validate_slot_key(key)
        # Job-bound subslot of the job's own slot: mounts on the job scope (freed
        # when the job ends); a run into it is attributed to this job.
        return await self._runner._mount_slot(
            self._job_scope,
            self._job.run_group_ref,
            f"{self._job.slot_path}/{key}",
            job=self._job,
        )

    @service_method
    async def log(self, message: str) -> None:
        _log.info("[workflow %s] %s", self._job.slot_path, message)
