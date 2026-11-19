"""The job record: everything needed to view, trace, and resume one job, as one entity.

A :class:`JobRecord` is a job's facts from the ledger: its own rows (the job, its invocations
with their nodes, edges, names, and spawns, its step requests and bindings, and the facts of its
job attempts), the rows those refer to (the step results its bindings name, and the facts of the
job attempts that produced them, another job's for a result taken from the cache), and, through
the entity ids in them, every entity those reach. Settled execution history accompanies those
facts. Active leases and the step cache do not travel. A record moves a job between databases:
exported as a CAR of the record and the entities it reaches (:mod:`fxtr.core.car`), imported as
facts and settled history.

The record is flat: one tuple of rows per table, each row a plain dataclass mirroring its table's
columns (see ``fxtr.runner.postgres.schema`` for what each means), sorted by the table's key so
equal facts make an equal record. Ids are uuid strings, timestamps ISO 8601 strings in UTC, node
identities raw cid bytes (a node is stored relationally, not as an entity, so its identity is not
a reference), and JSON columns JSON values. Entity references (results, input values, requests,
code versions) are entity ids, which is what makes the entities part of the record.

**The format is versioned in the type tag.** A record is kept after it moves (a hub keeps every
job published to it), so a reader accepts each format in :data:`SUPPORTED_RECORD_FORMATS`: the
current one, :data:`JOB_RECORD_TYPE`, and every earlier one that has an upgrade to the format after
it in ``_UPGRADES``. A change to the record's shape gets a new tag and an upgrade from the previous
one. A reader refuses any other tag.

**Reading a file from someone else.** :func:`read_record` takes :class:`RecordLimits`, so a file
too large to hold is refused while it is read (:mod:`fxtr.core.car`), and checks the record with
:func:`validate_record`, which needs no database: every row is unique by its table's key, the job's
own rows belong to the job, and every row refers only to rows the record holds. Whether the record
agrees with a database is the import's question (``fxtr.runner.postgres.job_records``).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Hashable, Iterable, Mapping
from pathlib import Path
from typing import Any, cast

from fxtr.core import codec
from fxtr.core.car import CarError, CarLimitError, CarLimits, read_car
from fxtr.core.entities import BoundID
from fxtr.core.entity_data import TYPE_KEY, EntityID
from fxtr.core.json import FrozenJSONObject, FrozenJSONValue
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import ConversionError
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.project.code_version import CodeVersion
from fxtr.project.execution_record import ExecutionRecord, validate_execution_record

JOB_RECORD_TYPE = "org.transluce.fxtr.project.JobRecord.v0.2"
"""The record's type tag, which carries the format's version."""

_UPGRADES: Mapping[str, tuple[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]]] = {
    "org.transluce.fxtr.project.JobRecord.v0.1": (
        JOB_RECORD_TYPE,
        lambda data: {**data, TYPE_KEY: JOB_RECORD_TYPE, "execution": None},
    ),
}
"""For each earlier format a reader accepts: the format after it and the function that rewrites
a record's data from one to the other."""

SUPPORTED_RECORD_FORMATS: frozenset[str] = frozenset({JOB_RECORD_TYPE, *_UPGRADES})
"""The record formats :func:`read_record` reads."""


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
        execution: Settled attempts, errors, events, and logs; absent in a v0.1 record file.
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
    execution: ExecutionRecord | None = None


class RecordFileError(ValueError):
    """A file is not a job record this fxtr can import, or it disagrees with the database."""


class RecordLimitError(RecordFileError):
    """A file exceeds one of the :class:`RecordLimits` it was read with."""


@dataclasses.dataclass(frozen=True, kw_only=True)
class RecordLimits:
    """How much of a record file :func:`read_record` accepts. ``None`` means no limit.

    Attributes:
        car: The limits on the file as a CAR.
    """

    car: CarLimits = CarLimits()


