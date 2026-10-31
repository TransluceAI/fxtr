"""One live workflow attempt of the runner (:mod:`fxtr.runner.postgres.runner`): the state its two
tasks share, the operations the runs of it perform, the requests those hand the tasks, and the
lifecycle.

Three parties act on an attempt. The **body's task** (running user code) calls its run session's
methods; the run (:class:`~fxtr.runner.assembly.WorkflowRun`) translates the body's node tokens into
nodes through its assembler and routes each call to one of this attempt's operations according to
its mode, so everything here is identity-based (the attempt
satisfies :class:`~fxtr.runner.assembly.WorkflowAttemptOps`): a primary run's calls record what
the body does, each turning into an :data:`Item` pushed onto the attempt's stream (an
:class:`Emit` returns at once, while the :class:`Request` kinds, :class:`Observe`,
:class:`Return`, are awaited until finished); a verification run's calls are
checked against the attempt's :class:`~fxtr.runner.assembly.Verifier` and record nothing. The
**recorder** (:mod:`fxtr.runner.postgres.recorder`) drains the stream and commits what the body
did, then publishes the results here. The **evaluator** (:mod:`fxtr.runner.postgres.evaluator`)
computes values from what is published and answers the observes. The body's task never touches
the database; the recorder and the evaluator each write it for their own concerns.

**The state machine.** An :class:`Attempt` moves through three phases:

* *running* (``returned is None``, no outcome): the body is executing, or its return is held
  while a verification run executes it again; the recorder accepts its items.
* *returned* (``returned`` set, no outcome): the recorder has committed the body's return; the
  evaluator waits for the returned node to have a value.
* *ended* (``outcome`` set; ``ended`` reads true): the attempt has its terminal
  :data:`Outcome`. Nothing more is recorded; every request still waiting is finished with an
  error; both tasks end.

**Two words for two places.** *Ended* is a fact about this object: the outcome exists in memory.
*Closed* is a fact about the ledger: the attempt's row has left ``running`` for a terminal state
(:data:`~fxtr.runner.postgres.execution.ClosedState`, written by
:func:`~fxtr.runner.postgres.execution.close_attempt`), which this object reports as ``closed``
once it has written that close or found it already written by someone else (an evictor may close a
row this object has not ended). An attempt ends before it is closed, and the gap between the two is
what :meth:`Attempt.close` and its callers exist to remove. The body's side sees an ended attempt
as :class:`~fxtr.runner.session.AttemptClosedError`: to it, the attempt is closed to further calls.

**Ending and closing are two operations.** An attempt *ends* once, in memory, when one of
:class:`Completed`, :class:`Failed`, :class:`Interrupted`, :class:`Abandoned` becomes its outcome,
through :meth:`Attempt.end` (:meth:`Attempt.fail` and :meth:`Attempt.abandon` are its two
synchronous faces): the first outcome wins and later ones are no-ops, ending refuses everything
further, and ending on a failure the body did not cause cancels the body. Ending is synchronous and
cheap, which is what lets a synchronous ``emit_<kind>`` fail the attempt on a determinism violation.
Who ends an attempt is exactly the parties inside its task: the evaluator (completed, interrupted,
or failed on a node the body depended on), the recorder (failed, on a refused emission or a
database error), the runs (failed, from the verification check or the body's own exception), and
the attempt's own task (abandoned, if nothing ended it first). Nothing outside that task touches an
attempt in memory.

*Closing* the attempt, :meth:`Attempt.close`, is a separate, asynchronous, idempotent write of its
outcome to the ledger as the row's terminal state
(:func:`~fxtr.runner.postgres.execution.close_attempt` under the guard). Whoever ended the attempt
closes it at once when it can: the evaluator completes an attempt by recording the result and
closing it in one transaction, then :meth:`Attempt.mark_closed`; the recorder and the evaluator
close the failures and interruptions they end on inline. The verification check cannot, being
synchronous. The attempt's task is the owner of last resort: after its body and helper tasks have
ended, whatever happened, it ends the attempt ``abandoned`` (a no-op if an outcome exists) and
closes it (a no-op if the ledger has it), under a shield with a deadline. So every close but that
last one may be cancelled harmlessly, the outcome survives in memory, and the attempt's row never
stays ``running`` after its task ends. If the guard refuses the write, the ledger already has a
terminal state for the attempt (an eviction abandoned it), which :meth:`Attempt.mark_closed` adopts.

A **step attempt** (:class:`StepAttempt`) is much simpler: the body is a step function that loads
its inputs and returns an array, so it has an entity store and the same end-and-close
lifecycle, but no stream, view, recorder, evaluator, or verifier. Its result is stored and
recorded, and the attempt completed, when its run reports ``complete_step``.

An attempt knows nothing of the ids a run is known by: those belong to its runs and to whoever
executes them. What an attempt can ask of whoever created it is to stop whatever is running its
body (``cancel_body``: cancelling the scope the driver runs in, see
:mod:`fxtr.runner.driver`), which :meth:`Attempt.fail` does when the body must be interrupted, and
to be told when it has ended (``on_change``).

**Concurrency between the two tasks.** The recorder adds to ``dag``, hands over observes
(``add_observe``) and sets ``returned`` only after its transaction commits, and does so without
yielding; the evaluator reads them and fills in values and errors in synchronous passes. Neither
ever sees the other's half-done work, and the ledger is always at least as far along as the
shared state. The evaluator does await between those passes, to expand run nodes, to collect their
targets' results, and to write to the database; each pass walks a copy of the view's nodes, so a
node the recorder adds meanwhile is seen by the next pass.

Ending is synchronous and closing is not, so the recorder can commit and publish a batch after
the attempt has ended. ``add_observe`` is what keeps that safe: an observe arriving then is
refused rather than queued for an evaluator that has stopped.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

import anyio
import anyio.abc
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.runner import nodes
from fxtr.runner.assembly import VerificationPhase, Verifier
from fxtr.runner.nodes import Node, NodeIdentity
from fxtr.runner.postgres import execution
from fxtr.runner.postgres.database import Database
from fxtr.runner.postgres.entities import PostgresEntityStorage
from fxtr.runner.postgres.execution import StaleAttemptError
from fxtr.runner.postgres.ledger import InvocationRecord, SpawnRecord
from fxtr.runner.postgres.logs import record_logs
from fxtr.runner.session import (
    AttemptClosedError,
    AttemptError,
    AttemptID,
    StepAssignment,
)

if TYPE_CHECKING:
    from fxtr.runner.postgres.evaluator import Dag

# ---- outcomes: how an attempt ends ----


@dataclasses.dataclass(frozen=True)
class Completed:
    """The attempt finished its work. For a workflow attempt the result is the invocation's, on
    record; for a step attempt ``result`` is the array the step returned, as stored.

    Attributes:
        result: The step's result, for a step attempt; ``None`` for a workflow attempt.
    """

    result: BoundID[Array[Any]] | None = None


@dataclasses.dataclass(frozen=True)
class Failed:
    """The attempt failed: user code raised, a node it depended on failed, a determinism
    violation, or the host could not record what it did.

    Attributes:
        error: What went wrong.
        stopped_on: The nodes whose failure this is, if a node's failure is what it is.
    """

    error: AttemptError
    stopped_on: tuple[NodeIdentity, ...] = ()


@dataclasses.dataclass(frozen=True)
class Interrupted:
    """Something the attempt depended on, owned by another invocation, stopped. Nothing is wrong
    with this invocation; resuming it once that is fixed lets it complete.

    Attributes:
        reason: Which targets stopped, and why.
        stopped_on: The run nodes whose targets stopped.
    """

    reason: str
    stopped_on: tuple[NodeIdentity, ...] = ()


@dataclasses.dataclass(frozen=True)
class Abandoned:
    """The body stopped for reasons unrelated to the attempt: its runner is stopping, or
    the attempt was found already closed in the ledger (an eviction).

    Attributes:
        reason: Why.
    """

    reason: str


type Outcome = Completed | Failed | Interrupted | Abandoned
"""An attempt's terminal outcome, reached once in memory and closed to the ledger once."""


