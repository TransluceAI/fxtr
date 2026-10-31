"""Host-side helpers shared by the backends: a worker's runs of an attempt, the assembler that
turns the worker's token-based descriptions into nodes, and the verifier behind determinism
verification.

**Runs.** Everything the worker is handed is a *run*: one execution of one of the host's
*attempts*, under one id (the :data:`~fxtr.runner.session.AttemptID` the worker uses in every call
about it). A :class:`WorkflowRun` executes a workflow attempt in a :data:`RunMode`: the ``primary``
run is the attempt's own execution of the body, and a ``verification`` run is verification's second
execution of it; a :class:`StepRun` is the one execution of a step attempt. A run *is* the
worker-facing session for that execution (:class:`~fxtr.runner.session.WorkflowRunSession` /
:class:`~fxtr.runner.session.StepRunSession`): it holds the per-execution state (the assignment,
the token table, the mode) and implements every session method as "translate tokens, then
dispatch to the attempt by mode". The attempt is any object satisfying
:class:`WorkflowAttemptOps` / :class:`StepAttemptOps`, the identity-based operations a backend
provides; it knows nothing about run ids or tokens, and outlives its runs. How a run is executed
(the body in this process, or a worker elsewhere fed over a message stream) is the executor's
business (:mod:`fxtr.runner.driver`).

**Assembly.** The worker never holds a node: it describes each with an ``emit_<kind>`` call whose
node arguments are tokens (:data:`~fxtr.runner.session.NodeToken`), names the new node with a token
of its own choosing, and refers to nodes by token thereafter. Tokens are per run (a verification
run has its own assignment and its own numbering), so each :class:`WorkflowRun` has a
:class:`NodeAssembler`: the run's token → identity table and one builder per kind, which resolves
the input tokens, builds the :mod:`~fxtr.runner.nodes` node, computes its identity (the one place a
node is hashed), binds the worker's token to it, and hands the node back for the session to
dispatch on the run's mode. The assembler never mints tokens: the session binds the host-numbered
input tokens when it starts the run, and every later token is the worker's. It does not deduplicate
(two emissions of one operation are two tokens bound to one identity; the backends' "same identity
is a no-op" rule handles the node), and it holds no values and no types.


**Determinism verification** is a host setting, a lint for workflow bodies in the manner of
React's strict mode. When a body returns, the host does not record the return at once: it holds
it, hands the worker a verification run of the same attempt (the same assignment under a fresh id,
indistinguishable to the worker from any other), and checks everything that run does against what
the attempt's body did instead of recording it. The verification run must emit the same nodes,
observe the same nodes, and return the same node; then the host
records the held return through the normal path. Any difference is a
:class:`~fxtr.runner.session.DeterminismError`, which the host records as the attempt's failure.
The comparisons are on sets: a deterministic body using a task group acts in an order that varies
from run to run. Observed *values* are not compared (the verification run is served the live
values, which cannot differ); the set of nodes observed is, since a body that looks at different
things took a different path.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, NoReturn, Protocol

from frozendict import frozendict

from fxtr.core.entity_data import (
    EntityID,
    FxtrDataValue,
    FxtrEntityDataStorage,
    StorableFxtrDataValue,
)
from fxtr.entity_defns.array import Array, ArrayType, CombineJoin, Join, KeyValue
from fxtr.runner import nodes
from fxtr.runner.nodes import Node, NodeIdentity
from fxtr.runner.session import (
    AttemptClosedError,
    AttemptError,
    AttemptID,
    DeterminismError,
    NodeToken,
    NodeTokenError,
    StepAssignment,
    WorkflowAssignment,
)

# ---- attempt operations: what a backend's attempt object offers a run ----


type VerificationPhase = Literal["recording", "verifying", "agreed", "failed"]
"""Where an attempt is with respect to determinism verification: ``recording`` while its body runs,
``verifying`` from the body's return until the verification run has returned, then ``agreed`` or
``failed``."""


class WorkflowAttemptOps(Protocol):
    """The operations a backend's *workflow attempt* object offers, identity-based, for a
    :class:`WorkflowRun` to drive it through.

    The run translates the worker's tokens into nodes and identities and dispatches by its mode:
    the ``primary`` run's calls go to the recording operations, a ``verification`` run's to the
    ``verify_*`` ones, which check against the attempt's :class:`Verifier` and record nothing. A
    ``verify_*`` call when no verification run is expected (the phase is not ``verifying``) raises
    :class:`~fxtr.runner.session.AttemptClosedError`; one that finds a difference raises
    :class:`~fxtr.runner.session.DeterminismError` and the run fails the attempt.

    **Ending begins synchronously.** :meth:`fail` and :meth:`abandon` end the attempt at once
    (everything further is refused, the body is cancelled when asked) and arrange for the durable
    write of its outcome, which may complete asynchronously. This is what lets a synchronous
    ``emit_<kind>`` fail the attempt on a determinism violation.
    """

    @property
    def ended(self) -> bool:
        """Whether the attempt has reached its terminal outcome and accepts nothing more; the
        write of that outcome may still be pending. Final."""
        ...

    @property
    def entities(self) -> FxtrEntityDataStorage:
        """The attempt's entity store: what the run's ``store_raw_data`` / ``load_raw_data`` /
        ``is_known`` forward to."""
        ...

    @property
    def workflow_name(self) -> str: ...

    @property
    def pathname(self) -> str: ...

    @property
    def cache_key_prefix(self) -> str: ...

    @property
    def inputs(self) -> Mapping[str, tuple[NodeIdentity, ArrayType]]:
        """The invocation's arguments, by name, as identity and type: what a run's assignment is
        seeded from."""
        ...

    # -- the primary run: what the body does, recorded --

    def emit(self, node: Node) -> None: ...

    def emit_source_array(self, node: nodes.SourceArray, array: Array[Any]) -> None:
        """The body registered a source array: store ``array`` and emit the node referring to
        it."""
        ...

    def log(self, message: str) -> None: ...

    async def observe(self, identity: NodeIdentity) -> Array[Any]:
        """The body asked for a node's value: record it and answer once the node has settled."""
        ...

    async def record_return(self, identity: NodeIdentity) -> None:
        """Record that the body returned ``identity``."""
        ...

    async def hold_return(self, identity: NodeIdentity) -> None:
        """The body returned ``identity`` with verification on: park it for a verification run to
        confirm, once everything the body did is on record; the phase becomes ``verifying``."""
        ...

    # -- the verification run: what the body does again, checked --

    def verify_emit(self, node: Node) -> None: ...

    def verify_log(self, message: str) -> None: ...

    def verify_observe(self, identity: NodeIdentity) -> Array[Any]: ...

    def verify_return(self, identity: NodeIdentity) -> NodeIdentity:
        """The verification run returned: compare with what the body did and, on agreement, hand
        back the held return for the run to record."""
        ...

    def verification_failed(self) -> None:
        """The verification run's body raised where the attempt's body did not."""
        ...

    @property
    def verification_phase(self) -> VerificationPhase: ...

    # -- ending --

    def fail(self, error: AttemptError, *, cancel_body: bool) -> None:
        """End the attempt as failed. ``cancel_body`` says whether whatever is running the body
        must be stopped (false when the body is about to stop on its own because the call
        it is waiting on raises). Idempotent."""
        ...

    def abandon(self, reason: str) -> None:
        """End the attempt as abandoned: the body stopped for reasons unrelated to the attempt.
        Idempotent."""
        ...

    async def flush(self) -> None:
        """Wait until everything the body did so far is on record. Returns normally even if the
        attempt ended meanwhile."""
        ...


