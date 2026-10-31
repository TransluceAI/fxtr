"""Nodes: the content-addressed form of a workflow DAG node, and its relational shape.

A node of a workflow invocation's DAG is identified by *what it is*, not by when it was emitted.
It is an entity (one :class:`~fxtr.entity_defns.dataclass_entity.DataclassEntity` per kind,
below) whose contents determine it completely: the operation's parameters, the
*identities* of its input nodes, and for the leaves a reference to the array they hold. Its
**identity** (its :meth:`~fxtr.core.entities.Entity.content_id`) is the entity cid of that
entity. Two emissions of the same
operation on the same inputs therefore coincide, whatever order a body emitted them in, and a
node's identity changes exactly when something that defines it changes. The identity is computed
with the ordinary entity codec, but not generally stored in the normal entity store; rather they
are stored in a separate table for better access from SQL.

Where array types come from. A node's contents determine its
:class:`~fxtr.entity_defns.array.ArrayType`: a leaf (:class:`Input`, :class:`SourceArray`)
references an array entity that carries its own type; a derived operation
(:class:`Combine`, :class:`Stack`, :class:`Query`)
has the type the typing functions give it from its parameters and its inputs' types; and a run
(:class:`RunStep`, :class:`RunChildWorkflow`) embeds the result type the worker computed from the
step or workflow's user-declared signature, the one place declared types enter the system, so a
changed declaration changes the identity. No other kind carries a type field. :func:`node_type`
derives a node's type by that rule; the host records it per node as a derived attribute rather
than receiving it from the worker, so the node's contents stay the only source of truth.

The host builds these from the worker's token-based descriptions: a run's assembler
(:class:`~fxtr.runner.assembly.NodeAssembler`) resolves each ``emit_<kind>`` call's input tokens
to identities, builds the node, and computes its identity, so emitting one node twice is
idempotent by construction. A source-array node refers to its array by the content id the array
has as a stored entity (:func:`source_array`). :class:`Input` is created by the host, for an
invocation's arguments.

**Relational shape.** The runner's database keeps nodes relationally rather than as entity bytes,
so they can be indexed and walked in SQL: :func:`to_row` splits a node into its ``kind`` (see
:data:`KINDS`), its ``params`` (everything that is neither an input reference nor the entity
tag, as JSON), and its ``inputs`` (the edges, each a label and an input identity), and
:func:`from_row` rebuilds the node, whose ``content_id()`` recomputes the same identity.
The encoding is the entity's own wire form with three explicit transforms: input identities move
to the edges, array references become cid strings, and a run node's type goes through the JSON
codec of :mod:`fxtr.runner.postgres.json_values`. Edge labels are argument names for combine and
run nodes, key values (``int`` or ``str``) for stack, and positions for a query. A query node's
encoded plan is base64 in its params, the one field JSON cannot carry directly.
"""

from __future__ import annotations

import base64
import dataclasses
import itertools
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID, cid_from_str, cid_to_str
from fxtr.entity_defns.array import (
    Array,
    ArrayType,
    CombineJoin,
    Dims,
    Join,
    Key,
    KeyValue,
    Row,
    combine_type,
    join_keys,
    stack_dim,
    stack_type,
)
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import ConversionError
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.query_plan import query_type
from fxtr.experiment.signatures import mapped_dims
from fxtr.runner.postgres.json_values import Json, array_type_from_json, array_type_to_json
from fxtr.runner.query_codec import decode_query

type NodeIdentity = EntityID
"""A node's identity: the cid it would have as a stored entity (its
:meth:`~fxtr.core.entities.Entity.content_id`)."""

type EdgeLabel = str | KeyValue
"""How a node knows one of its inputs: an argument name, a stack key, or a query position."""


class NodeRowError(ValueError):
    """A relational node row is malformed."""


# ---- the kinds ----


@dataclasses.dataclass(frozen=True, kw_only=True)
class Input(DataclassEntity, fxtr_type="org.transluce.fxtr.runner.InputNode"):
    """An invocation's argument: the array it was called with, by name. Created by the system
    when the invocation is created.

    Attributes:
        name: The argument name.
        array: The array.
    """

    name: str
    array: BoundID[Array[Any]]


