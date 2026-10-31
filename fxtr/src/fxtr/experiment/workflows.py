"""Experiment workflow definitions.

A *workflow* describes a computation as a DAG of steps and child workflows over *handles*
(:class:`~fxtr.experiment.handles.ArrayHandle`): lazy arrays whose
:class:`~fxtr.entity_defns.array.ArrayType` is known up front. A
:class:`WorkflowContext` provides the scheduling methods; each one checks the signature of the step
or workflow being scheduled against the types of the input handles synchronously, so the returned
handle's type is known as soon as it exists, and only later (and possibly elsewhere) does the
scheduled work actually run. Workflows are async and may ``await`` a handle's
:meth:`~fxtr.experiment.handles.ArrayHandle.observe` mid-body, so the DAG can depend on
computed values.

Every handle is an array (see :mod:`fxtr.entity_defns.array`): a scalar is an array with no dims.
Scheduling is one operation, ``run``, with a ``map_over`` list of dim names: the inputs are joined
along those dims (broadcasting inputs that lack them), the step or workflow runs once per joint
key, and the results are stacked back along the mapped dims. A run may also declare ``replicas``:
new dims with explicit values along which it is simply run again, invisible to the body. See
:mod:`fxtr.experiment.signatures` for the typing rule.

**Pathnames and cache addresses.** Every workflow invocation has a *pathname* placing it in the
experiment, and a *cache address prefix* under which the steps it schedules are cached. Both are
``/``-separated: a node scheduled by a workflow gets its prefix joined with the node's name
(:func:`join_path`), and mapping appends the mapped key as ``[dim:value,...]`` with JSON-encoded
values (:func:`append_mapped_key`), e.g. ``root/sample[prompt:3,split:"a"]``. A child workflow's
pathname and default cache address prefix are formed the same way from its parent's.
"""

from __future__ import annotations

import abc
import dataclasses
import json
import typing
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, Literal, Protocol, cast, overload

from frozendict import frozendict

from fxtr.core.entities import BoundID, Entity, FxtrEntityStorage
from fxtr.entity_defns.array import (
    Array,
    ArrayTypeSpec,
    CombineJoin,
    Join,
    KeyValue,
    is_assignable_array,
)
from fxtr.entity_defns.schema import IdSchema, NullableSchema, Schema
from fxtr.experiment.annotations import (
    AnnotatedType,
    body_hints,
    declared_signature_types,
    unwrap_annotation,
)
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.registry import GLOBAL_REGISTRY, default_name
from fxtr.experiment.signatures import (
    Replicas,
    Signature,
    SignatureError,
    input_view_type,
    resolve_signature,
)
from fxtr.experiment.steps import Step

# ---- pathnames and cache addresses ----


def join_path(prefix: str, name: str) -> str:
    """Join a pathname or cache address prefix with a node name: ``"root/foo"`` from ``"root"`` and
    ``"foo"``."""
    return f"{prefix}/{name}"


def append_mapped_key(prefix: str, key: Mapping[str, KeyValue]) -> str:
    """Append a mapped key to a pathname or cache address prefix.

    The key is rendered as ``[dim:value,...]`` in the mapping's order (which should be the
    ``map_over`` order), values JSON-encoded, e.g. ``"root/sample[prompt:3,split:\"a\"]"``. An
    empty key returns the prefix unchanged.
    """
    if not key:
        return prefix
    inner = ",".join(f"{dim}:{json.dumps(value, ensure_ascii=False)}" for dim, value in key.items())
    return f"{prefix}[{inner}]"


# ---- contexts ----


