"""The task-side half of the task-fn service boundary: the author adapter.

This module is **task-side code**: everything a task-fn *guest* needs to
implement `runner_api.WorkflowTaskFnProto` — the :class:`TaskFnAdapter`
service impl (the ``@task_fn`` decorator), the task context, and the
return-value normalization. It runs wherever the task fn runs: in the
runner process for in-process bindings, or inside a sandbox guest for
sandboxed task-fn bindings.

Because of that, this module sits on the **task-side import boundary**.
Half of that boundary is structural: the runner side (`ops_engine`,
`state_store`, `storage`, `executor`, …) ships in a different
distribution that a guest never installs, so its sqlite and session
machinery cannot be reached from here. The other half is a discipline
this module must keep: **polars stays lazy** — the frame-output arm
imports `tables` at call time — because `tables` is the sole
eager-polars module and polars lives behind the `[frames]` extra, so an
entity-only guest installs without it. The runner-side halves of the
boundary (the run capability skin, the binding machinery) live in
`task_service.py` and `bindings.py`.

The adapter resolves the pushed run-capability ref into a typed proxy
(which *is* structurally the `view_runtime.ViewBackend` — same
methods, so views run straight over it), builds the arg/internals
views from the pushed grants, constructs the task context, calls the
user fn, and normalizes the return.

**Error transit.** Workflow errors raised runner-side (engine ops)
cross the service boundary as fxtr2 ``IRException`` with the original
class name as ``category``. That transit form is *informational only* —
for task-visible tracebacks and test assertions. Nothing durable may be
classified from it: poisoning persistence and the errored/interrupted
split happen runner-side (see `errors.py` and
`notes/execution_semantics.md`).

Scope discipline (`v2/notes/scope_discipline.md`) notes: the only
active-scope use is the documented service-method dispatch exception.
"""

from __future__ import annotations

from typing import Any, Mapping

from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from fxtr_experiment_task._ns import WORKFLOWS3_NS
from fxtr_experiment_task.errors import TaskOutputError
from fxtr_experiment_task.model import (
    EntityOutput,
    FrameOutput,
    OutputSpec,
)
from fxtr_experiment_task.runner_api import (
    RunCapabilityProto,
    ViewGrant,
    WorkflowTaskFnProto,
)
from fxtr_experiment_task.task_api import TaskFnLike
from fxtr_experiment_task.view_runtime import build_view
from fxtr_experiment_task.views import SubtreeView

TASK_FN_ADAPTER_TYPE_ID = define_type(WORKFLOWS3_NS, "task_fn_adapter")


class _TaskContext:
    """The concrete :class:`~fxtr_experiment.task_api.TaskContext`."""

    def __init__(self, internals: SubtreeView, capability: RunCapabilityProto) -> None:
        """Hold the internals view (``own_internals``) and the run
        capability proxy (the ``log`` gateway; loadable in the task fn's
        dispatch scope — the same proxy the views run against)."""
        self._internals = internals
        self._capability = capability

    def own_internals(self) -> SubtreeView:
        """Contract on task_api.TaskContext.own_internals."""
        return self._internals

    async def log(self, message: str) -> None:
        """Contract on task_api.TaskContext.log — an outer-level run-log
        line (no step identity), over the run capability."""
        await self._capability.log(message)

    async def open_channel(self):
        """Contract on task_api.TaskContext.open_channel (plumbing is
        deferred there; string ``log`` is the implemented path)."""
        raise NotImplementedError("task output channels are not plumbed yet; use log()")


def normalize_outputs(
    result: object, outputs: Mapping[str, OutputSpec]
) -> dict[str, FxtrID | bytes]:
    """Normalize a task fn's return against its declared outputs.

    Implements the single-output bare-return convenience from
    `task_api.TaskFn.__call__`: a non-mapping return from a grid declaring
    exactly one output wraps under that output's name. ``FrameOutput``
    values are type-checked (a ``polars.DataFrame``) and serialized to the
    wire frame form here — *only* serialized: schema validation and
    identity minting are the host's (the declared schema is the host's
    authority; no canonical-encoding code runs task-side).

    Args:
        result: Whatever the user fn returned.
        outputs: The grid's declared outputs.

    Returns:
        One value per declared output name: the ``FxtrID`` for
        ``EntityOutput`` slots, wire frame bytes for ``FrameOutput`` slots.

    Raises:
        TaskOutputError: Bare return with several declared outputs; missing
            or undeclared names; a value of the wrong type for its slot.
    """
    if not isinstance(result, Mapping):
        if len(outputs) != 1:
            raise TaskOutputError(f"bare return but the grid declares outputs {sorted(outputs)}")
        result = {next(iter(outputs)): result}
    if set(result) != set(outputs):
        raise TaskOutputError(f"returned outputs {sorted(result)} != declared {sorted(outputs)}")
    normalized: dict[str, FxtrID | bytes] = {}
    for name, value in result.items():
        spec = outputs[name]
        if isinstance(spec, EntityOutput):
            if not isinstance(value, FxtrID):
                raise TaskOutputError(
                    f"output {name!r} is {type(value).__name__}, not an entity id"
                )
            normalized[name] = value
        elif isinstance(spec, FrameOutput):
            # Lazy import: the sole eager-polars module (frame outputs
            # only — an entity-only task fn never touches polars).
            from fxtr_experiment_task import tables

            if not isinstance(value, tables.pl.DataFrame):
                raise TaskOutputError(
                    f"output {name!r} is {type(value).__name__}, not a polars DataFrame"
                )
            normalized[name] = tables.frame_to_ipc(value)
        else:
            raise ValueError(f"unknown output spec type: {type(spec).__name__}")
    return normalized


