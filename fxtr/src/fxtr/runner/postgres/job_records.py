"""Job records: building a job's :class:`~fxtr.project.job_record.JobRecord` from the ledger,
exporting it with the entities it reaches as a CAR file, and importing one.

What a record holds is the ledger's rule made concrete (see :mod:`fxtr.runner.postgres.schema`): a
ledger row refers only to ledger rows and entities, so a job's rows (every row keyed by the job or
by one of its invocations), the rows they refer to (the step results its bindings name and the job
attempt facts those and its own rows name), and the entities all of those reach are the whole
record. :func:`build_record` reads them in the caller's transaction, which should be a snapshot so
the record is one consistent view; :func:`export_job` collects the entities and writes the file.

Importing is the reverse, in two halves. :func:`read_record` reads a file into a
:class:`RecordFile`: the record, and the blocks its rows reach, walked from the rows (so the
record's own block is never kept, and a block nothing reaches is ignored); a reached entity with no
block, a root that is not one record of this format, or a block that does not match its cid refuses
the file. :func:`insert_record` then writes it into the database in the caller's transaction: the
entities, then every row through one merge (``_merge``), which inserts the rows the database lacks
and compares the ones it has, a difference refusing the import. The facts the job shares with other
jobs (job attempt facts, step requests, step results) are merged by key alone. The job's own rows
are merged against all the database has of the job, which for a new job is nothing; for a job it
has, the import is an *update*: the record must hold each job attempt fact and each row the database
has of the job, and the invocation columns that are set as a job gets further are set where the
database has them null. Nothing is ever removed.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import ColumnElement, Table, select, update
from sqlalchemy.dialects.postgresql import insert

from fxtr.core import codec
from fxtr.core.car import CarError, read_car, write_car
from fxtr.core.entities import BoundID
from fxtr.core.entity_data import (
    TYPE_KEY,
    EntityID,
    freeze_fxtr_data,
    get_entity_type_tag,
    thaw_fxtr_data,
)
from fxtr.core.json import FrozenJSONObject, FrozenJSONValue
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import ConversionError
from fxtr.project.code_version import CodeVersion
from fxtr.project.job_record import (
    JOB_RECORD_TYPE,
    InvocationRow,
    JobAttemptProvenanceRow,
    JobRecord,
    JobRow,
    JobStepBindingRow,
    JobStepRequestRow,
    NodeInputRow,
    NodeNameRow,
    NodeRow,
    SpawnRow,
    StepResultRow,
)
from fxtr.runner.postgres.database import Transaction
from fxtr.runner.postgres.entities import PostgresEntityStorage
from fxtr.runner.postgres.ledger import JobID, NotFoundError
from fxtr.runner.postgres.schema import (
    entities,
    job_attempt_provenance,
    job_step_bindings,
    job_step_requests,
    jobs,
    node_inputs,
    node_names,
    nodes,
    spawns,
    step_requests,
    step_results,
    workflow_invocations,
)
from fxtr.runner.steps import StepRequest


async def build_record(tx: Transaction, job_id: JobID) -> JobRecord:
    """The job's record, read in ``tx``.

    Raises:
        NotFoundError: If there is no such job.
    """
    job = (await tx.execute(select(jobs).where(jobs.c.id == job_id))).first()
    if job is None:
        raise NotFoundError(f"No job {job_id}")
    invocation_ids = select(workflow_invocations.c.id).where(
        workflow_invocations.c.job_id == job_id
    )
    invocations = (
        await tx.execute(
            select(workflow_invocations).where(workflow_invocations.c.job_id == job_id)
        )
    ).all()
    node_rows = (
        await tx.execute(select(nodes).where(nodes.c.invocation_id.in_(invocation_ids)))
    ).all()
    edge_rows = (
        await tx.execute(select(node_inputs).where(node_inputs.c.invocation_id.in_(invocation_ids)))
    ).all()
    name_rows = (
        await tx.execute(select(node_names).where(node_names.c.invocation_id.in_(invocation_ids)))
    ).all()
    spawn_rows = (
        await tx.execute(select(spawns).where(spawns.c.invocation_id.in_(invocation_ids)))
    ).all()
    request_rows = (
        await tx.execute(select(job_step_requests).where(job_step_requests.c.job_id == job_id))
    ).all()
    binding_rows = (
        await tx.execute(select(job_step_bindings).where(job_step_bindings.c.job_id == job_id))
    ).all()
    result_rows = (
        await tx.execute(
            select(step_results).where(
                step_results.c.id.in_([row.step_result_id for row in binding_rows])
            )
        )
    ).all()
    # The job's own job attempts, and the producers its results name.
    producers = {row.job_attempt_id for row in result_rows} | {
        row.job_attempt_id for row in invocations if row.job_attempt_id is not None
    }
    attempt_rows = (
        await tx.execute(
            select(job_attempt_provenance).where(
                (job_attempt_provenance.c.job_id == job_id)
                | job_attempt_provenance.c.id.in_(producers)
            )
        )
    ).all()
    return JobRecord(
        job=JobRow(
            id=str(job.id),
            workflow_name=job.workflow_name,
            root=job.root,
            created_at=_time(job.created_at),
        ),
        job_attempts=_sorted(
            (
                JobAttemptProvenanceRow(
                    id=str(row.id),
                    job_id=str(row.job_id),
                    code_version=_ref(row.code_version_cid).bind_type(CodeVersion),
                    created_at=_time(row.created_at),
                )
                for row in attempt_rows
            ),
            lambda r: (r.created_at, r.id),
        ),
        invocations=_sorted(
            (
                InvocationRow(
                    id=str(row.id),
                    pathname=row.pathname,
                    workflow_name=row.workflow_name,
                    cache_key_prefix=row.cache_key_prefix,
                    returned_node=_bytes(row.returned_node),
                    result=_array(row.result_cid),
                    job_attempt_id=_str(row.job_attempt_id),
                    created_at=_time(row.created_at),
                    finished_at=None if row.finished_at is None else _time(row.finished_at),
                )
                for row in invocations
            ),
            lambda r: r.pathname,
        ),
        nodes=_sorted(
            (
                NodeRow(
                    invocation_id=str(row.invocation_id),
                    identity=bytes(row.identity),
                    kind=row.kind,
                    params=cast("FrozenJSONObject", _json(row.params)),
                    type=_json(row.type),
                    value=_array(row.value_cid),
                    created_at=_time(row.created_at),
                )
                for row in node_rows
            ),
            lambda r: (r.invocation_id, r.identity),
        ),
        node_inputs=_sorted(
            (
                NodeInputRow(
                    invocation_id=str(row.invocation_id),
                    node_identity=bytes(row.node_identity),
                    label=_json(row.label),
                    input_identity=bytes(row.input_identity),
                )
                for row in edge_rows
            ),
            lambda r: (r.invocation_id, r.node_identity, codec.encode(r.label)),
        ),
        node_names=_sorted(
            (
                NodeNameRow(
                    invocation_id=str(row.invocation_id),
                    name=row.name,
                    identity=bytes(row.identity),
                )
                for row in name_rows
            ),
            lambda r: (r.invocation_id, r.name),
        ),
        spawns=_sorted(
            (
                SpawnRow(
                    invocation_id=str(row.invocation_id),
                    node_identity=bytes(row.node_identity),
                    key=_json(row.key),
                    child_invocation_id=_str(row.child_invocation_id),
                    step_cache_key=row.step_cache_key,
                    created_at=_time(row.created_at),
                )
                for row in spawn_rows
            ),
            lambda r: (r.invocation_id, r.node_identity, codec.encode(r.key)),
        ),
        step_requests=_sorted(
            (
                JobStepRequestRow(
                    cache_key=row.cache_key,
                    request=_ref(row.request_cid),
                    requested_at=_time(row.requested_at),
                )
                for row in request_rows
            ),
            lambda r: r.cache_key,
        ),
        step_results=_sorted(
            (
                StepResultRow(
                    id=str(row.id),
                    request=_ref(row.request_cid),
                    result=_ref(row.result_cid).bind_type(Array[Any]),
                    job_attempt_id=str(row.job_attempt_id),
                    completed_at=_time(row.completed_at),
                )
                for row in result_rows
            ),
            lambda r: r.id,
        ),
        step_bindings=_sorted(
            (
                JobStepBindingRow(
                    cache_key=row.cache_key,
                    step_result_id=str(row.step_result_id),
                    bound_at=_time(row.bound_at),
                )
                for row in binding_rows
            ),
            lambda r: r.cache_key,
        ),
    )


async def export_job(
    tx: Transaction, entities: PostgresEntityStorage, job_id: JobID, path: Path
) -> EntityID:
    """Write the job's record and every entity it reaches to ``path`` as a gzipped CAR whose root
    is the record, reading in ``tx`` (a snapshot, for one consistent view). The record itself is
    encoded for the file and not stored. Returns the record's id, which names the export.

    Raises:
        NotFoundError: If there is no such job.
    """
    record = await build_record(tx, job_id)
    data = DEFAULT_CONVERTER.unstructure_entity(record)
    encoded = codec.encode(data)
    root = codec.compute_cid(encoded)
    blocks = await reachable_blocks(tx, entities, codec.iter_entity_ids(data))
    write_car(path, [root], [(root, encoded), *blocks])
    return EntityID(cid=root)


async def reachable_blocks(
    tx: Transaction, entities: PostgresEntityStorage, references: Iterable[EntityID]
) -> list[tuple[bytes, bytes]]:
    """Every stored entity ``references`` reach, transitively, as ``(cid, bytes)`` blocks in the
    order first reached.

    Raises:
        NotFoundError: If a reached entity is not stored.
    """
    blocks: dict[bytes, bytes] = {}
    frontier = _unseen_ids(references, blocks)
    while frontier:
        loaded = await entities.load_raw_bytes_many(frontier, tx)
        found: list[EntityID] = []
        for entity_id, raw in zip(frontier, loaded, strict=True):
            if raw is None:
                raise NotFoundError(f"Entity {entity_id} is referenced but not stored")
            data, _ = raw
            blocks[entity_id.cid] = data
            found.extend(codec.iter_entity_ids(codec.decode(data)))  # type: ignore[arg-type]
        frontier = _unseen_ids(found, blocks)
    return list(blocks.items())


class RecordFileError(ValueError):
    """A file is not a job record this fxtr can import, or it disagrees with the database."""


@dataclasses.dataclass(frozen=True)
class RecordFile:
    """A job record read from a file.

    Attributes:
        root: The record's id, which names the export.
        record: The record.
        blocks: The stored entities its rows reach, by cid: what an import writes.
    """

    root: EntityID
    record: JobRecord
    blocks: Mapping[bytes, bytes]

    def commits(self) -> list[str]:
        """The commits of the code versions its job attempt facts name, once each."""
        commits: dict[str, None] = {}
        for fact in self.record.job_attempts:
            data = codec.decode(self.blocks[fact.code_version.entity_id.cid])
            commits[cast("Mapping[str, Any]", data)["commit"]] = None
        return list(commits)


def read_record(path: Path) -> RecordFile:
    """Read a job record's CAR file (see the module docstring).

    Raises:
        RecordFileError: If it is not a CAR of one job record of this format whose rows reach
            only entities it carries.
    """
    try:
        roots, blocks = read_car(path)
    except CarError as exc:
        raise RecordFileError(str(exc)) from exc
    if len(roots) != 1 or roots[0] not in blocks:
        raise RecordFileError(f"{path} does not have exactly one root it carries")
    (root,) = roots
    data = codec.decode(blocks[root])
    tag = data.get(TYPE_KEY) if isinstance(data, Mapping) else None
    if tag != JOB_RECORD_TYPE:
        raise RecordFileError(f"{path}'s root is {tag!r}, not a job record ({JOB_RECORD_TYPE})")
    try:
        record = DEFAULT_CONVERTER.structure(data, JobRecord)
    except ConversionError as exc:
        raise RecordFileError(f"{path}'s record is malformed: {exc}") from exc
    reached: dict[bytes, bytes] = {}
    frontier = _unseen_ids(codec.iter_entity_ids(data), reached)  # type: ignore[arg-type]
    while frontier:
        found: list[EntityID] = []
        for entity_id in frontier:
            block = blocks.get(entity_id.cid)
            if block is None:
                raise RecordFileError(f"{path} lacks {entity_id}, which its record reaches")
            reached[entity_id.cid] = block
            found.extend(codec.iter_entity_ids(codec.decode(block)))  # type: ignore[arg-type]
        frontier = _unseen_ids(found, reached)
    return RecordFile(EntityID(cid=root), record, reached)


async def lock_job(tx: Transaction, job_id: JobID) -> bool:
    """Take the job's own row for the rest of the transaction, if the database has the job, and
    say whether it does. A resume takes the same row first, so an update of the job from a record
    serializes with anything that could start running it."""
    taken = await tx.execute(
        update(jobs).where(jobs.c.id == job_id).values(workflow_name=jobs.c.workflow_name)
    )
    return taken.rowcount > 0


async def insert_record(tx: Transaction, file: RecordFile) -> JobID:
    """Write a record read from a file into the database, in ``tx`` (see the module docstring),
    and return its job's id. When the database has the job already, this *extends* it: the record
    must hold every job attempt fact and every ledger row the database has for the job, and agree
    with each, beyond the columns an invocation sets as it gets further; what the record adds is
    inserted or set, and nothing is removed. The caller refuses a job a process holds.

    Raises:
        RecordFileError: If a fact the record shares with the database differs from it, or the
            database has a job attempt or a row of the job that the record lacks.
    """
    record = file.record
    job_id = JobID(uuid.UUID(record.job.id))
    local_attempts = {
        row.id
        for row in (
            await tx.execute(
                select(job_attempt_provenance.c.id).where(job_attempt_provenance.c.job_id == job_id)
            )
        ).all()
    }
    unknown = local_attempts - {uuid.UUID(row.id) for row in record.job_attempts}
    if unknown:
        raise RecordFileError(
            f"this database knows job attempts of job {job_id} the record lacks"
            f" ({', '.join(sorted(map(str, unknown)))}): the record is older than what is here, or"
            " the job has diverged from it here"
        )
    for cid, data in file.blocks.items():
        await tx.execute(
            insert(entities)
            .values(cid=cid, type_tag=get_entity_type_tag(codec.decode(data)), data=data)
            .on_conflict_do_nothing()
        )
    await _merge(
        tx,
        job_attempt_provenance,
        [
            {
                "id": uuid.UUID(row.id),
                "job_id": uuid.UUID(row.job_id),
                "code_version_cid": row.code_version.entity_id.cid,
                "created_at": _parse(row.created_at),
            }
            for row in record.job_attempts
        ],
    )
    requests = {row.request.cid for row in record.step_requests} | {
        row.request.cid for row in record.step_results
    }
    await _merge(
        tx,
        step_requests,
        [
            {
                "request_cid": cid,
                "step_name": DEFAULT_CONVERTER.structure(
                    codec.decode(file.blocks[cid]), StepRequest
                ).step_name,
            }
            for cid in sorted(requests)
        ],
    )
    await _merge(
        tx,
        step_results,
        [
            {
                "id": uuid.UUID(row.id),
                "request_cid": row.request.cid,
                "result_cid": row.result.entity_id.cid,
                "job_attempt_id": uuid.UUID(row.job_attempt_id),
                "completed_at": _parse(row.completed_at),
            }
            for row in record.step_results
        ],
    )
    invocation_ids = select(workflow_invocations.c.id).where(
        workflow_invocations.c.job_id == job_id
    )
    await _merge(
        tx,
        jobs,
        [
            {
                "id": job_id,
                "workflow_name": record.job.workflow_name,
                "root": record.job.root,
                "created_at": _parse(record.job.created_at),
            }
        ],
        local=jobs.c.id == job_id,
    )
    await _merge(
        tx,
        workflow_invocations,
        [
            {
                "id": uuid.UUID(row.id),
                "job_id": job_id,
                "pathname": row.pathname,
                "workflow_name": row.workflow_name,
                "cache_key_prefix": row.cache_key_prefix,
                "returned_node": row.returned_node,
                "result_cid": None if row.result is None else row.result.entity_id.cid,
                "job_attempt_id": None
                if row.job_attempt_id is None
                else uuid.UUID(row.job_attempt_id),
                "created_at": _parse(row.created_at),
                "finished_at": None if row.finished_at is None else _parse(row.finished_at),
            }
            for row in record.invocations
        ],
        local=workflow_invocations.c.job_id == job_id,
        monotonic=("returned_node", "result_cid", "job_attempt_id", "finished_at"),
    )
    await _merge(
        tx,
        nodes,
        [
            {
                "invocation_id": uuid.UUID(row.invocation_id),
                "identity": row.identity,
                "kind": row.kind,
                "params": thaw_fxtr_data(row.params),
                "type": thaw_fxtr_data(row.type),
                "value_cid": None if row.value is None else row.value.entity_id.cid,
                "created_at": _parse(row.created_at),
            }
            for row in record.nodes
        ],
        local=nodes.c.invocation_id.in_(invocation_ids),
    )
    await _merge(
        tx,
        node_inputs,
        [
            {
                "invocation_id": uuid.UUID(row.invocation_id),
                "node_identity": row.node_identity,
                "label": thaw_fxtr_data(row.label),
                "input_identity": row.input_identity,
            }
            for row in record.node_inputs
        ],
        local=node_inputs.c.invocation_id.in_(invocation_ids),
    )
    await _merge(
        tx,
        node_names,
        [
            {
                "invocation_id": uuid.UUID(row.invocation_id),
                "name": row.name,
                "identity": row.identity,
            }
            for row in record.node_names
        ],
        local=node_names.c.invocation_id.in_(invocation_ids),
    )
    await _merge(
        tx,
        spawns,
        [
            {
                "invocation_id": uuid.UUID(row.invocation_id),
                "node_identity": row.node_identity,
                "key": thaw_fxtr_data(row.key),
                "child_invocation_id": None
                if row.child_invocation_id is None
                else uuid.UUID(row.child_invocation_id),
                "step_cache_key": row.step_cache_key,
                "created_at": _parse(row.created_at),
            }
            for row in record.spawns
        ],
        local=spawns.c.invocation_id.in_(invocation_ids),
    )
    await _merge(
        tx,
        job_step_requests,
        [
            {
                "job_id": job_id,
                "cache_key": row.cache_key,
                "request_cid": row.request.cid,
                "requested_at": _parse(row.requested_at),
            }
            for row in record.step_requests
        ],
        local=job_step_requests.c.job_id == job_id,
    )
    await _merge(
        tx,
        job_step_bindings,
        [
            {
                "job_id": job_id,
                "cache_key": row.cache_key,
                "step_result_id": uuid.UUID(row.step_result_id),
                "bound_at": _parse(row.bound_at),
            }
            for row in record.step_bindings
        ],
        local=job_step_bindings.c.job_id == job_id,
    )
    return job_id


async def _insert(tx: Transaction, table: Table, rows: list[dict[str, Any]]) -> None:
    if rows:
        await tx.execute(insert(table), rows)


async def _merge(
    tx: Transaction,
    table: Table,
    rows: list[dict[str, Any]],
    *,
    local: ColumnElement[bool] | None = None,
    monotonic: Sequence[str] = (),
) -> None:
    """Merge the record's rows of one table into that table.

    This function matches each row to a database row by the table's primary key. If the database
    does not have the row, this function inserts it. If the database has the row, this function
    compares each column. If a column is different, this function refuses the merge.

    Args:
        tx: The transaction to write in.
        table: The table.
        rows: The record's rows of the table. Each value has the table's Python type.
        local: The condition that selects the database's rows of the record's job in this table.
            Use it for the job's own rows. Each database row that it selects must also be in
            ``rows``. If one is not, the database knows something about the job that the record
            does not, and this function refuses the merge. Use ``None`` for facts that the job
            shares with other jobs (job attempt facts, step requests, step results). These rows
            must have a primary key of one column. The database can have more of these rows than
            the record.
        monotonic: The columns that a row sets one time, when its job gets further (an
            invocation's result and the columns that go with it). If such a column is null in the
            database and set in ``rows``, this function sets it in the database. If it is set in
            the database and has a different value in ``rows``, this function refuses the merge.

    Raises:
        RecordFileError: If a row has a different value, or if ``local`` selects a database row
            that is not in ``rows``.
    """
    key = [column.name for column in table.primary_key.columns]
    if local is None:
        (single,) = key
        condition: ColumnElement[bool] = table.c[single].in_([row[single] for row in rows])
    else:
        condition = local
    existing = (
        {
            tuple(_normal(found._mapping[c]) for c in key): found
            for found in (await tx.execute(select(table).where(condition))).all()
        }
        if rows or local is not None
        else {}
    )
    new: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        row_key = tuple(_normal(row[c]) for c in key)
        seen.add(row_key)
        found = existing.get(row_key)
        if found is None:
            new.append(row)
            continue
        updates: dict[str, Any] = {}
        for column in row:
            here, there = _normal(found._mapping[column]), _normal(row[column])
            if here == there:
                continue
            if column in monotonic and here is None:
                updates[column] = row[column]
                continue
            raise RecordFileError(
                f"{table.name} {_show(row_key)} differs from this database's ({column}); the"
                " record and this database disagree about a fact they share"
            )
        if updates:
            await tx.execute(
                update(table).where(*(table.c[c] == row[c] for c in key)).values(**updates)
            )
    if local is not None:
        extra = [k for k in existing if k not in seen]
        if extra:
            raise RecordFileError(
                f"this database's {table.name} {_show(extra[0])} is not in the record: the job"
                " has diverged from it here"
            )
    await _insert(tx, table, new)


def _show(key: tuple[Any, ...]) -> str:
    parts = [part.hex() if isinstance(part, bytes) else str(part) for part in key]
    return parts[0] if len(parts) == 1 else "(" + ", ".join(parts) + ")"


def _normal(value: Any) -> Any:
    """A column's value in a form that compares and hashes alike whichever side it came from: the
    database's (bytes as memoryview, JSON parsed) or the record's."""
    if isinstance(value, (bytes, memoryview)):
        return bytes(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return value


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _unseen_ids(references: Iterable[EntityID], seen: dict[bytes, bytes]) -> list[EntityID]:
    """The ids among ``references`` not yet in ``seen``, once each."""
    return list(dict.fromkeys(r for r in references if r.cid not in seen))


def _sorted[T](rows: Iterable[T], key: Any) -> tuple[T, ...]:
    return tuple(sorted(rows, key=key))


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _bytes(value: bytes | None) -> bytes | None:
    return None if value is None else bytes(value)


def _ref(cid: bytes) -> EntityID:
    return EntityID(cid=bytes(cid))


def _array(cid: bytes | None) -> BoundID[Array[Any]] | None:
    return None if cid is None else _ref(cid).bind_type(Array[Any])


def _json(value: Any) -> FrozenJSONValue:
    return cast("FrozenJSONValue", freeze_fxtr_data(value))


__all__: Sequence[str] = [
    "RecordFile",
    "RecordFileError",
    "build_record",
    "export_job",
    "insert_record",
    "lock_job",
    "reachable_blocks",
    "read_record",
]
