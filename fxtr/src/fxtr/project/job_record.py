"""The job record: everything needed to view, trace, and resume one job, as one entity.

A :class:`JobRecord` is a job's facts from the ledger: its own rows (the job, its invocations
with their nodes, edges, names, and spawns, its step requests and bindings, and the facts of its
job attempts), the rows those refer to (the step results its bindings name, and the facts of the
job attempts that produced them, another job's for a result taken from the cache), and, through
the entity ids in them, every entity those reach. Execution state (leases, attempts, their events
and logs, the step cache) is not part of it. A record moves a job between databases: exported as
a CAR of the record and the entities it reaches (:mod:`fxtr.core.car`), imported as new facts.

The record is flat: one tuple of rows per table, each row a plain dataclass mirroring its table's
columns (see ``fxtr.runner.postgres.schema`` for what each means), sorted by the table's key so
equal facts make an equal record. Ids are uuid strings, timestamps ISO 8601 strings in UTC, node
identities raw cid bytes (a node is stored relationally, not as an entity, so its identity is not
a reference), and JSON columns JSON values. Entity references (results, input values, requests,
code versions) are entity ids, which is what makes the entities part of the record.

**The format is versioned in the type tag and subject to change**: a record moves a job between
databases of the same fxtr version; it is not yet an archive format. A reader refuses any other
tag.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID
from fxtr.core.json import FrozenJSONObject, FrozenJSONValue
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.project.code_version import CodeVersion

JOB_RECORD_TYPE = "org.transluce.fxtr.project.JobRecord.v0.1"
"""The record's type tag, which carries the format's version."""


@dataclasses.dataclass(frozen=True, kw_only=True)
class JobRow:
    """The ``jobs`` row."""

    id: str
    workflow_name: str
    root: str
    created_at: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class JobAttemptProvenanceRow:
    """A ``job_attempt_provenance`` row: a job attempt existed and ran this code."""

    id: str
    job_id: str
    code_version: BoundID[CodeVersion]
    created_at: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class InvocationRow:
    """A ``workflow_invocations`` row."""

    id: str
    pathname: str
    workflow_name: str
    cache_key_prefix: str
    returned_node: bytes | None
    result: BoundID[Array[Any]] | None
    job_attempt_id: str | None
    created_at: str
    finished_at: str | None


@dataclasses.dataclass(frozen=True, kw_only=True)
class NodeRow:
    """A ``nodes`` row."""

    invocation_id: str
    identity: bytes
    kind: str
    params: FrozenJSONObject
    type: FrozenJSONValue
    value: BoundID[Array[Any]] | None
    created_at: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class NodeInputRow:
    """A ``node_inputs`` row: one edge."""

    invocation_id: str
    node_identity: bytes
    label: FrozenJSONValue
    input_identity: bytes


@dataclasses.dataclass(frozen=True, kw_only=True)
class NodeNameRow:
    """A ``node_names`` row."""

    invocation_id: str
    name: str
    identity: bytes


@dataclasses.dataclass(frozen=True, kw_only=True)
class SpawnRow:
    """A ``spawns`` row."""

    invocation_id: str
    node_identity: bytes
    key: FrozenJSONValue
    child_invocation_id: str | None
    step_cache_key: str | None
    created_at: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class JobStepRequestRow:
    """A ``job_step_requests`` row. The request's ``step_requests`` row is not carried: it is
    rebuilt from the request entity."""

    cache_key: str
    request: EntityID
    requested_at: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class StepResultRow:
    """A ``step_results`` row."""

    id: str
    request: EntityID
    result: BoundID[Array[Any]]
    job_attempt_id: str
    completed_at: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class JobStepBindingRow:
    """A ``job_step_bindings`` row."""

    cache_key: str
    step_result_id: str
    bound_at: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class JobRecord(DataclassEntity, fxtr_type=JOB_RECORD_TYPE):
    """One job's facts and the facts they refer to (see the module docstring).

    Attributes:
        job: The job.
        job_attempts: The facts of the job's own job attempts and of the job attempts that
            produced the step results in the record.
        invocations: The job's invocations.
        nodes: Their nodes.
        node_inputs: Their edges.
        node_names: Their names.
        spawns: What their run nodes expanded into.
        step_requests: The job's request at each cache address.
        step_results: The step results its bindings name.
        step_bindings: The step result its request at each key is bound to.
    """

    job: JobRow
    job_attempts: tuple[JobAttemptProvenanceRow, ...]
    invocations: tuple[InvocationRow, ...]
    nodes: tuple[NodeRow, ...]
    node_inputs: tuple[NodeInputRow, ...]
    node_names: tuple[NodeNameRow, ...]
    spawns: tuple[SpawnRow, ...]
    step_requests: tuple[JobStepRequestRow, ...]
    step_results: tuple[StepResultRow, ...]
    step_bindings: tuple[JobStepBindingRow, ...]


__all__ = [
    "JOB_RECORD_TYPE",
    "InvocationRow",
    "JobAttemptProvenanceRow",
    "JobRecord",
    "JobRow",
    "JobStepBindingRow",
    "JobStepRequestRow",
    "NodeInputRow",
    "NodeNameRow",
    "NodeRow",
    "SpawnRow",
    "StepResultRow",
]
