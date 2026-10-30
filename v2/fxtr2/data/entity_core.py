"""Contextual entity system.

Fxtr's entity system is based around two fundamental concepts:
- Content-hashes: Data can be referred to uniquely by a canonical content-based hash, and this acts
  as a bidirectional link, so that provenance can be tracked in constant space.
- Provenance entities: A given piece of data can be viewed simultaneously on two "levels": there is
  the data itself, and there is also a "provenance" for how that data was generated. Two entities
  with the same data but different provenance should compare equal and be ignored by user code, but
  be distinguishable in the system for tracking the flow of data.

Conceptually, the system resolves around node IDs, which uniquely identify objects and pieces of
data in the the fxtr system, and are connected in a directed acyclic graph. Node IDs are each tagged
with a type (a single-character prefix). These types then determine which operations you can perform
on the node ID:
- Content decoding:
    - _Content-ful_ node IDs can be decoded to a CBOR object representing their content. (This may
      or may not require an external storage layer in order to invert content-based hashes.)
    - _Primitive_ node IDs do not have content and simply exist to anchor other objects.
- Provenance unwrapping:
    - _Provenance nodes_ can be "unwrapped" to reveal a node ID for their data, forgetting their
      provenance information.
    - _Ground nodes_ do not have any provenance information attached, and thus cannot be unwrapped.
      (Note that ground nodes can still _reference_ provenance nodes _as data_, which becomes
      important for composability in the system.)

Programs running with fxtr generally operate on two levels of data.
- The "data" level is the level that influences the program's behavior.
- The "provenance" level is an automatically-maintained metadata level that tracks how the data is
  being generated and where it is being accessed from.
The provenance level always operates on provenance nodes. The data level _usually_ operates on
ground nodes, but can also operate on provenance nodes. This makes it possible to manipulate and
inspect provenance information as data. (When the data level is operating on a provenance node, this
means that there must be _another_ provenance node on the provenance level that unwraps to that
provenance node, e.g. the data happens to be about provenance, but that provenance data itself has
automatically-maintained metadata associated with it.)

These relationships are designed to "commute" (to borrow a term from category theory, hopefully
correctly), in that they follow these laws:
- If X is a contentful provenance node ID, then X.unwrap() is contentful. Furthermore,
  X.unwrap().decode() == X.decode().tree_map_over_ids(lambda y: y.unwrap()), where the operation
  `tree_map_over_ids` is a recursive operation that applies the given function to all node IDs
  contained in the CBOR object. This implies that all such IDs are always themselves provenance
  nodes.
- If value.encode() is the function that produces the canonical node ID of the value, then
  value.encode().unwrap() == value.tree_map_over_ids(lambda y: y.unwrap()).encode(). (This may be a
  natural consequence of .encode() and .decode() being inverses.)
- A "typical", "well-behaved" fxtr function `f_py` (e.g. a user function written following fxtr's
  rules) looks like it defines a pure mathematical function `f_data` from data nodes to data nodes,
  but actually also structurally defines a function `f_prov` from provenance nodes to provenance
  nodes. If we suppose for simplicity that the function takes a single ID argument and returns a
  single ID result, then the function is well-behaved if these mathematical functions exist, and
  furthermore  the relationship `f_prov(X).unwrap() == f_data(X.unwrap())` always holds. (More
  generally, the relationship must hold when mapping `.unwrap()` over all IDs in the CBOR object.)
  This means that the data-level output of a well-behaved fxtr function is a pure function of its
  data-level inputs, and the provenance of the inputs can only affect the provenance of the outputs,
  not their data.

## CBOR encoding

This system is implemented around canonical CBOR encoding (RFC 8949, as implemented in cbor2), with
two fxtr-specific tags:
- 33001: Fxtr entity node ID. This is a unique identifier for a specific entity in the fxtr graph.
    - Fxtr node IDs are always encoded as bytes.
    - The first byte is a character code for the node ID. This code determines how the rest of the
      bytes are interpreted.
    - The supported character codes are:
        "." (here): Contentless ground node. Payload must be empty. This ID should be used as
            the "$fxtr.ctx" field for a ground node with no special interpretation.
        "d" (data): Contentful ground node. Payload is a SHA-256 hash of the CBOR-serialized
            contents of the entity.
            - Decodes to the content of the entity.
            - "d"-IDs are canonical if the content "$fxtr.ctx" field is the "." ID.
        "c" (context): Contentless provenance node. Payload is the SHA-256 hash from a "d"-ID node.
            Represents a node that was built in the context described by another node identified by
            the "d"-ID. The node identified by the "d"-version of the ID must have a field called
            "value" that contains the node ID that this context node unwraps to.
            - Unwraps to the node ID in the "value" field of the context node. Typically this
              node will be "." (ground).
        "n" (normal): Contentful provenance node. Payload is a SHA-256 hash of the CBOR-serialized
            contents of the entity.
            - Decodes to the content of the entity, like a "d" node.
            - Unwraps to the canonical node ID formed by (a) decoding it, (b) unwrapping all node
              IDs in the result, (c) encoding the result.
            - "n"-IDs are canonical if the content "$fxtr.ctx" field is a "c" ID.
        "p" (path): Provenance node. Payload is a CBOR-serialized list. The first element
            of the list must be the SHA-256 hash from a "d"-ID node, describing the context for a
            subgraph of nodes that are being spliced into this context and containing a root node to
            splice in. The remaining elements of the list indicate a path from the root node to the
            current node. Each element of the path is an integer, specifying the index of the node
            ID to follow in the canonical serialization order of the content of the node at that
            point in the path. The node identified by the "d"-version of the ID must have a field
            called "value" that contains the node ID of the root node of the path.
            - Unwraps to the node ID of the node you obtain by starting at the root node (referenced
              by the linked context node) and following the path.
            - If the node ID at the end of the path is contentful, this "p"-ID is contentful, and it
              decodes to the result of decoding the unwrapped node ID and then wrapping all node
              IDs in the result as "p"-tagged nodes, adding one more step to the path. Most p-IDs
              are contentful, but after unwrapping a contentful p-ID it may contain contentless
              p-IDs if any of the children are contentless.
            - "p"-IDs are canonical if the content at the "$fxtr.ctx" field is a "p"-ID and also the
              node can be faithfully represented by a "p"-ID (so all children are correct and the
              contents can be correctly decoded from the ID by following the path from the first
              node). Note that, by construction, you can simply _create_ a canonical "p"-ID by
              starting at a context entity. This means that "p"-IDs can be used to lift a subgraph
              of nodes into a new context by manually attaching provenance information.
        "!" (primitive): Contentless ground node. Payload is an ASCII string that names a built-in
            data type or atom. Usually used as the target of "$fxtr.type" fields.
        "u" (untracked): Provenance node. Payload is itself the bytes of a node ID.
            Indicates that a particular node does not have any attached provenance information but
            should still be treated as a provenance node. Most commonly used for (a) builtin
            entities that do not need any provenance information, and (b) entities that are
            created outside of any provenance context.
            - Unwraps to the node ID in the payload.
            - Contentful if the node ID in the payload is contentful, in which case it decodes to
              the result of decoding the node ID in the payload and then wrapping all node IDs in
              the result as "u"-tagged nodes.
            - "u"-IDs are canonical if the content at the "$fxtr.ctx" field is a "u"-ID and also the
              node can be faithfully represented by a "u"-ID (so all children are also "u"-tagged
              nodes).
        "l" (lift): Provenance node. Payload is the SHA-256 hash from a "d"-ID node.
            Represents a node that was built in the context described by another node identified by
            the "d"-ID. The node identified by the "d"-version of the ID must have a field called
            "value" that contains the node ID that this context node unwraps to.
            - Unwraps to the node ID in the "value" field of the lift node.
            - Contentful if the node ID in the "value" field is contentful, in which case it decodes
              to the result of decoding the node ID in the "value" field and then wrapping all node
              IDs in the result as "l"-tagged nodes, pointing at new d-IDs that replace the value
              field with the appropriate new one.
            - "l"-IDs are canonical if the content at the "$fxtr.ctx" field is a "l"-ID and also the
              node can be faithfully represented by a "l"-ID (so all children are also "l"-tagged
              nodes with compatible contexts).
            - NOT IMPLEMENTED YET
- 33002: "Strong" entity node ID link. Exactly the same as a 33001 ID, but represents a "child" link
    rather than a normal reference.
- 33000: Fxtr entity marker and context ID. Exactly the same as a 33001 ID, but should appear
    exactly once in each entity, as the "$fxtr.ctx" field. Any CBOR dictionary that includes this
    as a field declares itself to be a fxtr entity.
    - The value of the "$fxtr.ctx" field is a node ID identifying the interpretation of the data.
      If this ID is a ground node, this whole entity is a ground node; the specific ID value in this
      case is usually a simple singleton but can also be special information which identifies how the
      entity should be stored or manipulated. If this ID is a provenance node, this whole entity is
      a provenance node, and the provenance data attached to the "$fxtr.ctx" field is the provenance
      data for the entity itself. In this case all other IDs in the entity should be interpreted as
      provenance node IDs for the corresponding data IDs in the unwrapped entity.
        - Usually the "$fxtr.ctx" field is a primitive node ID and is not contentful.
    - Entity dictionaries will _usually_ also include a fixed field "$fxtr.type" mapping to a node
      ID identifying the type of the entity. This field is generally only meaningful for ground
      nodes.
      It can itself point to a contentful node ID or a primitive node ID; if a primitive, this
      usually means this entity has a builtin type.

## Interacting with the fxtr store and provenance tracing

Fxtr programs can manipulate entities and their IDs by interacting with the fxtr store. The fxtr
store conceptually maintains the bidirectional mapping between data and contentful node IDs, and
has two main operations: (1) saving an entity and returning a node ID, and (2) loading an entity
from a node ID. The fxtr store maintains the following invariant to simplify reasoning about the
system:
- If the fxtr store ever returns a contentful node ID, then it promises to be able to load the
  entity from that node ID. This includes any node IDs that are returned inside the data of another
  entity; thus, any transitive dependency of a contentful node ID can also be loaded.
- To ensure this, when saving an entity, the fxtr store must already be able to load every node ID
  that is referenced in that entity.

Internally, the fxtr store maintains reference counts to ensure that it always keeps around any
entities that are reachable from a "root" (some pinned entity that has been promised to stay alive).

To enable transparent provenance tracing and make it possible to write well-behaved fxtr functions
by default, the Python representation of a FxtrID can automatically carry with it another FxtrID
representing its provenance, with the invariant that the attached provenance ID always unwraps to
the "data" ID. In this case, only the "data" ID is used for comparisons and Python hashing, and
the provenance ID is carried internally without affecting the result of any normal computation.
However, when a node is saved or loaded from the fxtr store, it is the _provenance_ ID that is used
to actually save or load the entity.

Additionally, in any execution context where user code is running, fxtr maintains a default context
ID, which can be used as the "$fxtr.ctx" field for any entity created in that context. Fxtr entities
adopt this automatically when they are constructed. The default context ID is always "." at the
data level, and may have a single level of provenance at the provenance level.

At computation boundaries, the relationship between data and provenance can be explicitly
manipulated, usually by internal fxtr code. In particular, before dispatching to user code,
inputs may be unwrapped and then their data re-wrapped with a new "c"-ID that indicates this was an
input to the function, and after the function returns, the output _provenance_ entities may be
wrapped in a new "c"-ID that indicates this was an output from the function. This then re-ifies the
provenance information of the result so that it can be tracked like data.

Finally, for visualization, debugging, and analysis purposes, the fxtr store tracks enough
information to "trace" any given node back to some root pointer that identifies it. There are
roughly two kinds of path, both of which always operate at the provenance level (although they may
start at the data level):
- A "trusted" path is a path where every step indicates some sort of canonical ownership of the
  provenance nodes along the path. Trusted paths always start at a pinned root or arena root, and
  then can follow a specific sequence of allowed steps:
  - First stage (in storage)
    - From an "n" or "d" node to a strongly-linked child node.
        - (does NOT include going from an "n" node to the node it unwraps to; this is )
    - From a "p" or "u" node to its data node.
        - (does NOT include going from a "p" or "u" node to a strongly-linked child node; these are
          canonically represented by following the links from the child node itself.)
  - Second stage (projection)
    - From a "p" node to any other "p" node reachable from it by adding to its path.
    - From a "n" node down to any other "n" or "d" node reachable by unwrapping it.
  This corresponds to the following reverse mapping:
  - From an "n" or "d" node to a strongly-linked reachable parent node.
  - From data nodes to their corresponding "p" or "u" nodes that wrap them, if they exist.
- An untrusted path is like a trusted path except that it also allows "back" steps to follow
  arbitrary backlinks from any node to any other node that points to it, even if they are not in
  the same context.
"""

