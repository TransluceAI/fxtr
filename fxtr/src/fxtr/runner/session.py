"""The worker's session with its host: the boundary between running user functions and the system.

A *worker* is whatever runs user step and workflow bodies: a task in the host's own process, or a
process elsewhere. It does not own any experiment state; it talks to its *host*, whatever owns that
state and drives attempts, through a **run session**: the session for one *run*, one execution of
one attempt's body. The host creates a session per run and hands it to whatever executes the body;
the contexts of :mod:`fxtr.runner.contexts` are written against it and the body's code talks to
nothing else. The host's machinery is behind this protocol and is not the worker's concern: the
host is responsible for driving attempts, and may wait on, cancel, or replay them as it sees fit.

The two kinds are :class:`WorkflowRunSession` and :class:`StepRunSession` (the union
:data:`RunSession`); a wrong-kind call is a type error rather than a refusal.

The vocabulary:

* An **attempt** is one call of a user function served through the system. A run is one execution
  of an attempt's body, in one mode (see :mod:`fxtr.runner.assembly`), and its
  :attr:`~RunSessionBase.run_id` is the :data:`AttemptID` the worker knows it by, for display and
  for a multiplexed host to route calls by. Everything the run does is attributed to the attempt:
  the nodes it creates, the values it observes, the entities it stores.
* An **assignment** says what to run: a :class:`StepAssignment` names a step and its concrete
  inputs; a :class:`WorkflowAssignment` names a workflow, its position in the experiment, and its
  inputs as tokenized nodes. Every run session carries the assignment it executes.
* A workflow body's operations become **nodes** of the invocation's DAG. The worker never holds a
  node: it describes each one to the host with one of the ``emit_<kind>`` methods (the operation's
  parameters, with the input nodes named by token) and refers to it ever after by a
  :data:`NodeToken`, an opaque name for one node of one assignment, valid for the duration of that
  assignment. **The host names the inputs; the worker names everything after**: a
  :class:`WorkflowAssignment` carries its input nodes already tokenized, and the worker chooses the
  token for every node it introduces (each ``emit_<kind>``), passing it in with the call, distinct
  from every token bound in that assignment so far. Choosing a bound token or referring to an
  unbound one is a bug in the worker, refused with :class:`NodeTokenError`. Everything else about a
  node is the host's business: its type, which the host derives from the description rather than
  taking the worker's word for (except a run's declared result type), and however the host
  identifies and stores it (the in-process hosts hash the description into a content-addressed
  identity; the worker computes no hashes and never sees one).

**The id contract.** Entity ids do cross the boundary, in both directions, and the rule for them is
provenance: the only ids (:class:`~fxtr.core.entity_data.EntityID`) the worker passes back to the
session are ones the session handed it in the same attempt (a step assignment's inputs, the results
of ``store_raw_data``, ids inside loaded data and observed arrays). Arrays that cross the boundary
as values (``emit_source_array`` and ``complete_step`` going out, ``observe`` coming back) are the
host's to store or serve, and the worker never learns an id for them. This is an obligation the
worker meets; across the wire the remote adapter also checks it
(:mod:`fxtr.runner.experimental.remote.known_ids`).

The protocol is message passing throughout, so an implementation can be a proxy to a remote
service and neither side is tied to the other's concurrency machinery: every method is a request.
Emitting a node is synchronous and returns nothing, so a workflow body can describe its DAG without
awaiting; the host is expected to queue node descriptions and act on them asynchronously. Emissions
are one-way and later requests refer to their tokens, so a transport must deliver one session's
calls in order. Waiting happens only in :meth:`WorkflowRunSession.observe` and the completion
methods.

Failure policy: an error that originates in experiment state, rather than in user code, fails the
attempt unconditionally. A node whose computation failed makes
:meth:`WorkflowRunSession.observe` raise :class:`NodeFailedError`. The attempt is already *failed*
when the call raises, so the body cannot recover by catching: the host refuses everything further
from it with :class:`AttemptClosedError`. The only errors a body may usefully catch are the ones
its own code raised. A body must also be
deterministic: a later run of it against the same record must do the same things, or the attempt
fails with :class:`DeterminismError`. The host may run a body more than once for one invocation, as
a later attempt after a crash or as a proactive verification run of the same attempt under another
id; to the worker every such run is a run like any other. A run the worker performs ends in exactly
one of three ways: it completes; it **fails**, reported with ``fail`` when user code or the runner
raised; or it is **abandoned**, reported with ``abandon`` when the worker stopped running it for
reasons unrelated to the attempt itself (the worker is shutting down, or was interrupted). A run
the host stops on its own needs no report. Error handling and cancellation handling are the
worker's: the host only learns outcomes through the reports above.

**Paths and cache keys.** Workflow pathnames and cache key prefixes follow the formatting rule
of :mod:`fxtr.experiment.workflows`: ``/``-joined (:func:`~fxtr.experiment.workflows.join_path`),
with the mapped key appended as ``[dim:value,...]``
(:func:`~fxtr.experiment.workflows.append_mapped_key`), e.g. ``root/sample[prompt:3,split:"a"]``.

The same vocabulary exists in a multiplexed form, for a worker that runs many attempts over one
message stream: :class:`~fxtr.runner.experimental.multiplexed.WorkerSession` is this protocol with
the attempt id as an argument to every call, and a host implementing it forwards each call to the
run session the attempt id names.
"""

