"""Task-facing service protocols + workflow error types.

The stable API surface tasks and callers interact with. This module defines only
the protocols and error types; the workflow runner that implements them lives
elsewhere. (See ``plans/phase3/stage15_workflows/`` for the overall design and
the implementation roadmap.)

All capabilities are passed as bare ``ServiceRef``s: a ``TaskStorage`` is a
``ServiceRef[TaskSlotProto]`` and a step context is a
``ServiceRef[TaskStepContextProto]``. ``subslot`` is async (it mints a new
runner-side mount); callers must ``.resolve()`` a returned ref before calling
methods on it.
"""

from __future__ import annotations

from typing import Protocol

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import service_protocol


@service_protocol
class TaskSlotProto(Protocol):
    """A capability over one slot subtree of the realization map.

    Downward-only: run into this slot, or descend to a child slot. No read API,
    no way to walk *up* — holding a slot ref is a capability for that subtree
    only.
    """

    async def run(
        self,
        step_fn: EntityRef[UnknownEntity],
        *args: FxtrServiceData,
        **kwargs: FxtrServiceData,
    ) -> EntityRef[UnknownEntity]:
        """Run ``step_fn`` (a service-handle entity) into this slot with
        ``args`` / ``kwargs``, returning the task result ref (cache hit or
        fresh execution)."""
        ...

    async def subslot(self, key: str) -> ServiceRef[TaskSlotProto]:
        """Return a capability for the child slot ``key`` (a new mount)."""
        ...


@service_protocol
class TaskStepContextProto(Protocol):
    """Per-job capability available *inside* a running task. Deliberately
    distinct from ``TaskSlotProto`` so it can grow (checkpointing, richer
    output) without slot capabilities growing with it."""

    async def subslot(self, key: str) -> ServiceRef[TaskSlotProto]:
        """Return a slot capability under the running job's own slot path."""
        ...

    async def log(self, message: str) -> None:
        """Emit a log line for this task."""
        ...


@service_protocol
class TaskFnProto(Protocol):
    """The service a task function exposes. Authored as a ``TypedServiceImpl``
    (typed in the impl, untyped on the wire); the runner invokes ``run``
    untyped and treats positional args as opaque ``FxtrServiceData``."""

    async def run(
        self,
        step_ctx: ServiceRef[TaskStepContextProto],
        *args: FxtrServiceData,
        **kwargs: FxtrServiceData,
    ) -> EntityRef[UnknownEntity]: ...


@service_protocol
class RunGroupProto(Protocol):
    """Run-group capability for driving the runner over RPC. Defined for the
    eventual RPC surface; the in-process runner instead exposes a run group as a
    plain-python async CM (``WorkflowRunner.run_group`` → ``RunGroupHandle``), so
    this is not mounted as a service in v0."""

    async def get_storage(self, path: str) -> ServiceRef[TaskSlotProto]:
        """Return a slot capability rooted at ``path`` (must be nonempty)."""
        ...


@service_protocol
class WorkflowRunnerProto(Protocol):
    """Top-level runner capability for driving the runner over RPC. Defined for
    the eventual RPC surface; the in-process runner is a plain-python async CM
    and does not mount this in v0."""

    async def start_run_group(self, name: str, description: str) -> ServiceRef[RunGroupProto]: ...


class WorkflowError(Exception):
    """Base class for workflow-runner errors."""


class TaskConflictError(WorkflowError):
    """A run requested a slot that already holds a task with a different cache
    key (a live job or a saved ``Complete``)."""


class TaskFailedError(WorkflowError):
    """An awaited task failed or was poisoned by an observed failure."""

    def __init__(self, slot_path: str, kind: str, reason: str) -> None:
        super().__init__(f"task at {slot_path!r} failed ({kind}): {reason}")
        self.slot_path = slot_path
        self.kind = kind
        self.reason = reason


class WorkflowCycleError(WorkflowError):
    """A run would re-enter a slot already in the requester's chain (a slot
    running, directly or transitively, into itself or an ancestor)."""


class SlotDeletionBlockedError(WorkflowError):
    """Deleting a slot was refused because a retained result still depends on
    it (the coherence invariant forbids removing an observed result)."""