from __future__ import annotations

import base64
import dataclasses
import logging
import typing
from typing import Any

import cbor2

logger = logging.getLogger(__name__)


def _b32enc(data: bytes) -> str:
    return base64.b32encode(data).decode().lower().rstrip("=")


def _b32dec(data: str) -> bytes:
    data_bytes = data.encode()
    data_bytes += b"=" * ((-len(data_bytes)) % 8)
    return base64.b32decode(data_bytes, casefold=True)


_VARINT32_ALPHABET = "0123456789abcdefghijklmnopqrstuv"


def _encode_varint32(value: int) -> str:
    if value < 0:
        raise ValueError("Only non-negative integers supported")
    if value == 0:
        return _VARINT32_ALPHABET[0]

    # Collect nibbles from low to high
    nibbles = []
    while value > 0:
        nibbles.append(value & 0xF)
        value >>= 4

    # Reverse to get high-to-low order (big-endian)
    nibbles.reverse()

    # Encode: all but last are continuation (offset by 16), last is terminal
    result = []
    for i, nibble in enumerate(nibbles):
        if i < len(nibbles) - 1:
            result.append(_VARINT32_ALPHABET[nibble + 16])  # Continuation
        else:
            result.append(_VARINT32_ALPHABET[nibble])  # Terminal

    return "".join(result)