class WorkflowContext(FxtrEntityStorage):
    """Context for a durable idempotent workflow.

    WorkflowContext provides the methods that a workflow function needs in order to describe its
    computation DAG and data dependencies: registering source arrays, combining and stacking
    handles, and scheduling steps and child workflows. Every method derives
    the returned handle's type from the inputs' types synchronously, before anything runs.

    WorkflowContext extends FxtrEntityStorage and thus supports storing and loading entities.
    """

    @property
    @abc.abstractmethod
    def default_cache_address_prefix(self) -> str:
        """The default cache address prefix for the workflow execution.

        Joined (:func:`join_path`) with each scheduled node's name to form that node's cache address
        prefix, unless a different prefix is provided.
        """

    @property
    @abc.abstractmethod
    def pathname(self) -> str:
        """The name for this workflow in the experiment.

        Expressed as a path string e.g. ``"root/foo/bar"`` identifying this particular workflow
        execution in the context of an experiment run (see :func:`join_path` and
        :func:`append_mapped_key`).
        """

    @abc.abstractmethod
    def log(self, message: str) -> None:
        """Record a free-form log line for this attempt.

        Logs are stored per attempt and never deduplicated across a replay: a later attempt of the
        same invocation re-runs the body and writes its own lines. Fire-and-forget: the call does
        not wait for the line to be persisted, and lines emitted during strict mode's verification
        run are discarded.
        """

    @abc.abstractmethod
    def add_source_array(self, name: str, array: Array[Any]) -> ArrayHandle[Any]:
        """Register an array with the workflow context, returning a handle to it.

        The name must be unique within the workflow context. Synchronous, like the other
        DAG-building methods: the handle's identity is content-derived, and the implementation
        takes care of storing the array.

        Args:
            name: The name to attach to the array in the experiment.
            array: The array to register; the handle's type is the array's type.
        """

    @abc.abstractmethod
    def combine(
        self, inputs: Mapping[str, ArrayHandle[Any]], *, join: CombineJoin = "exact"
    ) -> ArrayHandle[Any]:
        """A handle to the arrays zipped into one struct-valued array (see
        :meth:`~fxtr.entity_defns.array.Array.combine`): its dims are the union of the inputs'
        dims, each input broadcast along the dims it lacks, and its value has one field per input
        (all nullable with ``join="outer"``).

        Raises:
            ArrayTypeError: If inputs disagree on a dim's kind.
        """

    @abc.abstractmethod
    def stack[V](self, inputs: Mapping[KeyValue, ArrayHandle[V]], *, dim: str) -> ArrayHandle[V]:
        """A handle to same-typed arrays stacked along a new leading dim ``dim``, keyed by the
        mapping's keys (see :meth:`~fxtr.entity_defns.array.Array.stack`).

        Raises:
            ArrayTypeError: If the inputs differ in type, the keys are not all of one key kind, or
                ``dim`` collides with an existing dim.
        """

    @abc.abstractmethod
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
        """Schedule a step over the given inputs, returning a handle to the result.

        With an empty ``map_over`` the step runs once and receives each input array whole. With
        mapped dims, the inputs are joined along them (see
        :func:`~fxtr.entity_defns.array.join_keys`; inputs lacking a mapped dim are broadcast, and
        ``join`` says whether key sets must agree exactly or are intersected), the step runs once
        per joint key on the inputs' slices, and the results are stacked so the result has the
        mapped dims followed by the step's declared output dims. ``replicas`` adds new dims after
        the mapped ones: the step runs once more per replica value with the same inputs (the body
        does not see the value; only the cache address differs), so
        ``replicas={"sample": range(3)}`` draws three samples per joint key. The result type is
        derived before anything runs (see :func:`~fxtr.experiment.signatures.run_output_type`).

        Args:
            step_name: The name of the step to run. Must be unique within the parent workflow.
            step: The step to run.
            inputs: The step's arguments, by name, each as a handle.
            map_over: The dim names to map over, in the order they appear in the result.
            join: How to reconcile the inputs' key sets along the mapped dims.
            replicas: New dims to replicate over, each with its values (all ``int`` or all
                ``str``), in the order they appear in the result after the mapped dims. A replica
                must not be a dim of any input.
            override_cache_address_prefix: The cache address prefix for the step. Cache addresses
                are derived by appending the mapped key, then the replica key
                (:func:`append_mapped_key`),
                formatted like ``path/to/step[prompt:123,split:"a",sample:0]`` with values JSON
                encoded (just the prefix when nothing is mapped or replicated over). If not
                provided, ``default_cache_address_prefix`` and ``step_name`` are joined with ``/``
                (:func:`join_path`).

        Raises:
            SignatureError: If the mapped dims cannot be resolved, the replicas are malformed or
                collide with input dims, or the inputs do not fit the step's signature.
        """

    @abc.abstractmethod
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
        """Schedule a child workflow over the given inputs, returning a handle to the result.

        Behaves exactly like :meth:`run_step` with a workflow in place of the step; see there for
        the arguments. The cache address prefix (with the mapped and replica keys appended) becomes
        the child's default cache address prefix.

        Raises:
            SignatureError: If the mapped dims cannot be resolved, the replicas are malformed or
                collide with input dims, or the inputs do not fit the workflow's signature.
        """


