"""Durable, trace-based task/workflow system on top of fxtr2.

This package currently provides the task-facing protocols + error types
(``task_types``), the realization-map / op-log entities (``entities``), the
cache-key canonicalizer (``cache_key``), the external-only stateless service
provider (``stateless_service_provider``), and a partial ``WorkflowRunner``
(``runner``) — run groups + slot mounting + op-log scaffolding, but not yet task
execution. See ``plans/phase3/stage15_workflows/`` for the design and roadmap.
"""

from __future__ import annotations

from .cache_key import build_invocation, canonicalize, strip_provenance
from .entities import (
    OpLogEvent,
    RunCompleted,
    RunGroup,
    RunGroupClosed,
    RunGroupOpened,
    RunStarted,
    SlotsDeleted,
    WorkflowOutcome,
    WorkflowTaskComplete,
    WorkflowTaskInvocation,
)
from .runner import RunGroupHandle, WorkflowRunner
from .stateless_service_provider import (
    BasicStatelessServiceProvider,
    BasicStatelessServiceProviderProto,
    ExternalConnectorProto,
    ExternalServiceHandle,
    ServiceHandle,
    StatelessServiceSpawnerProto,
    UnknownHandleError,
    spawn_resolved,
    wrap_singleton,
)
from .task_types import (
    RunGroupProto,
    SlotDeletionBlockedError,
    TaskConflictError,
    TaskFailedError,
    TaskFnProto,
    TaskSlotProto,
    TaskStepContextProto,
    WorkflowError,
    WorkflowRunnerProto,
)

__all__ = [
    "BasicStatelessServiceProvider",
    "BasicStatelessServiceProviderProto",
    "ExternalConnectorProto",
    "ExternalServiceHandle",
    "OpLogEvent",
    "RunCompleted",
    "RunGroup",
    "RunGroupClosed",
    "RunGroupHandle",
    "RunGroupOpened",
    "RunGroupProto",
    "RunStarted",
    "ServiceHandle",
    "SlotDeletionBlockedError",
    "SlotsDeleted",
    "StatelessServiceSpawnerProto",
    "TaskConflictError",
    "TaskFailedError",
    "TaskFnProto",
    "TaskSlotProto",
    "TaskStepContextProto",
    "UnknownHandleError",
    "WorkflowError",
    "WorkflowOutcome",
    "WorkflowRunner",
    "WorkflowRunnerProto",
    "WorkflowTaskComplete",
    "WorkflowTaskInvocation",
    "build_invocation",
    "canonicalize",
    "spawn_resolved",
    "strip_provenance",
    "wrap_singleton",
]