from __future__ import annotations

import dataclasses
import traceback
from collections.abc import Mapping, Sequence
from typing import Any, NewType, Protocol

from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID, FxtrDataValue, StorableFxtrDataValue
from fxtr.entity_defns.array import Array, ArrayType, CombineJoin, Join, KeyValue

AttemptID = NewType("AttemptID", str)
"""Identifies one attempt at running a user function; minted by the host."""

NodeToken = NewType("NodeToken", int)
"""Names one node of one assignment, opaquely, for the duration of that assignment (see the module
docstring for who chooses tokens). Tokens of different assignments are unrelated."""


# ---- errors ----


class RunnerError(Exception):
    """Base class for errors raised by the runner to user code or the worker."""


class AttemptClosedError(RunnerError):
    """The attempt can no longer act: it has completed, failed, been interrupted, or been
    cancelled, so the host refuses the operation."""


class NodeFailedError(RunnerError):
    """An observed node did not produce a value because the work behind it failed. Raised by
    :meth:`WorkflowRunSession.observe`; the attempt has been failed as a consequence."""


class DeterminismError(RunnerError):
    """A workflow body did not do the same thing twice. Raised by the host when a later run of a
    body (a replay against what an earlier attempt recorded, or the verification run that checks
    its determinism) emits a node the earlier run did not, gives a name to a different node, emits
    less by the time it returns, or returns a different node. The attempt is failed with it."""


class NodeTokenError(RunnerError):
    """A :data:`NodeToken` was misused: the worker chose one already bound in the assignment, or
    referred to one never bound in it. A bug in the worker (the contexts never do this), never a
    legitimate outcome; the attempt fails with it."""


@dataclasses.dataclass(frozen=True)
class AttemptError:
    """A serializable description of why an attempt failed.

    Attributes:
        type_name: The qualified name of the exception's class.
        message: The exception's message (``str(exc)``).
        traceback: The formatted traceback, as ``traceback.format_exception`` renders it.
    """

    type_name: str
    message: str
    traceback: str

    @classmethod
    def from_exception(cls, exc: BaseException) -> AttemptError:
        return cls(
            type_name=type(exc).__qualname__,
            message=str(exc),
            traceback="".join(traceback.format_exception(exc)),
        )


# ---- assignments ----


@dataclasses.dataclass(frozen=True, kw_only=True)
class WorkflowAssignment:
    """What a workflow run executes: an attempt at a workflow, and the body's inputs.

    Attributes:
        attempt_id: The attempt.
        workflow_name: The workflow to run (see :attr:`~fxtr.experiment.workflows.Workflow.name`).
        pathname: The workflow invocation's path in the experiment (see
            :func:`~fxtr.experiment.workflows.join_path` and
            :func:`~fxtr.experiment.workflows.append_mapped_key`).
        cache_key_prefix: The invocation's default cache key prefix.
        inputs: The workflow's arguments, by name, each a node of the invocation already bound to
            a token by the host, with its type (the argument's type as the workflow's signature
            sees it, mapped dims removed). The worker's own tokens must be distinct from these.
    """

    attempt_id: AttemptID
    workflow_name: str
    pathname: str
    cache_key_prefix: str
    inputs: frozendict[str, tuple[NodeToken, ArrayType]]


