"""The wire: what crosses the channel between a remote worker and its host, and how it is encoded.

**Frames.** Each frame is a frozen dataclass of this module (or one of the session's own message
dataclasses, framed as it is), tagged with its ``kind`` (the class name in snake case) and encoded
as one DAG-CBOR message by :func:`encode_frame`; :func:`decode_frame` reads one back strictly: an
unknown kind, a missing or extra field, a value of the wrong type (a bool where an int belongs, a
string where an int belongs) is a :class:`WireError`. The frames' shapes and rules are the wire's
own, defined by a dedicated `cattrs` converter in this module with nothing shared with the entity
layer's conversion; only the byte encoding (``cbrrr``'s DAG-CBOR) is the same library, used here
as a plain serialization format. From the worker: :class:`Hello` first; then *requests*, each
carrying a worker-chosen ``request_id`` that the host's :class:`Reply` or :class:`Refused`
answers, and *one-way* frames (the ``emit_<kind>`` family, :class:`LogMessage`, a pipelined
:class:`Store`) that are never answered. From the host: the replies, and the
:class:`~fxtr.runner.experimental.multiplexed.WorkerSession` message stream verbatim
(:class:`~fxtr.runner.experimental.multiplexed.Assigned`,
:class:`~fxtr.runner.experimental.multiplexed.AttemptCancelled`,
:class:`~fxtr.runner.experimental.multiplexed.Shutdown`).

**Values.** A stored value, a loaded value, or an array crosses as a :class:`WireValue`: its
canonical DAG-CBOR bytes, references as links. Arrays cross as their entity data value. In a
frame's own fields an :class:`~fxtr.core.entity_data.EntityID` is its cid's bytes. Which ids the
worker may send back is checked at each end (see :mod:`.known_ids`). A ``WireValue``'s bytes are
exactly what a store would hash, so :func:`cid_of` gives the cid a stored value has, and a
claimed cid is verifiable from the frame.

**Errors.** A host-side refusal crosses as :class:`Refused` carrying a :class:`Refusal` that names
one of the protocol's exception classes, which the worker re-raises as that class, or wraps an
exception of any other class as :class:`HostError` with its name and message.
:class:`ProtocolError` is what either end raises when the other broke the protocol (an id it
was neither given nor stored, an attempt it was never assigned).
"""

from __future__ import annotations

import dataclasses
import functools
import operator
import re
import types
import typing
from collections.abc import Callable
from typing import Any

import cattrs
import cbrrr
from cattrs.strategies import configure_tagged_union
from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID, FxtrDataValue, StorableFxtrDataValue
from fxtr.entity_defns.array import Array, ArrayType, CombineJoin, Join, KeyValue
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import ConversionError
from fxtr.runner.experimental.multiplexed import (
    AlreadyRegisteredError,
    Assigned,
    AttemptCancelled,
    FunctionSpec,
    HostDisconnectedError,
    Shutdown,
    StepSpec,
    WorkflowSpec,
)
from fxtr.runner.session import (
    AttemptClosedError,
    AttemptError,
    AttemptID,
    DeterminismError,
    NodeFailedError,
    NodeToken,
    NodeTokenError,
    RunnerError,
    StepAssignment,
    WorkflowAssignment,
)

PROTOCOL_VERSION = 4
"""The version :class:`Hello` announces; the host refuses one it does not speak."""


# ---- errors ----


class WireError(RunnerError):
    """A frame or value could not be encoded or decoded: malformed bytes, an unknown kind, or a
    field of the wrong shape."""


class ProtocolError(RunnerError):
    """The other end broke the protocol: it named an id it was neither given nor stored, an
    attempt it was never assigned, or claimed a cid that does not match what was stored."""


class HostError(RunnerError):
    """A host-side exception of a class the wire does not name, re-raised on the worker with the
    original's class name and message.

    Attributes:
        type_name: The qualified name of the original exception's class.
    """

    def __init__(self, type_name: str, message: str) -> None:
        super().__init__(message)
        self.type_name = type_name

    def __str__(self) -> str:
        return f"{self.type_name}: {self.args[0]}"