MAX_RECORD_FILE_BYTES = 512 << 20
"""The most bytes of a record file an import reads, as stored and as decompressed: what the
release hub accepts of a push.

A file an import reads can come from outside the project (a pull imports what it downloaded), so
what it may hold is bounded before it is read, or it could fill the disk and the memory. The
reader streams the file and keeps every decompressed block in memory, so the decompressed size
sets the memory an import uses; the stored size needs no lower bound of its own. The hub takes
the same bound of a push, so an import reads every record the hub accepted. A larger record
needs a reader and an import that do not hold the whole record in memory."""

IMPORT_LIMITS = RecordLimits(
    car=CarLimits(max_compressed_bytes=MAX_RECORD_FILE_BYTES, max_bytes=MAX_RECORD_FILE_BYTES)
)
"""The limits an import, and so a pull, reads a record file under (see
:data:`MAX_RECORD_FILE_BYTES`)."""


@dataclasses.dataclass(frozen=True)
class RecordFile:
    """A job record read from a file.

    Attributes:
        root: The record's id as the file has it, which names the export.
        record: The record, in the current format.
        blocks: The stored entities its rows reach, by cid: what an import writes.
        format: The record's format as the file has it, before any upgrade.
    """

    root: EntityID
    record: JobRecord
    blocks: Mapping[bytes, bytes]
    format: str = JOB_RECORD_TYPE

    def commits(self) -> list[str]:
        """The commits of the code versions its job attempt facts name, once each."""
        commits: dict[str, None] = {}
        for fact in self.record.job_attempts:
            commits[self.commit_of(fact)] = None
        return list(commits)

    def commit_of(self, fact: JobAttemptProvenanceRow) -> str:
        """The commit of the code version a job attempt fact names.

        Raises:
            KeyError: If the record does not carry the code version, or it names no commit.
            TypeError: If what it names as its commit is not a string.
        """
        data = codec.decode(self.blocks[fact.code_version.entity_id.cid])
        commit = cast("Mapping[str, Any]", data)["commit"]
        if not isinstance(commit, str):
            raise TypeError(f"the commit is a {type(commit).__name__}, not a string")
        return commit


def read_record(path: Path, *, limits: RecordLimits | None = None) -> RecordFile:
    """Read a job record's CAR file (see the module docstring), upgrading a record of an earlier
    supported format to the current one, and check it with :func:`validate_record`.

    Args:
        path: The file.
        limits: What the file may hold; no limits when not given.

    Raises:
        RecordLimitError: If the file exceeds one of ``limits``.
        RecordFileError: If it is not a CAR of one job record of a supported format whose rows
            reach only entities it carries, or the record is not valid.
    """
    limits = limits or RecordLimits()
    try:
        roots, blocks = read_car(path, limits=limits.car)
    except CarLimitError as exc:
        raise RecordLimitError(str(exc)) from exc
    except CarError as exc:
        raise RecordFileError(str(exc)) from exc
    if len(roots) != 1 or roots[0] not in blocks:
        raise RecordFileError(f"{path} does not have exactly one root it carries")
    (root,) = roots
    data = _decode(blocks[root], f"{path}'s root")
    tag = data.get(TYPE_KEY) if isinstance(data, Mapping) else None
    if tag not in SUPPORTED_RECORD_FORMATS:
        raise RecordFileError(
            f"{path}'s root is {tag!r}, not a job record of a format this fxtr reads"
            f" ({', '.join(sorted(SUPPORTED_RECORD_FORMATS))})"
        )
    original_format = cast("str", tag)
    data = _upgrade(cast("Mapping[str, Any]", data))
    try:
        record = DEFAULT_CONVERTER.structure(data, JobRecord)
    except ConversionError as exc:
        raise RecordFileError(f"{path}'s record is malformed: {exc}") from exc
    validate_record(record)
    reached: dict[bytes, bytes] = {}
    frontier = unseen_ids(codec.iter_entity_ids(data), reached)  # type: ignore[arg-type]
    while frontier:
        found: list[EntityID] = []
        for entity_id in frontier:
            block = blocks.get(entity_id.cid)
            if block is None:
                raise RecordFileError(f"{path} lacks {entity_id}, which its record reaches")
            reached[entity_id.cid] = block
            data = _decode(block, f"{path}'s block {entity_id}")
            found.extend(codec.iter_entity_ids(data))  # type: ignore[arg-type]
        frontier = unseen_ids(found, reached)
    file = RecordFile(EntityID(cid=root), record, reached, original_format)
    # The commits go to git as object names and in refspecs, so a record (which can come from a
    # remote) may name only full hex object names: "HEAD" or "*" would mean something else.
    try:
        commits = file.commits()
    except (KeyError, TypeError) as exc:
        raise RecordFileError(f"{path} has a code version that names no commit") from exc
    named = [c for c in commits if not _OBJECT_NAME.fullmatch(c)]
    if named:
        raise RecordFileError(f"{path} names commits that are not git object names: {named[:3]}")
    return file


