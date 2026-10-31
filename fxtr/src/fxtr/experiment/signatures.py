"""Static signatures of steps and workflows, and the typing rule for running them over dims.

Every step and workflow carries a :class:`Signature`: synchronous logic mapping the
:class:`~fxtr.entity_defns.array.ArrayType` of each named input to the array type of the output. A
signature is checked when a step or workflow is *scheduled*, before anything runs, so the handle for
the result has a known type as soon as it exists.

Scheduling is always a ``run`` with a ``map_over`` list of dim names (possibly empty), typed by
:func:`run_output_type`. Conceptually the inputs are joined along the mapped dims (inputs lacking
a mapped dim are broadcast along it), the step or workflow is applied once per joint key to the
inputs' slices, and the results are stacked back along the mapped dims. Statically, then: each
input's type minus the mapped dims must fit the signature's declared input type, and the output has
the mapped dims followed by the signature's declared output dims. This is the same shape as
xarray's ``apply_ufunc``, with the declared dims playing the role of core dims and ``map_over``
naming the broadcast dims explicitly.

A run may also declare *replicas*: new dims, each with an explicit list of values, along which the
step or workflow is simply run again with the same inputs. Replica dims appear in the output after
the mapped dims and before the declared output dims; they must not be dims of any input (to map
over data, put it in an array and map over it), and the body never sees the replica values, which
only distinguish the runs' cache addresses.

The common case of fixed types is :class:`FixedSignature`; anything more dynamic (a step generic
over its input schemas or dims, say) implements :class:`Signature` directly.
:func:`resolve_signature` is where a decorator's signature arguments — or, failing those, the
body's own annotations (see :mod:`fxtr.experiment.annotations`) — become one.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from frozendict import frozendict

from fxtr.entity_defns.array import (
    ArrayType,
    ArrayTypeError,
    ArrayTypeSpec,
    Dim,
    Dims,
    KeyValue,
    as_array_type,
    is_assignable_array,
    stack_dim,
    union_dims,
)
from fxtr.entity_defns.schema import is_assignable
from fxtr.experiment.annotations import AnnotatedType, declared_signature_types

type Replicas = Mapping[str, Sequence[KeyValue]]
"""Replica dims for a run: each name maps to the values along the new dim (all ``int`` or all
``str``, non-empty, unique)."""


class SignatureError(TypeError):
    """Inputs do not fit a signature, or a run's dims do not work out."""


@runtime_checkable
class Signature(Protocol):
    """The static, type-level behaviour of a step or workflow."""

    def output_type(self, inputs: Mapping[str, ArrayType]) -> ArrayType:
        """The output type for the given input types, one per named argument.

        Raises:
            SignatureError: If the inputs are not acceptable.
        """
        ...


@dataclasses.dataclass(frozen=True)
class FixedSignature:
    """A signature with fixed input and output types.

    Inputs must have exactly the declared argument names, each with a type assignable to the
    declared one (see :func:`~fxtr.entity_defns.array.is_assignable_array`: same dims in any
    order, assignable schema).

    Attributes:
        inputs: The declared arguments, by name.
        output: The declared output type.
    """

    inputs: frozendict[str, ArrayType]
    output: ArrayType

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", frozendict(self.inputs))

    @classmethod
    def of(cls, inputs: Mapping[str, ArrayTypeSpec], output: ArrayTypeSpec) -> FixedSignature:
        """Build a fixed signature from :data:`~fxtr.entity_defns.array.ArrayTypeSpec`\\ s, e.g.
        ``{"prompt": str, "n": int}`` and ``("[sample: int]", str)``."""
        return cls(
            inputs=frozendict({name: as_array_type(t) for name, t in inputs.items()}),
            output=as_array_type(output),
        )

    def output_type(self, inputs: Mapping[str, ArrayType]) -> ArrayType:
        if set(inputs) != set(self.inputs):
            raise SignatureError(f"Expected inputs {sorted(self.inputs)}, got {sorted(inputs)}")
        for name in sorted(self.inputs):
            provided, expected = inputs[name], self.inputs[name]
            if not is_assignable_array(provided, expected):
                raise SignatureError(f"Input {name!r}: expected {expected}, got {provided}")
        return self.output