@dataclasses.dataclass(frozen=True, kw_only=True)
class SourceArray(DataclassEntity, fxtr_type="org.transluce.fxtr.runner.SourceArrayNode"):
    """``add_source_array``: an array the body supplied, stored and referenced by id.

    Attributes:
        name: The node's name, unique among the invocation's named nodes.
        array: The array.
    """

    name: str
    array: BoundID[Array[Any]]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Combine(DataclassEntity, fxtr_type="org.transluce.fxtr.runner.CombineNode"):
    """``combine``: zip inputs into one struct-valued array.

    Attributes:
        inputs: The input nodes, by the field name each becomes.
        join: How the inputs' key sets are reconciled.
    """

    inputs: frozendict[str, NodeIdentity]
    join: CombineJoin


@dataclasses.dataclass(frozen=True, kw_only=True)
class Stack(DataclassEntity, fxtr_type="org.transluce.fxtr.runner.StackNode"):
    """``stack``: stack same-typed inputs along a new leading dim.

    Attributes:
        inputs: The input nodes with their keys along the new dim, sorted by key.
        dim: The new dim's name.
    """

    inputs: tuple[tuple[KeyValue, NodeIdentity], ...]
    dim: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Query(DataclassEntity, fxtr_type="org.transluce.fxtr.runner.QueryNode"):
    """An array query over the input nodes: every transform a workflow makes of its handles
    (``field``, ``sel``, ``filter``, ``fxtr.sql``, ...).

    What a workflow collects is one node: ``rows.filter(...).aggregate(...).collect()`` is a
    single plan over ``rows``. Its type is derived from the plan and its inputs' types like any
    other derived operation, and never declared.

    Attributes:
        inputs: The input nodes, in the order the plan's ``QuerySource`` positions index.
        plan: The plan, in the canonical encoding of
            :func:`~fxtr.runner.query_codec.encode_query`, which is what gives this node its
            identity: two equal plans over equal inputs are one node.
    """

    inputs: tuple[NodeIdentity, ...]
    plan: bytes


@dataclasses.dataclass(frozen=True, kw_only=True)
class RunStep(DataclassEntity, fxtr_type="org.transluce.fxtr.runner.RunStepNode"):
    """``run_step``: run a step over inputs, mapped over ``map_over`` with ``replicas``.

    Attributes:
        name: The node's name, unique among the invocation's named nodes.
        step_name: The step to run.
        inputs: The input nodes, by the step's argument names.
        map_over: The dim names to map over, in result order.
        join: How the inputs' key sets are reconciled along the mapped dims.
        replicas: The replica dims, each with its values, in result order after the mapped dims.
        cache_key_prefix: The fully resolved cache key prefix.
        type: The result type the worker computed from the step's declared signature.
    """

    name: str
    step_name: str
    inputs: frozendict[str, NodeIdentity]
    map_over: tuple[str, ...]
    join: Join
    replicas: frozendict[str, tuple[KeyValue, ...]]
    cache_key_prefix: str
    type: ArrayType


@dataclasses.dataclass(frozen=True, kw_only=True)
class RunChildWorkflow(DataclassEntity, fxtr_type="org.transluce.fxtr.runner.RunChildWorkflowNode"):
    """``run_child_workflow``: run a workflow over inputs, mapped over ``map_over`` with
    ``replicas``. Attributes as for :class:`RunStep`, with the workflow's name in place of the
    step's."""

    name: str
    workflow_name: str
    inputs: frozendict[str, NodeIdentity]
    map_over: tuple[str, ...]
    join: Join
    replicas: frozendict[str, tuple[KeyValue, ...]]
    cache_key_prefix: str
    type: ArrayType


type Node = Input | SourceArray | Combine | Stack | Query | RunStep | RunChildWorkflow
"""A node of a workflow DAG: one of the kinds above."""

KINDS: Mapping[type[Node], str] = {
    Input: "input",
    SourceArray: "source_array",
    Combine: "combine",
    Stack: "stack",
    Query: "query",
    RunStep: "run_step",
    RunChildWorkflow: "run_child_workflow",
}
"""The ``kind`` string of each node class, as stored in the ``nodes`` table."""

_CLASS_OF_KIND: Mapping[str, type[Node]] = {kind: cls for cls, kind in KINDS.items()}


# ---- identity, edges, names ----


