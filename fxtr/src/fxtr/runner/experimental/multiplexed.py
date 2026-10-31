"""The multiplexed shape of the worker protocol: one message stream carrying many runs.

A :class:`WorkerSession` is one worker's session with a host: the worker-facing vocabulary of
:mod:`fxtr.runner.session` but ``emit_query``, with the attempt id as an argument to every call,
plus registration, a stream of messages coming the other way (assignments, cancellations, the
shutdown), and a drain. It is how a worker that runs many attempts at once, in another process,
talks to a host: the remote layer (:mod:`fxtr.runner.experimental.remote`) carries exactly this
protocol over a channel, and the :class:`~fxtr.runner.experimental.worker.Worker` loop consumes
it. The per-run protocols of :mod:`fxtr.runner.session` are the primary vocabulary; this module
also holds the adapters between the two shapes.

:func:`bind_run` is the worker-side adapter: a run session over a ``WorkerSession`` and one
assignment, binding the attempt id into every call, so the contexts and
:func:`~fxtr.runner.worker.run_assignment` are written once against the per-run protocols
whatever the session behind them. Its workflow run refuses ``emit_query`` with
:class:`QueryNotSupportedError`, so a workflow that collects an array query fails there.

:class:`MultiplexedHost` is the host-side adapter, with two faces. Toward the host's driver
(:mod:`fxtr.runner.driver`) its :meth:`~MultiplexedHost.execute` is a
:data:`~fxtr.runner.driver.RunExecutor`: it registers a run, waits for the worker to have
credit, sends the run's assignment down the message stream, and awaits the run's end. Toward the
worker it implements ``WorkerSession``: each attempt-keyed call is forwarded to the run session
registered under that id, and the completion, ``fail``, and ``abandon`` calls end the run after
forwarding. It owns what is not a run's business: registration, the worker's capacity (at most
``max_concurrency`` runs sent and not ended), the drain and ``Shutdown``, and ``AttemptCancelled``
for a run whose ``execute`` was cancelled. It holds no policy: what runs exist, when they are
executed, and when they are stopped is the caller's, through ``execute`` and its cancellation.

A run's ``execute`` ends for exactly one of three reasons: the worker's completion call for it
returned; the worker reported ``fail`` or ``abandon`` for it; or the host's side cancelled the
``execute``. That cancellation is the *only* way a host ends a run itself, whatever the occasion (a
determinism violation, a hard cancel, the host going away), so a backend stops a body by
cancelling the scope its driver runs in and nothing else.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from typing import Any, Protocol, cast

import anyio

from fxtr.core.entity_data import EntityID, FxtrDataValue, StorableFxtrDataValue
from fxtr.entity_defns.array import Array, ArrayType, CombineJoin, Join, KeyValue
from fxtr.runner.session import (
    Assignment,
    AttemptClosedError,
    AttemptError,
    AttemptID,
    NodeToken,
    RunnerError,
    RunSession,
    StepAssignment,
    StepRunSession,
    WorkflowAssignment,
    WorkflowRunSession,
)

# ---- errors ----


class AlreadyRegisteredError(RunnerError):
    """:meth:`WorkerSession.register` was called a second time on one session instance."""


class QueryNotSupportedError(RunnerError):
    """A workflow collected an array query, which a worker session cannot carry to the host: array
    queries run only where the host runs the workflow function in its own process (the in-process
    executor of the in-memory and Postgres runners)."""


class HostDisconnectedError(RunnerError):
    """The host is gone: its message stream ended without a :class:`Shutdown`, or a request could
    not be answered because the connection to it was lost. Raised by the worker loop and by a
    remote session's waiting requests."""


# ---- function specs and messages ----


@dataclasses.dataclass(frozen=True)
class StepSpec:
    """A step a worker can run.

    Attributes:
        name: The step's :attr:`~fxtr.experiment.steps.Step.name`.
    """

    name: str


@dataclasses.dataclass(frozen=True)
class WorkflowSpec:
    """A workflow a worker can run.

    Attributes:
        name: The workflow's :attr:`~fxtr.experiment.workflows.Workflow.name`.
    """

    name: str


type FunctionSpec = StepSpec | WorkflowSpec
"""What a worker advertises with :meth:`WorkerSession.register`."""


@dataclasses.dataclass(frozen=True)
class Assigned:
    """The host hands this worker an attempt to run. The attempt is the worker's until it
    completes, fails, or abandons it, or the host cancels it with :class:`AttemptCancelled`.

    Attributes:
        assignment: What to run.
    """

    assignment: Assignment


