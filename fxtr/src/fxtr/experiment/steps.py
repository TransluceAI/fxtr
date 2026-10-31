"""Experiment step definitions.

A *step* is a unit of computation scheduled by a workflow: an async function taking a
:class:`StepContext` plus named array arguments and returning an array, wrapped by the :func:`step`
decorator into a :class:`Step` object that also carries a name and a
:class:`~fxtr.experiment.signatures.Signature`. The signature is what lets a workflow know the type
of a step's result before the step runs.

Steps receive and return :class:`~fxtr.entity_defns.array.Array` objects, except that scalars
(arrays with no dims) are unwrapped: an argument whose type has no dims arrives as its bare value,
and a body whose output type has no dims may return the bare value, which is wrapped into a scalar
array. On top of that, the body's *type annotations* steer how scalar entity references are passed
(as an id, a bound id, or the loaded entity) and whether a scalar is unwrapped at all; see
:func:`step` for the rules and what they desugar to. All of this is done by :meth:`Step.invoke`,
the entry point the runner uses; calling a step object directly passes arguments through
untouched.
"""

from __future__ import annotations

import abc
import dataclasses
import typing
from collections.abc import Awaitable, Mapping
from typing import Any, Literal, Protocol, cast, overload

from frozendict import frozendict

from fxtr.core.entities import BoundID, Entity, FxtrEntityStorage
from fxtr.entity_defns.array import Array, ArrayError, ArrayTypeSpec, is_assignable_array
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
    Signature,
    SignatureError,
    input_view_type,
    resolve_signature,
)


class StepContext(FxtrEntityStorage):
    """Context for a step.

    StepContext extends FxtrEntityStorage and thus supports storing and loading entities.

    Beyond entity storage, a step context exposes the step's :attr:`cache_address` and :meth:`log`.
    This is where further step-specific context can be attached, e.g. retrieving secrets, or
    getting access to runtime environment state outside of a sandbox.
    """

    @property
    @abc.abstractmethod
    def cache_address(self) -> str:
        """The cache address for the step: where its result is stored."""

    @abc.abstractmethod
    def log(self, message: str) -> None:
        """Record a free-form log line for this attempt.

        Logs are stored per attempt and never deduplicated across a replay: a later attempt of the
        same work re-runs the body and writes its own lines. Fire-and-forget: the call does not
        wait for the line to be persisted.
        """


class StepBody[**P, R](Protocol):
    """The user-written function behind a :class:`Step`: an ``async def`` taking the step context
    first, then the step's named arguments, and returning the result.

    Arguments with dims are arrays; arguments without dims are bare values. The result is an array,
    or a bare value when the output type has no dims (see :meth:`Step.invoke`). ``R`` is the
    Python type of the result's values either way, as the body returns them (so an entity class
    for a body that returns entities; see :class:`Step` for the type of the values it produces).

    A body is an ordinary function, so it also carries ``__module__`` and ``__qualname__`` (used
    to derive a default step name).
    """

    __module__: str
    __qualname__: str

    def __call__(
        self, context: StepContext, *args: P.args, **kwargs: P.kwargs
    ) -> Awaitable[Array[R] | R]: ...


class _ArrayStepBody[**P, R](Protocol):
    """A :class:`StepBody` that returns an array, matched first by the :func:`step` decorator's
    overloads: :class:`~fxtr.entity_defns.array.Array` is itself an entity, so a body returning an
    ``Array[X]`` would otherwise also match a :class:`StepBody` whose ``R`` is the entity
    ``Array[X]``."""

    __module__: str
    __qualname__: str

    def __call__(
        self, context: StepContext, *args: P.args, **kwargs: P.kwargs
    ) -> Awaitable[Array[R]]: ...


type ArgKind = Literal["array", "load", "plain"]
"""How :meth:`Step.invoke` hands one argument to the body (see :class:`ArgConversion`)."""