@dataclasses.dataclass(frozen=True, kw_only=True)
class StepAssignment:
    """What a step run executes: an attempt at a step, and the step's inputs.

    Attributes:
        attempt_id: The attempt.
        step_name: The step to run (see :attr:`~fxtr.experiment.steps.Step.name`).
        cache_key: The step invocation's cache key.
        inputs: The step's arguments, by name, as references to stored arrays (loadable through
            the attempt's storage).
    """

    attempt_id: AttemptID
    step_name: str
    cache_key: str
    inputs: frozendict[str, BoundID[Array[Any]]]


type Assignment = WorkflowAssignment | StepAssignment


# ---- run sessions: the protocol scoped to one run ----


class RunSessionBase(Protocol):
    """What every run session offers, whatever the kind of body it runs.

    A **run session** is the worker-facing protocol scoped to *one run*: one execution of one
    attempt's body, in one mode (see :mod:`fxtr.runner.assembly`). The host creates a session per
    run and hands it to whatever executes the body, and the body's context talks to nothing else.
    The two kinds are :class:`WorkflowRunSession` and :class:`StepRunSession`; a wrong-kind call is
    a type error rather than a refusal.

    The entity-store methods forward to the attempt's store (see
    :class:`~fxtr.core.entity_data.FxtrEntityDataStorage`) under the id contract of the module
    docstring; ``fail`` and ``abandon`` are how the worker reports how the run ended.
    """

    @property
    def run_id(self) -> AttemptID:
        """The id the worker knows the run by: for display, and for a multiplexed host to route
        calls by."""
        ...

    def log_message(self, message: str) -> None:
        """Record a free-form log line for the attempt.

        Synchronous and fire-and-forget, like the ``emit_<kind>`` methods: it queues the line to
        be persisted at the attempt's next commit point and returns at once. Logs are stored per
        attempt and never deduplicated across a replay.

        Raises:
            AttemptClosedError: If the attempt can no longer act.
        """
        ...

    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        """Store an entity on behalf of the attempt (see
        :meth:`~fxtr.core.entity_data.FxtrEntityDataStorage.store_raw_data`).

        Returns:
            The stored entity's id.

        Raises:
            AttemptClosedError: If the attempt can no longer act.
        """
        ...

    async def load_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        """Load an entity on behalf of the attempt (see
        :meth:`~fxtr.core.entity_data.FxtrEntityDataStorage.load_raw_data`).

        Returns:
            The entity's data.

        Raises:
            AttemptClosedError: If the attempt can no longer act (a session may choose not to
                refuse loads).
        """
        ...

    def is_known(self, entity_id: EntityID) -> bool:
        """Whether the attempt can reference an entity (see
        :meth:`~fxtr.core.entity_data.FxtrEntityDataStorage.is_known`): whether it can be loaded
        by, or referenced from data stored by, the attempt.

        Raises:
            AttemptClosedError: If the attempt can no longer act (a session may choose not to
                refuse this).
        """
        ...

    async def fail(self, error: AttemptError) -> None:
        """Report that the run's body failed: user code or the runner raised while running it.
        Idempotent on an attempt that can no longer act."""
        ...

    async def abandon(self, reason: str) -> None:
        """Report that the worker stopped running the body for reasons unrelated to the attempt
        (it is shutting down, or was interrupted). Not a failure: what happens to the attempt next
        (re-queueing it, say) is the host's decision. Idempotent on an attempt that can no longer
        act."""
        ...