# ---- items: what the body's task asks of the recorder ----


@dataclasses.dataclass(frozen=True)
class Emit:
    """The body emitted a node. Not awaited: the recorder records it in its next batch.

    Attributes:
        node: The node.
    """

    node: Node


@dataclasses.dataclass(frozen=True)
class Log:
    """The body recorded a free-form log line. Not awaited: the recorder writes it, with the
    others in its batch, in the batch's transaction.

    Attributes:
        message: The line.
    """

    message: str


@dataclasses.dataclass(eq=False)
class Request:
    """An item the body's task waits on. Whoever finishes it sets the result fields of the
    concrete kind, or ``error``, then ``done``; :meth:`Attempt.ask` then returns or raises.

    Attributes:
        done: Set when the request is finished, either way.
        error: What :meth:`Attempt.ask` raises to the body, if the request was refused.
    """

    done: anyio.Event = dataclasses.field(default_factory=anyio.Event, init=False)
    error: BaseException | None = dataclasses.field(default=None, init=False)

    def finish(self, error: BaseException | None = None) -> None:
        """Finish the request, with ``error`` if given. The first error given sticks."""
        if error is not None and self.error is None:
            self.error = error
        self.done.set()


@dataclasses.dataclass(eq=False)
class Observe(Request):
    """The body asked for a node's value. The recorder logs the request and hands the item to the
    evaluator, which finishes it once the node has settled: with the value, or with
    :class:`~fxtr.runner.session.NodeFailedError` if the node failed.

    Attributes:
        identity: The node.
        value: Its array, once known.
    """

    identity: NodeIdentity
    value: Array[Any] | None = dataclasses.field(default=None, init=False)