# ---- values ----


@dataclasses.dataclass(frozen=True)
class WireValue:
    """A value as it crosses the wire.

    Attributes:
        encoded: The value's canonical DAG-CBOR.
    """

    encoded: bytes


def encode_value(value: StorableFxtrDataValue) -> WireValue:
    """A value for the wire.

    Raises:
        WireError: If the value is not encodable.
    """
    try:
        return WireValue(codec.encode(value))
    except (TypeError, ValueError) as exc:
        raise WireError(f"Value cannot be encoded: {exc}") from exc


def decode_value(wire: WireValue) -> FxtrDataValue:
    """The value a :class:`WireValue` carries.

    Raises:
        WireError: If the bytes are not canonical DAG-CBOR.
    """
    try:
        return codec.decode(wire.encoded)
    except Exception as exc:  # cbrrr's decode error
        raise WireError(f"Malformed DAG-CBOR: {exc}") from exc


def decode_array(wire: WireValue) -> Array[Any]:
    """The array a :class:`WireValue` carries.

    Raises:
        WireError: If the value is not an array's data.
    """
    try:
        return DEFAULT_CONVERTER.structure(decode_value(wire), Array[Any])
    except ConversionError as exc:
        raise WireError(f"Value is not an array: {exc}") from exc


def cid_of(wire: WireValue) -> bytes:
    """The cid the carried value has as a stored entity: the hash of exactly these bytes."""
    return codec.compute_cid(wire.encoded)


# ---- frames ----


@dataclasses.dataclass(frozen=True)
class Hello:
    """The worker's first frame: the protocol version it speaks."""

    protocol_version: int


# -- requests (worker to host, answered by Reply or Refused) --


@dataclasses.dataclass(frozen=True)
class Register:
    request_id: int
    functions: tuple[FunctionSpec, ...]
    max_concurrency: int


@dataclasses.dataclass(frozen=True)
class Drain:
    request_id: int


@dataclasses.dataclass(frozen=True)
class Observe:
    request_id: int
    attempt_id: AttemptID
    node: NodeToken


@dataclasses.dataclass(frozen=True)
class CompleteWorkflow:
    request_id: int
    attempt_id: AttemptID
    result: NodeToken


@dataclasses.dataclass(frozen=True)
class CompleteStep:
    request_id: int
    attempt_id: AttemptID
    result: WireValue


@dataclasses.dataclass(frozen=True)
class Fail:
    request_id: int
    attempt_id: AttemptID
    error: AttemptError


@dataclasses.dataclass(frozen=True)
class Abandon:
    request_id: int
    attempt_id: AttemptID
    reason: str


@dataclasses.dataclass(frozen=True)
class StoreRequest:
    """A host-computed store: the host stores ``data`` and replies with the id."""

    request_id: int
    attempt_id: AttemptID
    data: WireValue


@dataclasses.dataclass(frozen=True)
class LoadRawData:
    request_id: int
    attempt_id: AttemptID
    entity_id: EntityID


# -- one-way (worker to host, never answered) --


@dataclasses.dataclass(frozen=True)
class EmitSourceArray:
    attempt_id: AttemptID
    token: NodeToken
    name: str
    array: WireValue


@dataclasses.dataclass(frozen=True)
class EmitCombine:
    attempt_id: AttemptID
    token: NodeToken
    inputs: frozendict[str, NodeToken]
    join: CombineJoin


@dataclasses.dataclass(frozen=True)
class EmitStack:
    """``inputs`` is a sequence of pairs, in the order given, since its keys may be ints and a
    DAG-CBOR map's may not."""

    attempt_id: AttemptID
    token: NodeToken
    inputs: tuple[tuple[KeyValue, NodeToken], ...]
    dim: str


