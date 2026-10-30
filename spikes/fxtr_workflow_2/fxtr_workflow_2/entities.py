"""Realization-map and op-log entities.

The workflow runner persists two kinds of durable state:

- The **realization map** holds successful results only: each slot's pin is a
  :class:`WorkflowTaskComplete` (keyed by the slot path). Its cache key is the
  id of the :class:`WorkflowTaskInvocation` it completed.
- The **op log** is an append-only history of operations: run groups
  opening/closing, jobs launched, results recorded (success or failure),
  observations of existing results, and slot deletions.

The invocation's ``args`` / ``kwargs`` are stored as ``FrozenFxtrCbor`` (the
canonicalized arg tree — see :mod:`fxtr_workflow_2.cache_key`); entity refs ride
as ``RawWeakFxtrID`` leaves inside that tree, so they are dep-tracked (their
closures travel with the invocation) and loadable, mirroring
``SandboxProvenanceContext.config_snapshot``.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import (
    ChildFxtrID,
    ChildRef,
    DataclassEntity,
    EntityRef,
)
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FrozenFxtrCbor
from fxtr2.data.system_entities import define_type

from ._ns import WORKFLOWS_NS

RUN_GROUP_TYPE_ID = define_type(WORKFLOWS_NS, "run_group")
INVOCATION_TYPE_ID = define_type(WORKFLOWS_NS, "workflow_task_invocation")
COMPLETE_TYPE_ID = define_type(WORKFLOWS_NS, "workflow_task_complete")
STORAGE_SLOT_PLACEHOLDER_TYPE_ID = define_type(WORKFLOWS_NS, "storage_slot_placeholder")
RUN_GROUP_OPENED_TYPE_ID = define_type(WORKFLOWS_NS, "oplog/run_group_opened")
RUN_GROUP_CLOSED_TYPE_ID = define_type(WORKFLOWS_NS, "oplog/run_group_closed")
RUN_STARTED_TYPE_ID = define_type(WORKFLOWS_NS, "oplog/run_started")
RUN_COMPLETED_TYPE_ID = define_type(WORKFLOWS_NS, "oplog/run_completed")
SLOTS_DELETED_TYPE_ID = define_type(WORKFLOWS_NS, "oplog/slots_deleted")

#: Outcome recorded when a run resolves. ``success`` is the only one that yields
#: a realization-map pin; the rest are failures recorded only in the op log.
WorkflowOutcome = Literal[
    "success",
    "failed",
    "conflict",
    "child_incomplete",
    "child_conflict",
    "interrupted",
    "cancelled",
]


@dataclasses.dataclass(frozen=True)
class RunGroup(DataclassEntity, fxtr_type=RUN_GROUP_TYPE_ID):
    """Identity of a group of runs launched together (op-log grouping only).

    ``created_at`` makes otherwise-identical groups distinct content-addressed
    entities.
    """

    name: str
    """Human-facing run-group name."""
    description: str
    """Human-facing description of what this group of runs is for."""
    created_at: float
    """Unix timestamp (seconds) when the group was opened; distinguishes
    otherwise-identical groups."""


@dataclasses.dataclass(frozen=True)
class WorkflowTaskInvocation(DataclassEntity, fxtr_type=INVOCATION_TYPE_ID):
    """A task function bound to its arguments. Its content-addressed id is the
    cache key for the slot it runs into.

    ``args`` / ``kwargs`` are the canonicalized arg tree (entity refs as
    ``RawWeakFxtrID`` leaves, provenance stripped) — built by
    :func:`fxtr_workflow_2.cache_key.build_invocation`.
    """

    step_fn: ChildRef[UnknownEntity]
    """The task function's service-handle entity (provenance stripped). Strong
    child so the handle's closure stays inspectable in the map."""
    args: FrozenFxtrCbor
    """Canonicalized positional args: a ``FrozenFxtrCbor`` tuple with entity refs
    as bare ``RawWeakFxtrID`` leaves (provenance stripped)."""
    kwargs: FrozenFxtrCbor
    """Canonicalized keyword args: a ``FrozenFxtrCbor`` mapping (str keys), same
    leaf treatment as ``args``."""


@dataclasses.dataclass(frozen=True)
class WorkflowTaskStorageSlotPlaceholder(
    DataclassEntity, fxtr_type=STORAGE_SLOT_PLACEHOLDER_TYPE_ID
):
    """Cache-key stand-in for a ``TaskStorage`` slot passed as a task arg.

    A live slot ``ServiceRef`` has no stable content identity, but its
    root-relative slot ``path`` does. The runner replaces a slot arg with one of
    these (referenced from the invocation's ``args``) so two calls passing "the
    slot at this path" cache-key identically. Introspectable (load it, read
    ``path``). (Followup: reject a *forged* placeholder passed as a real arg.)
    """

    path: str
    """The slot's root-relative path."""