def _decode_varint32(s: str) -> tuple[int, str]:
    value = 0
    for i, char in enumerate(s):
        idx = _VARINT32_ALPHABET.index(char)
        data = idx & 0xF  # Extract low 4 bits (the actual nibble value)
        value = (value << 4) | data  # Shift left and add new nibble
        if idx < 16:  # Terminal character
            return value, s[i + 1 :]
    raise ValueError(f"Invalid varint32 string: {s}")


class FxtrID:
    """A unique identifier for an entity in the fxtr graph.

    Each FxtrID corresponds to a single node ID in the fxtr graph at the data level. Optionally,
    the FxtrID may also contain a hidden provenance ID, used for automatic provenance tracing.

    All computations should be performed on the data-level ID, without depending on the extra
    provenance information.
    However, the provenance ID is used by the system to track ownership of entities and enable
    tracing of the results. The provenance ID must always uniquely identify the data-level ID, if
    both are present.
    """

    _raw_data_id: bytes
    _provenance_parent: FxtrID | None

    def __init__(self, *, raw_data_id: bytes, provenance_parent: FxtrID | None = None):
        super().__setattr__("_raw_data_id", raw_data_id)
        super().__setattr__("_provenance_parent", provenance_parent)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("FxtrID is immutable")

    def __eq__(self, other: Any) -> bool:
        return type(other) is type(self) and self._raw_data_id == other._raw_data_id

    def __hash__(self) -> int:
        return hash((type(self), self._raw_data_id))

    def split_tag_and_payload(self) -> tuple[str, bytes]:
        return chr(self._raw_data_id[0]), self._raw_data_id[1:]

    def get_tag(self) -> str:
        return chr(self._raw_data_id[0])

    @classmethod
    def from_tag_and_payload(
        cls, tag: str, payload: bytes, *, provenance_parent: FxtrID | None = None
    ) -> FxtrID:
        return cls(raw_data_id=bytes([ord(tag)]) + payload, provenance_parent=provenance_parent)

    @classmethod
    def builtin(cls, name: str) -> FxtrID:
        return cls.from_tag_and_payload("!", name.encode("utf-8"))

    def local_id_string(self) -> str:
        tag, payload = self.split_tag_and_payload()
        match tag:
            case ".":
                assert not payload
                return "."
            case "d":
                return f"d{_b32enc(payload)}"
            case "c":
                return f"c{_b32enc(payload)}"
            case "n":
                return f"n{_b32enc(payload)}"
            case "p":
                wrapped = cbor2.loads(payload)
                assert isinstance(wrapped, list) and len(wrapped) >= 1
                fst = _b32enc(wrapped[0])
                tail = "".join(_encode_varint32(n) for n in wrapped[1:])
                return f"p{fst}[{tail}]"
            case "!":
                builtin_name = payload.decode("utf-8")
                return f"!{builtin_name}"
            case "u":
                inner_node = FxtrID(raw_data_id=payload, provenance_parent=None)
                return f"u{inner_node.local_id_string()}"
            case _:
                raise ValueError(f"Unknown tag: {tag}")

    @classmethod
    def from_id_string(cls, id_str: str) -> FxtrID:
        if not id_str:
            raise ValueError("Empty ID string")

        if "@" in id_str:
            local_part, provenance_part = id_str.split("@", 1)
            local_id = cls.from_id_string(local_part)
            provenance_id = cls.from_id_string(provenance_part)
            return cls(raw_data_id=local_id._raw_data_id, provenance_parent=provenance_id)

        tag = id_str[0]
        rest = id_str[1:]

        match tag:
            case ".":
                if rest:
                    raise ValueError(f"Expected empty string after '.', got: {rest}")
                return cls.from_tag_and_payload(".", b"")
            case "d" | "c" | "n":
                payload = _b32dec(rest)
                return cls.from_tag_and_payload(tag, payload)
            case "p":
                # Format: p{base32_first}[{varint32_path}]
                if "[" not in rest:
                    raise ValueError(f"Invalid p-ID format: missing '['")
                bracket_idx = rest.index("[")
                fst_str = rest[:bracket_idx]
                tail_str = rest[bracket_idx + 1 :]
                if not tail_str.endswith("]"):
                    raise ValueError(f"Invalid p-ID format: missing ']'")
                tail_str = tail_str[:-1]  # Remove trailing ']'

                fst_bytes = _b32dec(fst_str)
                path = []
                remaining = tail_str
                while remaining:
                    value, remaining = _decode_varint32(remaining)
                    path.append(value)

                wrapped = [fst_bytes] + path
                payload = cbor2.dumps(wrapped, canonical=True)
                return cls.from_tag_and_payload("p", payload)
            case "!":
                builtin_name = rest
                payload = builtin_name.encode("utf-8")
                return cls.from_tag_and_payload("!", payload)
            case "u":
                assert "@" not in rest
                inner_id = cls.from_id_string(rest)
                return cls.from_tag_and_payload("u", inner_id._raw_data_id)
            case _:
                raise ValueError(f"Unknown tag: {tag}")

    def local_id_summary(self) -> str:
        tag, payload = self.split_tag_and_payload()
        match tag:
            case ".":
                return "."
            case "d":
                return f"d{_b32enc(payload)[:7]}"
            case "c":
                return f"c{_b32enc(payload)[:7]}"
            case "n":
                return f"n{_b32enc(payload)[:7]}"
            case "p":
                wrapped = cbor2.loads(payload)
                assert isinstance(wrapped, list) and len(wrapped) >= 1
                fst = _b32enc(wrapped[0])
                tail = "".join(_encode_varint32(n) for n in wrapped[1:])
                return f"p{fst[:4]}[{tail}]"
            case "!":
                builtin_name = payload.decode("utf-8")
                return f"!{builtin_name}"
            case "u":
                inner_node = FxtrID(raw_data_id=payload)
                return f"u{inner_node.local_id_summary()}"
            case _:
                raise ValueError(f"Unknown tag: {tag}")

    def id_string(self) -> str:
        res = self.local_id_string()
        if self._provenance_parent is not None:
            res += f"@{self._provenance_parent.id_string()}"
        return res

    def summary(self) -> str:
        res = self.local_id_summary()
        if self._provenance_parent is not None:
            pp = self._provenance_parent
            if pp.split_tag_and_payload()[0] == "u" and pp._provenance_parent is None:
                res += f"@(untracked)"
            else:
                res += f"@{self._provenance_parent.summary()}"
        return res

    def __repr__(self):
        return f"<FxtrID {self.summary()}>"