class StepAttemptOps(Protocol):
    """The operations a backend's *step attempt* object offers, for a :class:`StepRun`. Ending
    begins synchronously, as for :class:`WorkflowAttemptOps`."""

    @property
    def ended(self) -> bool: ...

    @property
    def entities(self) -> FxtrEntityDataStorage: ...

    @property
    def assignment(self) -> StepAssignment:
        """What the worker is handed to run it."""
        ...

    def add_log(self, message: str) -> None: ...

    async def complete(self, result: Array[Any]) -> None:
        """Store the result, record it, and end the attempt as completed."""
        ...

    def fail(self, error: AttemptError) -> None: ...

    def abandon(self, reason: str) -> None: ...


# ---- runs ----

type RunMode = Literal["primary", "verification"]
"""What a workflow run of an attempt is: the attempt's own execution of the body, or the second
execution that verifies its determinism."""


@dataclasses.dataclass(eq=False)
class WorkflowRun[A: WorkflowAttemptOps]:
    """One execution of a workflow attempt's body, in one mode, under the id the worker uses for
    it: the :class:`~fxtr.runner.session.WorkflowRunSession` the body's context talks to.

    The run owns what is *per execution*: the assignment it was seeded from, its token table
    (:class:`NodeAssembler`), its mode, and the dispatch rule below. The attempt (any object
    satisfying :class:`WorkflowAttemptOps`) owns what is *per attempt*: the DAG, the record, the
    verifier, the lifecycle. Each session method resolves the worker's tokens through the
    assembler, builds the node, and dispatches by mode: a ``primary`` run's calls go to the
    attempt's recording operations, a ``verification`` run's to its ``verify_*`` operations, a
    determinism violation there failing the attempt (with the body stopped) and surfacing to the
    body as :class:`~fxtr.runner.session.AttemptClosedError`, like any host-originated failure.
    ``complete_workflow`` on a primary run with verification on parks the return
    (:meth:`WorkflowAttemptOps.hold_return`) and returns; whoever drives the attempt then starts
    the verification run (see :mod:`fxtr.runner.driver`). A run never starts runs.

    Build one with :meth:`start`, which seeds the assembler from the attempt's inputs and builds
    the assignment. (The bare constructor is the host-internal form for a host that seeds the
    assignment itself.)

    Attributes:
        id: The id the worker knows the run by. A primary run's is its attempt's id.
        attempt: The attempt the run executes (a backend's attempt object).
        mode: See :data:`RunMode`.
        assembler: The run's token table and node builders (see :class:`NodeAssembler`).
        verify_determinism: Whether a primary run's return is held for a verification run.
    """

    id: AttemptID
    attempt: A
    mode: RunMode
    assembler: NodeAssembler = dataclasses.field(default_factory=lambda: NodeAssembler())
    verify_determinism: bool = False
    _assignment: WorkflowAssignment | None = dataclasses.field(default=None, init=False)

    @classmethod
    def start(
        cls, attempt: A, mode: RunMode, *, run_id: AttemptID, verify_determinism: bool
    ) -> WorkflowRun[A]:
        """A run of ``attempt`` in ``mode`` under ``run_id``, its assembler seeded with the
        attempt's input nodes and its assignment built from them."""
        run = cls(run_id, attempt, mode, verify_determinism=verify_determinism)
        ops = run._ops()
        run._assignment = run.assembler.seed_assignment(
            run_id,
            workflow_name=ops.workflow_name,
            pathname=ops.pathname,
            cache_key_prefix=ops.cache_key_prefix,
            inputs=ops.inputs,
        )
        return run

    # -- WorkflowRunSession --

    @property
    def run_id(self) -> AttemptID:
        return self.id

    @property
    def assignment(self) -> WorkflowAssignment:
        assert self._assignment is not None, "the run was not built with start()"
        return self._assignment

    def emit_source_array(self, token: NodeToken, name: str, array: Array[Any]) -> None:
        ops = self._ops()
        node = self.assembler.source_array(token, name, array)
        match self.mode:
            case "primary":
                ops.emit_source_array(node, array)  # stores the array, then emits
            case "verification":
                self._verify(lambda: ops.verify_emit(node))

    def emit_combine(
        self, token: NodeToken, inputs: Mapping[str, NodeToken], join: CombineJoin
    ) -> None:
        self._emit(self.assembler.combine(token, inputs, join))

    def emit_stack(self, token: NodeToken, inputs: Mapping[KeyValue, NodeToken], dim: str) -> None:
        self._emit(self.assembler.stack(token, inputs, dim))

    def emit_query(self, token: NodeToken, inputs: Sequence[NodeToken], plan: bytes) -> None:
        self._emit(self.assembler.query(token, inputs, plan))

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
        self._emit(
            self.assembler.run_step(
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
        self._emit(
            self.assembler.run_child_workflow(
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
        )

    def log_message(self, message: str) -> None:
        ops = self._ops()
        match self.mode:
            case "primary":
                ops.log(message)
            case "verification":
                ops.verify_log(message)  # a repeat of the body's line, dropped

    async def observe(self, node: NodeToken) -> Array[Any]:
        ops = self._ops()
        identity = self.assembler.resolve(node)
        match self.mode:
            case "primary":
                return await ops.observe(identity)
            case "verification":
                return self._verify(lambda: ops.verify_observe(identity))

    async def complete_workflow(self, result: NodeToken) -> None:
        ops = self._ops()
        returned = self.assembler.resolve(result)
        match self.mode:
            case "primary" if self.verify_determinism:
                await ops.hold_return(returned)  # a verification run confirms it (the driver's)
            case "primary":
                await ops.record_return(returned)
            case "verification":
                held = self._verify(lambda: ops.verify_return(returned))
                await ops.record_return(held)

    async def fail(self, error: AttemptError) -> None:
        ops = self._raw_ops()
        if ops.ended:
            return
        if self.mode == "verification":
            if ops.verification_phase != "verifying":
                # The run stopped because its verification already failed (the failure is being
                # recorded) or the attempt ended: nothing to add.
                return
            # The verification run's body raised where the attempt's body did not: a difference
            # like any other.
            ops.verification_failed()
            error = AttemptError.from_exception(
                DeterminismError(
                    "The body raised on the verification run but not the first time:"
                    f" {error.type_name}: {error.message}"
                )
            )
        await ops.flush()  # what the body did before failing goes on record first
        ops.fail(error, cancel_body=False)

    async def abandon(self, reason: str) -> None:
        ops = self._raw_ops()
        if ops.ended:
            return
        await ops.flush()
        ops.abandon(reason)

    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        return await self._ops().entities.store_raw_data(data)

    async def load_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        return await self._raw_ops().entities.load_raw_data(entity_id)

    def is_known(self, entity_id: EntityID) -> bool:
        return self._raw_ops().entities.is_known(entity_id)

    # -- dispatch --

    def _raw_ops(self) -> WorkflowAttemptOps:
        return self.attempt

    def _ops(self) -> WorkflowAttemptOps:
        """The attempt's operations, if it may still act.

        Raises:
            AttemptClosedError: If the attempt has ended.
        """
        ops = self._raw_ops()
        if ops.ended:
            raise AttemptClosedError(f"Attempt {self.id} has ended")
        return ops

    def _emit(self, node: Node) -> None:
        """Hand an assembled node to the attempt for the run's mode: recorded on a primary run,
        checked on a verification run. Every ``emit_<kind>`` but the source array ends here."""
        ops = self._ops()
        match self.mode:
            case "primary":
                ops.emit(node)
            case "verification":
                self._verify(lambda: ops.verify_emit(node))

    def _verify[T](self, check: Callable[[], T]) -> T:
        """Run one of the attempt's ``verify_*`` operations for this verification run. A violation
        fails the attempt with the :class:`~fxtr.runner.session.DeterminismError`, stopping the
        body, and the body sees :class:`~fxtr.runner.session.AttemptClosedError`, as for any
        failure the host originated."""
        try:
            return check()
        except DeterminismError as exc:
            self._raw_ops().fail(AttemptError.from_exception(exc), cancel_body=True)
            raise AttemptClosedError(f"Attempt {self.id} failed verification: {exc}") from None


@dataclasses.dataclass(eq=False)
class StepRun[A: StepAttemptOps]:
    """The (only) execution of a step attempt, under the attempt's own id: the
    :class:`~fxtr.runner.session.StepRunSession` the step's context talks to, over an attempt
    satisfying :class:`StepAttemptOps`.

    Attributes:
        id: The id the worker knows the run by: its attempt's id.
        attempt: The attempt the run executes (a backend's step attempt object).
    """

    id: AttemptID
    attempt: A

    @property
    def run_id(self) -> AttemptID:
        return self.id

    @property
    def assignment(self) -> StepAssignment:
        return self._raw_ops().assignment

    def log_message(self, message: str) -> None:
        self._ops().add_log(message)

    async def complete_step(self, result: Array[Any]) -> None:
        await self._ops().complete(result)

    async def fail(self, error: AttemptError) -> None:
        ops = self._raw_ops()
        if not ops.ended:
            ops.fail(error)

    async def abandon(self, reason: str) -> None:
        ops = self._raw_ops()
        if not ops.ended:
            ops.abandon(reason)

    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        return await self._ops().entities.store_raw_data(data)

    async def load_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        return await self._raw_ops().entities.load_raw_data(entity_id)

    def is_known(self, entity_id: EntityID) -> bool:
        return self._raw_ops().entities.is_known(entity_id)

    def _raw_ops(self) -> StepAttemptOps:
        return self.attempt

    def _ops(self) -> StepAttemptOps:
        ops = self._raw_ops()
        if ops.ended:
            raise AttemptClosedError(f"Attempt {self.id} has ended")
        return ops


# ---- assembly ----


class NodeAssembler:
    """One run's translation from the worker's tokens to nodes: a token → identity table and a
    builder per node kind (see the module docstring).

    Each builder takes the worker's token for the new node and the description's node arguments as
    tokens, resolves those, builds the node, binds the token to the node's identity, and returns
    the node. A token bound twice or resolved without being bound is a :class:`NodeTokenError`.
    """

    def __init__(self) -> None:
        self._identities: dict[NodeToken, NodeIdentity] = {}

    def bind(self, token: NodeToken, identity: NodeIdentity) -> None:
        """Bind a token to a node the host already has (an input).

        Raises:
            NodeTokenError: If the token is already bound.
        """
        if token in self._identities:
            raise NodeTokenError(f"Node token {token} is already bound in this assignment")
        self._identities[token] = identity

    def resolve(self, token: NodeToken) -> NodeIdentity:
        """The identity a token is bound to.

        Raises:
            NodeTokenError: If the token is not bound.
        """
        try:
            return self._identities[token]
        except KeyError:
            raise NodeTokenError(f"Node token {token} is not bound in this assignment") from None

    def seed_assignment(
        self,
        attempt_id: AttemptID,
        *,
        workflow_name: str,
        pathname: str,
        cache_key_prefix: str,
        inputs: Mapping[str, tuple[NodeIdentity, ArrayType]],
    ) -> WorkflowAssignment:
        """Bind a fresh token to each input node, numbered from zero in the order given, and build
        the run's assignment from them. Called once, on a fresh assembler, when the run starts."""
        assert not self._identities, "an assembler is seeded before anything is bound"
        tokenized: dict[str, tuple[NodeToken, ArrayType]] = {}
        for token, (name, (identity, type)) in enumerate(inputs.items()):
            self.bind(NodeToken(token), identity)
            tokenized[name] = (NodeToken(token), type)
        return WorkflowAssignment(
            attempt_id=attempt_id,
            workflow_name=workflow_name,
            pathname=pathname,
            cache_key_prefix=cache_key_prefix,
            inputs=frozendict(tokenized),
        )

    # -- builders, one per emit_<kind> --

    def source_array(self, token: NodeToken, name: str, array: Array[Any]) -> nodes.SourceArray:
        return self._bind_node(token, nodes.source_array(name, array))

    def combine(
        self, token: NodeToken, inputs: Mapping[str, NodeToken], join: CombineJoin
    ) -> nodes.Combine:
        return self._bind_node(token, nodes.Combine(inputs=self._resolve_map(inputs), join=join))

    def stack(
        self, token: NodeToken, inputs: Mapping[KeyValue, NodeToken], dim: str
    ) -> nodes.Stack:
        # Canonical order is by key. Keys are all of one kind (the host's type check rejects a
        # mix), so the kind's name only keeps the sort total on a description that is about to
        # be rejected anyway.
        resolved = self._resolve_map(inputs)
        stacked = tuple(
            sorted(resolved.items(), key=lambda item: (type(item[0]).__name__, item[0]))
        )
        return self._bind_node(token, nodes.Stack(inputs=stacked, dim=dim))

    def query(self, token: NodeToken, inputs: Sequence[NodeToken], plan: bytes) -> nodes.Query:
        return self._bind_node(
            token,
            nodes.Query(inputs=tuple(self.resolve(input) for input in inputs), plan=plan),
        )

    def run_step(
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
    ) -> nodes.RunStep:
        return self._bind_node(
            token,
            nodes.RunStep(
                name=name,
                step_name=step_name,
                inputs=self._resolve_map(inputs),
                map_over=tuple(map_over),
                join=join,
                replicas=frozendict(replicas),
                cache_key_prefix=cache_key_prefix,
                type=type,
            ),
        )

    def run_child_workflow(
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
    ) -> nodes.RunChildWorkflow:
        return self._bind_node(
            token,
            nodes.RunChildWorkflow(
                name=name,
                workflow_name=workflow_name,
                inputs=self._resolve_map(inputs),
                map_over=tuple(map_over),
                join=join,
                replicas=frozendict(replicas),
                cache_key_prefix=cache_key_prefix,
                type=type,
            ),
        )

    def _resolve_map[K](self, tokens: Mapping[K, NodeToken]) -> frozendict[K, NodeIdentity]:
        return frozendict({k: self.resolve(t) for k, t in tokens.items()})

    def _bind_node[N: Node](self, token: NodeToken, node: N) -> N:
        """Bind the worker's token to the node's identity. The inputs were resolved (and so
        checked) before the node was built, so a bad input token never binds anything."""
        self.bind(token, node.content_id())
        return node


# ---- determinism verification ----


class Verifier:
    """The record of what one workflow attempt's body did, as the host saw it, and the check that
    a verification run does the same.

    The host calls the ``note_*`` methods as the attempt's body acts and :meth:`note_return` when
    it returns, which holds the returned node and starts the ``verifying`` phase; it then calls
    the ``check_*`` methods as the verification run acts and :meth:`check_return` when that
    returns. Each check raises :class:`~fxtr.runner.session.DeterminismError` at the first
    difference and moves the phase to ``failed``; how that failure is recorded and how the worker
    learns of it are the host's.

    Attributes:
        phase: See :data:`VerificationPhase`.
        returned: The node the attempt's body returned, held from :meth:`note_return` until the
            host records it; ``None`` while ``recording``.
    """

    def __init__(self) -> None:
        self.phase: VerificationPhase = "recording"
        self.returned: NodeIdentity | None = None
        self._emitted: set[NodeIdentity] = set()
        self._observed: set[NodeIdentity] = set()
        self._verification_emitted: set[NodeIdentity] = set()
        self._verification_observed: set[NodeIdentity] = set()

    # -- the attempt's body --

    def note_emit(self, identity: NodeIdentity) -> None:
        """The body emitted a node (a source array, an operation, or a run)."""
        self._emitted.add(identity)

    def note_observe(self, identity: NodeIdentity) -> None:
        """The body asked for a node's value."""
        self._observed.add(identity)

    def note_return(self, identity: NodeIdentity) -> None:
        """The body returned this node: hold it, and enter the ``verifying`` phase."""
        assert self.phase == "recording", self.phase
        self.returned = identity
        self.phase = "verifying"

    # -- the verification run --

    def check_emit(self, identity: NodeIdentity, description: str) -> None:
        """The verification run emitted a node; it must be one the attempt's body emitted.

        Args:
            identity: The node.
            description: How to name the node in the error (its kind, say).

        Raises:
            DeterminismError: If the attempt's body did not emit it.
        """
        self._require_verifying()
        if identity not in self._emitted:
            self._violation(
                f"The body emitted {description} {identity!r} the second time but not the first"
            )
        self._verification_emitted.add(identity)

    def check_observe(self, identity: NodeIdentity) -> None:
        """The verification run asked for a node's value; it must be one the attempt's body
        observed.

        Raises:
            DeterminismError: If the attempt's body did not observe it.
        """
        self._require_verifying()
        if identity not in self._observed:
            self._violation(
                f"The body observed node {identity!r} the second time but not the first"
            )
        self._verification_observed.add(identity)

    def check_return(self, returned: NodeIdentity) -> NodeIdentity:
        """The verification run returned; compare it as a whole with what the attempt's body did,
        and on agreement enter the ``agreed`` phase.

        Returns:
            The held return, for the host to record.

        Raises:
            DeterminismError: If the returned node differs, or the attempt's body emitted or
                observed a node the verification run did not.
        """
        self._require_verifying()
        assert self.returned is not None
        if returned != self.returned:
            self._violation(
                f"The body returned node {self.returned!r} the first time and {returned!r} the"
                " second time"
            )
        missing = self._emitted - self._verification_emitted
        if missing:
            self._violation(
                f"The body emitted {len(missing)} node(s) the first time that it did not emit the"
                f" second time: {sorted(map(repr, missing))}"
            )
        unobserved = self._observed - self._verification_observed
        if unobserved:
            self._violation(
                f"The body observed {len(unobserved)} node(s) the first time that it did not"
                f" observe the second time: {sorted(map(repr, unobserved))}"
            )
        self.phase = "agreed"
        return self.returned

    def mark_failed(self) -> None:
        """A difference the host found itself (the verification run's body raised where the
        attempt's body did not): enter the ``failed`` phase."""
        self._require_verifying()
        self.phase = "failed"

    def _require_verifying(self) -> None:
        assert self.phase == "verifying", f"no verification run is expected in phase {self.phase}"

    def _violation(self, message: str) -> NoReturn:
        self.phase = "failed"
        raise DeterminismError(message)


__all__ = [
    "NodeAssembler",
    "RunMode",
    "StepAttemptOps",
    "StepRun",
    "VerificationPhase",
    "Verifier",
    "WorkflowAttemptOps",
    "WorkflowRun",
]