@dataclasses.dataclass(frozen=True)
class ArgConversion:
    """How :meth:`Step.invoke` passes one argument, derived from the parameter's annotation.

    Every input is first *retyped* to the type its annotation declares (:attr:`view`, or the step's
    declared input when the parameter is unannotated), which is what gives a scalar id its presented
    :class:`~fxtr.core.entities.BoundID`, a struct its TypedDict ``dict``, and so on (see
    :func:`~fxtr.entity_defns.schema.present_value`). :attr:`kind` then only says whether the body
    wants the array whole, a loaded entity, or the plain presented value.

    Attributes:
        kind: ``"array"`` (annotated ``Array[...]``: always the array, even a dim-less one);
            ``"load"`` (annotated a bare entity class ``T``: a scalar id is loaded through the
            context as ``target``); or ``"plain"`` (anything else: a dim-less input arrives as its
            presented value — a bound id for ``BoundID[T]``, a bare id for ``EntityID``, a ``dict``
            for a TypedDict, ... — and an input with dims arrives as the array).
        target: The entity class to load a scalar id as, for ``"load"``; ``None`` otherwise.
        view: The array type the parameter's annotation declares (its dims may be absent), used to
            retype the input before it is passed; ``None`` when the parameter is unannotated or
            annotated ``Any``.
    """

    kind: ArgKind
    target: Any = None
    view: AnnotatedType | None = None


PLAIN_ARG = ArgConversion("plain")
"""The conversion of an unannotated parameter: retyped to the declared input, scalars unwrapped."""


def arg_conversions(fn: Any) -> frozendict[str, ArgConversion]:
    """Derive an :class:`ArgConversion` per parameter of a step body from its annotations.

    The first parameter (the context) and the return annotation are skipped. Each parameter's kind
    comes from its (unwrapped) annotation and its :attr:`~ArgConversion.view` from
    :func:`~fxtr.experiment.annotations.declared_signature_types`. Annotations are resolved when the
    step is defined, so the types a step names must exist by then: define them before the step, or
    apply the decorator later.

    Raises:
        TypeError: If an annotation cannot be resolved or is not a valid array/value type (e.g. a
            bare ``BoundID`` with no entity class), or names an ``ArrayHandle``, which only a
            workflow holds.
    """
    names, hints = body_hints(fn)
    views, _ = declared_signature_types(fn)
    return frozendict(
        {name: _classify(hints[name], views[name]) for name in names if name in hints}
    )


def _classify(annotation: Any, view: AnnotatedType | None) -> ArgConversion:
    ann = unwrap_annotation(annotation)
    origin = typing.get_origin(ann) or ann
    if origin is Array:
        return ArgConversion("array", view=view)
    if isinstance(origin, type) and issubclass(origin, ArrayHandle):
        # A step runs on the arrays themselves; its queries are local ones over them.
        raise TypeError(
            f"A step parameter cannot be annotated {annotation!r}: a step receives concrete"
            " arrays, so annotate it Array[...] instead"
        )
    if isinstance(ann, type) and issubclass(ann, Entity):
        return ArgConversion("load", target=ann, view=view)
    return ArgConversion("plain", view=view)


def _core_schema(schema: Schema) -> Schema:
    return schema.inner if isinstance(schema, NullableSchema) else schema


