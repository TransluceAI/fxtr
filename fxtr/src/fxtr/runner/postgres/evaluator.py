"""The evaluator: the values of an attempt's DAG, and the task that delivers them.

An attempt's view of its invocation's DAG is a :data:`Dag`: one :class:`NodeState` per node the
ledger has, holding the node, its derived type, and, once known, its value or its error. Types are
fixed when a node is created (:func:`derive`, called by the recorder as it records an emission);
values and errors are filled in by :func:`evaluate`, which computes every derived node whose inputs
have values. Both are synchronous functions of the view alone, with no database or task machinery,
so they can be tested on their own.

**Query nodes** are derived nodes: :func:`~fxtr.runner.nodes.compute` runs a query's plan over
its inputs' arrays in DuckDB, in this process and on this thread. A query's value is not stored
for its own sake, so a restarted attempt runs the query again; it is stored where any value is,
when it feeds a step or a child workflow or is the attempt's return.

:class:`Evaluator` is the task that drives evaluation for one live attempt
(:class:`~fxtr.runner.postgres.attempts.Attempt`). It is woken by the recorder having committed a
batch, by the topic of every target it is waiting on (an attempt of a child ended; a step attempt
at a cache key ended, wherever it ran), and by the job attempt's topic (the job's active job
attempt changed, which changes what every target is), with a fallback interval for changes no
wake announced. On each wake it evaluates, answers
the observes whose node has settled (the value, or a failure of the attempt if the node failed),
and, once the body has returned and the returned node has a value, materializes the result and
closes the attempt as completed. It is the only writer of node values and errors in the view, and
the only one that finishes an attempt; the recorder only adds nodes.

**Run nodes.** A run node's value comes from other work. When all of its inputs have values the
evaluator *expands* it (:func:`~fxtr.runner.nodes.expand`): the sliced inputs are stored, and one
transaction records what the node expanded into (the ``spawns`` rows) and requests it. For a child
workflow node that is the child invocations, created
(:func:`~fxtr.runner.postgres.ledger.expand_run_node`); for a step node it is one
:class:`~fxtr.runner.steps.StepRequest` per joint key, each recorded as the job's request at its
cache key (:func:`~fxtr.runner.postgres.ledger.record_step_request`) and resolved against the
cache entry (:func:`~fxtr.runner.postgres.execution.resolve_step`). What the expansion created for
this job to run (children; step requests nothing else is computing) is pushed onto the runner's
queue (:class:`~fxtr.runner.postgres.runner.WorkSink`) when the transaction commits, so work is
created where the ledger fact is written. On later wakes it
*collects*, asking the policy's :func:`~fxtr.runner.postgres.execution.statuses` (children) and
:func:`~fxtr.runner.postgres.execution.step_statuses` (steps) about the targets that may have
changed. Done is permanent (a binding, or an invocation's result), so the evaluator keeps each done
target's result and never judges that target again. A step whose result
another job's equal attempt has produced is *taken*: the evaluator writes this job's binding
(:func:`~fxtr.runner.postgres.execution.take_step_result`), and only then is the target done; a
target found due with nothing running it (the one case a commit in this process cannot have pushed:
another job's attempt at a shared key failed or was abandoned) is pushed onto the runner's queue.
Once every target is done, the run node's value is the stack of their results
(:func:`~fxtr.runner.nodes.collect`), each checked assignable to the node's declared type; a target
that is stopped gives the run node an error marked as an *interruption*, since the fault is another
invocation's (a failed child or step, a cache conflict with another job's request, no active job
attempt). The evaluator never reads attempt states or the cache itself.

**What a pass judges, and when.** A job can have thousands of attempts live at once, each waiting
on its own few targets, and one attempt can wait on thousands of targets. So a pass costs what
changed, not what is outstanding: the evaluator holds one standing
:class:`~fxtr.runner.postgres.database.Subscription` to its targets' topics, which tells it which
were woken, and judges only those targets. A target is first judged when its run node is first
seen expanded, unless the runner holds it
(:meth:`~fxtr.runner.postgres.runner.WorkSink.holds`): then it is pending, and the runner wakes
its topic when it stops holding it. Every outstanding target is judged again when the job
attempt's topic wakes and when the fallback interval passes. That interval is the runner's poll
interval while some target is *polled for*, one that no commit of this process is sure to
announce: a step another job's process is computing, or a result on offer that could not be
taken. Otherwise it is the runner's reconciliation interval, a safety net for a missed wake,
since everything the attempt waits on is then this process's own to run and announce.

**Errors** are not written to node rows. A derived node's error is a deterministic function of the
leaves, like its value, so it is recomputed by whoever needs it; what the ledger records is the
attempt's closing error and the nodes it stopped on (``attempts.stopped_on``). Each node's error
in the view remembers its *origin*, the node whose own computation raised or whose child failed,
and whether it is an interruption, so that the attempt closes in the right state naming the right
nodes: every node must have a value for the attempt to complete, so at return the origins of all
outstanding errors are named; at observe, the observed node's origin is. An attempt whose only
troubles are interruptions is *interrupted* (nothing is wrong with it; resuming it once the children
are fixed lets it complete); any error of its own makes it *failed*.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import anyio

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import UnknownReferenceError
from fxtr.entity_defns.array import Array, ArrayType, Key
from fxtr.experiment.workflows import append_mapped_key, join_path
from fxtr.runner import nodes
from fxtr.runner.nodes import Node, NodeIdentity, node_inputs, node_type
from fxtr.runner.postgres import execution, ledger
from fxtr.runner.postgres.attempts import Attempt, Completed, Observe
from fxtr.runner.postgres.database import Database, Transaction
from fxtr.runner.postgres.execution import (
    StaleAttemptError,
    invocation_topic,
    job_attempt_topic,
    step_topic,
)
from fxtr.runner.postgres.ledger import CacheAddressReuseError, SpawnRecord
from fxtr.runner.session import (
    AttemptClosedError,
    AttemptError,
    NodeFailedError,
    RunnerError,
)
from fxtr.runner.steps import StepRequest, cache_key, request_for

if TYPE_CHECKING:
    from fxtr.runner.postgres.runner import WorkSink

# ---- the view ----


@dataclasses.dataclass
class NodeState:
    """What the host knows about one node of the attempt's invocation.

    Attributes:
        node: The node.
        type: Its array type, derived from its contents when it was created and never changed.
        value: Its array, once known: a leaf's is loaded when the node is created or seeded from
            the ledger; a derived node's, a query node's included, is computed by :func:`evaluate`;
            a run node's is collected from its targets' results. ``None`` while unknown.
        error: Why computing it failed, if it did (its own computation raised, or an input
            failed). A node with an error never gets a value.
        origin: Where the error came from: this node, if its own computation raised or its
            children failed, or the upstream node whose error propagated to it. ``None`` while
            there is no error.
        interrupted: Whether the error is an interruption (a child of the origin failed) rather
            than this invocation's own fault.
        stored: The value's reference when storage already holds it (a source array, a recorded
            value), so returning the node does not store it again. ``None`` otherwise.
    """

    node: Node
    type: ArrayType
    value: Array[Any] | None = None
    error: AttemptError | None = None
    origin: NodeIdentity | None = None
    interrupted: bool = False
    stored: BoundID[Array[Any]] | None = None

    def fail_from(self, upstream: NodeState) -> None:
        """Take over an input's error, origin, and interruption flag."""
        self.error = upstream.error
        self.origin = upstream.origin
        self.interrupted = upstream.interrupted

    @property
    def settled(self) -> bool:
        """Whether the node has a value or an error: nothing more will happen to it."""
        return self.value is not None or self.error is not None


