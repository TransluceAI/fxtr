"""The runner's contexts and handle: the user-facing API of a running attempt, over a session.

:class:`RunnerWorkflowContext` and :class:`RunnerArrayHandle` implement
:class:`~fxtr.experiment.workflows.WorkflowContext` and
:class:`~fxtr.experiment.workflows.ArrayHandle`
for one workflow attempt; :class:`RunnerStepContext` implements
:class:`~fxtr.experiment.steps.StepContext` for one step attempt. Each is built over the run's
session (:class:`~fxtr.runner.session.WorkflowRunSession` /
:class:`~fxtr.runner.session.StepRunSession`), reads the assignment off it, and forwards every
call to it.

The contexts do the static work themselves: each scheduling or handle operation computes the result
:class:`~fxtr.entity_defns.array.ArrayType` from its inputs' types (using the typing functions in
:mod:`fxtr.entity_defns.array` and :mod:`fxtr.experiment.signatures`), so type errors surface at
the call site, then describes the node to the session with the matching ``emit_<kind>`` method,
naming it with a fresh :data:`~fxtr.runner.session.NodeToken` and its inputs by the tokens of the
handles passed in. A handle is therefore a token plus the type computed here; what the node *is*
(its identity, its host-derived type) is the host's business, and the worker computes no hashes.
A query the body builds (:meth:`~fxtr.experiment.handles.ArrayHandle.query`, :func:`fxtr.sql`)
emits nothing until it is collected, and then the whole chain becomes one query node.
The context is the one place tokens are chosen: one counter per run, started above the
assignment's input tokens, so no token is ever reused. ArrayHandles are bound to the context that
created them; passing a handle to another attempt's context is an error, and names given to nodes
(source arrays, steps, child workflows) must be unique within the attempt.

The public surface of these classes is exactly the user-facing interface they implement. The
worker, which builds them, is privileged: it constructs :class:`RunnerArrayHandle` objects for an
attempt's input nodes directly, and uses :func:`node_token_of` to read the node behind the handle
a body returns.

The context touches the session at two seams (emitting a node, observing a node) plus the
storage methods; it keeps no record of what the body did. Verifying determinism is the host's
business (see :mod:`fxtr.runner.assembly`): a body may be run again as another run, over a fresh
context, and the host does the comparing.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from typing import Any

from frozendict import frozendict

from fxtr.core.entity_data import EntityID, FxtrDataValue, StorableFxtrDataValue
from fxtr.entity_defns.array import (
    Array,
    ArrayType,
    CombineJoin,
    Join,
    KeyValue,
    combine_type,
    stack_dim,
    stack_type,
)
from fxtr.entity_defns.query_plan import PlannedQuery
from fxtr.experiment.handles import ArrayHandle, ArrayHandleQuery
from fxtr.experiment.signatures import Replicas, run_output_type
from fxtr.experiment.steps import Step, StepContext
from fxtr.experiment.workflows import Workflow, WorkflowContext, join_path
from fxtr.runner.query_codec import encode_query
from fxtr.runner.session import (
    NodeToken,
    RunnerError,
    StepRunSession,
    WorkflowRunSession,
)


class ForeignArrayHandleError(RunnerError):
    """A handle was passed to a context other than the one that created it."""


class DuplicateNodeNameError(RunnerError):
    """A node name (source array, step, or child workflow) was used twice in one attempt."""


class RunnerArrayHandle[V](ArrayHandle[V]):
    """A handle in a workflow attempt: a node of the attempt, by its token, and the node's type.

    The context creates handles for the nodes it emits; the worker creates them for the attempt's
    input nodes before calling the body. A retype that changes the stored type emits its query
    node at once. A query built with :meth:`query` or a shortcut such as ``field`` emits one
    node, for the whole chain, when it is collected (see :meth:`_stored`).
    """

    def __init__(self, context: RunnerWorkflowContext, token: NodeToken, type: ArrayType) -> None:
        self._context = context
        # The node's stored type is ``type`` without presentation.
        self._token = token
        self._type = type

    @property
    def type(self) -> ArrayType:
        return self._type

    def _retyped(self, type: ArrayType) -> ArrayHandle[Any]:
        # Type equality ignores presentation: an equal type names the same node.
        if type == self._type:
            return RunnerArrayHandle(self._context, self._token, type)
        return self._stored(PlannedQuery.over(self, self._type).retype(type))

    async def observe(self) -> Array[V]:
        # The host returns the node's array under its stored (presentation-free) type; the
        # handle's type is what the workflow declared for it, so adopt it (restoring presentation).
        return (await self._context._observe(self._token)).retype(self._type)

    def _stored(self, query: PlannedQuery[ArrayHandle[Any]]) -> ArrayHandle[Any]:
        sources = tuple(node_token_of(self._context, source) for source in query.sources)
        if query.is_source:
            # A presentation-only retype of one handle: the node it reads.
            return RunnerArrayHandle(self._context, sources[0], query.type)
        context, token = self._context, self._context._next_token()
        context._session.emit_query(token, sources, encode_query(query.plan))
        return RunnerArrayHandle(context, token, query.type)

    def __repr__(self) -> str:
        return f"RunnerArrayHandle(token={self._token!r}, type={self._type})"


class RunnerWorkflowContext(WorkflowContext):
    """The workflow context of one workflow attempt, forwarding to a session.

    ArrayHandles to the attempt's input nodes are not built here: the worker builds them from the
    assignment with :class:`RunnerArrayHandle` and passes them to the body.

    Args:
        session: The run's session, whose assignment supplies the run id, pathname, and default
            cache address prefix.
    """

    def __init__(self, session: WorkflowRunSession) -> None:
        self._session = session
        assignment = session.assignment
        self._run_id = assignment.attempt_id
        self._pathname = assignment.pathname
        self._cache_key_prefix = assignment.cache_key_prefix
        self._names: set[str] = set()
        # The next token to hand out: above every token the host bound in the assignment.
        self._tokens = itertools.count(
            1 + max((token for token, _ in assignment.inputs.values()), default=-1)
        )

    @property
    def default_cache_address_prefix(self) -> str:
        return self._cache_key_prefix

    @property
    def pathname(self) -> str:
        return self._pathname

    # ---- internals shared with RunnerArrayHandle ----

    def _next_token(self) -> NodeToken:
        """A token no node of this attempt has yet: the name for the node about to be emitted."""
        return NodeToken(next(self._tokens))

    async def _observe(self, token: NodeToken) -> Array[Any]:
        return await self._session.observe(token)

    def log(self, message: str) -> None:
        self._session.log_message(message)

    def _tokens_of[K](self, handles: Mapping[K, ArrayHandle[Any]]) -> dict[K, NodeToken]:
        return {k: node_token_of(self, h) for k, h in handles.items()}

    def _claim_name(self, name: str) -> None:
        if name in self._names:
            raise DuplicateNodeNameError(
                f"Name {name!r} is already used in workflow {self.pathname!r}"
            )
        self._names.add(name)

    # ---- WorkflowContext ----

    def add_source_array(self, name: str, array: Array[Any]) -> ArrayHandle[Any]:
        self._claim_name(name)
        # The host stores the array (presentation-free) and adds the node; the handle keeps the
        # presented type.
        token = self._next_token()
        self._session.emit_source_array(token, name, array.strip_presentation())
        return RunnerArrayHandle(self, token, array.type)

    def combine(
        self, inputs: Mapping[str, ArrayHandle[Any]], *, join: CombineJoin = "exact"
    ) -> ArrayHandle[Any]:
        tokens = self._tokens_of(inputs)
        type = combine_type({name: h.type for name, h in inputs.items()}, join)
        token = self._next_token()
        self._session.emit_combine(token, tokens, join)
        return RunnerArrayHandle(self, token, type)

    def stack[V](self, inputs: Mapping[KeyValue, ArrayHandle[V]], *, dim: str) -> ArrayHandle[V]:
        tokens = self._tokens_of(inputs)
        type = stack_type((h.type for h in inputs.values()), stack_dim(inputs, dim))
        token = self._next_token()
        self._session.emit_stack(token, tokens, dim)
        return RunnerArrayHandle(self, token, type)

    def run_step[V](
        self,
        step_name: str,
        step: Step[Any, Any, V],
        inputs: Mapping[str, ArrayHandle[Any]],
        *,
        map_over: Sequence[str] = (),
        join: Join = "exact",
        replicas: Replicas | None = None,
        override_cache_address_prefix: str | None = None,
    ) -> ArrayHandle[V]:
        tokens = self._tokens_of(inputs)
        type = run_output_type(
            step.signature, {n: h.type for n, h in inputs.items()}, map_over, replicas
        )
        self._claim_name(step_name)
        token = self._next_token()
        self._session.emit_run_step(
            token,
            name=step_name,
            step_name=step.name,
            inputs=tokens,
            map_over=tuple(map_over),
            join=join,
            replicas=_freeze_replicas(replicas),
            cache_key_prefix=self._resolve_cache_key_prefix(
                step_name, override_cache_address_prefix
            ),
            type=type.strip_presentation(),
        )
        return RunnerArrayHandle(self, token, type)

    def run_child_workflow[V](
        self,
        workflow_name: str,
        workflow: Workflow[Any, Any, V],
        inputs: Mapping[str, ArrayHandle[Any]],
        *,
        map_over: Sequence[str] = (),
        join: Join = "exact",
        replicas: Replicas | None = None,
        override_cache_address_prefix: str | None = None,
    ) -> ArrayHandle[V]:
        tokens = self._tokens_of(inputs)
        type = run_output_type(
            workflow.signature, {n: h.type for n, h in inputs.items()}, map_over, replicas
        )
        self._claim_name(workflow_name)
        token = self._next_token()
        self._session.emit_run_child_workflow(
            token,
            name=workflow_name,
            workflow_name=workflow.name,
            inputs=tokens,
            map_over=tuple(map_over),
            join=join,
            replicas=_freeze_replicas(replicas),
            cache_key_prefix=self._resolve_cache_key_prefix(
                workflow_name, override_cache_address_prefix
            ),
            type=type.strip_presentation(),
        )
        return RunnerArrayHandle(self, token, type)

    def _resolve_cache_key_prefix(self, name: str, override: str | None) -> str:
        return (
            override if override is not None else join_path(self.default_cache_address_prefix, name)
        )

    # ---- FxtrEntityDataStorage ----

    def is_known(self, entity_id: EntityID) -> bool:
        return self._session.is_known(entity_id)

    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        return await self._session.store_raw_data(data)

    async def load_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        return await self._session.load_raw_data(entity_id)


def _freeze_replicas(replicas: Replicas | None) -> frozendict[str, tuple[KeyValue, ...]]:
    return frozendict({name: tuple(values) for name, values in (replicas or {}).items()})


def node_token_of(context: RunnerWorkflowContext, handle: ArrayHandle[Any]) -> NodeToken:
    """The node a handle refers to, as its token, checking that the handle belongs to ``context``.

    A :class:`RunnerArrayHandle` deliberately exposes nothing beyond the user-facing
    :class:`~fxtr.experiment.workflows.ArrayHandle` interface: which attempt it belongs to and
    which node it names are runner bookkeeping, not something a workflow body should read or build
    on. The runner itself does need both, in three places: the context, when a handle is passed as
    an input to one of its operations; a collected query, whose sources become its node's inputs;
    and the worker, when the body returns a handle and the worker must tell the host which node
    completes the attempt. This function is the single place that does that lookup, so the
    ownership check is not duplicated and the handle's internals stay private to this module. A
    handle from another context carries a token of another assignment, meaningless here, which is
    why the check comes first.

    Args:
        context: The context the handle is expected to belong to.
        handle: A handle returned to or created by user code.

    Returns:
        The node's token in ``context``'s assignment.

    Raises:
        TypeError: If ``handle`` is a query that was never collected into a node.
        ForeignArrayHandleError: If the handle was not created by ``context`` (including a handle
            from another attempt's context, or a foreign :class:`ArrayHandle` implementation).
    """
    if isinstance(handle, ArrayHandleQuery):
        raise TypeError(
            f"{handle!r} is a query, not a node: call .collect() to record it as one and pass the"
            " handle that returns"
        )
    if not isinstance(handle, RunnerArrayHandle) or handle._context is not context:
        raise ForeignArrayHandleError(
            f"ArrayHandle {handle!r} does not belong to workflow run {context._run_id!r}"
        )
    return handle._token


class RunnerStepContext(StepContext):
    """The step context of one step attempt, forwarding to its run's session.

    Args:
        session: The run's session, whose assignment supplies the cache address.
    """

    def __init__(self, session: StepRunSession) -> None:
        self._session = session
        self._cache_key = session.assignment.cache_key

    @property
    def cache_address(self) -> str:
        return self._cache_key

    def log(self, message: str) -> None:
        self._session.log_message(message)

    def is_known(self, entity_id: EntityID) -> bool:
        return self._session.is_known(entity_id)

    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        return await self._session.store_raw_data(data)

    async def load_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        return await self._session.load_raw_data(entity_id)