@dataclasses.dataclass(frozen=True)
class RawWeakFxtrID:
    """A raw weak fxtr ID (CBOR tag 33001)."""

    _CBOR_TAG: typing.ClassVar[int] = 33001

    inner_id: FxtrID


@dataclasses.dataclass(frozen=True)
class RawStrongFxtrID:
    """A raw strong fxtr ID (CBOR tag 33002)."""

    _CBOR_TAG: typing.ClassVar[int] = 33002
    inner_id: FxtrID


@dataclasses.dataclass(frozen=True)
class RawFxtrEntityMarkerID:
    """A raw fxtr entity marker ID (CBOR tag 33000)."""

    _CBOR_TAG: typing.ClassVar[int] = 33000
    inner_id: FxtrID


type FxtrCbor = (
    None
    | int
    | float
    | bool
    | str
    | bytes
    | list[FxtrCbor]
    | dict[str, FxtrCbor]
    | RawWeakFxtrID
    | RawStrongFxtrID
    | RawFxtrEntityMarkerID
)


def _fxtr_id_lift(fid: FxtrID, levels: int) -> FxtrID:
    cur_id = fid
    for _ in range(levels):
        if cur_id._provenance_parent is None:
            raise ValueError(
                f"Can't lift to provenance level {levels} for {cur_id.id_string()}: it does not"
                " have provenance attached"
            )
        cur_id = cur_id._provenance_parent
    return cur_id