type Dag = dict[NodeIdentity, NodeState]
"""An attempt's view of its invocation's DAG, by node identity. Mirrors the ledger's ``nodes``
rows for the invocation: a node is added when its row is committed (or when the attempt is seeded
from rows that exist), never removed."""


class MalformedEmissionError(RunnerError):
    """The body emitted a node the host cannot accept: an ``input`` node (only the system
    creates those), or a node whose input is not a node of the invocation."""


def derive(node: Node, dag: Dag, leaf: Array[Any] | None) -> NodeState:
    """A node's state at emission: its type derived from its contents, and its value if it is a
    leaf.

    Args:
        node: The emitted node.
        dag: The view, which must already hold every node ``node`` takes as an input.
        leaf: For a source array, the referenced array, loaded by the caller;
            ``None`` for derived nodes and run nodes.

    Raises:
        MalformedEmissionError: If ``node`` is an ``input`` node or names an unknown input.
    """
    if isinstance(node, nodes.Input):
        raise MalformedEmissionError("Input nodes are created with the invocation, not emitted")

    def input_type(identity: NodeIdentity) -> ArrayType:
        state = dag.get(identity)
        if state is None:
            raise MalformedEmissionError(f"Node input {identity!r} is not a node of the invocation")
        return state.type

    def leaf_type(ref: Any) -> ArrayType:
        assert leaf is not None, "a leaf's array is loaded before its type is derived"
        return leaf.type

    stored = node.array if isinstance(node, nodes.SourceArray) else None
    return NodeState(node, node_type(node, input_type, leaf_type), value=leaf, stored=stored)