@dataclasses.dataclass(frozen=True)
class EmitRunStep:
    attempt_id: AttemptID
    token: NodeToken
    name: str
    step_name: str
    inputs: frozendict[str, NodeToken]
    map_over: tuple[str, ...]
    join: Join
    replicas: frozendict[str, tuple[KeyValue, ...]]
    cache_key_prefix: str
    type: ArrayType


@dataclasses.dataclass(frozen=True)
class EmitRunChildWorkflow:
    attempt_id: AttemptID
    token: NodeToken
    name: str
    workflow_name: str
    inputs: frozendict[str, NodeToken]
    map_over: tuple[str, ...]
    join: Join
    replicas: frozendict[str, tuple[KeyValue, ...]]
    cache_key_prefix: str
    type: ArrayType


@dataclasses.dataclass(frozen=True)
class LogMessage:
    attempt_id: AttemptID
    message: str


@dataclasses.dataclass(frozen=True)
class Store:
    """A pipelined store: the worker has computed ``entity_id`` (the cid of ``data``'s bytes)
    and already holds it; the host stores ``data``, verifying the cid."""

    attempt_id: AttemptID
    entity_id: EntityID
    data: WireValue


# -- host to worker --


@dataclasses.dataclass(frozen=True)
class NoResult:
    """A request that returns nothing succeeded (the completions, the reports, ``register``,
    ``drain``)."""


@dataclasses.dataclass(frozen=True)
class ValueResult:
    """``observe``'s array or ``load_raw_data``'s data."""

    value: WireValue


@dataclasses.dataclass(frozen=True)
class IdResult:
    """A host-computed store's id."""

    id: EntityID


type ReplyResult = NoResult | ValueResult | IdResult
"""What a :class:`Reply` carries."""


@dataclasses.dataclass(frozen=True)
class Reply:
    """A request succeeded."""

    request_id: int
    result: ReplyResult


@dataclasses.dataclass(frozen=True)
class Refusal:
    """Why a request failed: the name of one of the protocol's exception classes (see
    :func:`refusal_of`) and its message, or ``HostError`` with the original class's name."""

    type: str
    message: str
    type_name: str | None = None

    def exception(self) -> RunnerError:
        """The exception the worker raises for this refusal.

        Raises:
            WireError: If the refusal names a class the wire does not know.
        """
        if self.type == "HostError":
            return HostError(self.type_name or "Exception", self.message)
        cls = _NAMED_ERRORS.get(self.type)
        if cls is None:
            raise WireError(f"Refusal names an unknown error class {self.type!r}")
        return cls(self.message)


@dataclasses.dataclass(frozen=True)
class Refused:
    """A request failed; the worker raises the refusal's exception from the awaiting call."""

    request_id: int
    error: Refusal


WORKER_FRAMES: tuple[type, ...] = (
    Hello,
    Register,
    Drain,
    Observe,
    CompleteWorkflow,
    CompleteStep,
    Fail,
    Abandon,
    StoreRequest,
    LoadRawData,
    EmitSourceArray,
    EmitCombine,
    EmitStack,
    EmitRunStep,
    EmitRunChildWorkflow,
    LogMessage,
    Store,
)
"""What the worker sends."""

HOST_FRAMES: tuple[type, ...] = (Reply, Refused, Assigned, AttemptCancelled, Shutdown)
"""What the host sends: the replies, and the ``WorkerSession`` message stream verbatim."""

type WorkerFrame = (
    Hello
    | Register
    | Drain
    | Observe
    | CompleteWorkflow
    | CompleteStep
    | Fail
    | Abandon
    | StoreRequest
    | LoadRawData
    | EmitSourceArray
    | EmitCombine
    | EmitStack
    | EmitRunStep
    | EmitRunChildWorkflow
    | LogMessage
    | Store
)
type HostFrame = Reply | Refused | Assigned | AttemptCancelled | Shutdown
type AnyFrame = WorkerFrame | HostFrame