def resolve_signature(
    *,
    inputs: Mapping[str, ArrayTypeSpec] | None = None,
    output: ArrayTypeSpec | None = None,
    signature: Signature | None = None,
    body: Any = None,
) -> Signature:
    """Normalize the signature-related decorator arguments of ``step`` / ``workflow``.

    A signature comes from exactly one of three places: ``inputs`` and ``output`` together (making
    a :class:`FixedSignature`), an explicit ``signature`` object, or — when neither is given — the
    ``body``'s annotations (see :mod:`fxtr.experiment.annotations`).

    Annotations and ``inputs``/``output`` may both be present, in which case ``inputs``/``output``
    is the declaration and the annotations are *checked* against it by function subtyping (dims
    exactly; an input's declared schema must be assignable to the annotated one, the result's
    annotated schema to the declared one), so they may be partial or looser. Deriving from
    annotations instead is all-or-nothing: every argument and the result must then carry a
    complete array type. An explicit ``signature`` may be dynamic, so annotations alongside it
    are documentation and are not checked.

    Raises:
        TypeError: If more than one form is given, only one of ``inputs`` / ``output`` is given,
            or no form is given and the body's annotations do not declare a complete signature.
        SignatureError: If the body's annotations disagree with ``inputs`` / ``output``.
    """
    fixed_given = inputs is not None or output is not None
    if signature is not None:
        if fixed_given:
            raise TypeError("Give either signature=..., or inputs=... and output=..., not both")
        return signature
    declared_args: Mapping[str, AnnotatedType | None] = {}
    declared_output: AnnotatedType | None = None
    if body is not None:
        declared_args, declared_output = declared_signature_types(body)
    where = "The signature" if body is None else f"The signature of {body.__qualname__}"
    if fixed_given:
        if inputs is None or output is None:
            raise TypeError("Give both inputs=... and output=..., or neither")
        fixed = FixedSignature.of(inputs, output)
        _check_declared(fixed, declared_args, declared_output, where)
        return fixed
    return _derive_signature(declared_args, declared_output, where)


def input_view_type(
    view: AnnotatedType | None, signature: Signature, name: str, current: ArrayType
) -> ArrayType | None:
    """The type to retype an input to before a step or workflow body sees it, or ``None`` to leave
    it unchanged.

    This is "check, then adopt the declared type" at the point a body is called: an input arrives
    with the schema it was stored under (loaders and presentation lost at the wire), and is retyped
    to what the body declares so that its values present as annotated. The parameter's annotation
    wins — its schema, with the input's own dims when the annotation names none (a bare
    ``Array[X]``) — else a :class:`FixedSignature`'s declared input for ``name``, else nothing.
    """
    if view is not None and view.schema is not None:
        dims = current.dims if view.dims is None else view.dims
        return ArrayType(dims, view.schema)
    if isinstance(signature, FixedSignature) and name in signature.inputs:
        return signature.inputs[name]
    return None


def _check_declared(
    fixed: FixedSignature,
    declared_args: Mapping[str, AnnotatedType | None],
    declared_output: AnnotatedType | None,
    where: str,
) -> None:
    """Check a body's annotated types against the signature the decorator gives."""
    for name, declared in declared_args.items():
        if declared is None:
            continue
        if name not in fixed.inputs:
            raise SignatureError(
                f"{where}: parameter {name!r} declares the type {declared} but is not in inputs="
            )
        _check_one(declared, fixed.inputs[name], "input", f"{where}, parameter {name!r}")
    if declared_output is not None:
        _check_one(declared_output, fixed.output, "output", f"{where}, result")


def _check_one(
    declared: AnnotatedType, against: ArrayType, position: Literal["input", "output"], where: str
) -> None:
    """Function-subtyping check of an annotation against the declared type: dims exactly; for
    an input the declared schema must be assignable to the annotated one, for the result the
    annotated schema to the declared one."""
    if declared.dims is not None and declared.dims != against.dims:
        raise SignatureError(
            f"{where}: annotated dims [{', '.join(map(str, declared.dims))}] but the declared type"
            f" is {against}"
        )
    if declared.schema is None:
        return
    if position == "input":
        if not is_assignable(against.schema, declared.schema):
            raise SignatureError(
                f"{where}: the annotation {declared.schema} does not accept the declared input"
                f" schema {against.schema}"
            )
    elif not is_assignable(declared.schema, against.schema):
        raise SignatureError(
            f"{where}: the annotated result schema {declared.schema} is not assignable to the"
            f" declared {against.schema}"
        )