def ready_inputs(dag: Dag, state: NodeState) -> dict[Any, Array[Any]] | None:
    """The input values a node needs to be computed or expanded, by edge label, if every input has
    a value.

    If an input has an error, the node takes it over (:meth:`NodeState.fail_from`), so failures
    propagate down the DAG; if an input is merely not settled yet, nothing changes. Either way
    ``None`` is returned and the node is not ready.
    """
    inputs: dict[Any, Array[Any]] = {}
    for label, input_identity in node_inputs(state.node):
        input_state = dag.get(input_identity)
        if input_state is None or not input_state.settled:
            return None
        if input_state.error is not None:
            state.fail_from(input_state)
            return None
        assert input_state.value is not None
        inputs[label] = input_state.value
    return inputs


def evaluate(dag: Dag) -> list[tuple[NodeIdentity, AttemptError]]:
    """Compute every derived node whose inputs all have values, repeating until nothing changes.

    A node whose computation raises gets the error, with itself as origin; a node one of whose
    inputs has an error gets that error and origin too, so failures propagate down the DAG. Leaves
    are never touched (they arrive settled) and run nodes are skipped. Synchronous, so the caller
    sees the view change atomically.

    Returns:
        The nodes whose own computation newly raised, with their errors.
    """
    failed: list[tuple[NodeIdentity, AttemptError]] = []
    changed = True
    while changed:
        changed = False
        for identity, state in list(dag.items()):
            if state.settled or isinstance(
                state.node, nodes.Input | nodes.SourceArray | nodes.RunStep | nodes.RunChildWorkflow
            ):
                continue
            inputs = ready_inputs(dag, state)
            if inputs is not None:
                try:
                    state.value = nodes.compute(state.node, inputs)
                except Exception as exc:
                    state.error = AttemptError.from_exception(exc)
                    state.origin = identity
                    failed.append((identity, state.error))
            if state.settled:
                changed = True
    return failed


# ---- the task ----