@dataclasses.dataclass(eq=False)
class Return(Request):
    """The body returned a node. Finished by the recorder once the return is recorded; the
    attempt then stays live until the evaluator has the node's value.

    Attributes:
        identity: The returned node.
    """

    identity: NodeIdentity


@dataclasses.dataclass(eq=False)
class Flush(Request):
    """A barrier: finished by the recorder once everything pushed before it has been recorded (or
    refused). One is pushed before closing an attempt on a run's report, so that the record holds
    everything the body did before it failed or was abandoned."""


type Item = Emit | Log | Observe | Return | Flush
"""What travels on an attempt's stream from the body's task to the recorder."""


# ---- the attempt ----


@dataclasses.dataclass(eq=False)
class Attempt:
    """One live attempt: the state shared by the body's task, the recorder, and the evaluator,
    and the lifecycle operations (see the module docstring for the state machine).

    Attributes:
        id: The attempt.
        invocation: The invocation it runs, as claimed.
        inputs: The invocation's arguments, by name: the identity and type of each of its input
            nodes. What each run's assignment is translated from (a run binds tokens to these
            when it starts).
        entities: The attempt's entity store, flushed in every transaction the attempt opens so
            that nothing a node references can be missing from the database.
        verifier: What the body has done, and the checks a verification run must pass (see
            :mod:`fxtr.runner.assembly`); its ``returned`` is the held return while verifying.
        dag: The view of the invocation's DAG (:data:`~fxtr.runner.postgres.evaluator.Dag`),
            seeded at claim from the ledger, extended by the recorder, valued by the evaluator.
        items_send: The body's end of the stream; closed when the attempt ends.
        items_receive: The recorder's end.
        changed: The evaluator's wake; set by :meth:`notify`, replaced by the evaluator after
            each wait.
        pending_observes: Observes the recorder has logged and the evaluator has not answered.
            The recorder adds through :meth:`add_observe`, which upholds what :meth:`end`
            promises; the evaluator replaces the list with what it could not yet answer.
        spawns: What each run node has expanded into, by node identity: a view of the ``spawns``
            table, seeded at claim and extended by the evaluator as it expands. A run node in
            here is expanded (an empty tuple means it expanded into nothing).
        returned: The node the body returned, once the return is recorded.
        outcome: The attempt's terminal outcome, once it has ended (see the module docstring).
    """

    id: AttemptID
    invocation: InvocationRecord
    inputs: frozendict[str, tuple[NodeIdentity, ArrayType]]
    entities: PostgresEntityStorage
    dag: Dag
    items_send: MemoryObjectSendStream[Item]
    items_receive: MemoryObjectReceiveStream[Item]
    _database: Database
    _cancel_body: Callable[[], None]
    _on_change: Callable[[Attempt], None]
    verifier: Verifier = dataclasses.field(default_factory=Verifier)
    changed: anyio.Event = dataclasses.field(default_factory=anyio.Event)
    pending_observes: list[Observe] = dataclasses.field(default_factory=list)
    spawns: dict[NodeIdentity, tuple[SpawnRecord, ...]] = dataclasses.field(default_factory=dict)
    returned: NodeIdentity | None = None
    outcome: Outcome | None = dataclasses.field(default=None, init=False)
    _refusal: BaseException | None = dataclasses.field(default=None, init=False)
    _closed: bool = dataclasses.field(default=False, init=False)
    _ended: anyio.Event = dataclasses.field(default_factory=anyio.Event, init=False)

    @property
    def ended(self) -> bool:
        """Whether the attempt has reached its terminal outcome and accepts nothing more (the
        ledger write may still be pending; :meth:`close` performs it). Final once true."""
        return self.outcome is not None

    @property
    def closed(self) -> bool:
        """Whether the ledger has the attempt's terminal state: this attempt wrote it, or found it
        already closed there."""
        return self._closed

    @property
    def workflow_name(self) -> str:
        return self.invocation.workflow_name

    @property
    def pathname(self) -> str:
        return self.invocation.pathname

    @property
    def cache_key_prefix(self) -> str:
        return self.invocation.cache_key_prefix

    @property
    def verification_phase(self) -> VerificationPhase:
        return self.verifier.phase

    @classmethod
    def create(
        cls,
        attempt_id: AttemptID,
        invocation: InvocationRecord,
        dag: Dag,
        *,
        inputs: Mapping[str, tuple[NodeIdentity, ArrayType]],
        spawns: Mapping[NodeIdentity, tuple[SpawnRecord, ...]],
        entities: PostgresEntityStorage,
        database: Database,
        cancel_body: Callable[[], None],
        on_change: Callable[[Attempt], None],
    ) -> Attempt:
        """A new live attempt with a fresh stream.

        Args:
            attempt_id: The attempt.
            invocation: The invocation it runs.
            dag: The seeded view of the invocation's DAG.
            inputs: The invocation's arguments, by name, as identity and type.
            spawns: What its run nodes have already expanded into.
            entities: The attempt's entity store (the one that loaded the seeded leaves, so it
                already knows them).
            database: The runner's database, for the closing transaction.
            cancel_body: How to stop whatever is running this attempt's body (cancel the scope
                its driver runs in).
            on_change: Called when the attempt has ended, so the runner can revisit what is
                still live.
        """
        send_stream, receive_stream = anyio.create_memory_object_stream[Item](float("inf"))
        attempt = cls(
            id=attempt_id,
            invocation=invocation,
            inputs=frozendict(inputs),
            entities=entities,
            dag=dag,
            spawns=dict(spawns),
            items_send=send_stream,
            items_receive=receive_stream,
            _database=database,
            _cancel_body=cancel_body,
            _on_change=on_change,
        )
        attempt.changed.set()  # the evaluator's first pass values the seeded view
        return attempt

    # -- the body's side --

    def push(self, item: Item) -> None:
        """Hand an item to the recorder.

        Raises:
            AttemptClosedError: If the attempt has ended.
        """
        if self.ended:
            raise AttemptClosedError(f"Attempt {self.id} has ended")
        self.items_send.send_nowait(item)

    async def ask[R: (Observe | Return | Flush)](self, request: R) -> R:
        """Hand a request to the recorder and wait for it to be finished.

        Returns:
            The request, with its result fields set.

        Raises:
            AttemptClosedError: If the attempt has ended before or while the request waits.
            BaseException: Whatever the request was refused with (a
                :class:`~fxtr.runner.session.NodeFailedError`, say).
        """
        self.push(request)
        await request.done.wait()
        if request.error is not None:
            raise request.error
        return request

    async def flush(self) -> None:
        """Wait until the recorder has recorded everything the body pushed so far. Returns normally
        even if recording failed or the attempt ended meanwhile: the caller is about to end it,
        and only wants what can be recorded to be recorded first."""
        if self.ended:
            return
        try:
            await self.ask(Flush())
        except Exception:
            pass

    # -- the primary run: what the body does, recorded --

    def emit(self, node: Node) -> None:
        """The body emitted a node: noted for verification, and handed to the recorder."""
        self.verifier.note_emit(node.content_id())
        self.push(Emit(node))

    def emit_source_array(self, node: nodes.SourceArray, array: Array[Any]) -> None:
        """The body registered a source array: the array goes into the attempt's write-behind
        store (so the recorder finds it when it records the node and flushes it in the same
        transaction), and the node (which refers to it) is emitted."""
        self.entities.store_sync(array)
        self.emit(node)

    def log(self, message: str) -> None:
        """The body recorded a log line."""
        self.push(Log(message))

    async def observe(self, identity: NodeIdentity) -> Array[Any]:
        """The body asked for a node's value: recorded, and answered once the node has settled."""
        self.verifier.note_observe(identity)
        item = await self.ask(Observe(identity))
        assert item.value is not None
        return item.value

    async def record_return(self, identity: NodeIdentity) -> None:
        """Record that the body returned ``identity`` (the ledger's checks at return apply)."""
        await self.ask(Return(identity))

    async def hold_return(self, identity: NodeIdentity) -> None:
        """The body returned ``identity`` and determinism is being verified: hold the return for a
        verification run to confirm, after making sure everything the body did is on record.

        Raises:
            AttemptClosedError: If the attempt ended meanwhile.
        """
        self.verifier.note_return(identity)
        await self.flush()
        if self.ended:
            raise AttemptClosedError(f"Attempt {self.id} has ended")

    # -- the verification run: what the body does again, checked --

    def verify_emit(self, node: Node) -> None:
        """The verification run emitted a node (a source array's is not stored again).

        Raises:
            AttemptClosedError: If no verification run is expected.
            DeterminismError: If the body did not emit it the first time.
        """
        self._require_verifying()
        match node:
            case nodes.SourceArray(name=name):
                description = f"source array {name!r}"
            case _:
                description = f"node ({nodes.kind_of(node)})"
        self.verifier.check_emit(node.content_id(), description)

    def verify_log(self, message: str) -> None:
        """The verification run recorded a log line: a repeat of the body's, dropped.

        Raises:
            AttemptClosedError: If no verification run is expected.
        """
        self._require_verifying()

    def verify_observe(self, identity: NodeIdentity) -> Array[Any]:
        """The verification run asked for a node's value: served from the live view, where it is
        complete because the body observed it.

        Raises:
            AttemptClosedError: If no verification run is expected.
            DeterminismError: If the body did not observe it the first time.
        """
        self._require_verifying()
        self.verifier.check_observe(identity)
        state = self.dag.get(identity)
        assert state is not None and state.value is not None, (
            "the attempt's body observed the node, so it has a value"
        )
        return state.value

    def verify_return(self, identity: NodeIdentity) -> NodeIdentity:
        """The verification run returned: compare it as a whole with what the body did.

        Returns:
            The held return, to record with :meth:`record_return`.

        Raises:
            AttemptClosedError: If no verification run is expected.
            DeterminismError: If the run differed.
        """
        self._require_verifying()
        return self.verifier.check_return(identity)

    def verification_failed(self) -> None:
        """The verification run's body raised where the attempt's body did not: a difference like
        any other, which is recorded as the attempt's failure."""
        self.verifier.mark_failed()

    def _require_verifying(self) -> None:
        if self.ended:
            raise AttemptClosedError(f"Attempt {self.id} has ended")
        if self.verifier.phase != "verifying":
            raise AttemptClosedError(
                f"Attempt {self.id} is not being verified (phase {self.verifier.phase})"
            )

    # -- the recorder's side --

    def notify(self) -> None:
        """Wake the evaluator: something it can act on was published."""
        self.changed.set()

    def add_observe(self, item: Observe) -> None:
        """Hand the evaluator an observe the recorder has recorded, for it to answer once the
        node settles.

        An attempt that has already ended refuses the observe here instead. Ending is synchronous
        but closing is not, so the recorder can commit a batch and publish it in the window
        between the two; only the observes :meth:`end` found are refused there, and nothing would
        ever answer one added after, leaving the body waiting on it forever.
        """
        if self.outcome is not None:
            item.finish(self._refusal or AttemptClosedError(f"Attempt {self.id} has ended"))
            return
        self.pending_observes.append(item)

    # -- ending and closing --

    def end(self, outcome: Outcome, *, refusal: BaseException | None = None) -> bool:
        """End the attempt with ``outcome``, if it has not ended already (see the module
        docstring): from now on nothing more is recorded, every request still waiting is refused
        (with ``refusal``, or :class:`~fxtr.runner.session.AttemptClosedError`), the stream ends so
        the recorder stops, the evaluator is woken so it stops, and the runner is told.

        Returns:
            Whether this call ended it (``False``: it had ended already; nothing changed).
        """
        if self.outcome is not None:
            return False
        self.outcome = outcome
        error = refusal or AttemptClosedError(f"Attempt {self.id} has ended")
        self._refusal = error  # what a late observe (:meth:`add_observe`) is refused with too
        for item in self.pending_observes:
            item.finish(error)
        self.pending_observes = []
        self.items_send.close()
        self.changed.set()
        self._ended.set()
        self._on_change(self)
        return True

    def fail(
        self,
        error: AttemptError,
        *,
        cancel_body: bool,
        stopped_on: Sequence[NodeIdentity] = (),
    ) -> None:
        """End the attempt as failed (a no-op if it has ended already). Synchronous: the
        ledger write is :meth:`close`'s, by whoever can await.

        Args:
            error: What went wrong; recorded on the attempt.
            cancel_body: Whether whatever is running the body must be stopped. False when the
                body is about to stop on its own because the call it is waiting on raises.
            stopped_on: The nodes whose failure this is, if a node's failure is what it is.
        """
        if self.end(Failed(error, tuple(stopped_on))) and cancel_body:
            self._cancel_body()

    def abandon(self, reason: str) -> None:
        """End the attempt as abandoned: the body stopped for reasons unrelated to the attempt
        (the runner is stopping). A no-op if it has ended already."""
        self.end(Abandoned(reason))

    async def interrupt(self, reason: str, *, stopped_on: Sequence[NodeIdentity]) -> None:
        """End the attempt as interrupted (something it depended on, owned by another
        invocation, stopped) and close it.

        Args:
            reason: Which targets stopped; recorded on the attempt.
            stopped_on: The run nodes whose targets stopped.
        """
        self.end(Interrupted(reason, tuple(stopped_on)))
        await self.close()

    def mark_closed(
        self, outcome: Outcome | None = None, *, refusal: BaseException | None = None
    ) -> None:
        """The ledger already has a terminal state for the attempt: adopt it. Either this attempt's
        evaluator wrote it, recording the result and closing the attempt as completed in one
        transaction (``outcome`` is that :class:`Completed`), or someone else did (an eviction
        abandoned the row; ``refusal`` is the guard's refusal, and the attempt ends ``abandoned``
        locally if it had not ended)."""
        self._closed = True
        if outcome is None:
            reason = "closed in the ledger by someone else" if refusal is None else str(refusal)
            outcome = Abandoned(reason)
        self.end(outcome, refusal=refusal)

    async def close(self) -> None:
        """Write the outcome to the ledger, if the ledger does not have it yet. Idempotent
        and safe to call from several tasks: a second writer finds the row already closed and
        adopts that. Cancelled mid-write, the write rolls back and the outcome is left in memory
        for the next caller; the attempt's task calls this last, under a shield.

        Raises:
            AssertionError: If the attempt has not ended.
        """
        if self._closed:
            return
        outcome = self.outcome
        assert outcome is not None, "nothing to close: the attempt has not ended"
        match outcome:
            case Completed():
                raise AssertionError("a workflow attempt is completed by the evaluator's write")
            case Failed(error=error, stopped_on=stopped_on):
                state: execution.ClosedState = "failed"
                reason: str | None = None
            case Interrupted(reason=reason, stopped_on=stopped_on):
                state, error = "interrupted", None
            case Abandoned(reason=reason):
                state, error, stopped_on = "abandoned", None, ()
        try:
            async with self._database.transaction() as tx:
                active = await execution.guard(tx, self.id, ending=True)
                await execution.close_attempt(
                    tx, active, state, error=error, reason=reason, stopped_on=stopped_on
                )
        except StaleAttemptError:
            pass  # closed by someone else already: the ledger has a terminal state
        self._closed = True

    async def wait_ended(self) -> None:
        """Wait until the attempt has ended."""
        await self._ended.wait()