def source_array(name: str, array: Array[Any]) -> SourceArray:
    """The source-array node registering ``array`` under ``name``.

    The node refers to the array by the content id it has as a stored entity, computed locally, so
    the node — and hence its identity — exists before (or without) the array being stored. The
    host is what stores the array, when it is handed both together
    (:meth:`~fxtr.runner.session.WorkflowRunSession.emit_source_array`); a worker replaying a
    body builds the same node from the same array to check the identity without a host.
    """
    return SourceArray(name=name, array=BoundID(array.content_id(), array._fxtr_loader()))


def kind_of(node: Node) -> str:
    """The node's ``kind`` string (see :data:`KINDS`)."""
    return KINDS[type(node)]


def node_inputs(node: Node) -> tuple[tuple[EdgeLabel, NodeIdentity], ...]:
    """The node's edges: each input's identity with the label the node knows it by."""
    match node:
        case Input() | SourceArray():
            return ()
        case Combine(inputs=inputs) | RunStep(inputs=inputs) | RunChildWorkflow(inputs=inputs):
            return tuple(inputs.items())
        case Stack(inputs=inputs):
            return inputs
        case Query(inputs=inputs):
            # Labelled by position: what the plan's QuerySource(i) reads.
            return tuple(enumerate(inputs))


def node_name(node: Node) -> str | None:
    """The name the node claims in its invocation's names map (source arrays, steps, and child
    workflows share one namespace), or ``None`` for kinds that claim none."""
    match node:
        case SourceArray(name=name) | RunStep(name=name) | RunChildWorkflow(name=name):
            return name
        case _:
            return None


def node_type(
    node: Node,
    input_type: Callable[[NodeIdentity], ArrayType],
    array_type: Callable[[BoundID[Array[Any]]], ArrayType],
) -> ArrayType:
    """The node's array type, derived from its contents.

    Args:
        node: The node.
        input_type: The type of one of its input nodes, by identity.
        array_type: The type of the array a leaf references.

    Raises:
        ArrayTypeError: If a derived operation is ill-typed for its inputs.
    """
    match node:
        case Input(array=ref) | SourceArray(array=ref):
            return array_type(ref)
        case Combine(inputs=inputs, join=join):
            return combine_type({k: input_type(v) for k, v in inputs.items()}, join)
        case Stack(inputs=inputs, dim=dim):
            keys = [k for k, _ in inputs]
            return stack_type((input_type(v) for _, v in inputs), stack_dim(keys, dim))
        case Query(inputs=inputs, plan=plan):
            return query_type(decode_query(plan), [input_type(input) for input in inputs])
        case RunStep(type=type) | RunChildWorkflow(type=type):
            return type


def compute(node: Node, inputs: Mapping[EdgeLabel, Array[Any]]) -> Array[Any]:
    """The value of a derived node from its inputs' values (by the labels of
    :func:`node_inputs`), with the :class:`~fxtr.entity_defns.array.Array` operations.

    A query node is one of these: its plan runs over the inputs' arrays, in this process and on
    this thread. Leaves and run nodes are not: a leaf references its array, and a run's value is
    what the worker returned.

    Raises:
        ValueError: If ``node`` is not a derived operation.
        ArrayError: If the operation fails at run time (an ``exact`` join with mismatched keys,
            a query's ``sel`` leaving a scalar with no rows).
    """
    match node:
        case Combine(join=join):
            return Array.combine({str(k): v for k, v in inputs.items()}, join)
        case Stack(dim=dim):
            return Array.stack(dict(inputs), dim)
        case Query(inputs=identities, plan=plan):
            # Imported here so that a process that holds nodes but runs no query node — a
            # worker describing a DAG — does not load DuckDB and PyArrow to do it.
            from fxtr.duckdb_engine.local_query import query_arrays

            return query_arrays(
                decode_query(plan), [inputs[position] for position in range(len(identities))]
            )
        case _:
            raise ValueError(
                f"{kind_of(node)} nodes are not derived; a leaf references its value and a run's"
                " is what the worker returned"
            )


# ---- run nodes: expansion and collection ----


@dataclasses.dataclass(frozen=True)
class Spawn:
    """One invocation a run node expands into.

    Attributes:
        key: The joint key: the mapped values in ``map_over`` order, then the replica values in
            replica order; ``()`` when nothing is mapped or replicated over. Keys the results the
            invocation produces in :func:`collect`.
        mapped: The same values by dim name, for naming the invocation (see
            :func:`~fxtr.experiment.workflows.append_mapped_key`).
        inputs: The invocation's inputs by argument name: each input array sliced to this key
            along the mapped dims it has (an input lacking them is passed whole).
    """

    key: Key
    mapped: frozendict[str, KeyValue]
    inputs: frozendict[str, Array[Any]]