type Request = (
    Register
    | Drain
    | Observe
    | CompleteWorkflow
    | CompleteStep
    | Fail
    | Abandon
    | StoreRequest
    | LoadRawData
)
"""The worker frames that carry a ``request_id`` and are answered."""


# ---- the frame codec ----
#
# A cattrs converter of the wire's own. Strict at the leaves (exact types, so a bool is not an
# int), forbidding extra fields, with hooks for the handful of types whose wire form is not their
# field structure: ids as their cid, array types as their wire form, bound ids as their id,
# frozendicts as maps. Frames, reply results, assignments, and function specs are tagged unions
# on a "kind" field. The bytes are cbrrr's DAG-CBOR, used as a plain serialization; nothing in a
# frame is a link, so the codec's reference handling never comes into play.

_LEAVES = (int, str, bytes, bool, float)


def _snake(cls: type) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()


def _strict_leaf(type_: type) -> Callable[[Any, Any], Any]:
    def hook(value: Any, _: Any) -> Any:
        if type(value) is not type_:
            raise ValueError(f"expected {type_.__name__}, got {type(value).__name__}")
        return value

    return hook


def _is_leaf_union(type_: Any) -> bool:
    if typing.get_origin(type_) not in (types.UnionType, typing.Union):
        return False
    members = typing.get_args(type_)
    return bool(members) and all(m in _LEAVES for m in members)


def _leaf_union_hook(type_: Any) -> Callable[[Any, Any], Any]:
    members = typing.get_args(type_)

    def hook(value: Any, _: Any) -> Any:
        if type(value) in members:
            return value
        raise ValueError(
            f"expected one of {[m.__name__ for m in members]}, got {type(value).__name__}"
        )

    return hook


def _id_to_wire(entity_id: EntityID) -> bytes:
    return entity_id.cid


def _id_from_wire(value: Any, _: Any) -> EntityID:
    if isinstance(value, bytes):
        return EntityID(cid=value)
    raise ValueError(f"expected an id as its cid's bytes, got {value!r}")


def _type_from_wire(value: Any, _: Any) -> ArrayType:
    try:
        return ArrayType.from_wire(value)
    except Exception as exc:
        raise ValueError(f"array type: {exc}") from exc


def _is_bound_id(type_: Any) -> bool:
    return type_ is BoundID or typing.get_origin(type_) is BoundID


def _is_frozendict(type_: Any) -> bool:
    return typing.get_origin(type_) is frozendict


def _make_converter() -> cattrs.Converter:
    conv = cattrs.Converter(forbid_extra_keys=True, unstruct_collection_overrides={tuple: list})
    for leaf in _LEAVES:
        conv.register_structure_hook(leaf, _strict_leaf(leaf))
    conv.register_structure_hook_factory(_is_leaf_union, _leaf_union_hook)
    conv.register_unstructure_hook(EntityID, _id_to_wire)
    conv.register_structure_hook(EntityID, _id_from_wire)
    conv.register_unstructure_hook(ArrayType, ArrayType.to_wire)
    conv.register_structure_hook(ArrayType, _type_from_wire)
    conv.register_unstructure_hook_func(_is_bound_id, lambda ref: _id_to_wire(ref.entity_id))
    conv.register_structure_hook_func(
        _is_bound_id, lambda value, _: _id_from_wire(value, None).bind_type(Array)
    )

    def frozendict_unstructure(type_: Any) -> Callable[[Any], Any]:
        key, value = typing.get_args(type_)
        inner = conv.get_unstructure_hook(dict[key, value])
        return lambda d: inner(dict(d))

    def frozendict_structure(type_: Any) -> Callable[[Any, Any], Any]:
        key, value = typing.get_args(type_)
        inner = conv.get_structure_hook(dict[key, value])
        return lambda d, _: frozendict(inner(d, dict[key, value]))

    conv.register_unstructure_hook_factory(_is_frozendict, frozendict_unstructure)
    conv.register_structure_hook_factory(_is_frozendict, frozendict_structure)
    for union in (
        StepSpec | WorkflowSpec,
        WorkflowAssignment | StepAssignment,
        NoResult | ValueResult | IdResult,
        _FRAME_UNION,
    ):
        configure_tagged_union(union, conv, tag_name="kind", tag_generator=_snake)
    return conv