@dataclasses.dataclass(eq=False)
class StepAttempt:
    """One live step attempt: its entity store and lifecycle (see the module docstring).

    **Logs.** A step has no recorder and only one commit point, so its :meth:`log` lines are
    buffered and written by a background flush task (:meth:`flush_logs`, started by the attempt's
    task for the duration of the attempt) that wakes whenever a line is added and writes what has
    accumulated in its own transaction. The task is the *only* writer of the step's logs: before
    :meth:`complete` or :meth:`close` writes anything, it stops the task and awaits a final flush
    (:meth:`_finalize_logs`), so a final line is closed while the attempt is still live and no
    two transactions ever contend on the log's ``seq``. A step process that dies mid-run loses
    whatever is still buffered.

    The lifecycle is :class:`Attempt`'s: the attempt ends once (:meth:`complete` records the
    result and ends it ``completed`` in one go; :meth:`fail` and :meth:`abandon` end it
    synchronously) and its outcome is closed by :meth:`close`, which the attempt's task calls
    last.

    Attributes:
        id: The attempt.
        cache_key: The key its result is saved under.
        assignment: What its run is handed to run it.
        entities: The attempt's entity store; the result is stored through it and flushed just
            before the completing transaction (see :meth:`complete`).
        outcome: The attempt's terminal outcome, once it has ended.
        _database: The runner's database, for the completing/closing and log-flush transactions.
        _on_change: Called when the attempt has ended, so the runner can revisit what is still
            live.
        _log_buffer: The :meth:`log` lines not yet written, in order. Drained by the flush task
            under no ``await`` so a concurrent :meth:`add_log` cannot lose or reorder a line.
        _log_signal: The flush task's wake, set by :meth:`add_log` and by :meth:`_finalize_logs` /
            :meth:`mark_closed`. Replaced with a fresh event each time the task drains, before it
            reads the buffer, so a line added during a flush wakes the next iteration (never lost).
        _log_done: Set by the flush task when it exits; :meth:`_finalize_logs` awaits it so the
            closing path knows the last lines are written before it opens its own transaction.
        _log_stop: Asks the flush task to write what remains and then exit. Set by
            :meth:`_finalize_logs` (a normal close) and :meth:`mark_closed` (the ledger already
            closed the attempt), and by the task itself when a write finds the attempt stale.
        _log_flushing: Whether the flush task has been started (set synchronously by
            :meth:`start_log_flusher`). :meth:`_finalize_logs` awaits the task only when this is
            set, so a close racing the task's very first scheduling still waits for it correctly.
    """

    id: AttemptID
    cache_key: str
    assignment: StepAssignment
    entities: PostgresEntityStorage
    _database: Database
    _on_change: Callable[[StepAttempt], None]
    outcome: Outcome | None = dataclasses.field(default=None, init=False)
    _closed: bool = dataclasses.field(default=False, init=False)
    _ended: anyio.Event = dataclasses.field(default_factory=anyio.Event, init=False)
    _log_buffer: list[str] = dataclasses.field(default_factory=list, init=False)
    _log_signal: anyio.Event = dataclasses.field(default_factory=anyio.Event, init=False)
    _log_done: anyio.Event = dataclasses.field(default_factory=anyio.Event, init=False)
    _log_stop: bool = dataclasses.field(default=False, init=False)
    _log_flushing: bool = dataclasses.field(default=False, init=False)

    @property
    def ended(self) -> bool:
        """Whether the attempt has reached its terminal outcome and accepts nothing more. Final once
        true."""
        return self.outcome is not None

    @property
    def closed(self) -> bool:
        """Whether the ledger has the attempt's terminal state."""
        return self._closed

    # -- logs --

    def add_log(self, message: str) -> None:
        """Buffer a log line and wake the flush task. Fire-and-forget."""
        self._log_buffer.append(message)
        self._log_signal.set()

    def start_log_flusher(self, tg: anyio.abc.TaskGroup) -> None:
        """Start the background log flush task in ``tg``. Marks the flusher live synchronously so
        that a :meth:`complete` or :meth:`close` racing the task's startup still awaits it."""
        self._log_flushing = True
        tg.start_soon(self.flush_logs)

    async def flush_logs(self) -> None:
        """The background task: write buffered log lines as they arrive until the attempt is
        finalized (:meth:`_finalize_logs`) or closed. Started by :meth:`start_log_flusher` for the
        life of the attempt; the only writer of this step's logs."""
        try:
            while True:
                await self._log_signal.wait()
                self._log_signal = anyio.Event()  # reset before draining, so no wake is lost
                stop = self._log_stop
                await self._write_pending()
                if stop or self.ended:
                    break
        finally:
            self._log_done.set()

    async def _write_pending(self) -> None:
        """Write whatever is buffered, in one guarded transaction. Drops the buffer and stops the
        task if the attempt is no longer live (reaped)."""
        messages = self._log_buffer
        if not messages:
            return
        self._log_buffer = []
        try:
            async with self._database.transaction() as tx:
                active = await execution.guard_step(tx, self.id)
                await record_logs(tx, active.id, messages)
        except StaleAttemptError:
            self._log_stop = True  # the attempt is gone: stop trying, drop the rest

    async def _finalize_logs(self) -> None:
        """Ask the flush task to write any remaining lines and stop, and wait for it. Called by
        :meth:`complete` and :meth:`close` while the attempt is still live, before their own
        transaction, so the task is the sole writer and the last lines are closed."""
        if not self._log_flushing:
            return
        self._log_stop = True
        self._log_signal.set()
        await self._log_done.wait()

    # -- ending and closing --

    def end(self, outcome: Outcome) -> bool:
        """End the attempt with ``outcome``, if it has not ended already: the log flusher is woken
        so it writes what it has and stops, and the runner is told. Returns whether this call
        ended it."""
        if self.outcome is not None:
            return False
        self.outcome = outcome
        self._log_signal.set()
        self._ended.set()
        self._on_change(self)
        return True

    async def complete(self, result: Array[Any]) -> None:
        """Store the result, then record it and close the attempt as completed in one transaction;
        then end it ``completed``, closed, with no checkpoint between the commit and the ending
        (a task cancelled during the write rolls it back and ends the attempt ``abandoned``
        instead). A no-op if the attempt has ended (a stale report); a second call racing the
        first would find the row closed and change nothing, so no other guard is needed.

        The result's entities are written in a transaction of their own first, so the completing
        transaction, which holds the job attempt's row (the lock every step of the job takes),
        writes only the ledger's rows. Entities are content-addressed and never deleted, so one
        written for a completion that then rolls back is only unreferenced. They cannot be written
        in the completing transaction ahead of the row instead: two steps with an equal result
        would each wait for the other's entity row while one holds the job attempt's."""
        if self.ended:
            return
        await self._finalize_logs()
        ref = await self.entities.store(result)
        try:
            await self.entities.flush()
            async with self._database.transaction() as tx:
                active = await execution.guard_step(tx, self.id, ending=True)
                await execution.complete_step(tx, active, ref)
        except StaleAttemptError as exc:
            self.mark_closed(exc)  # closed by someone else: the result is not recorded
            return
        self._closed = True
        self.end(Completed(ref))

    def fail(self, error: AttemptError) -> None:
        """End the attempt as failed: what the step raised. A no-op if it has ended already."""
        self.end(Failed(error))

    def abandon(self, reason: str) -> None:
        """End the attempt as abandoned. A no-op if it has ended already."""
        self.end(Abandoned(reason))

    def mark_closed(self, refusal: BaseException | None = None) -> None:
        """The ledger already has a terminal state for the attempt, written by someone else (an
        eviction): adopt it, ending the attempt ``abandoned`` locally if it had not ended."""
        self._closed = True
        self.end(
            Abandoned("closed in the ledger by someone else" if refusal is None else str(refusal))
        )

    async def close(self) -> None:
        """Write the outcome to the ledger, if the ledger does not have it yet: the last log
        lines first (this task is then the only writer of them), then the close. Idempotent; see
        :meth:`Attempt.close`.

        Raises:
            AssertionError: If the attempt has not ended.
        """
        if self._closed:
            return
        outcome = self.outcome
        assert outcome is not None, "nothing to close: the attempt has not ended"
        match outcome:
            case Completed():
                raise AssertionError("a step attempt is completed by complete()'s write")
            case Failed(error=error):
                state: Literal["failed", "abandoned"] = "failed"
                reason: str | None = None
            case Interrupted():
                raise AssertionError("step attempts are never interrupted")
            case Abandoned(reason=reason):
                state, error = "abandoned", None
        await self._finalize_logs()
        try:
            async with self._database.transaction() as tx:
                active = await execution.guard_step(tx, self.id, ending=True)
                await execution.close_step_attempt(tx, active, state, error=error, reason=reason)
        except StaleAttemptError:
            pass  # closed by someone else already
        self._closed = True

    async def wait_ended(self) -> None:
        """Wait until the attempt has ended."""
        await self._ended.wait()


__all__ = [
    "Abandoned",
    "Attempt",
    "Completed",
    "Emit",
    "Failed",
    "Flush",
    "Interrupted",
    "Item",
    "Log",
    "Observe",
    "Outcome",
    "Request",
    "Return",
    "StepAttempt",
]