@dataclasses.dataclass(frozen=True)
class Step[**P, R, V]:
    """A step: a named, signed body. Produced by :func:`step`; not constructed directly.

    Only ``Step`` objects can be scheduled by a workflow, so an undecorated function cannot be
    passed where a step is expected.

    ``P`` is the body's parameters and ``R`` the Python type of the values it returns (what
    calling the step directly gives). ``V`` is the Python type of the values of the array the step
    produces (what :meth:`invoke` gives, and so the ``ArrayHandle[V]`` scheduling the step
    returns). They differ only for a body that returns entities: an entity is stored and its id
    becomes the value, so ``R = T`` gives ``V = BoundID[T]`` (and ``R = T | None`` gives
    ``V = BoundID[T] | None``). The :func:`step` decorator's overloads work out ``V`` from ``R``.

    Attributes:
        name: The step's name, defaulting to the body's fully-qualified name (``module.qualname``).
        signature: Maps the types of the step's arguments to the type of its result.
        body: The wrapped function.
        conversions: How :meth:`invoke` passes each argument, by parameter name (see
            :class:`ArgConversion`); parameters not listed get :data:`PLAIN_ARG`. :func:`step`
            derives these from the body's annotations.
    """

    name: str
    signature: Signature
    body: StepBody[P, R]
    conversions: frozendict[str, ArgConversion] = frozendict()

    async def __call__(
        self, context: StepContext, *args: P.args, **kwargs: P.kwargs
    ) -> Array[R] | R:
        """Call the body directly, passing the arguments through untouched."""
        return await self.body(context, *args, **kwargs)

    async def invoke(self, context: StepContext, inputs: Mapping[str, Array[Any]]) -> Array[V]:
        """Run the step on concrete input arrays, the way the runner does.

        The inputs are checked against the signature, which also yields the output type. Each input
        is then retyped to the type its annotation declares (see :func:`present_value
        <fxtr.entity_defns.schema.present_value>` and :class:`ArgConversion`) and passed according
        to its conversion: an input with dims is passed as an array (an ``"array"`` conversion also
        passes a dim-less array whole); otherwise the single value is unwrapped — so a scalar id
        annotated ``BoundID[T]`` arrives as a bound id, a struct annotated by a TypedDict as a
        ``dict`` — and a bare entity class annotation loads the entity through ``context``. If the
        output type has no dims, the body may return a scalar array, the bare value, or, for an
        ``id`` output, an :class:`~fxtr.core.entities.Entity` (stored through ``context``, its id
        becoming the value) or a bound id (its id becoming the value). The result's type must be
        assignable to the output type, and it is returned retyped to it, so the step fixes its own
        output to the type it declared (the scheduling node separately matches the possibly-stacked
        type it precomputed).

        Args:
            context: The step context.
            inputs: The step's arguments, by name, as arrays.

        Raises:
            SignatureError: If the inputs do not fit the signature, a parameter annotated to load a
                scalar entity receives an input with dims, or the result does not fit the output
                type.
            ArrayError: If a bare result does not conform to the output schema.
        """
        output = self.signature.output_type({name: t.type for name, t in inputs.items()})
        args = {name: await self._convert_arg(context, name, t) for name, t in inputs.items()}
        result = await self.body(context, **args)  # type: ignore[arg-type]
        if not isinstance(result, Array):
            if output.dims:
                raise SignatureError(
                    f"Step {self.name!r} returned a bare value but its output type {output} has"
                    " dims; return an Array"
                )
            if isinstance(_core_schema(output.schema), IdSchema):
                if isinstance(result, Entity):
                    result = (await context.store(result)).entity_id
                elif isinstance(result, BoundID):
                    result = result.entity_id
            try:
                result = Array.scalar(cast("Any", result), output.schema)
            except ArrayError as exc:
                raise ArrayError(f"Step {self.name!r} result: {exc}") from exc
        if not is_assignable_array(result.type, output):
            raise SignatureError(
                f"Step {self.name!r} returned an array of type {result.type}, expected {output}"
            )
        return result.retype(output)

    async def _convert_arg(self, context: StepContext, name: str, array: Array[Any]) -> Any:
        conv = self.conversions.get(name, PLAIN_ARG)
        target = input_view_type(conv.view, self.signature, name, array.type)
        if conv.kind != "array" and array.dims and target is not None and not target.dims:
            raise SignatureError(
                f"Step {self.name!r} parameter {name!r} is annotated for a dim-less value but"
                f" received an array of type {array.type}"
            )
        if target is not None:
            array = array.retype(target)
        if conv.kind == "array" or array.dims:
            return array
        value = array.item()
        if conv.kind == "load" and value is not None:
            return await context.load(value, as_type=conv.target)
        return value


class _StepDecorator(Protocol):
    """The decorator returned by :func:`step`.

    The overloads work out the :class:`Step`'s ``V`` from the body's return type ``R``: an entity
    class ``T``, bare or as an ``Array``'s values, is produced as ``BoundID[T]``, a bare
    ``T | None`` as ``BoundID[T] | None``, and any other type as is (an ``Array[T | None]`` is not
    a valid annotation; see :mod:`fxtr.experiment.annotations`). Array-returning bodies are matched
    first, since :class:`~fxtr.entity_defns.array.Array` is itself an entity. The entity overloads
    overlap later ones (a ``T`` body also fits ``T | None``, and an ``Array[T]`` body fits a bare
    entity return); the first match wins, which is the intended one.
    """

    @overload
    def __call__[**P, E: Entity](  # pyright: ignore[reportOverlappingOverload]
        self, fn: _ArrayStepBody[P, E]
    ) -> Step[P, E, BoundID[E]]: ...
    @overload
    def __call__[**P, R](self, fn: _ArrayStepBody[P, R]) -> Step[P, R, R]: ...
    @overload
    def __call__[**P, E: Entity](  # pyright: ignore[reportOverlappingOverload]
        self, fn: StepBody[P, E]
    ) -> Step[P, E, BoundID[E]]: ...
    @overload
    def __call__[**P, E: Entity](
        self, fn: StepBody[P, E | None]
    ) -> Step[P, E | None, BoundID[E] | None]: ...
    @overload
    def __call__[**P, R](self, fn: StepBody[P, R]) -> Step[P, R, R]: ...