class WorkflowBody[**P, R](Protocol):
    """The user-written function behind a :class:`Workflow`: an ``async def`` taking the workflow
    context first, then the workflow's named arguments, and returning the result.

    Arguments arrive as handles by default; a body's *annotations* can ask for an observed array
    or a converted scalar value instead, the way a step's do (see :meth:`Workflow.invoke`). The
    result is a handle, or a bare value when the output type has no dims (wrapped into a scalar
    source array). ``R`` is the Python type of the result's values either way, as the body returns
    them (so an entity class for a body that returns an entity; see :class:`Workflow` for the type
    of the values it produces).

    A body is an ordinary function, so it also carries ``__module__`` and ``__qualname__`` (used
    to derive a default workflow name).
    """

    __module__: str
    __qualname__: str

    def __call__(
        self, context: WorkflowContext, *args: P.args, **kwargs: P.kwargs
    ) -> Awaitable[ArrayHandle[R] | R]: ...


RETURN_NODE_NAME = "$return"
"""The reserved source-array node name a workflow body's bare return value is registered under
(see :meth:`Workflow.invoke`)."""


type WorkflowArgKind = Literal["handle", "observe", "load", "plain"]
"""How :meth:`Workflow.invoke` hands one argument to the body (see
:class:`WorkflowArgConversion`)."""


@dataclasses.dataclass(frozen=True)
class WorkflowArgConversion:
    """How :meth:`Workflow.invoke` passes one argument, derived from the parameter's annotation.

    As for a step (:class:`~fxtr.experiment.steps.ArgConversion`), the input is first retyped to the
    type its annotation declares (:attr:`view`), so an observed array or an unwrapped scalar
    *presents* as annotated. :attr:`kind` then says what the body receives.

    Attributes:
        kind: ``"handle"`` (annotated ``ArrayHandle[...]``, unannotated, or ``Any``: the handle
            itself); ``"observe"`` (annotated ``Array[...]``: the awaited, observed array);
            ``"load"`` (annotated a bare entity class ``T``: the scalar item, loaded through the
            context as ``target``); or ``"plain"`` (anything else: the scalar item as its presented
            value — a bound id for ``BoundID[T]``, a bare id for ``EntityID``, a ``dict`` for a
            TypedDict, ...). ``"load"`` and ``"plain"`` need a scalar (no dims) handle.
        target: The entity class to load a scalar id as, for ``"load"``; ``None`` otherwise.
        view: The array type the parameter's annotation declares (its dims may be absent), used to
            retype the handle before it is passed; ``None`` when unannotated or annotated ``Any``.
    """

    kind: WorkflowArgKind
    target: Any = None
    view: AnnotatedType | None = None


HANDLE_ARG = WorkflowArgConversion("handle")
"""The conversion of an unannotated parameter: the handle itself, retyped to the declared input."""


def workflow_arg_conversions(fn: Any) -> frozendict[str, WorkflowArgConversion]:
    """Derive a :class:`WorkflowArgConversion` per parameter of a workflow body from its annotations
    (parameters not listed get :data:`HANDLE_ARG`).

    Raises:
        TypeError: If an annotation cannot be resolved or is not a valid array/value type.
    """
    names, hints = body_hints(fn)
    views, _ = declared_signature_types(fn)
    return frozendict(
        {name: _classify(hints[name], views[name]) for name in names if name in hints}
    )


def _classify(annotation: Any, view: AnnotatedType | None) -> WorkflowArgConversion:
    ann = unwrap_annotation(annotation)
    origin = typing.get_origin(ann) or ann
    if origin is ArrayHandle or ann is Any or ann is object:
        return WorkflowArgConversion("handle", view=view)
    if origin is Array:
        return WorkflowArgConversion("observe", view=view)
    if isinstance(ann, type) and issubclass(ann, Entity):
        return WorkflowArgConversion("load", target=ann, view=view)
    return WorkflowArgConversion("plain", view=view)


def _core_schema(schema: Schema) -> Schema:
    return schema.inner if isinstance(schema, NullableSchema) else schema