_OBJECT_NAME = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
"""A full git object name: 40 hex digits (SHA-1), or 64 (SHA-256)."""


def _decode(block: bytes, what: str) -> Any:
    """Decode a block of a record file as entity data.

    A block's bytes match its cid (the CAR reader checked), but anyone can write bytes and
    name them by their hash: bytes that are not DAG-CBOR, or that carry a reference an entity
    may not hold (a storage id), are refused as a record file's fault, not reported as the
    codec's error.

    Raises:
        RecordFileError: If the block is not entity data.
    """
    try:
        return codec.decode(block)
    except ValueError as exc:
        raise RecordFileError(f"{what} is not entity data: {exc}") from exc


def _upgrade(data: Mapping[str, Any]) -> Mapping[str, Any]:
    while (tag := data[TYPE_KEY]) != JOB_RECORD_TYPE:
        _, upgrade = _UPGRADES[tag]
        data = upgrade(data)
    return data


def validate_record(record: JobRecord) -> None:
    """Check a record's rows against each other, with no database (see the module docstring).

    Raises:
        RecordFileError: If two rows of a table share their key; the root invocation is missing
            or runs another workflow than the job; an invocation other than the root has no spawn
            that created it; a row of the job's names another job, or an invocation or node the
            record does not hold (a returned node included); a spawn has no target or two; a
            binding names a step result or a request the record does not hold, or a step result of
            another request; or a job attempt the rows name is not among the record's facts, or a
            fact is neither the job's nor the producer of one of its step results.
    """
    job_id = record.job.id
    if record.execution is not None:
        execution = record.execution
        validate_execution_record(execution)
        if execution.job_id != job_id:
            raise RecordFileError("execution history belongs to another job")
        own_facts = {row.id for row in record.job_attempts if row.job_id == job_id}
        if not {row.id for row in execution.job_attempts} <= own_facts:
            raise RecordFileError("execution history names an unknown job attempt")
        invocation_ids = {row.id for row in record.invocations}
        requests = {row.request.cid for row in record.step_requests}
        for attempt in execution.attempts:
            if attempt.invocation_id is not None and attempt.invocation_id not in invocation_ids:
                raise RecordFileError("execution history names an unknown invocation")
            if attempt.request is not None and attempt.request not in requests:
                raise RecordFileError("execution history names an unknown step request")
    _unique(record.job_attempts, lambda r: r.id, "job attempt fact")
    _unique(record.invocations, lambda r: r.id, "invocation")
    _unique(record.invocations, lambda r: r.pathname, "invocation pathname")
    _unique(record.nodes, lambda r: (r.invocation_id, r.identity), "node")
    _unique(record.node_inputs, lambda r: (r.invocation_id, r.node_identity, r.label), "edge")
    _unique(record.node_names, lambda r: (r.invocation_id, r.name), "node name")
    _unique(record.spawns, lambda r: (r.invocation_id, r.node_identity, r.key), "spawn")
    _unique(record.step_requests, lambda r: r.cache_key, "step request")
    _unique(record.step_results, lambda r: r.id, "step result")
    _unique(record.step_bindings, lambda r: r.cache_key, "step binding")

    invocations = {row.id for row in record.invocations}
    nodes = {(row.invocation_id, row.identity) for row in record.nodes}
    for row in record.nodes:
        _require(row.invocation_id in invocations, f"node of invocation {row.invocation_id}")
    # A job is created with its root invocation, at the job's root pathname and of its workflow;
    # every other invocation is created by expanding a run node, so a spawn names it.
    roots = [row for row in record.invocations if row.pathname == record.job.root]
    _require(bool(roots), f"the root invocation {record.job.root!r}")
    if roots[0].workflow_name != record.job.workflow_name:
        raise RecordFileError(
            f"the root invocation runs {roots[0].workflow_name!r}, but the job runs"
            f" {record.job.workflow_name!r}"
        )
    for row in record.invocations:
        if row.returned_node is not None:
            _require((row.id, row.returned_node) in nodes, f"the node invocation {row.id} returned")
    spawned = {row.child_invocation_id for row in record.spawns}
    for row in record.invocations:
        if row.pathname != record.job.root:
            _require(row.id in spawned, f"a spawn that created invocation {row.pathname!r}")
    for row in record.node_inputs:
        _require((row.invocation_id, row.node_identity) in nodes, "an edge of a node")
        _require((row.invocation_id, row.input_identity) in nodes, "an edge's input node")
    for row in record.node_names:
        _require((row.invocation_id, row.identity) in nodes, f"the node named {row.name!r}")
    requests = {row.cache_key: row.request for row in record.step_requests}
    for row in record.spawns:
        _require((row.invocation_id, row.node_identity) in nodes, "a spawn's run node")
        if (row.child_invocation_id is None) == (row.step_cache_key is None):
            raise RecordFileError("a spawn must have exactly one target: an invocation or a step")
        if row.child_invocation_id is not None:
            _require(row.child_invocation_id in invocations, "a spawn's child invocation")
        if row.step_cache_key is not None:
            _require(row.step_cache_key in requests, f"the request at {row.step_cache_key!r}")
    results = {row.id: row for row in record.step_results}
    for row in record.step_bindings:
        result = results.get(row.step_result_id)
        _require(result is not None, f"the step result bound at {row.cache_key!r}")
        request = requests.get(row.cache_key)
        _require(request is not None, f"the request at {row.cache_key!r}")
        if cast("StepResultRow", result).request != request:
            raise RecordFileError(
                f"the binding at {row.cache_key!r} names a step result of another request"
            )

    facts = {row.id: row for row in record.job_attempts}
    producers = {row.job_attempt_id for row in record.step_results}
    for fact in record.job_attempts:
        if fact.job_id != job_id and fact.id not in producers:
            raise RecordFileError(
                f"job attempt fact {fact.id} is of another job and produced none of the record's"
                " step results"
            )
    for producer in producers:
        _require(producer in facts, f"job attempt fact {producer}")
    for row in record.invocations:
        if row.job_attempt_id is not None:
            fact = facts.get(row.job_attempt_id)
            _require(fact is not None, f"job attempt fact {row.job_attempt_id}")
            if cast("JobAttemptProvenanceRow", fact).job_id != job_id:
                raise RecordFileError(
                    f"invocation {row.id}'s result was produced by another job's job attempt"
                )


def _unique[T](rows: Iterable[T], key: Callable[[T], Hashable], what: str) -> None:
    seen: set[Hashable] = set()
    for row in rows:
        value = key(row)
        if value in seen:
            raise RecordFileError(f"the record has two rows for one {what} ({value!r})")
        seen.add(value)


def _require(present: bool, what: str) -> None:
    if not present:
        raise RecordFileError(f"the record does not hold {what}, which one of its rows names")


def unseen_ids(references: Iterable[EntityID], seen: Mapping[bytes, Any]) -> list[EntityID]:
    """Return the ids in ``references`` whose cids are not keys of ``seen``, each one time."""
    return list(dict.fromkeys(r for r in references if r.cid not in seen))


__all__ = [
    "IMPORT_LIMITS",
    "JOB_RECORD_TYPE",
    "MAX_RECORD_FILE_BYTES",
    "SUPPORTED_RECORD_FORMATS",
    "RecordFile",
    "RecordFileError",
    "RecordLimitError",
    "RecordLimits",
    "unseen_ids",
    "read_record",
    "validate_record",
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