@dataclasses.dataclass(frozen=True)
class Expansion:
    """What a run node expands into: see :func:`expand`.

    Attributes:
        dims: The mapped dims, resolved against the inputs, in ``map_over`` order.
        spawns: One per joint key and replica key, in key order.
    """

    dims: Dims
    spawns: tuple[Spawn, ...]


def expand(node: RunStep | RunChildWorkflow, inputs: Mapping[str, Array[Any]]) -> Expansion:
    """Expand a run node whose inputs have values into the invocations it requests.

    The mapped dims are resolved against the input types, the joint keys computed with the node's
    join mode, and every input sliced along the mapped dims it has: each input's rows are grouped
    by their mapped values in one pass, and each joint key looks its slice up. An input with none
    of the mapped dims is passed whole, and a joint key that an ``outer`` join gives an input no
    rows at gets an empty slice. Each joint key is then repeated once per combination of replica
    values, with the same sliced inputs; the replica values only extend the key. With nothing
    mapped or replicated over there is one spawn, keyed ``()``.

    Args:
        node: The run node.
        inputs: Its input arrays, by the argument names of ``node.inputs``.

    Raises:
        SignatureError: If ``map_over`` does not resolve against the inputs.
    """
    dims = mapped_dims({name: array.type for name, array in inputs.items()}, node.map_over)
    replica_keys = list(itertools.product(*node.replicas.values()))
    mapped = {
        name: [d.name for d in dims if d.name in array.dim_names] for name, array in inputs.items()
    }
    slices = {name: _slices(inputs[name], names) for name, names in mapped.items() if names}
    spawns: list[Spawn] = []
    for key in join_keys(inputs, dims, node.join):
        values = dict(zip((d.name for d in dims), key, strict=True))
        sliced: dict[str, Array[Any]] = {}
        for name, array in inputs.items():
            if not mapped[name]:
                sliced[name] = array
                continue
            part = slices[name].get(tuple(values[n] for n in mapped[name]))
            if part is None:  # an outer join's key this input has no rows at
                type = array.type.without(mapped[name])
                part = Array(dims=type.dims, schema=type.schema, raw_rows=())
            sliced[name] = part
        for replica_key in replica_keys:
            spawns.append(
                Spawn(
                    key=(*key, *replica_key),
                    mapped=frozendict(
                        {
                            **dict(zip(node.map_over, key, strict=True)),
                            **dict(zip(node.replicas, replica_key, strict=True)),
                        }
                    ),
                    inputs=frozendict(sliced),
                )
            )
    return Expansion(dims=dims, spawns=tuple(spawns))


def _slices(array: Array[Any], names: Sequence[str]) -> dict[Key, Array[Any]]:
    """``array.sel`` at every combination of values along the named dims that has rows, keyed by
    those values in the order of ``names``, in one pass over the rows: slicing once per joint key
    would read every row once per key."""
    selected = [array.dim_names.index(name) for name in names]
    kept = [i for i in range(len(array.dims)) if i not in selected]
    groups: dict[Key, list[Row[Any]]] = {}
    for key, value in array.raw_items():
        part = tuple(key[i] for i in selected)
        groups.setdefault(part, []).append((tuple(key[i] for i in kept), value))
    type = array.type.without(names)
    return {
        part: Array(dims=type.dims, schema=type.schema, raw_rows=rows)
        for part, rows in groups.items()
    }


def collect(node: RunStep | RunChildWorkflow, values: Mapping[Key, Array[Any]]) -> Array[Any]:
    """A run node's value from the results of the invocations it expanded into.

    Each result is widened to the node's declared schema (a step may return a narrower, still
    assignable, schema), the results are stacked back along the mapped and replica dims, and the
    whole is transposed to the declared dim order.

    Args:
        node: The run node.
        values: Every spawn's result, by its :attr:`Spawn.key`.
    """
    widened = {
        key: value.retype(ArrayType(value.dims, node.type.schema)) for key, value in values.items()
    }
    n_stacked = len(node.map_over) + len(node.replicas)
    if n_stacked == 0:
        array = widened[()]
    elif widened:
        array = Array.stack_over(widened, node.type.dims[:n_stacked])
    else:
        array = Array(dims=node.type.dims, schema=node.type.schema, raw_rows=())
    return array.transpose(node.type.dim_names)