class Evaluator:
    """The evaluator task for one live attempt (see the module docstring).

    Args:
        attempt: The attempt whose view this evaluates.
        database: The runner's database.
        sink: Where the work an expansion creates is pushed: the runner's queue.
        poll_interval: Seconds to wait for a wake before judging everything again anyway, while
            the attempt waits on a target no commit of this process is sure to announce.
        reconcile_interval: The same wait while every target is this process's own to run: a
            safety net, since its commits wake this task.
    """

    def __init__(
        self,
        attempt: Attempt,
        database: Database,
        sink: WorkSink,
        *,
        poll_interval: float,
        reconcile_interval: float,
    ) -> None:
        self._attempt = attempt
        self._database = database
        self._sink = sink
        self._poll_interval = poll_interval
        self._reconcile_interval = reconcile_interval
        # The results of each run node's targets found done so far. A target once done stays
        # done (its binding, or its invocation's result, is permanent), so it is not judged
        # again.
        self._done: dict[NodeIdentity, dict[Key, BoundID[Array[Any]]]] = {}
        # The targets still waited on, by the topic that announces a change of theirs: the spawns
        # of every watched run node that have not been found done, each with its position in the
        # order they were first waited on, which is the order a pass judges them in.
        self._waiting: dict[str, dict[tuple[NodeIdentity, Key], tuple[int, SpawnRecord]]] = {}
        self._watched: set[NodeIdentity] = set()
        self._positions = itertools.count()
        # The topics whose targets the next pass judges, and those whose targets have to be
        # polled for (see the module docstring).
        self._to_judge: set[str] = set()
        self._polled: set[str] = set()
        self._job_attempt_topic = job_attempt_topic(attempt.invocation.job_id)
        self._subscription = database.subscribe()
        self._subscription.add(self._job_attempt_topic)

    async def run(self) -> None:
        """Evaluate on every wake until the attempt is closed."""
        attempt = self._attempt
        try:
            while not attempt.ended:
                await self._wait_for_change()
                if attempt.ended:
                    return
                await self.step()
        finally:
            self._subscription.close()

    async def _wait_for_change(self) -> None:
        """Wait until the recorder has published something, a target this attempt waits on
        changed, the job's active job attempt changed, or the fallback interval passes; note
        which targets the next pass has to judge; then arm the next wait.

        A wake of a target's topic marks that target. A change of the job attempt changes what
        every target is, and the fallback is for changes no wake announced, so both mark every
        target. The fallback is the poll interval while some target is polled for, and the
        reconciliation interval otherwise."""
        attempt = self._attempt
        woken = await self._subscription.wait(
            attempt.changed, self._poll_interval if self._polled else self._reconcile_interval
        )
        attempt.changed = anyio.Event()
        if woken is None or self._job_attempt_topic in woken:
            self._to_judge.update(self._waiting)
        else:
            self._to_judge.update(woken)

    def _watch(self, identity: NodeIdentity) -> None:
        """Start waiting on the targets of an expanded run node: every spawn is waited on under
        its topic, and judged by the next pass unless the runner holds it. What the runner holds
        is pending, as an expansion has just found it, and its topic is woken when that changes
        (:meth:`~fxtr.runner.postgres.runner.WorkSink.holds`)."""
        self._watched.add(identity)
        for spawn in self._attempt.spawns[identity]:
            topic = _topic(spawn)
            self._waiting.setdefault(topic, {})[identity, spawn.key] = (
                next(self._positions),
                spawn,
            )
            self._subscription.add(topic)
            if not self._sink.holds(_item(spawn)):
                self._to_judge.add(topic)

    def _forget(self, topic: str, identity: NodeIdentity, key: Key) -> None:
        """Stop waiting on one target: it is done, or its run node has settled without it."""
        waiting = self._waiting.get(topic)
        if waiting is None or waiting.pop((identity, key), None) is None or waiting:
            return
        del self._waiting[topic]
        self._subscription.discard(topic)
        self._polled.discard(topic)

    async def step(self) -> None:
        """One evaluation pass: compute what is computable, expand run nodes whose inputs have
        values, collect those whose children have finished, compute what the collected values
        make computable, answer the observes that have settled, and finish the attempt if its
        return has settled."""
        attempt = self._attempt
        evaluate(attempt.dag)
        await self._expand_ready()
        if attempt.ended:
            return
        await self._collect()
        if attempt.ended:
            return
        evaluate(attempt.dag)
        observed_failure = self._answer_observes()
        if observed_failure is not None:
            # The body asked for a node that failed. Its observe call already got NodeFailedError;
            # the body stops there, so the worker is not cancelled.
            await self._stop_on([observed_failure])
            return
        if attempt.returned is not None:
            await self._finish()

    async def _expand_ready(self) -> None:
        """Settle what can be settled about the run nodes that are not expanded yet: a run node
        with a failed input takes the input's error like any other node (:func:`ready_inputs`); a
        run node whose inputs all have values is expanded (:meth:`_expand` for a child workflow,
        :meth:`_expand_step` for a step). Run nodes with an unsettled input wait."""
        attempt = self._attempt
        for identity, state in list(attempt.dag.items()):
            node = state.node
            if state.settled or identity in attempt.spawns:
                continue
            if not isinstance(node, nodes.RunChildWorkflow | nodes.RunStep):
                continue
            inputs = ready_inputs(attempt.dag, state)
            if inputs is None:
                continue
            named = {str(k): v for k, v in inputs.items()}
            if isinstance(node, nodes.RunChildWorkflow):
                await self._expand(identity, node, named)
            else:
                await self._expand_step(identity, node, named)
            if attempt.ended:
                return

    def _push_on_commit(self, tx: Transaction, item: execution.WorkItem) -> None:
        """Queue one unit of work the transaction is creating, once it has committed: nothing is
        started for a transaction that rolls back."""
        tx.on_commit(lambda: self._sink.enqueue(item))

    async def _expand(
        self, identity: NodeIdentity, node: nodes.RunChildWorkflow, inputs: Mapping[str, Array[Any]]
    ) -> None:
        """Expand one run node: create its child invocations, or find them if an earlier attempt
        already did, and remember them in the attempt's spawns.

        The sliced inputs go into the attempt's entity store and are flushed in the expansion
        transaction, so the children's input nodes reference entities the database holds before
        the children can be started, and each child created is pushed onto the runner's queue once
        the transaction commits. The spawns are read back in the same transaction rather than taken
        from what was inserted, so an already-expanded node (a resumed parent) yields the earlier
        attempt's children, which are queued by the runner's own reconciliation rather than from
        here. An expansion that raises (a ``map_over`` that does not resolve) is the run node's own
        error, and so is an input that names an entity storage does not have (a query can write
        any entity ID).

        Args:
            identity: The run node.
            node: The node itself.
            inputs: Its input arrays by argument name, all of which have values.
        """
        attempt = self._attempt
        try:
            expansion = nodes.expand(node, inputs)
        except Exception as exc:
            self._fail_node(identity, exc)
            return
        base = join_path(attempt.invocation.pathname, node.name)
        try:
            children = [
                ledger.ChildSpec(
                    key=spawn.key,
                    pathname=append_mapped_key(base, spawn.mapped),
                    workflow_name=node.workflow_name,
                    cache_key_prefix=append_mapped_key(node.cache_key_prefix, spawn.mapped),
                    inputs={
                        name: (await attempt.entities.store(array), array.type)
                        for name, array in spawn.inputs.items()
                    },
                )
                for spawn in expansion.spawns
            ]
        except UnknownReferenceError as exc:
            self._fail_node(identity, exc)
            return
        try:
            async with self._database.transaction() as tx:
                active = await execution.guard(tx, attempt.id)
                await attempt.entities.flush(tx)
                created = await ledger.expand_run_node(tx, active, identity, children)
                for child in created:
                    self._push_on_commit(tx, execution.StartInvocation(child.id))
                spawned = await ledger.load_spawns(tx, active.invocation_id, identity)
        except StaleAttemptError as exc:
            attempt.mark_closed(refusal=AttemptClosedError(str(exc)))
            return
        attempt.spawns[identity] = tuple(spawned)

    async def _expand_step(
        self, identity: NodeIdentity, node: nodes.RunStep, inputs: Mapping[str, Array[Any]]
    ) -> None:
        """Expand one step node: for each spawn, store the sliced inputs and the request they make,
        record the job's request at its cache key, resolve it against the cache, and record the
        spawns.

        A key nothing live owns is pushed onto the runner's queue as this job's to run, once the
        transaction commits; a key an equal request has already completed at is taken on the next
        collect pass, like any other completion.

        Everything happens in one transaction, so a refusal for any key records nothing for the
        node. The one refusal is the job's own earlier request at a key carrying a different
        configuration (one key for two configurations), the body's fault: the node's own error. A
        key another job's live request owns is not a refusal: the request is recorded and the key
        resolves as a cache conflict, which
        :func:`~fxtr.runner.postgres.execution.step_status` reports as an interruption until the
        key is cleared and the job resumed. An input that names an entity storage does not have (a
        query can write any entity ID) is the node's own error too, before anything is recorded.

        Args:
            identity: The run node.
            node: The node itself.
            inputs: Its input arrays by argument name, all of which have values.
        """
        attempt = self._attempt
        try:
            expansion = nodes.expand(node, inputs)
        except Exception as exc:
            self._fail_node(identity, exc)
            return
        requests: list[tuple[Key, str, BoundID[StepRequest]]] = []
        try:
            for spawn in expansion.spawns:
                refs = {
                    name: await attempt.entities.store(array)
                    for name, array in spawn.inputs.items()
                }
                request = await attempt.entities.store(request_for(node, refs))
                requests.append((spawn.key, cache_key(node, spawn.mapped), request))
        except UnknownReferenceError as exc:
            self._fail_node(identity, exc)
            return
        try:
            async with self._database.transaction() as tx:
                active = await execution.guard(tx, attempt.id)
                await attempt.entities.flush(tx)
                # A node can spawn many thousands of steps: record and resolve them all in a few
                # statements, not a few per key.
                by_key = {key: request for _, key, request in requests}
                await ledger.record_step_requests(tx, active, by_key, node.step_name)
                resolutions = await execution.resolve_steps(
                    tx, active.job_id, {key: r.entity_id for key, r in by_key.items()}
                )
                for _, key, _ in requests:
                    if isinstance(resolutions[key], execution.RunIt):
                        self._push_on_commit(tx, execution.StartStep(key))
                await ledger.record_step_spawns(
                    tx, active, identity, {spawn_key: key for spawn_key, key, _ in requests}
                )
                spawned = await ledger.load_spawns(tx, active.invocation_id, identity)
        except StaleAttemptError as exc:
            attempt.mark_closed(refusal=AttemptClosedError(str(exc)))
            return
        except CacheAddressReuseError as exc:
            state = attempt.dag[identity]
            state.error = AttemptError.from_exception(exc)
            state.origin = identity
            return
        attempt.spawns[identity] = tuple(spawned)

    async def _collect(self) -> None:
        """Give every expanded, unsettled run node its value once all its targets have finished,
        or an interruption if any target has failed.

        A pass judges the targets marked for it (:meth:`_wait_for_change`) and, of a run node
        seen for the first time, those the runner does not hold (:meth:`_watch`). They are read
        in one snapshot transaction (a
        child's row and its attempts must agree for its state to be judged), and only what was
        read is kept; the view is changed and the results loaded afterwards, so the transaction
        holds no I/O but its own. A target found done is not judged again: done is permanent (a
        binding, or an invocation's result), so the evaluator keeps its result and stops waiting
        on it. Each target is judged by the execution policy,
        :func:`~fxtr.runner.postgres.execution.statuses` for the children and
        :func:`~fxtr.runner.postgres.execution.step_statuses` for the steps (each kind in one
        query for all of the pass's targets): done, ready to take (an equal attempt completed at
        the key; this job binds to it in a short write transaction after the snapshot, through
        :func:`~fxtr.runner.postgres.execution.take_step_result`, and the target is done once the
        binding is written), pending (the node waits), or stopped (an interruption of the node). A
        pending target that nothing is running is due, and is pushed onto the runner's queue: its
        trigger was another job's attempt at the key ending without a result, which no commit in
        this process saw. A step running under an attempt the runner does not hold, and a result
        on offer that could not be taken, are polled for from here on. Several stopped targets
        are all named in the node's error; an interruption follows as soon as one has stopped,
        without waiting for the rest. A result that does not fit the node's declared type is the
        node's own error.
        """
        attempt = self._attempt
        pending = {
            identity: state
            for identity, state in attempt.dag.items()
            if not state.settled and identity in attempt.spawns
        }
        for identity in pending:
            if identity not in self._watched:
                self._watch(identity)
        judging, self._to_judge = self._to_judge, set()
        # In the order the targets were first waited on, whatever order their topics woke in, so
        # that what a pass reports does not depend on timing.
        targets = [
            (topic, identity, spawn)
            for _, topic, identity, spawn in sorted(
                (
                    (position, topic, identity, spawn)
                    for topic in judging
                    for (identity, _), (position, spawn) in self._waiting.get(topic, {}).items()
                ),
                key=lambda target: target[0],
            )
        ]
        failures: dict[NodeIdentity, list[str]] = {}
        if targets:
            await self._judge(targets, failures)
            if attempt.ended:
                return
        for identity, state in pending.items():
            node = state.node
            assert isinstance(node, nodes.RunChildWorkflow | nodes.RunStep)
            refs = self._done.setdefault(identity, {})
            if identity in failures:
                state.error = AttemptError(
                    type_name="RunTargetFailedError",
                    message=", ".join(failures[identity]),
                    traceback="",
                )
                state.origin = identity
                state.interrupted = True
            elif len(refs) == len(attempt.spawns[identity]):
                data = await attempt.entities.load_raw_data_many(
                    [ref.entity_id for ref in refs.values()]
                )
                values = {
                    key: ref.loader.convert(raw)
                    for (key, ref), raw in zip(refs.items(), data, strict=True)
                }
                try:
                    state.value = nodes.collect(node, values)
                except Exception as exc:  # a result that does not fit the declared type
                    state.error = AttemptError.from_exception(exc)
                    state.origin = identity
            else:
                continue
            # Settled: whatever of it is still outstanding is no longer waited on.
            for spawn in attempt.spawns[identity]:
                self._forget(_topic(spawn), identity, spawn.key)

    async def _judge(
        self,
        targets: Sequence[tuple[str, NodeIdentity, SpawnRecord]],
        failures: dict[NodeIdentity, list[str]],
    ) -> None:
        """Judge the targets of one pass (see :meth:`_collect`): record the done ones, take the
        results on offer, queue what is due, and note which are polled for.

        Args:
            targets: The targets, each with its topic and its run node.
            failures: Filled in with the reasons of each run node's stopped targets.
        """
        attempt = self._attempt
        job_id = attempt.invocation.job_id
        conflicts: dict[NodeIdentity, list[str]] = {}
        to_take: list[tuple[str, NodeIdentity, Key, str]] = []
        # One snapshot: a target's rows must agree.
        async with self._database.transaction(snapshot=True) as tx:
            steps = await execution.step_statuses(
                tx,
                job_id,
                {
                    spawn.step_cache_key
                    for _, _, spawn in targets
                    if spawn.step_cache_key is not None
                },
            )
            children = await execution.statuses(
                tx,
                job_id,
                (
                    await ledger.get_invocations(
                        tx,
                        {
                            spawn.child_invocation_id
                            for _, _, spawn in targets
                            if spawn.child_invocation_id is not None
                        },
                    )
                ).values(),
            )
        for topic, identity, spawn in targets:
            cache_key = spawn.step_cache_key
            if spawn.child_invocation_id is not None:
                judged = children[spawn.child_invocation_id]
            else:
                assert cache_key is not None
                judged = steps[cache_key]
            polled = False
            match judged:
                case execution.Done(result=result):
                    self._done.setdefault(identity, {})[spawn.key] = result
                    self._forget(topic, identity, spawn.key)
                    continue
                case execution.Ready():
                    assert cache_key is not None
                    to_take.append((topic, identity, spawn.key, cache_key))
                case execution.Stopped(cache_conflict=True):
                    assert cache_key is not None
                    conflicts.setdefault(identity, []).append(cache_key)
                case execution.Stopped(reason=reason):
                    failures.setdefault(identity, []).append(reason)
                case execution.Pending(attempt_id=None):
                    # Due, and nothing is running it: make sure the runner knows. The trigger
                    # was another job's attempt at the key ending without a result, which no
                    # commit in this process saw.
                    self._sink.enqueue(_item(spawn))
                case execution.Pending():
                    # Running. If the runner does not hold it, another job's process runs it,
                    # and nothing here is woken when it ends.
                    polled = not self._sink.holds(_item(spawn))
            if polled:
                self._polled.add(topic)
            else:
                self._polled.discard(topic)
        for identity, cache_keys in conflicts.items():
            # One clause for all of a run node's conflicts, however many spawns it has.
            failures.setdefault(identity, []).append(execution.conflict_reason(cache_keys))
        if to_take:
            # Take what is on offer: this job's bindings, written on behalf of this attempt. A
            # result no longer on offer (the key was cleared meanwhile) is simply not done yet,
            # and nothing announces what becomes of it: it is polled for.
            try:
                async with self._database.transaction() as tx:
                    active = await execution.guard(tx, attempt.id)
                    taken = await execution.take_step_results(
                        tx, active, {cache_key for _, _, _, cache_key in to_take}
                    )
            except StaleAttemptError as exc:
                attempt.mark_closed(refusal=AttemptClosedError(str(exc)))
                return
            for topic, identity, key, cache_key in to_take:
                if cache_key in taken:
                    self._done.setdefault(identity, {})[key] = taken[cache_key]
                    self._forget(topic, identity, key)
                else:
                    self._polled.add(topic)

    def _fail_node(self, identity: NodeIdentity, exc: Exception) -> None:
        """Give a run node the error its expansion raised, with itself as origin: the node's own
        failure, like a derived node whose computation raised."""
        state = self._attempt.dag[identity]
        state.error = AttemptError.from_exception(exc)
        state.origin = identity

    async def _stop_on(self, failed: Sequence[NodeIdentity]) -> None:
        """Close the attempt on account of failed nodes: as interrupted if every one of them is an
        interruption (a child failed), naming the run nodes; as failed otherwise, with the single
        node's own error or one error naming them all.

        Args:
            failed: Nodes with errors; each is resolved to its origin, which is what is named.
        """
        attempt = self._attempt
        origins: dict[NodeIdentity, NodeState] = {}
        for identity in failed:
            state = attempt.dag[identity]
            assert state.origin is not None
            origins[state.origin] = attempt.dag[state.origin]
        if all(state.interrupted for state in origins.values()):
            reason = "; ".join(
                state.error.message for state in origins.values() if state.error is not None
            )
            await attempt.interrupt(reason, stopped_on=list(origins))
            return
        errors = {
            identity: state.error for identity, state in origins.items() if state.error is not None
        }
        attempt.fail(_incomplete(errors), cancel_body=False, stopped_on=list(origins))
        await attempt.close()

    def _answer_observes(self) -> NodeIdentity | None:
        """Finish every pending observe whose node has settled: with its value, or with
        :class:`NodeFailedError` if the node failed. Returns the first such failed node."""
        attempt = self._attempt
        failure: NodeIdentity | None = None
        still_pending: list[Observe] = []
        for item in attempt.pending_observes:
            state = attempt.dag.get(item.identity)
            if state is None or not state.settled:
                still_pending.append(item)
            elif state.error is not None:
                failure = failure or item.identity
                item.finish(
                    NodeFailedError(
                        f"Node {item.identity!r} failed: {state.error.type_name}:"
                        f" {state.error.message}"
                    )
                )
            else:
                item.value = state.value
                item.finish()
        attempt.pending_observes = still_pending
        return failure

    async def _finish(self) -> None:
        """Finish the attempt once every node has settled: materialize the result and close as
        completed, or stop on the nodes that could not be computed. Every node must complete for
        the attempt to complete, so an unfinished child anywhere keeps the attempt open and a
        failed node anywhere stops it at return, observed or not."""
        attempt = self._attempt
        assert attempt.returned is not None
        if any(not s.settled for s in attempt.dag.values()):
            return  # waits for work that has not finished
        failed = [identity for identity, s in attempt.dag.items() if s.error is not None]
        if failed:
            await self._stop_on(failed)
            return
        state = attempt.dag[attempt.returned]
        assert state.value is not None
        ref = state.stored
        if ref is None:
            try:
                ref = await attempt.entities.store(state.value)
            except UnknownReferenceError as exc:
                # A query can write any entity ID, so a returned value can name an entity storage
                # does not have. That is the returned node's failure, as a raising node's is.
                attempt.fail(
                    AttemptError.from_exception(exc),
                    cancel_body=False,
                    stopped_on=[attempt.returned],
                )
                await attempt.close()
                return
        try:
            async with self._database.transaction() as tx:
                active = await execution.guard(tx, attempt.id, ending=True)
                await attempt.entities.flush(tx)
                await execution.materialize_result(tx, active, ref)
        except StaleAttemptError as exc:
            attempt.mark_closed(refusal=AttemptClosedError(str(exc)))
            return
        attempt.mark_closed(Completed())