def fxtr_cbor_lift_to_provenance(value: FxtrCbor, levels: int = 1) -> FxtrCbor:
    match value:
        case RawWeakFxtrID():
            return RawWeakFxtrID(inner_id=_fxtr_id_lift(value.inner_id, levels))
        case RawStrongFxtrID():
            return RawStrongFxtrID(inner_id=_fxtr_id_lift(value.inner_id, levels))
        case RawFxtrEntityMarkerID():
            return RawFxtrEntityMarkerID(inner_id=_fxtr_id_lift(value.inner_id, levels))
        case list():
            return [fxtr_cbor_lift_to_provenance(v, levels) for v in value]
        case dict():
            return {k: fxtr_cbor_lift_to_provenance(v, levels) for k, v in value.items()}
        case None | bool() | int() | float() | str() | bytes():
            return value
        case _:
            raise TypeError(f"Unsupported type: {type(value)}")


def fxtr_cbor_encode_hook(encoder, value):
    match value:
        case RawWeakFxtrID() | RawStrongFxtrID() | RawFxtrEntityMarkerID():
            inner = value.inner_id
            return encoder.encode(cbor2.CBORTag(value._CBOR_TAG, inner._raw_data_id))
        case _:
            raise TypeError(f"Unsupported type: {type(value)}")