class WorkflowRunSession(RunSessionBase, Protocol):
    """A run session for one execution of a workflow body.

    The ``emit_<kind>`` methods add one node each to the invocation's DAG: ``token`` is the
    worker's name for the new node, distinct from every token bound in :attr:`assignment` so far,
    and every other node argument is the token of a node already bound in it. Each is
    fire-and-forget: the host may check and record the node at the attempt's next commit point,
    and a node the invocation already has (the same operation on the same inputs) is a no-op
    beyond binding the token. Every one of them raises :class:`AttemptClosedError` if the attempt
    can no longer act and :class:`NodeTokenError` if ``token`` is already bound or an input token
    is not; over a transport where the call has returned before the host sees it, such a refusal
    arrives instead as the attempt's failure, so a body must not rely on catching one.

    The node kinds mirror the operations of :mod:`fxtr.runner.nodes`, and ``name``, where a method
    takes one, is the node's name, unique among the invocation's named nodes.
    """

    @property
    def assignment(self) -> WorkflowAssignment:
        """What this run executes: the workflow, its position, its inputs tokenized."""
        ...

    def emit_source_array(self, token: NodeToken, name: str, array: Array[Any]) -> None:
        """Add a source array: the host stores ``array`` (presentation-free; the context strips
        it) under the attempt and adds the node referring to it."""
        ...

    def emit_combine(
        self, token: NodeToken, inputs: Mapping[str, NodeToken], join: CombineJoin
    ) -> None:
        """Add a combine node: zip the inputs, by the field name each becomes, into one
        struct-valued array, with ``join`` reconciling their key sets."""
        ...

    def emit_stack(self, token: NodeToken, inputs: Mapping[KeyValue, NodeToken], dim: str) -> None:
        """Add a stack node: stack same-typed inputs, by their key along the new leading dim
        ``dim``, in any order (the host canonicalizes)."""
        ...

    def emit_query(self, token: NodeToken, inputs: Sequence[NodeToken], plan: bytes) -> None:
        """Add a query node: the result of running an array query over the input nodes.

        Args:
            token: The token naming the new node.
            inputs: The input nodes, in the order the plan's ``QuerySource`` positions index.
            plan: The query plan, encoded by :func:`~fxtr.runner.query_codec.encode_query`.
        """
        ...

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
        """Add a run-step node: run the step ``step_name`` on ``inputs`` (by the step's argument
        names), mapped over the dims ``map_over`` in result order with ``join`` reconciling the
        inputs' key sets along them, and with ``replicas`` (each replica dim's values, in result
        order after the mapped dims), under ``cache_key_prefix``.

        ``type`` is the result type the worker computed from the step's declared signature,
        presentation-free: the one part of a node's type the host takes from the worker, since the
        signature is user code the host does not have.
        """
        ...

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
        """Add a run-child-workflow node: as :meth:`emit_run_step`, with the workflow
        ``workflow_name`` (see :attr:`~fxtr.experiment.workflows.Workflow.name`) in place of the
        step."""
        ...

    async def observe(self, node: NodeToken) -> Array[Any]:
        """Wait for a node bound in this assignment to have a value.

        Returns:
            The node's array, under its stored (presentation-free) type.

        Raises:
            AttemptClosedError: If the attempt can no longer act.
            NodeTokenError: If ``node`` is not bound.
            NodeFailedError: If the node's work failed; the attempt has been failed.
        """
        ...

    async def complete_workflow(self, result: NodeToken) -> None:
        """Record that the body returned the node ``result``, whose type the worker has checked
        against the workflow's signature.

        Raises:
            AttemptClosedError: If the attempt can no longer act.
            NodeTokenError: If ``result`` is not bound.
        """
        ...


class StepRunSession(RunSessionBase, Protocol):
    """A run session for the one execution of a step attempt."""

    @property
    def assignment(self) -> StepAssignment:
        """What this run executes: the step, its cache key, its inputs as array refs."""
        ...

    async def complete_step(self, result: Array[Any]) -> None:
        """Record the step's result, which the host stores: the array the step produced, whose
        type the worker has checked against the step's signature.

        Raises:
            AttemptClosedError: If the attempt can no longer act.
        """
        ...


type RunSession = WorkflowRunSession | StepRunSession
"""A run session of either kind."""