def _topic(spawn: SpawnRecord) -> str:
    """The topic that announces a change of a spawn's target: an attempt of the child ending, or
    the cache key's current attempt ending, wherever it ran."""
    if spawn.step_cache_key is not None:
        return step_topic(spawn.step_cache_key)
    assert spawn.child_invocation_id is not None
    return invocation_topic(spawn.child_invocation_id)


def _item(spawn: SpawnRecord) -> execution.WorkItem:
    """The unit of work that runs a spawn's target."""
    if spawn.step_cache_key is not None:
        return execution.StartStep(spawn.step_cache_key)
    assert spawn.child_invocation_id is not None
    return execution.StartInvocation(spawn.child_invocation_id)


def _incomplete(failed: Mapping[NodeIdentity, AttemptError]) -> AttemptError:
    """The closing error for an attempt stopped by nodes that could not be computed: a single
    node's own error, or, for several, one error naming them all (no one of them is the cause)."""
    if len(failed) == 1:
        return next(iter(failed.values()))
    listed = "; ".join(
        f"{identity!r}: {error.type_name}: {error.message}" for identity, error in failed.items()
    )
    return AttemptError(
        type_name="IncompleteDagError",
        message=f"{len(failed)} nodes could not be computed: {listed}",
        traceback="",
    )


__all__ = [
    "Dag",
    "Evaluator",
    "MalformedEmissionError",
    "NodeState",
    "derive",
    "evaluate",
]
