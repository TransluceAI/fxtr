"""The ledger: the facts of the runner's durable record, and the rules that keep them valid.

The ledger is the half of the runner's state that says what is true about an experiment: jobs and
their invocations, each invocation's nodes with their edges and names, what its body returned, what
its run nodes expanded into (``spawns``: a child invocation or a step cache key), the queryable
fields of the step requests made, each attempt's event log, materialized results, and which code
produced them (each job attempt's fact, which an invocation's result and each step result name).
A ledger row refers only to ledger rows and entities, never to execution state. Every
function here does one logical thing against the database given a
:class:`~fxtr.runner.postgres.database.Transaction`, never opens one, and checks its own
preconditions inside it, so higher layers compose them into single commits. How the work is
executed (job attempts and their leases, attempts and their lifecycle, the step cache) is the
execution state's, in :mod:`fxtr.runner.postgres.execution`; nothing here decides what runs
or judges how it is going.

**How to read this module.** After the errors and the record types: *jobs and invocations* (a job
is created with its root invocation and input nodes; children are created by expansion); what a
running attempt *records* (emissions, observations, the return, the result, expansion of run nodes
into child invocations, step spawns, step requests), each taking the write token below; *step
results and bindings* (a completed step attempt's step result; the policy binding a job's request
to a step result, set once); then the *readers*
(``get_*``, ``find_*``, ``list_*``, ``load_*``), which decode rows into the record types; and
finally the row encoding and decoding helpers.

**The write token.** Every function that records on behalf of an attempt takes an
:class:`ActiveAttempt`, which only :func:`fxtr.runner.postgres.execution.guard` issues, in the
same transaction, after locking the attempt's row and checking it may write (it is running, in a
fresh job attempt; at most one attempt of an invocation is running at a time, a database
invariant). The ledger does not re-check who is writing;
the token is the proof, and writes to an invocation's nodes, names, and events need no further
locking because the active attempt is their only writer.

**The acting attempt's event log.** ``attempt_events`` is execution state (which attempt did what,
as pointers into the invocation's nodes), and it is the one piece of execution state this module
touches: the recording functions append the acting attempt's events as they record the facts,
since a pointer exists exactly because a fact was recorded on that attempt's behalf, and
:func:`record_return` reads the acting attempt's ``emit`` events for the determinism check, which
is a rule about what *this* attempt did. The ledger reads and writes no other execution state
(attempt lifecycle, job attempts, the queue, subscriptions, the cache), and touches the log only
through the token, for the attempt it names.

**Determinism (replay).** A later attempt of an invocation runs the same body against what earlier
attempts recorded. :func:`record_emissions` treats an identity already present as a match and
inserts nothing, but a name already mapped to a different identity is a violation;
:func:`record_return` requires every non-input node of the invocation to have been emitted by the
returning attempt, so a body that emits less than before is a violation while one that emits more
(it got further) is fine. Violations raise :class:`~fxtr.runner.session.DeterminismError`. A job
requests one configuration per cache key (:func:`record_step_request`); a different one is
:class:`CacheAddressReuseError`.

**Concurrency.** Transactions run at ``READ COMMITTED``; readers composing several queries into
one view open their transaction with ``snapshot=True``. The locking model for attempts, leases,
and cache keys is documented in the execution module.
"""

from __future__ import annotations

import dataclasses
import itertools
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, NewType