@dataclasses.dataclass(frozen=True)
class AttemptCancelled:
    """An attempt assigned to this worker has been failed or cancelled by the host. The worker
    must stop running it; anything it does afterwards is refused with :class:`AttemptClosedError`.

    Attributes:
        attempt_id: The attempt.
        reason: A human-readable explanation.
    """

    attempt_id: AttemptID
    reason: str


@dataclasses.dataclass(frozen=True)
class Shutdown:
    """The host asks the worker to stop: abandon running attempts (the host still accepts
    those reports), and return. After a :meth:`WorkerSession.drain` there is nothing running when
    this arrives.

    Attributes:
        reason: A human-readable explanation.
    """

    reason: str


type WorkerMessage = Assigned | AttemptCancelled | Shutdown
"""What :meth:`WorkerSession.messages` yields."""


# ---- the protocol ----


class WorkerSession(Protocol):
    """One worker's session with its host: the host, as the worker sees it.

    The run sessions of :mod:`fxtr.runner.session`, but ``emit_query``, with the attempt id as an
    argument, plus what belongs to the worker rather than to one run: registration, the message
    stream, the drain. Everything an attempt does is keyed by its :data:`AttemptID`. Methods that
    only record (the ``emit_<kind>`` family, ``log_message``) are synchronous and return nothing;
    methods that need an answer from the system are async.

    The ``emit_<kind>`` methods add one node each to a workflow attempt's DAG: ``token`` is the
    worker's name for the new node (see the module docstring), and every other node argument is
    the token of a node already bound in the same assignment. Each is fire-and-forget: the host may
    check and record the node at the attempt's next commit point, and a node the invocation already
    has (the same operation on the same inputs) is a no-op beyond binding the token. Every one of
    them raises :class:`AttemptClosedError` if the attempt can no longer act and
    :class:`NodeTokenError` if ``token`` is already bound or an input token is not; over a
    transport where the call has returned before the host sees it, such a refusal arrives instead
    as the attempt's failure (an :class:`AttemptCancelled` and a recorded error), so a body must
    not rely on catching one.
    """

    # -- receiving work --

    async def register(self, functions: Collection[FunctionSpec], *, max_concurrency: int) -> None:
        """Declare what this worker can run and how many attempts it will run at once.

        Called once per session instance, before any work can be assigned. Only attempts matching
        one of ``functions`` are ever assigned: a workflow attempt matches a :class:`WorkflowSpec`
        by name, a step attempt matches a :class:`StepSpec` by name. The host keeps at most
        ``max_concurrency`` attempts outstanding on this worker (see the module docstring for what
        outstanding means), and may keep fewer.

        Args:
            functions: The steps and workflows this worker can run.
            max_concurrency: The most attempts the worker is willing to run at once.

        Raises:
            AlreadyRegisteredError: If this session instance has already registered.
        """
        ...

    async def drain(self) -> None:
        """Ask to stop cleanly: the host assigns no more attempts to this worker and sends
        :class:`Shutdown` once nothing is outstanding and the attempts it ran are all closed.
        Returns at once; the worker keeps reading :meth:`messages` until the ``Shutdown``
        arrives. Idempotent."""
        ...

    def messages(self) -> AsyncIterator[WorkerMessage]:
        """The stream from the host to this worker.

        Returns:
            A stream of :class:`Assigned` (attempts for this worker to run, only after
            :meth:`register`), :class:`AttemptCancelled` (attempts of this worker's that the
            host has cancelled) and, as its last message, :class:`Shutdown`. Messages sent
            before the worker starts reading are buffered. A stream that ends without a
            ``Shutdown`` means the host disconnected; the worker still abandons its attempts,
            but reports the disconnect as an error rather than returning normally.
        """
        ...

    # -- workflow attempts --

    def emit_source_array(
        self, attempt_id: AttemptID, token: NodeToken, name: str, array: Array[Any]
    ) -> None:
        """Add a source array: the host stores ``array`` under the attempt and adds the node
        referring to it (``add_source_array``).

        Args:
            attempt_id: The workflow attempt.
            token: The worker's token for the new node.
            name: The node's name, unique among the invocation's named nodes.
            array: The array, presentation-free (the context strips it).
        """
        ...

    def emit_combine(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        inputs: Mapping[str, NodeToken],
        join: CombineJoin,
    ) -> None:
        """Add a combine node: zip the inputs into one struct-valued array (``combine``).

        Args:
            attempt_id: The workflow attempt.
            token: The worker's token for the new node.
            inputs: The input nodes, by the field name each becomes.
            join: How the inputs' key sets are reconciled.
        """
        ...

    def emit_stack(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        inputs: Mapping[KeyValue, NodeToken],
        dim: str,
    ) -> None:
        """Add a stack node: stack same-typed inputs along a new leading dim (``stack``).

        Args:
            attempt_id: The workflow attempt.
            token: The worker's token for the new node.
            inputs: The input nodes by their key along the new dim, in any order (the host
                canonicalizes).
            dim: The new dim's name.
        """
        ...

    def emit_run_step(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        *,
        name: str,
        step_name: str,
        inputs: Mapping[str, NodeToken],
        map_over: tuple[str, ...],
        join: Join,
        replicas: Mapping[str, tuple[KeyValue, ...]],
        cache_key_prefix: str,
        type: ArrayType,
    ) -> None:
        """Add a run-step node: run a step over the inputs, mapped over ``map_over`` with
        ``replicas`` (``run_step``).

        Args:
            attempt_id: The workflow attempt.
            token: The worker's token for the new node.
            name: The node's name, unique among the invocation's named nodes.
            step_name: The step to run (see :attr:`~fxtr.experiment.steps.Step.name`).
            inputs: The input nodes, by the step's argument names.
            map_over: The dim names to map over, in result order.
            join: How the inputs' key sets are reconciled along the mapped dims.
            replicas: The replica dims, each with its values, in result order after the mapped
                dims.
            cache_key_prefix: The fully resolved cache key prefix.
            type: The result type the worker computed from the step's declared signature,
                presentation-free. The one part of a node's type the host takes from the worker,
                since the signature is user code the host does not have.
        """
        ...

    def emit_run_child_workflow(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        *,
        name: str,
        workflow_name: str,
        inputs: Mapping[str, NodeToken],
        map_over: tuple[str, ...],
        join: Join,
        replicas: Mapping[str, tuple[KeyValue, ...]],
        cache_key_prefix: str,
        type: ArrayType,
    ) -> None:
        """Add a run-child-workflow node: run a workflow over the inputs, mapped over ``map_over``
        with ``replicas`` (``run_child_workflow``). Arguments as for :meth:`emit_run_step`, with
        the workflow's name (see :attr:`~fxtr.experiment.workflows.Workflow.name`) in place of the
        step's."""
        ...

    async def observe(self, attempt_id: AttemptID, node: NodeToken) -> Array[Any]:
        """Wait for a node to have a value.

        Args:
            attempt_id: The workflow attempt.
            node: A node bound in this assignment.

        Returns:
            The node's array, under its stored (presentation-free) type.

        Raises:
            AttemptClosedError: If the attempt can no longer act.
            NodeTokenError: If ``node`` is not bound.
            NodeFailedError: If the node's work failed; the attempt has been failed.
        """
        ...

    async def complete_workflow(self, attempt_id: AttemptID, result: NodeToken) -> None:
        """Record that a workflow body returned.

        Args:
            attempt_id: The workflow attempt.
            result: The node the body returned, whose type the worker has checked against the
                workflow's signature.

        Raises:
            AttemptClosedError: If the attempt can no longer act.
            NodeTokenError: If ``result`` is not bound.
        """
        ...

    # -- step attempts --

    async def complete_step(self, attempt_id: AttemptID, result: Array[Any]) -> None:
        """Record a step's result; the host stores it.

        Args:
            attempt_id: The step attempt.
            result: The array the step produced, whose type the worker has checked against the
                step's signature.

        Raises:
            AttemptClosedError: If the attempt can no longer act.
        """
        ...

    # -- either --

    async def fail(self, attempt_id: AttemptID, error: AttemptError) -> None:
        """Record that an attempt failed: user code or the runner raised while running it.
        Idempotent on an already closed attempt.

        Args:
            attempt_id: The attempt.
            error: What went wrong.
        """
        ...

    async def abandon(self, attempt_id: AttemptID, reason: str) -> None:
        """Record that the worker stopped running an attempt for reasons unrelated to the attempt
        (it is shutting down, or was interrupted). Not a failure: what happens to the attempt next
        (re-queueing it, say) is the host's decision. Idempotent on an already closed attempt.

        Args:
            attempt_id: The attempt.
            reason: A human-readable explanation.
        """
        ...

    def log_message(self, attempt_id: AttemptID, message: str) -> None:
        """Record a free-form log line for an attempt (workflow or step).

        Synchronous and fire-and-forget, like the ``emit_<kind>`` methods: it queues the line to
        be persisted at the attempt's next commit point and returns at once. Logs are stored per
        attempt and never deduplicated across a replay.

        Args:
            attempt_id: The attempt.
            message: The line to record.

        Raises:
            AttemptClosedError: If the attempt can no longer act.
        """
        ...

    async def store_raw_data(self, attempt_id: AttemptID, data: StorableFxtrDataValue) -> EntityID:
        """Store an entity on behalf of an attempt.

        Args:
            attempt_id: The attempt.
            data: The value to store (see
                :meth:`~fxtr.core.entity_data.FxtrEntityDataStorage.store_raw_data`).

        Returns:
            The stored entity's id.

        Raises:
            AttemptClosedError: If the attempt can no longer act.
        """
        ...

    async def load_raw_data(self, attempt_id: AttemptID, entity_id: EntityID) -> FxtrDataValue:
        """Load an entity on behalf of an attempt.

        Args:
            attempt_id: The attempt.
            entity_id: The id to load (see
                :meth:`~fxtr.core.entity_data.FxtrEntityDataStorage.load_raw_data`).

        Returns:
            The entity's data.

        Raises:
            AttemptClosedError: If the attempt can no longer act (a session may choose not to
                refuse loads).
        """
        ...

    def is_known(self, attempt_id: AttemptID, entity_id: EntityID) -> bool:
        """Whether an attempt can reference an entity.

        Args:
            attempt_id: The attempt.
            entity_id: The id to check (see
                :meth:`~fxtr.core.entity_data.FxtrEntityDataStorage.is_known`).

        Returns:
            ``True`` if the entity can be loaded by, or referenced from data stored by, the attempt.

        Raises:
            AttemptClosedError: If the attempt can no longer act (a session may choose not to
                refuse this).
        """
        ...