@dataclasses.dataclass(frozen=True)
class WorkflowTaskComplete(DataclassEntity, fxtr_type=COMPLETE_TYPE_ID):
    """A successful result pinned in the realization map for one slot.

    ``result`` is a provenance-layer node (strong-ref'd so its closure travels).
    ``depends_on`` maps each observed slot's root-relative path to the
    ``WorkflowTaskComplete`` value this task observed there; the coherence
    invariant requires every entry to remain pinned while this pin remains.
    """

    invocation: ChildRef[WorkflowTaskInvocation]
    """The invocation this result completed; its id is the slot's cache key."""
    result: ChildFxtrID
    """The result as a provenance-layer node (strong child so its closure
    travels with the pin)."""
    depends_on: frozendict[str, EntityRef[WorkflowTaskComplete]]
    """Root-relative slot path -> the ``WorkflowTaskComplete`` value this task
    observed there. The coherence invariant requires every entry to remain
    pinned (unchanged) while this pin remains. (Always empty until nesting.)"""


@dataclasses.dataclass(frozen=True)
class RunGroupOpened(DataclassEntity, fxtr_type=RUN_GROUP_OPENED_TYPE_ID):
    """Op-log event: a run group was opened."""

    run_group: ChildRef[RunGroup]
    """The run group that opened."""
    owner: str
    """Identifier of the runner that recorded this event."""
    timestamp: float
    """Unix timestamp (seconds) when the event was recorded."""


@dataclasses.dataclass(frozen=True)
class RunGroupClosed(DataclassEntity, fxtr_type=RUN_GROUP_CLOSED_TYPE_ID):
    """Op-log event: a run group was closed."""

    run_group: ChildRef[RunGroup]
    """The run group that closed."""
    owner: str
    """Identifier of the runner that recorded this event."""
    timestamp: float
    """Unix timestamp (seconds) when the event was recorded."""


@dataclasses.dataclass(frozen=True)
class RunStarted(DataclassEntity, fxtr_type=RUN_STARTED_TYPE_ID):
    """Op-log event: a run group began a ``run`` into ``slot_path`` (before
    waiting on its outcome).

    Emitted once per ``run`` request (so concurrent groups racing one slot each
    record their own started/completed pair). ``launched_job`` is True iff *this*
    request started the physical job execution, False if it joined an in-flight
    job or observed an already-complete result.
    """

    run_group: ChildRef[RunGroup]
    """The run group this request was made under."""
    slot_path: str
    """Root-relative slot path the run targeted."""
    invocation: ChildRef[WorkflowTaskInvocation]
    """The invocation (cache key) this request resolved to."""
    launched_job: bool
    """True iff *this* request started the physical job execution; False if it
    joined an in-flight job or observed an already-complete result."""
    owner: str
    """Identifier of the runner that recorded this event."""
    timestamp: float
    """Unix timestamp (seconds) when the event was recorded."""


@dataclasses.dataclass(frozen=True)
class RunCompleted(DataclassEntity, fxtr_type=RUN_COMPLETED_TYPE_ID):
    """Op-log event: a run group's ``run`` resolved. ``outcome`` distinguishes
    success from the failure kinds; ``result`` is set only on success, ``reason``
    only on a failure/conflict/interruption.

    Emitted once per ``run`` request (paired with a ``RunStarted``), so every
    waiter on a shared job records its own outcome even if the job fails or is
    cancelled.
    """

    run_group: ChildRef[RunGroup]
    """The run group this request was made under."""
    slot_path: str
    """Root-relative slot path the run targeted."""
    invocation: ChildRef[WorkflowTaskInvocation]
    """The invocation (cache key) this request resolved to."""
    outcome: WorkflowOutcome
    """How the run resolved (``success`` / ``failed`` / ``conflict`` /
    ``interrupted`` / ...)."""
    result: ChildFxtrID | None
    """The provenance-layer result node on ``success``; None otherwise."""
    reason: str | None
    """Diagnostic detail on a failure/conflict/interruption (e.g. the full
    traceback for ``failed``); None on ``success``."""
    owner: str
    """Identifier of the runner that recorded this event."""
    timestamp: float
    """Unix timestamp (seconds) when the event was recorded."""


@dataclasses.dataclass(frozen=True)
class SlotsDeleted(DataclassEntity, fxtr_type=SLOTS_DELETED_TYPE_ID):
    """Op-log event: realization-map slots were deleted."""

    run_group: EntityRef[RunGroup] | None
    """The run group the deletion was performed under, if any (deletion may be
    a runner-level operation outside any group)."""
    paths: tuple[str, ...]
    """The root-relative slot paths that were deleted."""
    owner: str
    """Identifier of the runner that recorded this event."""
    timestamp: float
    """Unix timestamp (seconds) when the event was recorded."""


#: Convenience union of the op-log event entity types (each is pinned
#: independently in the op log; this alias is for typing, not a stored field).
OpLogEvent = RunGroupOpened | RunGroupClosed | RunStarted | RunCompleted | SlotsDeleted
