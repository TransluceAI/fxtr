"""Job records: building a job's :class:`~fxtr.project.job_record.JobRecord` from the ledger,
exporting it with the entities it reaches as a CAR file, and importing one.

What a record holds is the ledger's rule made concrete (see :mod:`fxtr.runner.postgres.schema`): a
ledger row refers only to ledger rows and entities, so a job's rows (every row keyed by the job or
by one of its invocations), the rows they refer to (the step results its bindings name and the job
attempt facts those and its own rows name), and the entities all of those reach form its facts.
The record also embeds the job's settled attempts, events, errors, and logs. :func:`build_record`
reads them in the caller's transaction, which should be a snapshot so
the record is one consistent view; :func:`export_job` collects the entities and writes the file.

Importing is the reverse, in two halves. :func:`fxtr.project.job_record.read_record` reads a file
into a :class:`~fxtr.project.job_record.RecordFile`: the record, and the blocks its rows reach,
walked from the rows (so the record's own block is never kept, and a block nothing reaches is
ignored); a reached entity with no block, a root that is not one record of this format, or a block
that does not match its cid refuses the file. :func:`insert_record` then writes it into the database
in the caller's transaction: the entities, then every row through one merge (``_merge``), which
inserts the rows the database lacks and compares the ones it has, a difference refusing the import.
The facts the job shares with other jobs (job attempt facts, step requests, step results) are merged
by key alone. A job attempt fact of another job (the producer of a step result the job reused) is
marked ``from_other_job`` when it is new here: it explains that result, but the other job's own
attempts are only those its own run or import established, so another job's record cannot add an
attempt to that job's record. That job's own import takes the fact over, with its record's values;
a fact that is a job's own is never changed, and one naming another job is never given to the
importing job (``_merge_attempt_facts``). The job's own rows are merged against all the database
has of the job, which for a new
job is nothing; for a job it has, the import is an *update*: the record must hold each job attempt
fact and each row the database has of the job, and the invocation columns that are set as a job gets
further are set where the database has them null. An update removes nothing. To take a record that
an update refuses, :func:`forget_job` first removes the database's copy of the job.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import ColumnElement, Table, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from fxtr.core import codec
from fxtr.core.car import write_car
from fxtr.core.entities import BoundID
from fxtr.core.entity_data import (
    EntityID,
    FxtrDataValue,
    cid_to_str,
    freeze_fxtr_data,
    get_entity_type_tag,
    thaw_fxtr_data,
)
from fxtr.core.json import FrozenJSONObject, FrozenJSONValue
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.project.code_version import CodeVersion
from fxtr.project.execution_record import (
    EXPORTED_OWNER,
    AttemptEventRow,
    AttemptLogRow,
    AttemptRow,
    ExecutionRecord,
    JobAttemptRow,
)
from fxtr.project.job_record import (
    InvocationRow,
    JobAttemptProvenanceRow,
    JobRecord,
    JobRow,
    JobStepBindingRow,
    JobStepRequestRow,
    NodeInputRow,
    NodeNameRow,
    NodeRow,
    RecordFile,
    RecordFileError,
    SpawnRow,
    StepResultRow,
    unseen_ids,
)
from fxtr.runner.postgres.database import Transaction, any_of
from fxtr.runner.postgres.entities import PostgresEntityStorage
from fxtr.runner.postgres.ledger import JobID, NotFoundError
from fxtr.runner.postgres.schema import (
    attempt_events,
    attempt_logs,
    attempts,
    entities,
    job_attempt_provenance,
    job_attempts,
    job_entities,
    job_step_bindings,
    job_step_requests,
    jobs,
    node_inputs,
    node_names,
    nodes,
    spawns,
    step_cache,
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
                any_of(step_results.c.id, [row.step_result_id for row in binding_rows])
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
                _own_attempts(job_id) | any_of(job_attempt_provenance.c.id, producers)
            )
        )
    ).all()
    return JobRecord(
        execution=await build_execution_record(tx, job_id),
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
    record = await _encode_record(tx, job_id)
    blocks = await reachable_blocks(tx, entities, codec.iter_entity_ids(record.data))
    write_car(path, [record.cid], [(record.cid, record.encoded), *blocks])
    return EntityID(cid=record.cid)


async def record_job_entities(
    tx: Transaction, entities: PostgresEntityStorage, job_id: JobID
) -> int:
    """Record in ``job_entities`` every entity the job's record reaches, as an import of that
    record does (:func:`insert_record`), and return how many the job reaches.

    For a job imported before the table existed: a viewer scoped to some jobs serves only the
    entities the table names for them, so an upgrade across the revision that adds the table
    records every stored job's entities, or those jobs would show no data until pushed again.
    Recording is idempotent, so a job recorded already is unchanged.

    Raises:
        NotFoundError: If there is no such job.
    """
    record = await _encode_record(tx, job_id)
    blocks = await reachable_blocks(tx, entities, codec.iter_entity_ids(record.data))
    if blocks:
        await tx.execute(
            insert(job_entities).on_conflict_do_nothing(),
            [{"job_id": job_id, "cid": cid} for cid, _ in blocks],
        )
    return len(blocks)


async def record_cid(tx: Transaction, job_id: JobID) -> str:
    """The id of the record :func:`export_job` would write for the job as the database holds it
    now, in DASL string form, read in ``tx`` (a snapshot, for one consistent view). It names the
    job's state: a sync compares it with the id the other side holds to tell whether the job
    changed since.

    Raises:
        NotFoundError: If there is no such job.
    """
    return cid_to_str((await _encode_record(tx, job_id)).cid)


@dataclasses.dataclass(frozen=True)
class _EncodedRecord:
    """A job's record as :func:`export_job` writes it: the unstructured data, its DRISL bytes,
    and their cid."""

    data: dict[str, FxtrDataValue]
    encoded: bytes
    cid: bytes


async def _encode_record(tx: Transaction, job_id: JobID) -> _EncodedRecord:
    record = await build_record(tx, job_id)
    data = DEFAULT_CONVERTER.unstructure_entity(record)
    encoded = codec.encode(data)
    return _EncodedRecord(data, encoded, codec.compute_cid(encoded))


async def reachable_blocks(
    tx: Transaction, entities: PostgresEntityStorage, references: Iterable[EntityID]
) -> list[tuple[bytes, bytes]]:
    """Every stored entity ``references`` reach, transitively, as ``(cid, bytes)`` blocks in the
    order first reached.

    Raises:
        NotFoundError: If a reached entity is not stored.
    """
    blocks: dict[bytes, bytes] = {}
    frontier = unseen_ids(references, blocks)
    while frontier:
        loaded = await entities.load_raw_bytes_many(frontier, tx)
        found: list[EntityID] = []
        for entity_id, raw in zip(frontier, loaded, strict=True):
            if raw is None:
                raise NotFoundError(f"Entity {entity_id} is referenced but not stored")
            data, _ = raw
            blocks[entity_id.cid] = data
            found.extend(codec.iter_entity_ids(codec.decode(data)))  # type: ignore[arg-type]
        frontier = unseen_ids(found, blocks)
    return list(blocks.items())


async def lock_job(tx: Transaction, job_id: JobID) -> bool:
    """Take the job's own row for the rest of the transaction, if the database has the job, and
    say whether it does. A resume takes the same row first, so an update of the job from a record
    serializes with anything that could start running it."""
    taken = await tx.execute(
        update(jobs).where(jobs.c.id == job_id).values(workflow_name=jobs.c.workflow_name)
    )
    return taken.rowcount > 0


async def forget_job(tx: Transaction, job_id: JobID) -> None:
    """Remove the database's copy of a job, so that a record of the job can replace it. The caller
    refuses a job a process holds.

    This function removes the job's own rows, with their events and logs. It keeps what other jobs
    and the step cache can use: the step results the job's attempts produced, and for each attempt,
    the fact that it existed and which code it ran. Such a fact becomes ``from_other_job``: it
    explains a result, and is no attempt of the job. An import of the job takes back the facts
    that its record holds.
    """
    owners = select(job_attempts.c.id).where(job_attempts.c.job_id == job_id)
    owned = select(attempts.c.id).where(attempts.c.owner_job_attempt_id.in_(owners))
    # A key whose latest attempt failed or was abandoned points at it. With the attempt gone, the
    # key offers nothing, so the next job to reach it runs the step.
    await tx.execute(delete(step_cache).where(step_cache.c.attempt_id.in_(owned)))
    await tx.execute(delete(attempts).where(attempts.c.owner_job_attempt_id.in_(owners)))
    await tx.execute(delete(jobs).where(jobs.c.id == job_id))
    await tx.execute(
        update(job_attempt_provenance)
        .where(job_attempt_provenance.c.job_id == job_id)
        .values(from_other_job=True)
    )


async def insert_record(tx: Transaction, file: RecordFile) -> JobID:
    """Write a record read from a file into the database, in ``tx`` (see the module docstring),
    and return its job's id. When the database has the job already, this *extends* it: the record
    must hold every job attempt fact and every ledger row the database has for the job, and agree
    with each, beyond the columns an invocation sets as it gets further; what the record adds is
    inserted or set, and nothing is removed. The caller refuses a job a process holds.

    The entities the file carries are stored, and each is recorded against the job in
    ``job_entities``. A viewer that may show only some jobs needs to know which entities belong
    to them, and the import is where a job's reachable set is known: the file carries exactly
    the entities the record's rows reach, as ``read_record`` walked them. Only an import fills
    the table; a job run here has no rows in it.

    Raises:
        RecordFileError: If a fact the record shares with the database differs from it, or the
            database has a job attempt or a row of the job that the record lacks.
    """
    record = file.record
    job_id = JobID(uuid.UUID(record.job.id))
    local_attempts = set(
        (await tx.execute(select(job_attempt_provenance.c.id).where(_own_attempts(job_id))))
        .scalars()
        .all()
    )
    unknown = local_attempts - {uuid.UUID(row.id) for row in record.job_attempts}
    if unknown:
        raise RecordFileError(
            f"the existing copy of job {job_id} has job attempts the record lacks"
            f" ({', '.join(sorted(map(str, unknown)))}): the record is older than that copy, or"
            " each was resumed without the other"
        )
    # One parameter set per row, so SQLAlchemy sends them in batches (as for job_entities
    # below): a round trip per entity would make a large record's import slow. Sorted by cid so
    # two imports that share entities cannot deadlock.
    if file.blocks:
        await tx.execute(
            insert(entities).on_conflict_do_nothing(),
            [
                {"cid": cid, "type_tag": get_entity_type_tag(codec.decode(data)), "data": data}
                for cid, data in sorted(file.blocks.items())
            ],
        )
    await _merge_attempt_facts(tx, job_id, record)
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
    # After the entities and the job's row, which the rows refer to. An update's record reaches
    # everything the earlier one did and more; what is already recorded is left as it is.
    if file.blocks:
        await tx.execute(
            insert(job_entities).on_conflict_do_nothing(),
            [{"job_id": job_id, "cid": cid} for cid in sorted(file.blocks)],
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
    if record.execution is not None:
        await insert_execution_record(tx, record.execution)
    return job_id


async def _insert(tx: Transaction, table: Table, rows: list[dict[str, Any]]) -> None:
    if rows:
        await tx.execute(insert(table), rows)


async def _merge_attempt_facts(tx: Transaction, job_id: JobID, record: JobRecord) -> None:
    """Merge the record's job attempt facts into ``job_attempt_provenance``.

    A fact the database lacks is inserted: as the job's own, or, for another job's attempt (the
    producer of a step result the job reused), marked ``from_other_job``. A fact the database has
    must agree with the record, with one exception: the job's own import takes over a fact that
    another job's record brought naming this job, with its record's values, since the job's
    record is the authority on its own attempts. A fact that is the job's own here is never
    changed, so no import changes another job's record, and a fact naming another job is never
    given to this one.

    Insertion and takeover are one statement per fact, so an import of another job that writes
    the same fact at the same time is seen or waited for, never missed.

    Raises:
        RecordFileError: If a fact the database has disagrees with the record's, other than one
            this job takes over.
    """
    facts = [
        {
            "id": uuid.UUID(row.id),
            "job_id": uuid.UUID(row.job_id),
            "code_version_cid": row.code_version.entity_id.cid,
            "created_at": _parse(row.created_at),
            "from_other_job": uuid.UUID(row.job_id) != job_id,
        }
        for row in record.job_attempts
    ]
    if not facts:
        return
    own = [fact for fact in facts if not fact["from_other_job"]]
    others = [fact for fact in facts if fact["from_other_job"]]
    if own:
        statement = insert(job_attempt_provenance)
        await tx.execute(
            statement.on_conflict_do_update(
                index_elements=[job_attempt_provenance.c.id],
                set_={
                    "code_version_cid": statement.excluded.code_version_cid,
                    "created_at": statement.excluded.created_at,
                    "from_other_job": False,
                },
                where=job_attempt_provenance.c.from_other_job
                & (job_attempt_provenance.c.job_id == statement.excluded.job_id),
            ),
            own,
        )
    if others:
        await tx.execute(insert(job_attempt_provenance).on_conflict_do_nothing(), others)
    found = {
        row.id: row
        for row in (
            await tx.execute(
                select(job_attempt_provenance).where(
                    any_of(job_attempt_provenance.c.id, [fact["id"] for fact in facts])
                )
            )
        ).all()
    }
    for fact in facts:
        here = found[fact["id"]]
        if fact["from_other_job"] or not here.from_other_job:
            differing = [
                column
                for column in ("job_id", "code_version_cid", "created_at")
                if _normal(here._mapping[column]) != _normal(fact[column])
            ]
            if not differing:
                continue
            raise RecordFileError(
                f"job_attempt_provenance {fact['id']} differs from this database's"
                f" ({', '.join(differing)}); the record and this database disagree about a fact"
                " they share"
            )
        raise RecordFileError(
            f"job attempt {fact['id']} arrived here with another job's record as an attempt of job"
            f" {here.job_id}, and this record says it is one of job {job_id}'s; the two records"
            " disagree about which job ran it"
        )


def _own_attempts(job_id: JobID) -> ColumnElement[bool]:
    """The job attempt facts that are the job's own: not those another job's record brought,
    which describe that job's reused results but are not part of this job's record."""
    return (job_attempt_provenance.c.job_id == job_id) & ~job_attempt_provenance.c.from_other_job


async def _merge(
    tx: Transaction,
    table: Table,
    rows: list[dict[str, Any]],
    *,
    local: ColumnElement[bool] | None = None,
    monotonic: Sequence[str] = (),
    ignored: Sequence[str] = (),
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
        ignored: Local-only columns excluded from comparisons, retained when rows exist.
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
        condition: ColumnElement[bool] = any_of(table.c[single], [row[single] for row in rows])
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
            if column in ignored:
                continue
            here, there = _normal(found._mapping[column]), _normal(row[column])
            if here == there:
                continue
            if column in monotonic and here is None:
                updates[column] = row[column]
                continue
            raise RecordFileError(
                f"{table.name} {_show(row_key)} differs from the existing copy's ({column}); the"
                " record and the database disagree about a fact they share"
            )
        if updates:
            await tx.execute(
                update(table).where(*(table.c[c] == row[c] for c in key)).values(**updates)
            )
    if local is not None:
        extra = [k for k in existing if k not in seen]
        if extra:
            raise RecordFileError(
                f"the existing copy's {table.name} {_show(extra[0])} is not in the record: the"
                " two copies of the job have diverged"
            )
    await _insert(tx, table, new)


async def build_execution_record(tx: Transaction, job_id: JobID) -> ExecutionRecord:
    """The job's execution record (:mod:`fxtr.project.execution_record`), read in ``tx``: its job
    attempts, with their owners written as ``EXPORTED_OWNER``, the attempts they owned, and those
    attempts' events and log lines.

    Raises:
        NotFoundError: If there is no such job.
    """
    if (await tx.execute(select(jobs.c.id).where(jobs.c.id == job_id))).first() is None:
        raise NotFoundError(f"No job {job_id}")
    job_attempt_rows = (
        await tx.execute(select(job_attempts).where(job_attempts.c.job_id == job_id))
    ).all()
    attempt_rows = (
        await tx.execute(
            select(attempts).where(
                any_of(attempts.c.owner_job_attempt_id, [row.id for row in job_attempt_rows])
            )
        )
    ).all()
    attempt_ids = [row.id for row in attempt_rows]
    event_rows = (
        await tx.execute(
            select(attempt_events).where(any_of(attempt_events.c.attempt_id, attempt_ids))
        )
    ).all()
    log_rows = (
        await tx.execute(select(attempt_logs).where(any_of(attempt_logs.c.attempt_id, attempt_ids)))
    ).all()
    return ExecutionRecord(
        job_id=str(job_id),
        job_attempts=_sorted(
            (
                JobAttemptRow(
                    id=str(row.id),
                    job_id=str(row.job_id),
                    state=row.state,
                    reason=row.reason,
                    owner=EXPORTED_OWNER,
                    # A settled attempt's heartbeat goes on moving (its owner keeps
                    # heartbeating while it drains), and the merge ignores it; exporting the
                    # settling time instead keeps the record's cid the same across exports.
                    heartbeat_at=_time(
                        row.heartbeat_at if row.settled_at is None else row.settled_at
                    ),
                    settled_at=None if row.settled_at is None else _time(row.settled_at),
                    evicted_at=None if row.evicted_at is None else _time(row.evicted_at),
                    evicted_reason=row.evicted_reason,
                    overwrite_cache_conflicts=row.overwrite_cache_conflicts,
                )
                for row in job_attempt_rows
            ),
            lambda r: r.id,
        ),
        attempts=_sorted(
            (
                AttemptRow(
                    id=str(row.id),
                    kind=row.kind,
                    invocation_id=_str(row.invocation_id),
                    cache_key=row.cache_key,
                    request=_bytes(row.request_cid),
                    owner_job_attempt_id=str(row.owner_job_attempt_id),
                    state=row.state,
                    error=_json(row.error),
                    reason=row.reason,
                    stopped_on=_json(row.stopped_on),
                    started_at=_time(row.started_at),
                    finished_at=None if row.finished_at is None else _time(row.finished_at),
                )
                for row in attempt_rows
            ),
            lambda r: r.id,
        ),
        events=_sorted(
            (
                AttemptEventRow(
                    attempt_id=str(row.attempt_id),
                    seq=row.seq,
                    kind=row.kind,
                    identity=bytes(row.identity),
                    at=_time(row.at),
                )
                for row in event_rows
            ),
            lambda r: (r.attempt_id, r.seq),
        ),
        logs=_sorted(
            (
                AttemptLogRow(
                    attempt_id=str(row.attempt_id),
                    seq=row.seq,
                    message=row.message,
                    at=_time(row.at),
                )
                for row in log_rows
            ),
            lambda r: (r.attempt_id, r.seq),
        ),
    )


async def insert_execution_record(tx: Transaction, record: ExecutionRecord) -> None:
    """Merge settled history into a job, preserving local process-owner labels.

    The job's facts must exist already: the record's job attempts must each have their
    ``job_attempt_provenance`` fact, and its attempts must name the job's invocations and
    step requests the database has; the tables' foreign keys refuse history that names a fact
    the database lacks. For each job attempt the record includes, missing or conflicting local
    history refuses the import; identical rows are unchanged. History of the job's other job
    attempts is kept: the record says nothing about them. The caller holds the job lock and
    refuses active execution.

    Raises:
        RecordFileError: If the record names a job attempt, invocation, or step request the
            database has no fact for, or its history differs from what the database has.
    """
    try:
        await _merge_history(tx, record)
    except IntegrityError as exc:
        raise RecordFileError(
            f"execution history names what this database does not have ({_violation(exc)})"
        ) from exc


def _violation(exc: IntegrityError) -> str:
    """The constraint an integrity error names and the database's detail of it, as asyncpg
    reports them (SQLAlchemy's asyncpg adapter raises its error from asyncpg's)."""
    cause = getattr(exc.orig, "__cause__", None)
    constraint = getattr(cause, "constraint_name", None) or "a constraint"
    detail = getattr(cause, "detail", None) or str(exc.orig)
    return f"{constraint}: {detail}"


async def _merge_history(tx: Transaction, record: ExecutionRecord) -> None:
    job_id = uuid.UUID(record.job_id)
    included = (job_attempts.c.job_id == job_id) & job_attempts.c.id.in_(
        [uuid.UUID(row.id) for row in record.job_attempts]
    )
    owners = select(job_attempts.c.id).where(included)
    owned_attempts = select(attempts.c.id).where(attempts.c.owner_job_attempt_id.in_(owners))
    await _merge(
        tx,
        job_attempts,
        [
            {
                "id": uuid.UUID(row.id),
                "job_id": uuid.UUID(row.job_id),
                "state": row.state,
                "reason": row.reason,
                "owner": row.owner,
                "heartbeat_at": _parse(row.heartbeat_at),
                "settled_at": None if row.settled_at is None else _parse(row.settled_at),
                "evicted_at": None if row.evicted_at is None else _parse(row.evicted_at),
                "evicted_reason": row.evicted_reason,
                "overwrite_cache_conflicts": row.overwrite_cache_conflicts,
            }
            for row in record.job_attempts
        ],
        local=included,
        # Lease state is not a fact. The owner is this database's process label, and
        # heartbeat_at is refreshed by the owning process after the job attempt settles too (a
        # draining soft cancel keeps heartbeating), so two exports of one settled job attempt can
        # differ there without disagreeing about the job.
        ignored=("owner", "heartbeat_at"),
    )
    await _merge(
        tx,
        attempts,
        [
            {
                "id": uuid.UUID(row.id),
                "kind": row.kind,
                "invocation_id": None
                if row.invocation_id is None
                else uuid.UUID(row.invocation_id),
                "cache_key": row.cache_key,
                "request_cid": row.request,
                "owner_job_attempt_id": uuid.UUID(row.owner_job_attempt_id),
                "state": row.state,
                "error": thaw_fxtr_data(row.error),
                "reason": row.reason,
                "stopped_on": thaw_fxtr_data(row.stopped_on),
                "started_at": _parse(row.started_at),
                "finished_at": None if row.finished_at is None else _parse(row.finished_at),
            }
            for row in record.attempts
        ],
        local=attempts.c.owner_job_attempt_id.in_(owners),
    )
    await _merge(
        tx,
        attempt_events,
        [
            {
                "attempt_id": uuid.UUID(row.attempt_id),
                "seq": row.seq,
                "kind": row.kind,
                "identity": row.identity,
                "at": _parse(row.at),
            }
            for row in record.events
        ],
        local=attempt_events.c.attempt_id.in_(owned_attempts),
    )
    await _merge(
        tx,
        attempt_logs,
        [
            {
                "attempt_id": uuid.UUID(row.attempt_id),
                "seq": row.seq,
                "message": row.message,
                "at": _parse(row.at),
            }
            for row in record.logs
        ],
        local=attempt_logs.c.attempt_id.in_(owned_attempts),
    )


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
    "build_execution_record",
    "build_record",
    "export_job",
    "forget_job",
    "insert_execution_record",
    "insert_record",
    "lock_job",
    "reachable_blocks",
    "record_cid",
    "record_job_entities",
]