_FRAME_UNION: Any = functools.reduce(operator.or_, WORKER_FRAMES + HOST_FRAMES)
_CONVERTER = _make_converter()


def encode_frame(frame: AnyFrame) -> bytes:
    """A frame's bytes.

    Raises:
        WireError: If the frame holds something that cannot be encoded.
    """
    try:
        return cbrrr.encode_dag_cbor(_CONVERTER.unstructure(frame, _FRAME_UNION))
    except WireError:
        raise
    except Exception as exc:
        raise WireError(f"Frame cannot be encoded: {exc}") from exc


def decode_frame(data: bytes) -> AnyFrame:
    """The frame some bytes carry, strictly.

    Raises:
        WireError: If the bytes are not a frame of a known kind with exactly its fields.
    """
    try:
        decoded = cbrrr.decode_dag_cbor(data)
    except Exception as exc:
        raise WireError(f"Malformed frame: {exc}") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("kind"), str):
        raise WireError("Malformed frame: expected a map with a 'kind'")
    try:
        return _CONVERTER.structure(decoded, _FRAME_UNION)
    except cattrs.BaseValidationError as exc:
        raise WireError(f"Malformed frame: {'; '.join(cattrs.transform_error(exc))}") from exc
    except KeyError as exc:
        raise WireError(f"Unknown frame kind {decoded['kind']!r}") from exc
    except Exception as exc:
        raise WireError(f"Malformed frame: {exc}") from exc


def request_id_of(frame: AnyFrame) -> int | None:
    """The request id a worker frame carries, or ``None`` for a one-way frame or a host frame."""
    return getattr(frame, "request_id", None)


# ---- the error envelope ----

_NAMED_ERRORS: dict[str, type[RunnerError]] = {
    cls.__name__: cls
    for cls in (
        AttemptClosedError,
        NodeFailedError,
        NodeTokenError,
        DeterminismError,
        AlreadyRegisteredError,
        HostDisconnectedError,
        ProtocolError,
        WireError,
    )
}


def refusal_of(exc: BaseException) -> Refusal:
    """How an exception crosses in a :class:`Refused`: by class name if it is one of the
    protocol's, else as ``HostError`` with the original's name."""
    if isinstance(exc, HostError):
        return Refusal("HostError", exc.args[0], exc.type_name)
    name = type(exc).__name__
    if name in _NAMED_ERRORS and isinstance(exc, _NAMED_ERRORS[name]):
        return Refusal(name, str(exc))
    return Refusal("HostError", str(exc), type(exc).__qualname__)


__all__ = [
    "HOST_FRAMES",
    "PROTOCOL_VERSION",
    "WORKER_FRAMES",
    "Abandon",
    "AnyFrame",
    "CompleteStep",
    "CompleteWorkflow",
    "Drain",
    "EmitCombine",
    "EmitRunChildWorkflow",
    "EmitRunStep",
    "EmitSourceArray",
    "EmitStack",
    "Fail",
    "Hello",
    "HostError",
    "HostFrame",
    "IdResult",
    "LoadRawData",
    "LogMessage",
    "NoResult",
    "Observe",
    "ProtocolError",
    "Refusal",
    "Refused",
    "Register",
    "Reply",
    "ReplyResult",
    "Request",
    "Store",
    "StoreRequest",
    "ValueResult",
    "WireError",
    "WireValue",
    "WorkerFrame",
    "cid_of",
    "decode_array",
    "decode_frame",
    "decode_value",
    "encode_frame",
    "encode_value",
    "refusal_of",
    "request_id_of",
]