@dataclasses.dataclass(frozen=True)
class Workflow[**P, R, V]:
    """A workflow: a named, signed body. Produced by :func:`workflow`; not constructed directly.

    Only ``Workflow`` objects can be scheduled as child workflows, so an undecorated function
    cannot be passed where a workflow is expected. The handle a body returns must have a type
    matching the signature's output for the given inputs; the runtime checks this when the body
    finishes.

    The type parameters mean what they do for :class:`~fxtr.experiment.steps.Step`: ``P`` is the
    body's parameters, ``R`` the Python type of the values it returns, and ``V`` that of the
    values of the array it produces (what :meth:`invoke` and scheduling it as a child return),
    which is ``BoundID[T]`` where the body returns an entity ``T`` and ``R`` otherwise.

    Attributes:
        name: The workflow's name, defaulting to the body's fully-qualified name
            (``module.qualname``).
        signature: Maps the types of the workflow's arguments to the type of its result.
        body: The wrapped function.
        conversions: How :meth:`invoke` passes each argument, by parameter name (see
            :class:`WorkflowArgConversion`); parameters not listed get :data:`HANDLE_ARG`.
            :func:`workflow` derives these from the body's annotations.
    """

    name: str
    signature: Signature
    body: WorkflowBody[P, R]
    conversions: frozendict[str, WorkflowArgConversion] = frozendict()

    async def __call__(
        self, context: WorkflowContext, *args: P.args, **kwargs: P.kwargs
    ) -> ArrayHandle[R] | R:
        """Call the body directly, passing the arguments through untouched."""
        return await self.body(context, *args, **kwargs)

    async def invoke(
        self, context: WorkflowContext, inputs: Mapping[str, ArrayHandle[Any]]
    ) -> ArrayHandle[V]:
        """Run the workflow on concrete input handles, the way the runner does.

        The inputs are checked against the signature, which also yields the output type. Each input
        is retyped to the type its annotation declares and passed according to its
        :class:`WorkflowArgConversion`: the handle itself, the awaited observed array, or the
        awaited scalar value (a bare entity class loads the entity through ``context``; see
        :meth:`~fxtr.experiment.steps.Step.invoke` for the scalar rules, which this mirrors on top
        of handles). The body returns a handle, whose type must be assignable to the output, or a
        bare value when the output type has no dims (for an ``id`` output an
        :class:`~fxtr.core.entities.Entity` is stored and a bound id unwrapped to its id); a bare
        value is registered as a scalar source array under :data:`RETURN_NODE_NAME` at the declared
        output type, and that handle is the result.

        Args:
            context: The workflow context.
            inputs: The workflow's arguments, by name, as handles.

        Raises:
            SignatureError: If the inputs do not fit the signature, a scalar-value parameter
                receives a handle with dims, the returned handle's type does not fit the output, or
                a bare value is returned for an output type with dims.
        """
        output = self.signature.output_type({name: h.type for name, h in inputs.items()})
        args = {name: await self._convert_arg(context, name, h) for name, h in inputs.items()}
        result = await self.body(context, **args)  # type: ignore[arg-type]
        if isinstance(result, ArrayHandle):
            if not is_assignable_array(result.type, output):
                raise SignatureError(
                    f"Workflow {self.name!r} returned a handle of type {result.type}, expected"
                    f" {output}"
                )
            # The handle's values are ``R``s; for an entity ``R`` the checked type is an id, so
            # they are the ``V``s (bound ids) the workflow produces.
            return cast("ArrayHandle[V]", result)
        if output.dims:
            raise SignatureError(
                f"Workflow {self.name!r} returned a bare value but its output type {output} has"
                " dims; return a handle"
            )
        value: Any = result
        if isinstance(_core_schema(output.schema), IdSchema):
            if isinstance(value, Entity):
                value = (await context.store(value)).entity_id
            elif isinstance(value, BoundID):
                value = value.entity_id
        array = Array.scalar(value, output.schema)
        return context.add_source_array(RETURN_NODE_NAME, array)

    async def _convert_arg(
        self, context: WorkflowContext, name: str, handle: ArrayHandle[Any]
    ) -> Any:
        conv = self.conversions.get(name, HANDLE_ARG)
        target = input_view_type(conv.view, self.signature, name, handle.type)
        if (
            conv.kind in ("load", "plain")
            and handle.dims
            and target is not None
            and not target.dims
        ):
            raise SignatureError(
                f"Workflow {self.name!r} parameter {name!r} is annotated for a dim-less value but"
                f" received a handle of type {handle.type}"
            )
        if target is not None:
            handle = handle.retype(target)
        if conv.kind == "handle":
            return handle
        if conv.kind == "observe":
            return await handle.observe()
        value = await handle.item()
        if conv.kind == "load" and value is not None:
            return await context.load(value, as_type=conv.target)
        return value