# ---- relational shape ----


@dataclasses.dataclass(frozen=True)
class NodeRow:
    """A node split for the ``nodes`` and ``node_inputs`` tables.

    Attributes:
        kind: See :data:`KINDS`.
        params: The node's parameters as JSON: its fields other than the input references,
            with array references as cid strings and a run node's ``type`` in the form of
            :func:`~fxtr.runner.postgres.json_values.array_type_to_json`.
        inputs: The edges, as :func:`node_inputs` gives them.
    """

    kind: str
    params: dict[str, Json]
    inputs: tuple[tuple[EdgeLabel, NodeIdentity], ...]


_INPUT_FIELDS = frozenset({"inputs"})
_ARRAY_REF_FIELDS = frozenset({"array", "snapshot"})
_PLAN_FIELD = "plan"
"""A query node's encoded plan: bytes, which JSON cannot carry, so it is base64 there."""


def to_row(node: Node) -> NodeRow:
    """Split a node into its relational shape (see :class:`NodeRow`)."""
    data = DEFAULT_CONVERTER.unstructure_entity(node)
    params: dict[str, Json] = {}
    for name, value in data.items():
        if name == "$type" or name in _INPUT_FIELDS:
            continue
        if name in _ARRAY_REF_FIELDS:
            assert isinstance(value, EntityID)
            params[name] = cid_to_str(value.cid)
        elif name == _PLAN_FIELD:
            assert isinstance(value, bytes)
            params[name] = base64.b64encode(value).decode("ascii")
        elif name == "type":
            assert isinstance(node, RunStep | RunChildWorkflow)
            params[name] = array_type_to_json(node.type)
        else:
            params[name] = _plain_json(value, name)
    return NodeRow(kind=kind_of(node), params=params, inputs=node_inputs(node))


def from_row(row: NodeRow) -> Node:
    """Rebuild a node from its relational shape.

    Raises:
        NodeRowError: If the row is malformed.
    """
    cls = _CLASS_OF_KIND.get(row.kind)
    if cls is None:
        raise NodeRowError(f"Unknown node kind {row.kind!r}")
    data: dict[str, Any] = {"$type": cls._fxtr_require_type_id()}
    try:
        for name, value in row.params.items():
            if name in _ARRAY_REF_FIELDS:
                if not isinstance(value, str):
                    raise NodeRowError(f"{row.kind} node: {name!r} must be a cid string")
                data[name] = EntityID(cid=cid_from_str(value))
            elif name == _PLAN_FIELD:
                if not isinstance(value, str):
                    raise NodeRowError(f"{row.kind} node: {name!r} must be a base64 string")
                data[name] = base64.b64decode(value, validate=True)
            elif name == "type":
                data[name] = array_type_from_json(value).to_wire()
            else:
                data[name] = value
        if cls is Stack:
            data["inputs"] = [[label, identity] for label, identity in row.inputs]
        elif cls in (Combine, RunStep, RunChildWorkflow):
            data["inputs"] = {label: identity for label, identity in row.inputs}
        elif cls is Query:
            positions = sorted(row.inputs, key=lambda edge: _input_position(row.kind, edge[0]))
            if [position for position, _ in positions] != list(range(len(positions))):
                raise NodeRowError(f"{row.kind} node: inputs are not labelled 0..n-1")
            data["inputs"] = [identity for _, identity in positions]
        elif row.inputs:
            raise NodeRowError(f"{row.kind} node: expected no inputs")
        return DEFAULT_CONVERTER.structure(data, cls)
    except (ConversionError, ValueError) as exc:
        raise NodeRowError(f"{row.kind} node: {exc}") from exc


def _input_position(kind: str, label: EdgeLabel) -> int:
    if not isinstance(label, int) or isinstance(label, bool):
        raise NodeRowError(f"{kind} node: input label {label!r} is not a position")
    return label


def _plain_json(value: Any, where: str) -> Json:
    """Convert an unstructured parameter to JSON: tuples become lists, and anything JSON cannot
    carry (ids, bytes) is an error, since every such field is handled explicitly."""
    match value:
        case None | bool() | int() | float() | str():
            return value
        case list() | tuple():
            return [_plain_json(v, where) for v in value]
        case dict():
            return {k: _plain_json(v, where) for k, v in value.items()}
        case _:
            raise NodeRowError(f"Parameter {where!r} holds a non-JSON value {value!r}")