# ---- the host-side adapter ----


class NotAcceptingError(RunnerError):
    """:meth:`MultiplexedHost.execute` was called after the host stopped accepting runs (it is
    draining or shut down)."""


class _Entry:
    def __init__(self, run: RunSession) -> None:
        self.run = run
        self.assigned = False  # sent to the worker (as opposed to waiting for credit)
        self.finished = anyio.Event()


class MultiplexedHost:
    """The host end of a multiplexed :class:`WorkerSession` over per-run sessions (see the module
    docstring).

    Args:
        ready_to_shutdown: Consulted when a drain could complete (no run outstanding): the host
            sends ``Shutdown`` only if this returns true, so a backend can hold the shutdown until
            it has nothing left to record. Call :meth:`maybe_finish_drain` when that changes.
        on_credit: Called whenever a run ends, so a backend's scheduler can look for more work.
    """

    def __init__(
        self,
        *,
        ready_to_shutdown: Callable[[], bool] = lambda: True,
        on_credit: Callable[[], None] = lambda: None,
    ) -> None:
        self._ready_to_shutdown = ready_to_shutdown
        self._on_credit = on_credit
        self._registration: tuple[frozenset[FunctionSpec], int] | None = None
        self._runs: dict[AttemptID, _Entry] = {}
        self._credit_changed = anyio.Event()
        self._messages_send, self._messages_receive = anyio.create_memory_object_stream[
            WorkerMessage
        ](math.inf)
        self._messages_closed = False
        self._draining = False

    # ---- the driver's face: the executor ----

    async def execute(self, run: RunSession) -> None:
        """Execute ``run`` on the worker: send its assignment once a worker has registered and
        has capacity, and return when the run has ended (its completion call returned, or the
        worker reported ``fail`` or ``abandon`` for it). Cancelling this call is how a run is
        stopped from the host's side: the worker is told to stop it (``AttemptCancelled``) and the
        run is ended here. The driver (:mod:`fxtr.runner.driver`) owns the runs it executes, so a
        body is stopped by cancelling the driver, whichever executor it runs through.

        Raises:
            NotAcceptingError: If the host is draining or shut down, now or before the run could be
                sent.
            RunnerError: If a run with this id is already executing.
        """
        if not self.accepting:
            raise NotAcceptingError(f"The host is not accepting runs; cannot execute {run.run_id}")
        if run.run_id in self._runs:
            raise RunnerError(f"A run with id {run.run_id} is already executing")
        entry = self._runs[run.run_id] = _Entry(run)
        try:
            while not self._may_send():
                await self._credit_changed.wait()
                if not self.accepting:
                    raise NotAcceptingError(f"The host stopped accepting; {run.run_id} not sent")
            entry.assigned = True
            self._send(Assigned(run.assignment))
            await entry.finished.wait()
        finally:
            if not entry.finished.is_set():
                if entry.assigned:
                    self._send(AttemptCancelled(run.run_id, "cancelled by the host"))
                self._finish(entry)
            del self._runs[run.run_id]

    @property
    def runs(self) -> Mapping[AttemptID, RunSession]:
        """The run sessions handed to :meth:`execute` and not yet ended, by run id (for
        inspection; tests and displays)."""
        return {rid: e.run for rid, e in self._runs.items() if not e.finished.is_set()}

    @property
    def room(self) -> int:
        """How many runs :meth:`execute` would send at once rather than queue: the worker's
        declared concurrency minus every run handed to ``execute`` that has not ended, those still
        waiting to be sent included. Zero before a worker registers. A scheduler that wants no run
        to wait in ``execute`` calls it only while this is positive; ``execute`` itself only needs
        the worker's actual capacity (:meth:`_may_send`).
        """
        if self._registration is None:
            return 0
        outstanding = sum(1 for e in self._runs.values() if not e.finished.is_set())
        return self._registration[1] - outstanding

    def _may_send(self) -> bool:
        """Whether one more run may be sent now: a worker has registered and fewer runs than its
        declared concurrency have been sent and not ended. The one bound the protocol promises."""
        if self._registration is None:
            return False
        sent = sum(1 for e in self._runs.values() if e.assigned and not e.finished.is_set())
        return sent < self._registration[1]

    @property
    def accepting(self) -> bool:
        """Whether :meth:`execute` may still be called."""
        return not (self._draining or self._messages_closed)

    @property
    def functions(self) -> frozenset[FunctionSpec]:
        """What the worker registered it can run (empty before registration)."""
        return frozenset() if self._registration is None else self._registration[0]

    def shutdown(self, reason: str) -> None:
        """Send ``Shutdown`` now and end the message stream. Runs still executing are left to the
        worker, which abandons what it is running and reports each (``abandon``), ending the run
        with the worker's reason. For a worker that will never report, the host's owner ends the
        runs the one way a host ever ends a run: by cancelling their ``execute`` calls."""
        self._send(Shutdown(reason))
        self._end_messages()

    def end_messages(self) -> None:
        """End the message stream without a ``Shutdown``: from the worker's side, a disconnect.
        For a host going away abruptly; :meth:`shutdown` is the clean way."""
        self._end_messages()

    def maybe_finish_drain(self) -> None:
        """Send ``Shutdown`` if a drain was requested, no run is outstanding, and the backend is
        ready (see ``ready_to_shutdown``)."""
        if (
            self._draining
            and not self._messages_closed
            and all(e.finished.is_set() for e in self._runs.values())
            and self._ready_to_shutdown()
        ):
            self.shutdown("drained")

    # ---- WorkerSession: registration and messages ----

    async def register(self, functions: Collection[FunctionSpec], *, max_concurrency: int) -> None:
        if self._registration is not None:
            raise AlreadyRegisteredError("This host already has a registered worker")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._registration = (frozenset(functions), max_concurrency)
        self._wake_credit()

    async def drain(self) -> None:
        self._draining = True
        self._wake_credit()  # executes waiting for credit give up
        self.maybe_finish_drain()

    async def messages(self) -> AsyncIterator[WorkerMessage]:
        async with self._messages_receive:
            async for message in self._messages_receive:
                yield message

    # ---- WorkerSession: forwarding to runs ----

    def _run(self, run_id: AttemptID) -> RunSession:
        entry = self._runs.get(run_id)
        if entry is None:
            raise AttemptClosedError(f"Unknown attempt {run_id}")
        return entry.run

    def _workflow_run(self, run_id: AttemptID) -> WorkflowRunSession:
        run = self._run(run_id)
        if not isinstance(run.assignment, WorkflowAssignment):
            raise AttemptClosedError(f"Attempt {run_id} is a step attempt")
        return cast(WorkflowRunSession, run)

    def _step_run(self, run_id: AttemptID) -> StepRunSession:
        run = self._run(run_id)
        if not isinstance(run.assignment, StepAssignment):
            raise AttemptClosedError(f"Attempt {run_id} is a workflow attempt")
        return cast(StepRunSession, run)

    def emit_source_array(
        self, attempt_id: AttemptID, token: NodeToken, name: str, array: Array[Any]
    ) -> None:
        self._workflow_run(attempt_id).emit_source_array(token, name, array)

    def emit_combine(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        inputs: Mapping[str, NodeToken],
        join: CombineJoin,
    ) -> None:
        self._workflow_run(attempt_id).emit_combine(token, inputs, join)

    def emit_stack(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        inputs: Mapping[KeyValue, NodeToken],
        dim: str,
    ) -> None:
        self._workflow_run(attempt_id).emit_stack(token, inputs, dim)

    def emit_run_step(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        *,
        name: str,
        step_name: str,
        inputs: Mapping[str, NodeToken],
        map_over: tuple[str, ...],
        join: Join,
        replicas: Mapping[str, tuple[KeyValue, ...]],
        cache_key_prefix: str,
        type: ArrayType,
    ) -> None:
        self._workflow_run(attempt_id).emit_run_step(
            token,
            name=name,
            step_name=step_name,
            inputs=inputs,
            map_over=map_over,
            join=join,
            replicas=replicas,
            cache_key_prefix=cache_key_prefix,
            type=type,
        )

    def emit_run_child_workflow(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        *,
        name: str,
        workflow_name: str,
        inputs: Mapping[str, NodeToken],
        map_over: tuple[str, ...],
        join: Join,
        replicas: Mapping[str, tuple[KeyValue, ...]],
        cache_key_prefix: str,
        type: ArrayType,
    ) -> None:
        self._workflow_run(attempt_id).emit_run_child_workflow(
            token,
            name=name,
            workflow_name=workflow_name,
            inputs=inputs,
            map_over=map_over,
            join=join,
            replicas=replicas,
            cache_key_prefix=cache_key_prefix,
            type=type,
        )

    def log_message(self, attempt_id: AttemptID, message: str) -> None:
        self._run(attempt_id).log_message(message)

    async def observe(self, attempt_id: AttemptID, node: NodeToken) -> Array[Any]:
        return await self._workflow_run(attempt_id).observe(node)

    async def complete_workflow(self, attempt_id: AttemptID, result: NodeToken) -> None:
        run = self._workflow_run(attempt_id)
        try:
            await run.complete_workflow(result)
        finally:
            self._finish_by_id(attempt_id)

    async def complete_step(self, attempt_id: AttemptID, result: Array[Any]) -> None:
        run = self._step_run(attempt_id)
        try:
            await run.complete_step(result)
        finally:
            self._finish_by_id(attempt_id)

    async def fail(self, attempt_id: AttemptID, error: AttemptError) -> None:
        entry = self._runs.get(attempt_id)
        if entry is None:
            return
        try:
            await entry.run.fail(error)
        finally:
            self._finish(entry)

    async def abandon(self, attempt_id: AttemptID, reason: str) -> None:
        entry = self._runs.get(attempt_id)
        if entry is None:
            return
        try:
            await entry.run.abandon(reason)
        finally:
            self._finish(entry)

    async def store_raw_data(self, attempt_id: AttemptID, data: StorableFxtrDataValue) -> EntityID:
        return await self._run(attempt_id).store_raw_data(data)

    async def load_raw_data(self, attempt_id: AttemptID, entity_id: EntityID) -> FxtrDataValue:
        return await self._run(attempt_id).load_raw_data(entity_id)

    def is_known(self, attempt_id: AttemptID, entity_id: EntityID) -> bool:
        return self._run(attempt_id).is_known(entity_id)

    # ---- internals ----

    def _finish_by_id(self, run_id: AttemptID) -> None:
        entry = self._runs.get(run_id)
        if entry is not None:
            self._finish(entry)

    def _finish(self, entry: _Entry) -> None:
        """The run has ended: release its credit, let its ``execute`` return, and revisit the
        drain."""
        if entry.finished.is_set():
            return
        entry.finished.set()
        self._wake_credit()
        self._on_credit()
        self.maybe_finish_drain()

    def _wake_credit(self) -> None:
        self._credit_changed.set()
        self._credit_changed = anyio.Event()

    def _send(self, message: WorkerMessage) -> None:
        if self._messages_closed:
            return
        try:
            self._messages_send.send_nowait(message)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            self._end_messages()  # the worker's end is gone; there is no one to tell

    def _end_messages(self) -> None:
        if not self._messages_closed:
            self._messages_closed = True
            self._messages_send.close()