from sqlalchemy import Row, func, select, update
from sqlalchemy.dialects.postgresql import insert

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ArrayType, Key
from fxtr.project import client
from fxtr.project.code_version import CodeVersion
from fxtr.runner.nodes import (
    Input,
    Node,
    NodeIdentity,
    NodeRow,
    SourceArray,
    from_row,
    node_name,
    to_row,
)
from fxtr.runner.postgres.database import Transaction
from fxtr.runner.postgres.json_values import array_type_from_json, array_type_to_json, key_to_json
from fxtr.runner.postgres.schema import (
    attempt_events,
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
from fxtr.runner.session import AttemptID, DeterminismError, RunnerError
from fxtr.runner.steps import StepRequest

JobAttemptID = NewType("JobAttemptID", uuid.UUID)
"""A job attempt's id: its fact's (``job_attempt_provenance.id``) and, where this database ran it,
its execution state's (``job_attempts.id``)."""
StepResultID = NewType("StepResultID", uuid.UUID)
"""A step result's id (``step_results.id``): the id of the step attempt that produced it."""
JobID = client.JobID
"""A job's id (``jobs.id``): :data:`fxtr.project.client.JobID`, the identity every client speaks."""
InvocationID = NewType("InvocationID", uuid.UUID)
"""A workflow invocation's id (``workflow_invocations.id``)."""

BATCH_SIZE = 5_000
"""The most rows one statement of a many-key function writes, or keys it reads: well under the
32,767 bind parameters a Postgres statement may carry, at a few columns a row."""


# ---- errors ----


class LedgerError(RunnerError):
    """Base class for the ledger's errors."""


class CacheAddressReuseError(LedgerError):
    """A job used one cache address for two different requests: the body used one address for
    two configurations. This is a fault of the body. It is not a cache conflict, which is a
    different request of another job at the address."""


class NotFoundError(LedgerError, KeyError):
    """No such job, invocation, attempt, job attempt, node, cache entry, or request."""


# ---- records ----


@dataclasses.dataclass(frozen=True)
class ActiveAttempt:
    """What the execution policy's guard establishes about an attempt that may write: the
    attempt and the invocation, job, and (fresh) job attempt it belongs to. Passed to every write
    on the attempt's behalf, so that only a guarded attempt can write.

    Attributes:
        id: The attempt.
        invocation_id: The invocation it is the active attempt of.
        job_id: The invocation's job.
        owner_job_attempt_id: The job attempt whose process runs it.
        ending: Whether the guard took the owner's job attempt row first, which the transaction
            that ends the attempt must (see the execution module's docstring).
    """

    id: AttemptID
    invocation_id: InvocationID
    job_id: JobID
    owner_job_attempt_id: JobAttemptID
    ending: bool = False


@dataclasses.dataclass(frozen=True)
class Emission:
    """A node an attempt emitted, ready to record.

    Attributes:
        node: The node.
        type: Its array type, derived by the host (see :func:`~fxtr.runner.nodes.node_type`).
        value: Its materialized value, if the host has one to record beyond what the node
            itself references; a leaf's value defaults to the array it references.
    """

    node: Node
    type: ArrayType
    value: BoundID[Array[Any]] | None = None


@dataclasses.dataclass(frozen=True)
class JobRecord:
    """One row of ``jobs``.

    Attributes:
        id: The job.
        workflow_name: The root workflow's name.
        root: The root invocation's pathname and default cache key prefix.
        created_at: When the job was created.
    """

    id: JobID
    workflow_name: str
    root: str
    created_at: datetime


@dataclasses.dataclass(frozen=True)
class InvocationRecord:
    """One row of ``workflow_invocations``; see the schema for the meaning of each field.

    Attributes:
        id: The invocation.
        job_id: Its job.
        pathname: Its path in the experiment, unique within the job.
        workflow_name: The workflow it runs.
        cache_key_prefix: The default prefix for the steps it schedules.
        returned_node: The node its body returned, once some attempt's body has.
        result: Its materialized result, once finished.
        job_attempt_id: The job attempt whose process produced the result, once finished.
        created_at: When it was created.
        finished_at: When its result was set.
    """

    id: InvocationID
    job_id: JobID
    pathname: str
    workflow_name: str
    cache_key_prefix: str
    returned_node: NodeIdentity | None
    result: BoundID[Array[Any]] | None
    job_attempt_id: JobAttemptID | None
    created_at: datetime
    finished_at: datetime | None

    @property
    def finished(self) -> bool:
        """Whether the result is materialized."""
        return self.result is not None


@dataclasses.dataclass(frozen=True)
class NodeRecord:
    """One node of an invocation, decoded from its row and edges.

    Attributes:
        node: The node.
        identity: Its identity.
        type: Its (derived, stored) array type.
        value: Its materialized value, for leaves.
    """

    node: Node
    identity: NodeIdentity
    type: ArrayType
    value: BoundID[Array[Any]] | None


@dataclasses.dataclass(frozen=True)
class SpawnRecord:
    """One row of ``spawns``: a target a run node expanded into at one joint key.

    Attributes:
        invocation_id: The invocation whose run node this is (the parent).
        node_identity: The run node.
        key: The joint key (see :attr:`~fxtr.runner.nodes.Spawn.key`).
        child_invocation_id: For a child workflow, the invocation created for it.
        step_cache_key: For a step, the cache key requested; the job's request there says with
            which fingerprint, its binding which result it got.
    """

    invocation_id: InvocationID
    node_identity: NodeIdentity
    key: Key
    child_invocation_id: InvocationID | None = None
    step_cache_key: str | None = None


@dataclasses.dataclass(frozen=True)
class StepRequestRecord:
    """One row of ``step_requests``: the queryable fields of a request.

    Attributes:
        request: The fingerprint.
        step_name: The step it calls.
    """

    request: EntityID
    step_name: str


@dataclasses.dataclass(frozen=True)
class JobStepRequestRecord:
    """One row of ``job_step_requests``: what a job asked for at a cache key.

    Attributes:
        job_id: The job.
        cache_key: The key.
        request: The request's fingerprint.
    """

    job_id: JobID
    cache_key: str
    request: EntityID


@dataclasses.dataclass(frozen=True)
class JobAttemptProvenanceRecord:
    """One row of ``job_attempt_provenance``: that a job attempt existed and which code it ran.

    Attributes:
        id: The job attempt.
        job_id: Its job (which this database may not have).
        code_version: The code its process ran.
        created_at: When it was created.
    """

    id: JobAttemptID
    job_id: JobID
    code_version: BoundID[CodeVersion]
    created_at: datetime


@dataclasses.dataclass(frozen=True)
class StepResultRecord:
    """One row of ``step_results``: a request, run under a job attempt, produced a result.

    Attributes:
        id: The step result, which is also the id of the step attempt that produced it.
        request: The request that was run (its fingerprint).
        result: What it produced.
        job_attempt_id: The job attempt whose process ran it.
        completed_at: When it completed.
    """

    id: StepResultID
    request: EntityID
    result: BoundID[Array[Any]]
    job_attempt_id: JobAttemptID
    completed_at: datetime


@dataclasses.dataclass(frozen=True)
class StepBindingRecord:
    """One row of ``job_step_bindings``, with its step result's value: the step result a job's
    request at a key is bound to.

    Attributes:
        job_id: The job.
        cache_key: The key.
        step_result_id: The step result.
        result: Its result array.
    """

    job_id: JobID
    cache_key: str
    step_result_id: StepResultID
    result: BoundID[Array[Any]]


@dataclasses.dataclass(frozen=True)
class ChildSpec:
    """What :func:`expand_run_node` needs to create one child invocation.

    Attributes:
        key: The spawn's joint key.
        pathname: The child's pathname (the parent's, the run node's name, the mapped key).
        workflow_name: The workflow it runs.
        cache_key_prefix: Its default cache key prefix.
        inputs: Its arguments, each a reference to its (sliced) array, already in the entity
            store, and the array's type.
    """

    key: Key
    pathname: str
    workflow_name: str
    cache_key_prefix: str
    inputs: Mapping[str, tuple[BoundID[Array[Any]], ArrayType]]


@dataclasses.dataclass(frozen=True)
class EventRecord:
    """One row of ``attempt_events``: something the body did to a node, in log order.

    Attributes:
        seq: The event's position in the attempt's log, from 0.
        kind: ``emit`` (the body emitted the node), ``observe`` (the body asked for its
            value), or ``return`` (the body returned it).
        identity: The node concerned.
        at: When it was recorded.
    """

    seq: int
    kind: Literal["emit", "observe", "return"]
    identity: NodeIdentity
    at: datetime


# ---- jobs and invocations ----


async def create_job(
    tx: Transaction,
    job_id: JobID,
    workflow_name: str,
    root: str,
    inputs: Mapping[str, tuple[BoundID[Array[Any]], ArrayType]],
) -> InvocationRecord:
    """Create a job and its root invocation, with one ``input`` node per argument.

    The input arrays must already be in the entity store (flushed in this transaction or
    committed earlier).

    Args:
        job_id: The new job's id.
        workflow_name: The root workflow's name.
        root: The root invocation's pathname and default cache key prefix.
        inputs: The arguments, each a reference to its array and the array's type.

    Returns:
        The root invocation, unclaimed; the caller hands it to the execution policy.
    """
    await tx.execute(insert(jobs).values(id=job_id, workflow_name=workflow_name, root=root))
    invocation = await create_invocation(
        tx,
        job_id=job_id,
        pathname=root,
        workflow_name=workflow_name,
        cache_key_prefix=root,
        inputs=inputs,
    )
    return invocation


async def create_invocation(
    tx: Transaction,
    *,
    job_id: JobID,
    pathname: str,
    workflow_name: str,
    cache_key_prefix: str,
    inputs: Mapping[str, tuple[BoundID[Array[Any]], ArrayType]],
) -> InvocationRecord:
    """Create an invocation with its ``input`` nodes (which have their values from the start and
    belong to no attempt). The input arrays must already be in the entity store. Who requested the
    invocation is recorded separately, in ``spawns`` (see :func:`expand_run_node`); a root has no
    such record.

    Args:
        job_id: The job the invocation belongs to.
        pathname: Its path in the experiment, unique within the job.
        workflow_name: The workflow it runs.
        cache_key_prefix: The default prefix for the steps it schedules.
        inputs: The arguments, each a reference to its array and the array's type.

    Returns:
        The invocation as created.
    """
    invocation_id = InvocationID(uuid.uuid4())
    await tx.execute(
        insert(workflow_invocations).values(
            id=invocation_id,
            job_id=job_id,
            pathname=pathname,
            workflow_name=workflow_name,
            cache_key_prefix=cache_key_prefix,
        )
    )
    for name, (ref, ttype) in inputs.items():
        node = Input(name=name, array=ref)
        await _insert_node(tx, invocation_id, node, ttype, value=ref)
    return await get_invocation(tx, invocation_id)


# ---- what a running attempt records ----


async def record_emissions(
    tx: Transaction, active: ActiveAttempt, emissions: Iterable[Emission]
) -> None:
    """Record nodes an attempt emitted: insert the new ones with their edges, claim their names,
    and log an ``emit`` event for each, applying the determinism checks.

    An identity the invocation already has is a match: nothing is inserted, but the event is
    still logged (the check at return counts what this attempt emitted). The entities the nodes
    reference (a source array's) must already be in the store, flushed in this transaction or
    earlier.

    Args:
        active: The attempt, as :func:`guard` returned it in this transaction.
        emissions: The nodes, in the order the body emitted them.

    Raises:
        DeterminismError: If a name is already mapped to a different node.
    """
    events: list[tuple[str, NodeIdentity]] = []
    for emission in emissions:
        identity = emission.node.content_id()
        value = emission.value if emission.value is not None else _leaf_value(emission.node)
        await _insert_node(
            tx,
            active.invocation_id,
            emission.node,
            emission.type,
            value=value,
        )
        name = node_name(emission.node)
        if name is not None:
            await _claim_name(tx, active.invocation_id, name, identity)
        events.append(("emit", identity))
    await _append_events(tx, active.id, events)


async def _claim_name(
    tx: Transaction, invocation_id: InvocationID, name: str, identity: NodeIdentity
) -> None:
    """Map a name to a node within the invocation, or check that it is already mapped to it.

    Raises:
        DeterminismError: If the name is mapped to a different node.
    """
    result = await tx.execute(
        insert(node_names)
        .values(invocation_id=invocation_id, name=name, identity=identity.cid)
        .on_conflict_do_nothing()
        .returning(node_names.c.name)
    )
    if result.first() is not None:
        return
    existing = (
        await tx.execute(
            select(node_names.c.identity).where(
                node_names.c.invocation_id == invocation_id, node_names.c.name == name
            )
        )
    ).scalar_one()
    if bytes(existing) != identity.cid:
        raise DeterminismError(
            f"Name {name!r} was given to node {EntityID(cid=bytes(existing))!r} by an earlier"
            f" attempt, now to {identity!r}"
        )


async def record_observation(
    tx: Transaction, active: ActiveAttempt, identity: NodeIdentity
) -> None:
    """Log that the attempt's body asked for a node's value. Recorded when the body asks, whether
    or not a value ever comes: the request is what the body did; the value, if any, is derivable
    from the leaves, and a failure to produce one is recorded on the node and the attempt.

    Args:
        active: The attempt, as :func:`guard` returned it in this transaction.
        identity: The node observed.

    Raises:
        NotFoundError: If the node is not one of the invocation's.
    """
    await _require_node(tx, active.invocation_id, identity)
    await _append_events(tx, active.id, [("observe", identity)])


async def record_return(tx: Transaction, active: ActiveAttempt, identity: NodeIdentity) -> None:
    """Record that the attempt's body returned a node: set the invocation's ``returned_node`` and
    log a ``return`` event, after the determinism checks at return.

    Args:
        active: The attempt, as :func:`guard` returned it in this transaction.
        identity: The node the body returned.

    Raises:
        NotFoundError: If the node is not one of the invocation's.
        DeterminismError: If an earlier attempt returned a different node, or this attempt did
            not emit every non-input node the invocation has.
    """
    await _require_node(tx, active.invocation_id, identity)
    previous = (
        await tx.execute(
            select(workflow_invocations.c.returned_node).where(
                workflow_invocations.c.id == active.invocation_id
            )
        )
    ).scalar_one()
    if previous is not None and bytes(previous) != identity.cid:
        raise DeterminismError(
            f"An earlier attempt returned node {EntityID(cid=bytes(previous))!r}, this one"
            f" {identity!r}"
        )
    emitted = select(attempt_events.c.identity).where(
        attempt_events.c.attempt_id == attempt_uuid(active.id), attempt_events.c.kind == "emit"
    )
    missing = (
        await tx.execute(
            select(nodes.c.identity).where(
                nodes.c.invocation_id == active.invocation_id,
                nodes.c.kind != "input",
                nodes.c.identity.not_in(emitted),
            )
        )
    ).scalars()
    missing_ids = [EntityID(cid=bytes(m)) for m in missing]
    if missing_ids:
        raise DeterminismError(
            f"The body returned without emitting {len(missing_ids)} node(s) an earlier attempt"
            f" emitted: {missing_ids!r}"
        )
    await tx.execute(
        update(workflow_invocations)
        .where(workflow_invocations.c.id == active.invocation_id)
        .values(returned_node=identity.cid)
    )
    await _append_events(tx, active.id, [("return", identity)])


async def record_result(
    tx: Transaction, active: ActiveAttempt, result: BoundID[Array[Any]]
) -> None:
    """Set the invocation's result, finishing it, with the job attempt whose process produced it
    (the attempt's owner).

    Args:
        active: The attempt, as the guard returned it in this transaction.
        result: The returned node's value, already stored as an entity (flushed in this
            transaction or earlier).

    Raises:
        LedgerError: If the invocation has no ``returned_node`` yet, or is already finished.
    """
    result_row = await tx.execute(
        update(workflow_invocations)
        .where(
            workflow_invocations.c.id == active.invocation_id,
            workflow_invocations.c.returned_node.is_not(None),
            workflow_invocations.c.result_cid.is_(None),
        )
        .values(
            result_cid=result.entity_id.cid,
            job_attempt_id=active.owner_job_attempt_id,
            finished_at=func.now(),
        )
    )
    if result_row.rowcount == 0:
        raise LedgerError(f"Invocation {active.invocation_id} has not returned, or is finished")


async def expand_run_node(
    tx: Transaction, active: ActiveAttempt, identity: NodeIdentity, children: Sequence[ChildSpec]
) -> list[InvocationRecord]:
    """Expand a run node of the attempt's invocation into child workflow invocations, one per
    spawn, and record them in ``spawns``.

    Idempotent per node: if the node already has spawns (an earlier attempt expanded it), nothing
    is created and nothing is returned; a resumed attempt reaches the same children through
    :func:`load_spawns`. The children's input arrays must already be in the entity store (flushed
    in this transaction or earlier).

    Args:
        active: The attempt, as :func:`guard` returned it in this transaction.
        identity: The run node, one of the invocation's nodes.
        children: One per joint key, as :func:`~fxtr.runner.nodes.expand` produced them.

    Returns:
        The new invocations, unclaimed (the caller hands them to the execution policy); empty if
        already expanded.

    Raises:
        NotFoundError: If the node is not one of the invocation's.
    """
    await _require_node(tx, active.invocation_id, identity)
    if await load_spawns(tx, active.invocation_id, identity):
        return []
    created: list[InvocationRecord] = []
    for child in children:
        invocation = await create_invocation(
            tx,
            job_id=active.job_id,
            pathname=child.pathname,
            workflow_name=child.workflow_name,
            cache_key_prefix=child.cache_key_prefix,
            inputs=child.inputs,
        )
        await tx.execute(
            insert(spawns).values(
                invocation_id=active.invocation_id,
                node_identity=identity.cid,
                key=key_to_json(child.key),
                child_invocation_id=invocation.id,
            )
        )
        created.append(invocation)
    return created


async def record_step_spawns(
    tx: Transaction, active: ActiveAttempt, identity: NodeIdentity, keys: Mapping[Key, str]
) -> None:
    """Record what a ``run_step`` node expanded into: one spawn per joint key naming the cache key
    requested for it (the job's request at that key, :func:`record_step_request`, says which call).

    Idempotent per node, like :func:`expand_run_node`: if the node already has spawns nothing is
    written.

    Args:
        active: The attempt, as :func:`guard` returned it in this transaction.
        identity: The run node, one of the invocation's nodes.
        keys: The cache key requested at each joint key.

    Raises:
        NotFoundError: If the node is not one of the invocation's.
    """
    await _require_node(tx, active.invocation_id, identity)
    if not keys or await load_spawns(tx, active.invocation_id, identity):
        return
    await tx.execute(
        insert(spawns),
        [
            {
                "invocation_id": active.invocation_id,
                "node_identity": identity.cid,
                "key": key_to_json(key),
                "step_cache_key": cache_key,
            }
            for key, cache_key in keys.items()
        ],
    )


async def record_step_request(
    tx: Transaction,
    active: ActiveAttempt,
    cache_key: str,
    request: BoundID[StepRequest],
    step_name: str,
) -> JobStepRequestRecord:
    """Record that the attempt's job requests a step at a cache key: the request's queryable
    fields (``step_requests``, shared by every job) and the job's request at the key
    (``job_step_requests``). A job requests one configuration per key: an equal request already
    there is a match (a replay, or a second run node asking for the same call), a different one is
    the body's fault. The request entity must already be in the store (flushed in this transaction
    or earlier).

    Args:
        active: The attempt, as the guard returned it in this transaction.
        cache_key: The key.
        request: The request (its cid is the fingerprint).
        step_name: The request's step, recorded for claim matching.

    Raises:
        CacheAddressReuseError: If the job already requested a different configuration at the key.
    """
    recorded = await record_step_requests(tx, active, {cache_key: request}, step_name)
    return recorded[cache_key]


async def record_step_requests(
    tx: Transaction,
    active: ActiveAttempt,
    requests: Mapping[str, BoundID[StepRequest]],
    step_name: str,
) -> dict[str, JobStepRequestRecord]:
    """:func:`record_step_request` for many keys of one step, in a few statements whatever their
    number (a run node's spawns, say), with the same rule at each key.

    Args:
        active: The attempt, as the guard returned it in this transaction.
        requests: The request at each key.
        step_name: The step every request is for.

    Returns:
        The job's request at each key.

    Raises:
        CacheAddressReuseError: If the job already requested a different configuration at any
            of the keys.
    """
    fingerprints = {request.entity_id.cid for request in requests.values()}
    for batch in itertools.batched(sorted(fingerprints), BATCH_SIZE):
        await tx.execute(
            insert(step_requests)
            .values([dict(request_cid=cid, step_name=step_name) for cid in batch])
            .on_conflict_do_nothing()
        )
    inserted: set[str] = set()
    for batch in itertools.batched(requests.items(), BATCH_SIZE):
        rows = await tx.execute(
            insert(job_step_requests)
            .values(
                [
                    dict(job_id=active.job_id, cache_key=key, request_cid=request.entity_id.cid)
                    for key, request in batch
                ]
            )
            .on_conflict_do_nothing()
            .returning(job_step_requests.c.cache_key)
        )
        inserted.update(row.cache_key for row in rows)
    recorded = {
        key: JobStepRequestRecord(job_id=active.job_id, cache_key=key, request=request.entity_id)
        for key, request in requests.items()
        if key in inserted
    }
    existing = await get_job_step_requests(tx, active.job_id, requests.keys() - inserted)
    assert existing.keys() == requests.keys() - inserted
    for key in sorted(existing):
        if existing[key].request != requests[key].entity_id:
            raise CacheAddressReuseError(
                f"Cache key {key!r} is already used in this job for a different request"
                f" ({existing[key].request!r}); one key names one call"
            )
    return recorded | existing


async def record_step_result(
    tx: Transaction,
    step_result_id: StepResultID,
    request: EntityID,
    result: BoundID[Array[Any]],
    job_attempt_id: JobAttemptID,
) -> None:
    """Record that a request, run under a job attempt, produced a result. The result must already
    be an entity in the store. Nothing is read back: it is written in the transaction that ends
    a step attempt, which holds the job attempt's row (see :mod:`fxtr.runner.postgres.execution`).

    Args:
        step_result_id: The id of the step attempt that produced it.
        request: The request that was run (its fingerprint).
        result: What it produced.
        job_attempt_id: The job attempt whose process ran it.
    """
    await tx.execute(
        insert(step_results).values(
            id=step_result_id,
            request_cid=request.cid,
            result_cid=result.entity_id.cid,
            job_attempt_id=job_attempt_id,
        )
    )


async def bind_step_result(
    tx: Transaction, job_id: JobID, cache_key: str, step_result_id: StepResultID
) -> None:
    """Bind a job's request at a key to a step result: set once, a later binding of the same key
    is ignored (the first stands).

    Args:
        job_id: The job.
        cache_key: The key its request was made at.
        step_result_id: The step result.
    """
    await bind_step_results(tx, job_id, {cache_key: step_result_id})


async def bind_step_results(
    tx: Transaction, job_id: JobID, step_result_ids: Mapping[str, StepResultID]
) -> None:
    """:func:`bind_step_result` for many keys, in one statement per :data:`BATCH_SIZE` keys.

    Args:
        job_id: The job.
        step_result_ids: The step result to bind the job's request at each key to.
    """
    for batch in itertools.batched(step_result_ids.items(), BATCH_SIZE):
        await tx.execute(
            insert(job_step_bindings)
            .values(
                [
                    dict(job_id=job_id, cache_key=key, step_result_id=step_result_id)
                    for key, step_result_id in batch
                ]
            )
            .on_conflict_do_nothing()
        )


# ---- readers ----


async def get_job(tx: Transaction, job_id: JobID) -> JobRecord:
    """The job.

    Raises:
        NotFoundError: If there is no such job.
    """
    row = (await tx.execute(select(jobs).where(jobs.c.id == job_id))).first()
    if row is None:
        raise NotFoundError(f"No job {job_id}")
    return JobRecord(
        id=JobID(row.id), workflow_name=row.workflow_name, root=row.root, created_at=row.created_at
    )


async def list_jobs(tx: Transaction) -> list[JobRecord]:
    """Every job, newest first."""
    rows = (await tx.execute(select(jobs).order_by(jobs.c.created_at.desc(), jobs.c.id))).all()
    return [
        JobRecord(
            id=JobID(row.id),
            workflow_name=row.workflow_name,
            root=row.root,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def get_invocation(tx: Transaction, invocation_id: InvocationID) -> InvocationRecord:
    """The invocation.

    Raises:
        NotFoundError: If there is no such invocation.
    """
    row = (
        await tx.execute(
            select(workflow_invocations).where(workflow_invocations.c.id == invocation_id)
        )
    ).first()
    if row is None:
        raise NotFoundError(f"No invocation {invocation_id}")
    return _invocation(row)


async def get_invocations(
    tx: Transaction, invocation_ids: Collection[InvocationID]
) -> dict[InvocationID, InvocationRecord]:
    """The invocations with these ids, by id, in one query: :func:`get_invocation` for many.

    Raises:
        NotFoundError: If any of them does not exist.
    """
    if not invocation_ids:
        return {}
    rows = (
        await tx.execute(
            select(workflow_invocations).where(workflow_invocations.c.id.in_(list(invocation_ids)))
        )
    ).all()
    found = {InvocationID(row.id): _invocation(row) for row in rows}
    missing = set(invocation_ids) - found.keys()
    if missing:
        raise NotFoundError(f"No invocations {sorted(map(str, missing))}")
    return found


async def find_invocation(tx: Transaction, job_id: JobID, pathname: str) -> InvocationRecord:
    """The job's invocation at a pathname.

    Raises:
        NotFoundError: If the job has no invocation there.
    """
    row = (
        await tx.execute(
            select(workflow_invocations).where(
                workflow_invocations.c.job_id == job_id,
                workflow_invocations.c.pathname == pathname,
            )
        )
    ).first()
    if row is None:
        raise NotFoundError(f"Job {job_id} has no invocation at {pathname!r}")
    return _invocation(row)


async def list_invocations(tx: Transaction, job_id: JobID) -> list[InvocationRecord]:
    """A job's invocations, in creation order (then by pathname)."""
    rows = (
        await tx.execute(
            select(workflow_invocations)
            .where(workflow_invocations.c.job_id == job_id)
            .order_by(workflow_invocations.c.created_at, workflow_invocations.c.pathname)
        )
    ).all()
    return [_invocation(row) for row in rows]


async def load_spawns(
    tx: Transaction, invocation_id: InvocationID, identity: NodeIdentity | None = None
) -> list[SpawnRecord]:
    """The spawns of an invocation's run nodes, in creation order: of one node if ``identity`` is
    given, else of all of them. Empty for a node not yet expanded."""
    query = select(spawns).where(spawns.c.invocation_id == invocation_id)
    if identity is not None:
        query = query.where(spawns.c.node_identity == identity.cid)
    rows = (await tx.execute(query.order_by(spawns.c.created_at, spawns.c.key))).all()
    return [_spawn(row) for row in rows]


async def find_parent(tx: Transaction, invocation_id: InvocationID) -> SpawnRecord | None:
    """The spawn that requested an invocation (with the parent as its ``invocation_id``), or
    ``None`` for a job's root."""
    row = (
        await tx.execute(select(spawns).where(spawns.c.child_invocation_id == invocation_id))
    ).first()
    return None if row is None else _spawn(row)


async def get_step_request(tx: Transaction, fingerprint: EntityID) -> StepRequestRecord:
    """The queryable fields of a step request, by fingerprint.

    Raises:
        NotFoundError: If no request with that fingerprint has been made.
    """
    row = (
        await tx.execute(
            select(step_requests).where(step_requests.c.request_cid == fingerprint.cid)
        )
    ).first()
    if row is None:
        raise NotFoundError(f"No step request {fingerprint!r}")
    return StepRequestRecord(request=fingerprint, step_name=row.step_name)


async def get_step_requests(
    tx: Transaction, fingerprints: Collection[EntityID]
) -> dict[EntityID, StepRequestRecord]:
    """:func:`get_step_request` for many, by fingerprint.

    Raises:
        NotFoundError: If any of them has not been made.
    """
    by_cid = {fingerprint.cid: fingerprint for fingerprint in fingerprints}
    found: dict[EntityID, StepRequestRecord] = {}
    for batch in itertools.batched(by_cid, BATCH_SIZE):
        rows = await tx.execute(select(step_requests).where(step_requests.c.request_cid.in_(batch)))
        for row in rows:
            fingerprint = by_cid[bytes(row.request_cid)]
            found[fingerprint] = StepRequestRecord(request=fingerprint, step_name=row.step_name)
    missing = set(by_cid.values()) - found.keys()
    if missing:
        raise NotFoundError(f"No step requests {sorted(map(repr, missing))}")
    return found


async def get_job_step_request(
    tx: Transaction, job_id: JobID, cache_key: str
) -> JobStepRequestRecord | None:
    """The job's request at a key, or ``None`` if it made none."""
    row = (
        await tx.execute(
            select(job_step_requests).where(
                job_step_requests.c.job_id == job_id, job_step_requests.c.cache_key == cache_key
            )
        )
    ).first()
    return None if row is None else _job_step_request(row)


async def get_job_step_requests(
    tx: Transaction, job_id: JobID, cache_keys: Collection[str]
) -> dict[str, JobStepRequestRecord]:
    """:func:`get_job_step_request` for many keys, by key; a key the job made no request at is
    absent."""
    found: dict[str, JobStepRequestRecord] = {}
    for batch in itertools.batched(cache_keys, BATCH_SIZE):
        rows = await tx.execute(
            select(job_step_requests).where(
                job_step_requests.c.job_id == job_id, job_step_requests.c.cache_key.in_(batch)
            )
        )
        found.update((row.cache_key, _job_step_request(row)) for row in rows)
    return found


async def list_job_step_requests(tx: Transaction, job_id: JobID) -> list[JobStepRequestRecord]:
    """A job's step requests, oldest first."""
    rows = (
        await tx.execute(
            select(job_step_requests)
            .where(job_step_requests.c.job_id == job_id)
            .order_by(job_step_requests.c.requested_at, job_step_requests.c.cache_key)
        )
    ).all()
    return [_job_step_request(row) for row in rows]


async def get_step_binding(
    tx: Transaction, job_id: JobID, cache_key: str
) -> StepBindingRecord | None:
    """The result the job's request at a key is bound to, or ``None`` if not yet."""
    row = (
        await tx.execute(
            _binding_query().where(
                job_step_bindings.c.job_id == job_id, job_step_bindings.c.cache_key == cache_key
            )
        )
    ).first()
    return None if row is None else _step_binding(row)


async def get_step_bindings(
    tx: Transaction, job_id: JobID, cache_keys: Collection[str]
) -> dict[str, StepBindingRecord]:
    """:func:`get_step_binding` for many keys, by key; a key the job's request is not yet bound
    at is absent."""
    found: dict[str, StepBindingRecord] = {}
    for batch in itertools.batched(cache_keys, BATCH_SIZE):
        rows = await tx.execute(
            _binding_query().where(
                job_step_bindings.c.job_id == job_id, job_step_bindings.c.cache_key.in_(batch)
            )
        )
        found.update((row.cache_key, _step_binding(row)) for row in rows)
    return found


async def list_step_bindings(tx: Transaction, job_id: JobID) -> list[StepBindingRecord]:
    """A job's step bindings, oldest first."""
    rows = (
        await tx.execute(
            _binding_query()
            .where(job_step_bindings.c.job_id == job_id)
            .order_by(job_step_bindings.c.bound_at, job_step_bindings.c.cache_key)
        )
    ).all()
    return [_step_binding(row) for row in rows]


async def get_step_result(tx: Transaction, step_result_id: StepResultID) -> StepResultRecord:
    """A step result.

    Raises:
        NotFoundError: If there is no such step result.
    """
    row = (
        await tx.execute(select(step_results).where(step_results.c.id == step_result_id))
    ).first()
    if row is None:
        raise NotFoundError(f"No step result {step_result_id}")
    return _step_result(row)


async def get_step_results(
    tx: Transaction, step_result_ids: Collection[StepResultID]
) -> dict[StepResultID, StepResultRecord]:
    """:func:`get_step_result` for many, by id.

    Raises:
        NotFoundError: If any of them does not exist.
    """
    found: dict[StepResultID, StepResultRecord] = {}
    for batch in itertools.batched(step_result_ids, BATCH_SIZE):
        rows = await tx.execute(select(step_results).where(step_results.c.id.in_(batch)))
        found.update((StepResultID(row.id), _step_result(row)) for row in rows)
    missing = set(step_result_ids) - found.keys()
    if missing:
        raise NotFoundError(f"No step results {sorted(map(str, missing))}")
    return found


async def get_job_attempt_provenance(
    tx: Transaction, job_attempt_id: JobAttemptID
) -> JobAttemptProvenanceRecord:
    """That a job attempt existed and which code it ran.

    Raises:
        NotFoundError: If there is no such job attempt fact.
    """
    row = (
        await tx.execute(
            select(job_attempt_provenance).where(job_attempt_provenance.c.id == job_attempt_id)
        )
    ).first()
    if row is None:
        raise NotFoundError(f"No job attempt {job_attempt_id}")
    return _job_attempt_provenance(row)


async def get_job_attempt_provenances(
    tx: Transaction, job_attempt_ids: Collection[JobAttemptID]
) -> dict[JobAttemptID, JobAttemptProvenanceRecord]:
    """:func:`get_job_attempt_provenance` for many, by id.

    Raises:
        NotFoundError: If any of them has no job attempt fact.
    """
    found: dict[JobAttemptID, JobAttemptProvenanceRecord] = {}
    for batch in itertools.batched(job_attempt_ids, BATCH_SIZE):
        rows = await tx.execute(
            select(job_attempt_provenance).where(job_attempt_provenance.c.id.in_(batch))
        )
        found.update((JobAttemptID(row.id), _job_attempt_provenance(row)) for row in rows)
    missing = set(job_attempt_ids) - found.keys()
    if missing:
        raise NotFoundError(f"No job attempts {sorted(map(str, missing))}")
    return found


async def load_nodes(tx: Transaction, invocation_id: InvocationID) -> list[NodeRecord]:
    """Every node of an invocation, in creation order, rebuilt from its row and edges."""
    node_rows = (
        await tx.execute(
            select(nodes).where(nodes.c.invocation_id == invocation_id).order_by(nodes.c.created_at)
        )
    ).all()
    edge_rows = (
        await tx.execute(select(node_inputs).where(node_inputs.c.invocation_id == invocation_id))
    ).all()
    edges: dict[bytes, list[tuple[Any, NodeIdentity]]] = {}
    for edge in edge_rows:
        edges.setdefault(bytes(edge.node_identity), []).append(
            (edge.label, EntityID(cid=bytes(edge.input_identity)))
        )
    records: list[NodeRecord] = []
    for row in node_rows:
        identity = bytes(row.identity)
        node = from_row(
            NodeRow(kind=row.kind, params=dict(row.params), inputs=_ordered_edges(row, edges))
        )
        records.append(
            NodeRecord(
                node=node,
                identity=EntityID(cid=identity),
                type=array_type_from_json(row.type),
                value=bound_array(row.value_cid),
            )
        )
    return records


async def list_events(tx: Transaction, attempt_id: AttemptID) -> list[EventRecord]:
    """An attempt's event log, in order."""
    rows = (
        await tx.execute(
            select(attempt_events)
            .where(attempt_events.c.attempt_id == attempt_uuid(attempt_id))
            .order_by(attempt_events.c.seq)
        )
    ).all()
    return [
        EventRecord(
            seq=row.seq, kind=row.kind, identity=EntityID(cid=bytes(row.identity)), at=row.at
        )
        for row in rows
    ]


# ---- row encoding and decoding ----


def step_result_of(attempt_id: AttemptID) -> StepResultID:
    """The id of the step result a step attempt produces: the attempt's own id. The one place the
    relationship is turned into an id (see ``step_results.id`` in the schema)."""
    return StepResultID(attempt_uuid(attempt_id))


def producing_attempt(step_result_id: StepResultID) -> AttemptID:
    """The id of the step attempt that produced a step result, which this database has only if it
    ran that attempt: the step result's own id (see :func:`step_result_of`)."""
    return AttemptID(str(step_result_id))


def attempt_uuid(attempt_id: AttemptID) -> uuid.UUID:
    """An attempt id (a string on the protocol) as the uuid the ``attempts`` table stores."""
    return uuid.UUID(attempt_id)


def attempt_id_of(value: uuid.UUID | None) -> AttemptID | None:
    """A stored attempt uuid as the protocol's attempt id."""
    return None if value is None else AttemptID(str(value))


def bound_array(cid: bytes | None) -> BoundID[Array[Any]] | None:
    """A stored cid as a reference to the ``Array`` entity it names."""
    return None if cid is None else EntityID(cid=bytes(cid)).bind_type(Array[Any])


def identity_from_bytes(value: bytes | None) -> NodeIdentity | None:
    """A stored identity as a node identity."""
    return None if value is None else EntityID(cid=bytes(value))


def _leaf_value(node: Node) -> BoundID[Array[Any]] | None:
    """The array a leaf node references, which is its value; ``None`` for other kinds."""
    match node:
        case Input(array=ref) | SourceArray(array=ref):
            return ref
        case _:
            return None


def _invocation(row: Row[Any]) -> InvocationRecord:
    return InvocationRecord(
        id=InvocationID(row.id),
        job_id=JobID(row.job_id),
        pathname=row.pathname,
        workflow_name=row.workflow_name,
        cache_key_prefix=row.cache_key_prefix,
        returned_node=identity_from_bytes(row.returned_node),
        result=bound_array(row.result_cid),
        job_attempt_id=None if row.job_attempt_id is None else JobAttemptID(row.job_attempt_id),
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


def _job_step_request(row: Row[Any]) -> JobStepRequestRecord:
    return JobStepRequestRecord(
        job_id=JobID(row.job_id),
        cache_key=row.cache_key,
        request=EntityID(cid=bytes(row.request_cid)),
    )


def _binding_query() -> Any:
    return select(job_step_bindings, step_results.c.result_cid).join(
        step_results, step_results.c.id == job_step_bindings.c.step_result_id
    )


def _step_binding(row: Row[Any]) -> StepBindingRecord:
    result = bound_array(row.result_cid)
    assert result is not None
    return StepBindingRecord(
        job_id=JobID(row.job_id),
        cache_key=row.cache_key,
        step_result_id=StepResultID(row.step_result_id),
        result=result,
    )


def _step_result(row: Row[Any]) -> StepResultRecord:
    result = bound_array(row.result_cid)
    assert result is not None
    return StepResultRecord(
        id=StepResultID(row.id),
        request=EntityID(cid=bytes(row.request_cid)),
        result=result,
        job_attempt_id=JobAttemptID(row.job_attempt_id),
        completed_at=row.completed_at,
    )


def _job_attempt_provenance(row: Row[Any]) -> JobAttemptProvenanceRecord:
    return JobAttemptProvenanceRecord(
        id=JobAttemptID(row.id),
        job_id=JobID(row.job_id),
        code_version=EntityID(cid=bytes(row.code_version_cid)).bind_type(CodeVersion),
        created_at=row.created_at,
    )


def _spawn(row: Row[Any]) -> SpawnRecord:
    return SpawnRecord(
        invocation_id=InvocationID(row.invocation_id),
        node_identity=EntityID(cid=bytes(row.node_identity)),
        key=tuple(row.key),
        child_invocation_id=None
        if row.child_invocation_id is None
        else InvocationID(row.child_invocation_id),
        step_cache_key=row.step_cache_key,
    )


def _ordered_edges(
    row: Row[Any], edges: Mapping[bytes, list[tuple[Any, NodeIdentity]]]
) -> tuple[tuple[Any, NodeIdentity], ...]:
    """A node's edges in the order its entity form expects: the order of its input labels in
    ``params`` is not recorded, so combine and run nodes (frozendict inputs) are order-free,
    while stack needs its key order and a query its source positions, which sorting restores."""
    found = edges.get(bytes(row.identity), [])
    if row.kind in ("stack", "query"):
        return tuple(sorted(found, key=lambda e: e[0]))
    return tuple(found)


async def _insert_node(
    tx: Transaction,
    invocation_id: InvocationID,
    node: Node,
    ttype: ArrayType,
    *,
    value: BoundID[Array[Any]] | None,
) -> bool:
    """Insert a node (and its edges) unless the invocation has it. Returns whether it was new."""
    row = to_row(node)
    identity = node.content_id()
    result = await tx.execute(
        insert(nodes)
        .values(
            invocation_id=invocation_id,
            identity=identity.cid,
            kind=row.kind,
            params=row.params,
            type=array_type_to_json(ttype),
            value_cid=None if value is None else value.entity_id.cid,
        )
        .on_conflict_do_nothing()
        .returning(nodes.c.identity)
    )
    if result.first() is None:
        return False
    if row.inputs:
        await tx.execute(
            insert(node_inputs),
            [
                {
                    "invocation_id": invocation_id,
                    "node_identity": identity.cid,
                    "label": label,
                    "input_identity": input_identity.cid,
                }
                for label, input_identity in row.inputs
            ],
        )
    return True


async def _require_node(
    tx: Transaction, invocation_id: InvocationID, identity: NodeIdentity
) -> None:
    """Check the invocation has the node.

    Raises:
        NotFoundError: If it does not.
    """
    row = (
        await tx.execute(
            select(nodes.c.identity).where(
                nodes.c.invocation_id == invocation_id, nodes.c.identity == identity.cid
            )
        )
    ).first()
    if row is None:
        raise NotFoundError(f"Invocation {invocation_id} has no node {identity!r}")


async def _next_seq(tx: Transaction, attempt_id: AttemptID) -> int:
    """The sequence number the attempt's next event gets."""
    row = (
        await tx.execute(
            select(func.coalesce(func.max(attempt_events.c.seq) + 1, 0)).where(
                attempt_events.c.attempt_id == attempt_uuid(attempt_id)
            )
        )
    ).first()
    assert row is not None
    return int(row[0])


async def _append_events(
    tx: Transaction, attempt_id: AttemptID, events: Sequence[tuple[str, NodeIdentity]]
) -> None:
    """Append ``(kind, identity)`` events to the attempt's log, numbered after its last."""
    if not events:
        return
    seq = await _next_seq(tx, attempt_id)
    await tx.execute(
        insert(attempt_events),
        [
            {
                "attempt_id": attempt_uuid(attempt_id),
                "seq": seq + i,
                "kind": kind,
                "identity": identity.cid,
            }
            for i, (kind, identity) in enumerate(events)
        ],
    )


__all__ = [
    "ActiveAttempt",
    "CacheAddressReuseError",
    "ChildSpec",
    "Emission",
    "EventRecord",
    "InvocationID",
    "InvocationRecord",
    "JobID",
    "JobRecord",
    "JobStepRequestRecord",
    "LedgerError",
    "NodeRecord",
    "NotFoundError",
    "JobAttemptID",
    "JobAttemptProvenanceRecord",
    "SpawnRecord",
    "StepBindingRecord",
    "StepRequestRecord",
    "StepResultID",
    "StepResultRecord",
    "attempt_id_of",
    "attempt_uuid",
    "bind_step_result",
    "bound_array",
    "create_invocation",
    "create_job",
    "expand_run_node",
    "find_invocation",
    "find_parent",
    "get_invocation",
    "get_invocations",
    "get_job",
    "get_job_attempt_provenance",
    "get_job_step_request",
    "get_step_binding",
    "get_step_request",
    "get_step_result",
    "identity_from_bytes",
    "list_events",
    "list_invocations",
    "list_job_step_requests",
    "list_jobs",
    "list_step_bindings",
    "load_nodes",
    "load_spawns",
    "producing_attempt",
    "record_emissions",
    "record_observation",
    "record_result",
    "record_return",
    "record_step_request",
    "record_step_result",
    "record_step_spawns",
    "step_result_of",
]