class _WorkflowDecorator(Protocol):
    """The decorator returned by :func:`workflow`.

    The overloads work out the :class:`Workflow`'s ``V`` from the body's return type ``R`` the way
    :func:`~fxtr.experiment.steps.step`'s do: an entity class ``T`` (or ``T | None``), bare or as
    a handle's values, is produced as ``BoundID[T]`` (or ``BoundID[T] | None``), and any other
    type as is. A handle is not an entity, so, unlike for steps, one set of overloads covers
    both. The ``T`` overload overlaps the ``T | None`` one; the first match wins, which is the
    intended one.
    """

    @overload
    def __call__[**P, E: Entity](  # pyright: ignore[reportOverlappingOverload]
        self, fn: WorkflowBody[P, E]
    ) -> Workflow[P, E, BoundID[E]]: ...
    @overload
    def __call__[**P, E: Entity](
        self, fn: WorkflowBody[P, E | None]
    ) -> Workflow[P, E | None, BoundID[E] | None]: ...
    @overload
    def __call__[**P, R](self, fn: WorkflowBody[P, R]) -> Workflow[P, R, R]: ...


def workflow(
    *,
    name: str | None = None,
    inputs: Mapping[str, ArrayTypeSpec] | None = None,
    output: ArrayTypeSpec | None = None,
    signature: Signature | None = None,
) -> _WorkflowDecorator:
    """Decorator turning an async function into a :class:`Workflow`.

    Takes the same arguments as :func:`fxtr.experiment.steps.step`: a signature, as ``inputs`` and
    ``output`` array types or an explicit ``signature`` object, and ``name``, defaulting to the
    function's fully-qualified name.

    Example::

        @workflow(
            inputs={"prompts": ("[prompt: int]", str), "n": int},
            output=("[prompt: int, sample: int]", str),
        )
        async def sample_all(
            context: WorkflowContext, prompts: ArrayHandle[str], n: ArrayHandle[int]
        ) -> ArrayHandle[str]:
            return context.run_step(
                "sample", sample, {"prompt": prompts, "n": n}, map_over=["prompt"]
            )

    The same signature can be written on the body instead (see
    :mod:`fxtr.experiment.annotations`): the handles' type parameters give the value schemas and
    a string in ``Annotated`` gives the dims, which no Python type implies::

        @workflow()
        async def sample_all(
            context: WorkflowContext,
            prompts: Annotated[ArrayHandle[str], "[prompt: int]"],
            n: Annotated[ArrayHandle[int], "[]"],
        ) -> Annotated[ArrayHandle[str], "[prompt: int, sample: int]"]:
            ...

    **Annotation-driven conversions.** Bodies receive handles by default, but their annotations can
    ask :meth:`Workflow.invoke` for more, the way a step's do (see
    :class:`WorkflowArgConversion`): ``Array[...]`` observes the input, and a scalar value type
    (``EntityID``, ``BoundID[T]``, an entity class ``T`` to load, a leaf, a TypedDict) observes a
    scalar handle and hands over its value. A body may likewise ``return`` a bare value (or an
    entity for an ``id`` output) instead of a handle; it becomes the workflow's scalar result.

    Raises:
        TypeError: If the signature arguments are given in more than one form, or no form is given
            and the body's annotations do not declare a complete signature.
        SignatureError: If the body's annotations disagree with ``inputs`` / ``output``.
    """

    def decorate(fn: WorkflowBody[..., Any]) -> Workflow[..., Any, Any]:
        wf = Workflow(
            name=default_name(fn) if name is None else name,
            signature=resolve_signature(inputs=inputs, output=output, signature=signature, body=fn),
            body=fn,
            conversions=workflow_arg_conversions(fn),
        )
        GLOBAL_REGISTRY.add(wf)
        return wf

    return cast(_WorkflowDecorator, decorate)