def bind_run(session: WorkerSession, assignment: Assignment) -> RunSession:
    """A run session for ``assignment`` over a multiplexed session: each call is the
    ``WorkerSession`` call with the assignment's attempt id bound in."""
    match assignment:
        case StepAssignment():
            return _BoundStepRun(session, assignment)
        case WorkflowAssignment():
            return _BoundWorkflowRun(session, assignment)


class _Bound:
    def __init__(self, session: WorkerSession, attempt_id: AttemptID) -> None:
        self._session = session
        self._id = attempt_id

    @property
    def run_id(self) -> AttemptID:
        return self._id

    def log_message(self, message: str) -> None:
        self._session.log_message(self._id, message)

    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        return await self._session.store_raw_data(self._id, data)

    async def load_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        return await self._session.load_raw_data(self._id, entity_id)

    def is_known(self, entity_id: EntityID) -> bool:
        return self._session.is_known(self._id, entity_id)

    async def fail(self, error: AttemptError) -> None:
        await self._session.fail(self._id, error)

    async def abandon(self, reason: str) -> None:
        await self._session.abandon(self._id, reason)


class _BoundWorkflowRun(_Bound):
    def __init__(self, session: WorkerSession, assignment: WorkflowAssignment) -> None:
        super().__init__(session, assignment.attempt_id)
        self.assignment = assignment

    def emit_source_array(self, token: NodeToken, name: str, array: Array[Any]) -> None:
        self._session.emit_source_array(self._id, token, name, array)

    def emit_combine(
        self, token: NodeToken, inputs: Mapping[str, NodeToken], join: CombineJoin
    ) -> None:
        self._session.emit_combine(self._id, token, inputs, join)

    def emit_stack(self, token: NodeToken, inputs: Mapping[KeyValue, NodeToken], dim: str) -> None:
        self._session.emit_stack(self._id, token, inputs, dim)

    def emit_query(self, token: NodeToken, inputs: Sequence[NodeToken], plan: bytes) -> None:
        raise QueryNotSupportedError(
            "array queries cannot run on a remote worker; run this workflow in the Postgres"
            " runner's own process"
        )

    def emit_run_step(
        self,
        token: NodeToken,
        *,
        name: str,
        step_name: str,
        inputs: Mapping[str, NodeToken],
        map_over: tuple[str, ...],
        join: Join,
        replicas: Mapping[str, tuple[KeyValue, ...]],
        cache_key_prefix: str,
        type: ArrayType,
    ) -> None:
        self._session.emit_run_step(
            self._id,
            token,
            name=name,
            step_name=step_name,
            inputs=inputs,
            map_over=map_over,
            join=join,
            replicas=replicas,
            cache_key_prefix=cache_key_prefix,
            type=type,
        )

    def emit_run_child_workflow(
        self,
        token: NodeToken,
        *,
        name: str,
        workflow_name: str,
        inputs: Mapping[str, NodeToken],
        map_over: tuple[str, ...],
        join: Join,
        replicas: Mapping[str, tuple[KeyValue, ...]],
        cache_key_prefix: str,
        type: ArrayType,
    ) -> None:
        self._session.emit_run_child_workflow(
            self._id,
            token,
            name=name,
            workflow_name=workflow_name,
            inputs=inputs,
            map_over=map_over,
            join=join,
            replicas=replicas,
            cache_key_prefix=cache_key_prefix,
            type=type,
        )

    async def observe(self, node: NodeToken) -> Array[Any]:
        return await self._session.observe(self._id, node)

    async def complete_workflow(self, result: NodeToken) -> None:
        await self._session.complete_workflow(self._id, result)


class _BoundStepRun(_Bound):
    def __init__(self, session: WorkerSession, assignment: StepAssignment) -> None:
        super().__init__(session, assignment.attempt_id)
        self.assignment = assignment

    async def complete_step(self, result: Array[Any]) -> None:
        await self._session.complete_step(self._id, result)


__all__ = ["MultiplexedHost", "NotAcceptingError", "bind_run"]