class TaskFnAdapter(TypedServiceImpl[WorkflowTaskFnProto]):
    """A user task fn, mounted as the ``WorkflowTaskFnProto`` service.

    This is the task-side half of the boundary: everything the user fn
    touches (views, context) is built here from the pushed wire values,
    and nothing else reaches it. The resolved run-capability proxy is
    passed to views directly — it satisfies the ``ViewBackend`` seam
    structurally.

    Constructing the adapter is exactly what the :func:`task_fn` decorator
    does — ``@task_fn`` over a callable and ``TaskFnAdapter(callable)`` are
    interchangeable. The adapter is itself a :class:`~fxtr_experiment.
    task_api.TaskFn` (see :meth:`__call__`), so a decorated fn behaves like
    the undecorated one when called locally.
    """

    def __init__(self, task_fn: "TaskFnLike") -> None:
        """Wrap ``task_fn`` for mounting (the binder's trust assertion)."""
        super().__init__()
        self._task_fn = task_fn

    def _target_type_annotation(self) -> Any:
        """The proxy type this service resolves as."""
        return WorkflowTaskFnProto

    def _service_type_id(self) -> FxtrID:
        """The adapter service type id (a registrant-controlled
        `define_type` id; the type entity lives in GLOBAL_SCOPE)."""
        return TASK_FN_ADAPTER_TYPE_ID

    @service_method
    async def run_task(
        self,
        capability: ServiceRef[UnknownService],
        args: Mapping[str, ViewGrant],
        internals: ViewGrant,
        outputs: "frozendict[str, OutputSpec]",
    ) -> Mapping[str, FxtrID | bytes]:
        """Execute one run: build views, call the user fn, normalize.

        Contract on `runner_api.WorkflowTaskFnProto.run_task`. Workflow
        errors raised by ops propagate out of this call as their
        ``IRException`` transit form — informational only; raise-time
        poisoning persistence and classification are runner-side (see the
        module docstring).
        """
        # Active scope: the per-call dispatch scope. The typed-service
        # framework decoded `capability` into it, so the ref is resolvable
        # here — the documented service-method exception in
        # v2/notes/scope_discipline.md.
        call_scope = get_active_scope()
        proxy = await capability.replace_service_type(RunCapabilityProto).resolve(call_scope)
        views = {name: build_view(proxy, grant) for name, grant in args.items()}
        # The proxy is the log gateway too (loadable in this dispatch
        # scope — the same one the views run against).
        ctx = _TaskContext(build_view(proxy, internals), proxy)  # type: ignore[arg-type]
        result = await self._task_fn(ctx, **views)  # type: ignore[arg-type]
        return normalize_outputs(result, outputs)

    async def __call__(self, ctx: Any, /, **args: Any) -> Any:
        """Delegate to the wrapped fn — the :class:`~fxtr_experiment.task_api.
        TaskFn` contract, so a decorated fn is interchangeable with the
        undecorated one when called locally.

        Not a ``@service_method``: dunders are excluded from service-method
        discovery (``walk_service_methods(..., forbid_dunders=True)`` walks
        only tagged methods), so this is never exposed on the wire — the
        service surface is :meth:`run_task` alone.
        """
        return await self._task_fn(ctx, **args)


def task_fn(fn: "TaskFnLike") -> TaskFnAdapter:
    """Declare a task fn: ``@task_fn`` over an async ``(ctx, /, **args)`` callable.

    Decorating *is* wrapping in :class:`TaskFnAdapter` — the decorator and the
    constructor are interchangeable.

    Args:
        fn: The task callable (a plain async fn or any object with a matching
            ``__call__``).

    Returns:
        The :class:`TaskFnAdapter` wrapping ``fn`` (mount it behind a handle
        like any other task fn; call it locally like the undecorated fn).
    """
    return TaskFnAdapter(fn)