def fxtr_cbor_decode_hook(decoder, tag, shareable_index=None):
    assert shareable_index is None
    match tag.tag:
        case 33000:
            assert isinstance(tag.value, bytes)
            inner_id = FxtrID(raw_data_id=tag.value, provenance_parent=None)
            return RawFxtrEntityMarkerID(inner_id=inner_id)
        case 33001:
            assert isinstance(tag.value, bytes)
            inner_id = FxtrID(raw_data_id=tag.value, provenance_parent=None)
            return RawWeakFxtrID(inner_id=inner_id)
        case 33002:
            assert isinstance(tag.value, bytes)
            inner_id = FxtrID(raw_data_id=tag.value, provenance_parent=None)
            return RawStrongFxtrID(inner_id=inner_id)
        case _:
            raise TypeError(f"Unknown tag: {tag.tag}")


def fxtr_cbor_dumps(value: FxtrCbor) -> bytes:
    return cbor2.dumps(value, default=fxtr_cbor_encode_hook, canonical=True)


def fxtr_cbor_loads(data: bytes | memoryview) -> FxtrCbor:
    return cbor2.loads(data, tag_hook=fxtr_cbor_decode_hook)


def fxtr_cbor_dumps_with_marker_check(value: FxtrCbor) -> bytes:
    """Dump an entity and check that it has exactly one RawFxtrEntityMarkerID at "$fxtr.ctx"."""
    if not isinstance(value, dict):
        raise ValueError("Entity must be a dictionary")
    if "$fxtr.ctx" not in value:
        raise ValueError('Entity must have a "$fxtr.ctx" field')
    if not isinstance(value["$fxtr.ctx"], RawFxtrEntityMarkerID):
        raise ValueError('"$fxtr.ctx" field must be a RawFxtrEntityMarkerID')

    found = False

    def enc_hook(encoder, value):
        nonlocal found
        if isinstance(value, RawFxtrEntityMarkerID):
            if found:
                raise ValueError(
                    "Found more than one RawFxtrEntityMarkerID in this entity! Not allowed."
                )
            found = True
            return encoder.encode(cbor2.CBORTag(value._CBOR_TAG, value.inner_id._raw_data_id))
        elif isinstance(value, (RawWeakFxtrID, RawStrongFxtrID)):
            return encoder.encode(cbor2.CBORTag(value._CBOR_TAG, value.inner_id._raw_data_id))
        else:
            raise ValueError(f"Unsupported type: {type(value)}")

    return cbor2.dumps(value, default=enc_hook, canonical=True)