def _derive_signature(
    declared_args: Mapping[str, AnnotatedType | None],
    declared_output: AnnotatedType | None,
    where: str,
) -> FixedSignature:
    """Build a signature out of a body's annotations alone, which must all be complete."""
    missing = [name for name, declared in declared_args.items() if declared is None]
    if missing or declared_output is None:
        if declared_output is None:
            missing.append("the result")
        raise TypeError(
            f"{where} is not declared: give inputs=... and output=..., or signature=..., or"
            f" annotate {', '.join(missing)} (see fxtr.experiment.annotations)"
        )
    args = {
        name: declared.complete(f"{where}, parameter {name!r}")
        for name, declared in declared_args.items()
        if declared is not None
    }
    result = declared_output.complete(f"{where}, result")
    return FixedSignature(inputs=frozendict(args), output=result)


def mapped_dims(inputs: Mapping[str, ArrayType], map_over: Sequence[str]) -> Dims:
    """The dims named by ``map_over``, in that order, resolved against the inputs.

    Raises:
        SignatureError: If a name repeats, no input has a named dim, or inputs disagree on its
            kind.
    """
    if len(set(map_over)) != len(map_over):
        raise SignatureError(f"map_over names must be unique, got {list(map_over)}")
    try:
        available = union_dims(t.dims for t in inputs.values())
    except ValueError as exc:
        raise SignatureError(str(exc)) from exc
    by_name = {d.name: d for d in available}
    missing = [n for n in map_over if n not in by_name]
    if missing:
        raise SignatureError(f"No input has dims {missing}; cannot map over them")
    return tuple(by_name[n] for n in map_over)


def replica_dims(replicas: Replicas) -> Dims:
    """The dims a run's ``replicas`` add, in the mapping's order, each with the kind of its values.

    Raises:
        SignatureError: If a replica has no values, values not all of one key kind, or repeated
            values.
    """
    dims: list[Dim] = []
    for name, values in replicas.items():
        try:
            dims.append(stack_dim(values, name))
        except ArrayTypeError as exc:
            raise SignatureError(f"Replica {name!r}: {exc}") from exc
        if len(set(values)) != len(values):
            raise SignatureError(f"Replica {name!r}: values must be unique, got {list(values)}")
    return tuple(dims)


def run_output_type(
    signature: Signature,
    inputs: Mapping[str, ArrayType],
    map_over: Sequence[str] = (),
    replicas: Replicas | None = None,
) -> ArrayType:
    """The output type of running a step or workflow over ``inputs``, mapped over ``map_over``,
    with ``replicas``.

    Each input's type with the mapped dims removed is what the signature sees; the result is the
    mapped dims (in ``map_over`` order), then the replica dims (in ``replicas`` order), then the
    signature's output dims.

    Raises:
        SignatureError: If the mapped dims cannot be resolved (see :func:`mapped_dims`), the
            replicas are malformed (see :func:`replica_dims`), the inputs' remaining types do not
            fit the signature, a replica dim is a dim of some input or a mapped dim, or the
            signature's output has a dim with the same name as a mapped or replica one.
    """
    dims = mapped_dims(inputs, map_over)
    reps = replica_dims(replicas or {})
    available = {d.name for t in inputs.values() for d in t.dims}
    taken = sorted({d.name for d in reps} & (available | set(map_over)))
    if taken:
        raise SignatureError(
            f"Replica dims {taken} are dims of the inputs or mapped dims; a replica must be a new"
            " dim (to map over data, put it in an array and map over it)"
        )
    core = {name: t.without(map_over) for name, t in inputs.items()}
    out = signature.output_type(core)
    collisions = sorted(set(out.dim_names) & ({*map_over} | {d.name for d in reps}))
    if collisions:
        raise SignatureError(f"Output dims {collisions} collide with mapped or replica dims")
    return ArrayType(dims + reps + out.dims, out.schema)