def step(
    *,
    name: str | None = None,
    inputs: Mapping[str, ArrayTypeSpec] | None = None,
    output: ArrayTypeSpec | None = None,
    signature: Signature | None = None,
) -> _StepDecorator:
    """Decorator turning an async function into a :class:`Step`.

    A signature is required, in one of three forms: ``inputs`` (argument name to array type, e.g.
    ``{"prompt": str, "n": int}``; ``("[sample: int]", str)`` for an argument with dims; see
    :data:`~fxtr.entity_defns.array.ArrayTypeSpec`) together with ``output`` (an array type); an
    explicit ``signature`` object for anything more dynamic; or the body's own annotations (see
    below). ``name`` defaults to the function's fully-qualified name (``module.qualname``). The
    name is the whole of a step's code identity as far as the runner is concerned: a cached result
    is matched by the step's name and its inputs, so a step whose behaviour changed keeps
    matching until its cache entries are cleared.

    Example (scalar arguments arrive as bare values, see :meth:`Step.invoke`)::

        @step(inputs={"prompt": str, "n": int}, output=("[sample: int]", str))
        async def sample(context: StepContext, prompt: str, n: int) -> Array[str]:
            ...

    **Annotation-declared signatures.** The same signature can be written on the body, which keeps
    each argument's array type next to the argument (see :mod:`fxtr.experiment.annotations`).
    Value schemas are read off the Python types (leaves, ``BoundID[T]``, entity types, TypedDicts,
    ...); dims, which no Python type implies, are spelled out in a string::

        @step()
        async def sample(
            context: StepContext, prompt: str, n: int
        ) -> Annotated[Array[str], "[sample: int]"]:
            ...

    Declaring the signature this way is all-or-nothing: with no ``inputs``/``output``, every
    argument and the result must end up with a complete array type. When ``inputs``/``output`` *is*
    given it remains the declaration and the annotations are checked against it (dims exactly,
    schemas by assignability), so they may then be partial or looser.

    **Annotation-driven conversions.** The decorator reads the body's parameter annotations (at
    decoration time, so the types it names must already exist) and :meth:`Step.invoke` uses them
    as conveniences over the plain calling convention, in which every dim-less argument arrives as
    its value (an :class:`~fxtr.core.entity_data.EntityID` for an ``id``) and the body returns a
    value or array. Each is equivalent to a few lines the body could write itself:

    * ``cfg: ModelConfig`` for a scalar ``id`` argument: the entity is loaded before the call.
      Equivalent to ``cfg: EntityID`` plus ``cfg = await context.load(cfg, as_type=ModelConfig)``.
    * ``cfg: BoundID[ModelConfig]``: the id arrives bound. Equivalent to ``cfg: EntityID`` plus
      ``cfg = cfg.bind_type(ModelConfig)``.
    * ``cfg: EntityID`` (or no annotation, or ``Any``): the bare id, as in the plain convention.
    * ``n: Array[int]`` for a dim-less argument: the one-row array itself instead of ``n.item()``.
    * A body whose output is a scalar ``id`` may return an :class:`~fxtr.core.entities.Entity`
      (equivalent to ``return (await context.store(entity)).entity_id``) or a bound id
      (equivalent to ``return bound.entity_id``).

    A nullable id (``id?``) arriving as ``None`` is passed as ``None``; annotate ``T | None``.
    Arguments with dims are always arrays; an array of refs is an array of bound ids when the
    annotation names the entity type. Annotating a parameter for a dim-less value (anything but
    ``Array[...]``) and giving it an input with dims is an error at invocation.

    Raises:
        TypeError: If the signature arguments are given in more than one form, no form is given
            and the body's annotations do not declare a complete signature, or the body's
            annotations cannot be resolved (see :func:`arg_conversions`).
        SignatureError: If the body's annotations disagree with ``inputs`` / ``output``.
    """

    def decorate(fn: StepBody[..., Any]) -> Step[..., Any, Any]:
        step = Step(
            name=default_name(fn) if name is None else name,
            signature=resolve_signature(inputs=inputs, output=output, signature=signature, body=fn),
            body=fn,
            conversions=arg_conversions(fn),
        )
        GLOBAL_REGISTRY.add(step)
        return step

    return cast(_StepDecorator, decorate)
