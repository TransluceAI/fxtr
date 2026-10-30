"""Canonical encodings: storage identity text + entity forms of blobby values.

Two encoding families live here, both *identity-bearing* (equal values must
encode identically, forever — these strings and entity ids are compared,
fingerprinted, and stored):

1. **Storage identity text** — the canonical JSON forms of the state db's
   identity columns (see `storage.py`): key-paths (arrays of key strings)
   and flattened coordinate tuples (arrays of str|int). Canonical flags:
   no spaces, sorted keys, ``allow_nan=False``, unicode kept verbatim.
   Codecs here are *context-free*: model → storage is pure; decoding back
   to *typed* model paths (step kinds, per-grid coordinate splits) needs
   grid-table context and belongs to the state store.

2. **Canonical entity forms** — grid configs and slice templates are
   carried by tiny entities (:class:`GridConfigEntity` /
   :class:`SliceTemplateEntity`) whose runtime attribute is the *actual
   model object* (a field-level serialization strategy); the entity id is
   the fingerprint (``grid_config`` observations, ``targets`` columns).
   What we own is the **tagged JSONable form** the strategy produces —
   deliberately not a codec library's guess: several model unions are
   shape-ambiguous under untagged try-in-order decoding
   (Data/Handle/Checkpoint/Subtree configs are all ``axes``-only; subgrid
   vs internals path steps have identical fields), and fingerprints must
   never change out from under us via library behavior. Determinism of
   the *bytes* (and hence ids) is supplied by fxtr2's canonical entity
   CBOR (sorted map keys etc.) — there is no string step. Tags: axis
   templates ``["lit", v] | ["param", i] | ["keep"]``; path steps
   ``["sub" | "int", [templates...], key]``; configs ``{"kind": ...}``;
   output specs ``["entity"] | ["frame", schema]``; schema columns
   ``[name, kind]`` / ``[name, kind, true]`` (nullable; flag omitted
   when default). Frame *values* have their own canonical entity form —
   `frame_encoding.py` owns it. The schema-name law is the one charset
   law that does *not* live here: `model.validate_frame_schema` sits with
   the schema types, upstream of its three consumers (:func:`store_config`
   below, `frame_encoding.build_frame_entity`, and the frame runtime's
   write boundary in `tables.py`).

**What lives where** (orientation for the state db vs the entity store):
directly in state-db columns — identity text (key-paths, coords,
parent_path: the only canonical strings anywhere), generation uuids,
status text, the int enums, and FxtrID BLOBs (config_id, value_id,
fingerprint, args_id, step_type_id, slice_id, expected_config).
Entity-encoded (fingerprint carriers / closure pinners) — grid configs and
slice templates here; later checkpoint args and frame values. Never stored
at all — views/specs/shadows (ephemeral runtime/wire state) and the
model's record dataclasses (ObservationRecord, TaskCellExecution,
TargetRecord), which are conceptual: their storage form *is* the table
rows.

Coordinate values are validated by *exact* type: ``type(x) is int`` (which
rejects ``bool``, an int subclass) or ``type(x) is str``; floats are
rejected on both encode and decode.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping as AbcMapping
from collections.abc import Sequence as AbcSequence
from typing import TYPE_CHECKING, Sequence, TypeGuard, cast

from frozendict import frozendict
from mashumaro.types import SerializationStrategy

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import (
    FrozenFxtrCbor,
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, ServiceRef, get_active_scope
from fxtr2.data.system_entities import define_type

from fxtr_experiment_task._ns import WORKFLOWS3_NS
from fxtr_experiment_task.errors import EncodingError, StepPayloadError

if TYPE_CHECKING:
    from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr_experiment_task.model import (
    AssertionFact,
    AxisKind,
    AxisSpec,
    AxisTemplate,
    CellRef,
    CheckpointFact,
    CheckpointGridConfig,
    ColumnSpec,
    CoordBinding,
    CoordTuple,
    CoordValue,
    DataFrameGridConfig,
    DataGridConfig,
    EntityOutput,
    FailureFact,
    FrameOutput,
    FrameSchema,
    GenerationRecord,
    GridConfig,
    GridPath,
    HandleGridConfig,
    InputBinding,
    InternalsStepTemplate,
    KeepAxis,
    LiteralCoord,
    ObservationFact,
    OutputSpec,
    Param,
    PathStepTemplate,
    RequestFact,
    SliceBinding,
    SliceMode,
    SliceTemplate,
    SubgridStepTemplate,
    SubtreeGridConfig,
    TaskGridConfig,
    TaskRule,
    validate_frame_schema,
)

ROOT_KEY_PATH = "[]"
"""The root namespace's key-path text (the empty key array)."""


def _canonical_json(value: object) -> str:
    """Canonical JSON text: sorted keys, no spaces, no NaN, unicode kept."""
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False
    )


def canonical_json_text(value: object) -> str:
    """Canonical JSON text (sorted keys, compact) for a plain-JSON value.

    The public form of the internal canonical encoder, for the sandboxed-
    binding ``config_json`` column (`storage.py`): storing config as
    canonical text keeps chunk-4 export diffs deterministic. Object key
    order is normalized; arrays are preserved. Deliberately not identity-
    bearing (bindings never enter a fingerprint).
    """
    return _canonical_json(value)


def _is_seq(v: object) -> "TypeGuard[AbcSequence[object]]":
    """A JSONable sequence, tolerant of CBOR-roundtrip containers."""
    return isinstance(v, AbcSequence) and not isinstance(v, (str, bytes))


def _is_map(v: object) -> "TypeGuard[AbcMapping[object, object]]":
    """A JSONable mapping, tolerant of CBOR-roundtrip containers."""
    return isinstance(v, AbcMapping)


# ---------------------------------------------------------------------------
# Storage identity text: coords and key-paths
# ---------------------------------------------------------------------------


def _check_coord(value: object) -> CoordValue:
    """Validate one coordinate by exact type (bool/float rejected); return it."""
    if type(value) is int or type(value) is str:  # exact: rejects bool, floats
        return value
    raise EncodingError(f"coordinate values are str|int, got {value!r}")


def encode_coords(coords: Sequence[CoordValue]) -> str:
    """Encode a coordinate tuple as canonical JSON text.

    Args:
        coords: Position-aligned coordinate values (str|int, exact types).

    Returns:
        The canonical text form (a JSON array).

    Raises:
        EncodingError: On non-str|int values (including bool).
    """
    return _canonical_json([_check_coord(c) for c in coords])


def decode_coords(text: str) -> CoordTuple:
    """Decode canonical coordinate text back to a tuple.

    Args:
        text: A value produced by :func:`encode_coords`.

    Returns:
        The coordinate tuple.

    Raises:
        EncodingError: If the text is not a JSON array of str|int.
    """
    value = json.loads(text)
    if not isinstance(value, list):
        raise EncodingError(f"coords text must be a JSON array, got {text!r}")
    return tuple(_check_coord(v) for v in value)


GRID_KEY_MAX_LEN = 100
"""Maximum grid-key length (filesystem-safe headroom under common limits)."""

_GRID_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def validate_grid_key(key: str) -> None:
    """Validate one grid key against the key-charset law.

    Grid keys become directory names in the at-rest export layout
    (``notes/at_rest_format.md``), so they are constrained at grid
    creation time: lowercase ``a-z0-9_-``, starting with a letter or
    digit, at most :data:`GRID_KEY_MAX_LEN` characters. Lowercase-only
    sidesteps case-insensitive-filesystem collisions; excluding ``.``
    makes collision with the layout's reserved file names structurally
    impossible. Deliberately maximally strict to start — relaxing later
    is compatible, un-relaxing is not.

    Raises:
        EncodingError: On an invalid key.
    """
    if not _GRID_KEY_RE.fullmatch(key):
        raise EncodingError(
            f"invalid grid key {key!r}: keys must match [a-z0-9][a-z0-9_-]*"
            " (lowercase; filesystem-safe — see notes/at_rest_format.md)"
        )
    if len(key) > GRID_KEY_MAX_LEN:
        raise EncodingError(f"invalid grid key {key!r}: longer than {GRID_KEY_MAX_LEN} chars")


def encode_key_path(keys: Sequence[str]) -> str:
    """Encode a key-path (grid keys from the root) as canonical JSON text.

    Args:
        keys: The key strings, root-outward. Step kinds are not encoded
            (derivable from parent grid kinds; see `storage.py`).

    Returns:
        The canonical text form (a JSON array of strings).

    Raises:
        EncodingError: On non-str keys.
    """
    for k in keys:
        if type(k) is not str:
            raise EncodingError(f"key-path keys are str, got {k!r}")
    return _canonical_json(list(keys))


def decode_key_path(text: str) -> tuple[str, ...]:
    """Decode canonical key-path text back to a key tuple.

    Args:
        text: A value produced by :func:`encode_key_path`.

    Returns:
        The key strings.

    Raises:
        EncodingError: If the text is not a JSON array of strings.
    """
    value = json.loads(text)
    if not isinstance(value, list) or any(type(k) is not str for k in value):
        raise EncodingError(f"key-path text must be a JSON array of strings, got {text!r}")
    return tuple(value)


def parent_key_path(text: str) -> str:
    """The parent grid's key-path text (for ``grids.parent_path``).

    Args:
        text: A non-root key-path text.

    Returns:
        The key-path text minus its last key (:data:`ROOT_KEY_PATH` for
        top-level grids).

    Raises:
        EncodingError: If ``text`` is the root (the root has no parent).
    """
    keys = decode_key_path(text)
    if not keys:
        raise EncodingError("the root key-path has no parent")
    return encode_key_path(keys[:-1])


# ---------------------------------------------------------------------------
# Model ↔ storage repacking (pure direction only; typed decode needs the
# grid table and lives with the state store)
# ---------------------------------------------------------------------------


def key_path_of(path: GridPath) -> tuple[str, ...]:
    """The key-path (keys only) of a model grid path."""
    return tuple(step.key for step in path)


def flattened_coords_of(cell: CellRef) -> CoordTuple:
    """A cell's full flattened coordinates (path step cells, then own)."""
    parts: list[CoordValue] = []
    for step in cell.grid:
        parts.extend(step.cell)
    parts.extend(cell.coords)
    return tuple(parts)


def storage_cell_location(cell: CellRef) -> tuple[str, str]:
    """A cell's storage identity: ``(key_path_text, flattened_coords_text)``."""
    return (
        encode_key_path(key_path_of(cell.grid)),
        encode_coords(flattened_coords_of(cell)),
    )


# ---------------------------------------------------------------------------
# Tagged payload encoding (JSONable) for configs and slice templates
# ---------------------------------------------------------------------------

type JsonTypes = None | bool | int | float | str | list[JsonTypes] | dict[str, JsonTypes]
"""The JSON value tree the tagged config/slice encoders produce — the
id-free subset of `fxtr2.data.entity_core.FxtrCbor` (byte
canonicalization happens at the entity-CBOR layer). The ``*_from_json``
decoders deliberately take ``object``, not this alias: their input is
whatever came back from decoding stored bytes, narrowed value by value."""


def _axis_to_json(a: AxisSpec) -> list[JsonTypes]:
    """Tagged form of an AxisSpec: ``[name, kind]``."""
    return [a.name, a.kind]


def _axis_from_json(v: object) -> AxisSpec:
    """Decode ``[name, kind]``; EncodingError on bad shape/kind."""
    if not (_is_seq(v) and len(v) == 2 and v[1] in ("str", "int")):
        raise EncodingError(f"bad axis spec: {v!r}")
    return AxisSpec(name=str(v[0]), kind=v[1])


def _column_to_json(c: ColumnSpec) -> list[JsonTypes]:
    """Tagged form of a ColumnSpec: ``[name, kind]`` / ``[name, kind, true]``.

    The nullable flag is omitted when default (non-nullable), so schemas
    predating the flag keep their encoding — and their config fingerprints
    — unchanged."""
    if c.nullable:
        return [c.name, c.kind, True]
    return [c.name, c.kind]


def _column_from_json(v: object) -> ColumnSpec:
    """Decode a tagged column spec; EncodingError on bad shape/kind."""
    if (
        _is_seq(v)
        and len(v) in (2, 3)
        and v[1] in ("str", "int", "float", "bool", "bytes")
        and (len(v) == 2 or v[2] is True)
    ):
        return ColumnSpec(name=str(v[0]), kind=v[1], nullable=len(v) == 3)
    raise EncodingError(f"bad column spec: {v!r}")


def _schema_to_json(schema: FrameSchema) -> list[JsonTypes]:
    """Tagged form of a FrameSchema (list of column pairs)."""
    return [_column_to_json(c) for c in schema]


def _schema_from_json(v: object) -> FrameSchema:
    """Decode a frame schema; EncodingError on bad shape."""
    if not _is_seq(v):
        raise EncodingError(f"bad frame schema: {v!r}")
    return tuple(_column_from_json(c) for c in v)


def _template_to_json(t: AxisTemplate) -> list[JsonTypes]:
    """Tagged form of an AxisTemplate: ``["lit",v] | ["param",i] | ["keep"]``."""
    if isinstance(t, LiteralCoord):
        return ["lit", _check_coord(t.value)]
    if isinstance(t, Param):
        return ["param", t.position]
    return ["keep"]


def _template_from_json(v: object) -> AxisTemplate:
    """Decode a tagged axis template; EncodingError on bad tag."""
    if _is_seq(v) and len(v) >= 1:
        if v[0] == "lit" and len(v) == 2:
            return LiteralCoord(value=_check_coord(v[1]))
        if v[0] == "param" and len(v) == 2 and type(v[1]) is int:
            return Param(position=v[1])
        if v[0] == "keep" and len(v) == 1:
            return KeepAxis()
    raise EncodingError(f"bad axis template: {v!r}")


def _step_to_json(s: PathStepTemplate) -> list[JsonTypes]:
    """Tagged form of a path step: ``["sub"|"int", cells, key]``."""
    tag = "int" if isinstance(s, InternalsStepTemplate) else "sub"
    return [tag, [_template_to_json(t) for t in s.cell], s.key]


def _step_from_json(v: object) -> PathStepTemplate:
    """Decode a tagged path step; EncodingError on bad tag/shape."""
    if not (_is_seq(v) and len(v) == 3 and v[0] in ("sub", "int")):
        raise EncodingError(f"bad path step: {v!r}")
    cls = InternalsStepTemplate if v[0] == "int" else SubgridStepTemplate
    if not _is_seq(v[1]):
        raise EncodingError(f"bad path step cell: {v!r}")
    return cls(cell=tuple(_template_from_json(t) for t in v[1]), key=str(v[2]))


def _slice_to_json(s: SliceTemplate) -> dict[str, JsonTypes]:
    """Tagged form of a SliceTemplate (path + target templates + output)."""
    return {
        "path": [_step_to_json(st) for st in s.path],
        "target": [_template_to_json(t) for t in s.target_axes],
        "output": s.output,
    }


def _slice_from_json(v: object) -> SliceTemplate:
    """Decode a slice template; EncodingError on bad shape."""
    if not _is_map(v):
        raise EncodingError(f"bad slice template: {v!r}")
    path, target = v.get("path"), v.get("target")
    if not (_is_seq(path) and _is_seq(target)):
        raise EncodingError(f"bad slice template: {v!r}")
    output = v.get("output")
    if output is not None and type(output) is not str:
        raise EncodingError(f"bad slice output: {output!r}")
    return SliceTemplate(
        path=tuple(_step_from_json(st) for st in path),
        target_axes=tuple(_template_from_json(t) for t in target),
        output=output,
    )


def _binding_to_json(b: InputBinding) -> dict[str, JsonTypes]:
    """Tagged form of an input binding.

    The two variants are map-shaped with disjoint required keys (``slice``
    vs ``coord``) — the discriminator, since the slice form predates the
    union and its encoding (hence fingerprints) must not change.
    """
    if isinstance(b, CoordBinding):
        return {
            "coord": b.kind,
            "template": _template_to_json(b.template),
            "received": b.received_axis_name,
        }
    return {
        "slice": _slice_to_json(b.slice),
        "mode": b.mode,
        "received": list(b.received_axis_names),
    }


def _binding_from_json(v: object) -> InputBinding:
    """Decode a tagged input binding; EncodingError on bad shape."""
    if _is_map(v) and "coord" in v:
        raw_kind = v.get("coord")
        if raw_kind not in ("str", "int"):
            raise EncodingError(f"bad coord binding kind: {v!r}")
        kind = cast("AxisKind", raw_kind)
        received = v.get("received")
        if received is not None and type(received) is not str:
            raise EncodingError(f"bad coord received name: {received!r}")
        return CoordBinding(
            kind=kind,
            template=_template_from_json(v.get("template")),
            received_axis_name=received,
        )
    if not _is_map(v):
        raise EncodingError(f"bad slice binding: {v!r}")
    raw_mode = v.get("mode")
    if raw_mode not in ("read", "write"):
        raise EncodingError(f"bad slice binding: {v!r}")
    mode = cast("SliceMode", raw_mode)
    received_names = v.get("received")
    if not _is_seq(received_names):
        raise EncodingError(f"bad received names: {received_names!r}")
    names: list[str] = []
    for n in received_names:
        if type(n) is not str:
            raise EncodingError(f"bad received names: {received_names!r}")
        names.append(n)
    return SliceBinding(
        slice=_slice_from_json(v.get("slice")),
        mode=mode,
        received_axis_names=tuple(names),
    )


def _rule_to_json(r: TaskRule) -> dict[str, JsonTypes]:
    """Tagged form of a TaskRule (task_fn slice + named input bindings)."""
    return {
        "task_fn": _slice_to_json(r.task_fn),
        "inputs": {name: _binding_to_json(b) for name, b in r.inputs.items()},
    }


def _rule_from_json(v: object) -> TaskRule:
    """Decode a task rule; EncodingError on bad shape."""
    if not _is_map(v):
        raise EncodingError(f"bad task rule: {v!r}")
    inputs = v.get("inputs")
    if not _is_map(inputs):
        raise EncodingError(f"bad task rule: {v!r}")
    return TaskRule(
        task_fn=_slice_from_json(v.get("task_fn")),
        inputs=frozendict({str(name): _binding_from_json(b) for name, b in inputs.items()}),
    )


def _output_to_json(o: OutputSpec) -> list[JsonTypes]:
    """Tagged form of an OutputSpec: ``["entity"] | ["frame", schema]``."""
    if isinstance(o, FrameOutput):
        return ["frame", _schema_to_json(o.schema)]
    return ["entity"]


def _output_from_json(v: object) -> OutputSpec:
    """Decode a tagged output spec; EncodingError on bad tag."""
    if _is_seq(v) and len(v) >= 1:
        if v[0] == "entity" and len(v) == 1:
            return EntityOutput()
        if v[0] == "frame" and len(v) == 2:
            return FrameOutput(schema=_schema_from_json(v[1]))
    raise EncodingError(f"bad output spec: {v!r}")


def config_to_json(config: GridConfig) -> dict[str, JsonTypes]:
    """Encode a grid config to its tagged JSONable form."""
    axes: list[JsonTypes] = [_axis_to_json(a) for a in config.axes]
    if isinstance(config, DataGridConfig):
        return {"kind": "data", "axes": axes}
    if isinstance(config, DataFrameGridConfig):
        return {"kind": "dataframe", "axes": axes, "schema": _schema_to_json(config.schema)}
    if isinstance(config, HandleGridConfig):
        return {"kind": "handle", "axes": axes}
    if isinstance(config, CheckpointGridConfig):
        return {"kind": "checkpoint", "axes": axes}
    if isinstance(config, SubtreeGridConfig):
        return {"kind": "subtree", "axes": axes}
    if isinstance(config, TaskGridConfig):
        return {
            "kind": "task",
            "axes": axes,
            "rule": _rule_to_json(config.rule),
            "outputs": {name: _output_to_json(o) for name, o in config.outputs.items()},
        }
    raise EncodingError(f"unknown config type: {config!r}")


def config_from_json(v: object) -> GridConfig:
    """Decode the tagged JSONable form back to a grid config."""
    if not _is_map(v):
        raise EncodingError(f"bad grid config: {v!r}")
    raw_axes = v.get("axes")
    if not _is_seq(raw_axes):
        raise EncodingError(f"bad grid config: {v!r}")
    axes = tuple(_axis_from_json(a) for a in raw_axes)
    kind = v.get("kind")
    if kind == "data":
        return DataGridConfig(axes=axes)
    if kind == "dataframe":
        return DataFrameGridConfig(axes=axes, schema=_schema_from_json(v.get("schema")))
    if kind == "handle":
        return HandleGridConfig(axes=axes)
    if kind == "checkpoint":
        return CheckpointGridConfig(axes=axes)
    if kind == "subtree":
        return SubtreeGridConfig(axes=axes)
    if kind == "task":
        outputs = v.get("outputs")
        if not _is_map(outputs):
            raise EncodingError(f"bad task outputs: {outputs!r}")
        return TaskGridConfig(
            axes=axes,
            rule=_rule_from_json(v.get("rule")),
            outputs=frozendict({str(name): _output_from_json(o) for name, o in outputs.items()}),
        )
    raise EncodingError(f"unknown config kind: {kind!r}")


def slice_to_json(s: SliceTemplate) -> dict[str, JsonTypes]:
    """Encode a slice template to its tagged JSONable form."""
    return _slice_to_json(s)


def slice_from_json(v: object) -> SliceTemplate:
    """Decode the tagged JSONable form back to a slice template."""
    return _slice_from_json(v)


def schema_to_json(schema: FrameSchema) -> list[JsonTypes]:
    """Encode a frame schema to its tagged JSONable form (the form the
    frame entity embeds — `frame_encoding.py`)."""
    return _schema_to_json(schema)


def schema_from_json(v: object) -> FrameSchema:
    """Decode the tagged JSONable form back to a frame schema."""
    return _schema_from_json(v)


# ---------------------------------------------------------------------------
# Entity forms (the fingerprint carriers)
# ---------------------------------------------------------------------------

GRID_CONFIG_TYPE_ID = define_type(WORKFLOWS3_NS, "grid_config_v0")
SLICE_TEMPLATE_TYPE_ID = define_type(WORKFLOWS3_NS, "slice_template_v0")


class _GridConfigStrategy(SerializationStrategy):
    """Field strategy: GridConfig ↔ its tagged JSONable tree (module doc)."""

    def serialize(self, value: GridConfig) -> dict[str, JsonTypes]:
        """Encode for entity CBOR (byte canonicalization happens there)."""
        return config_to_json(value)

    def deserialize(self, value: object) -> GridConfig:
        """Decode the (container-tolerant) tagged tree back to the model."""
        return config_from_json(value)


class _SliceTemplateStrategy(SerializationStrategy):
    """Field strategy: SliceTemplate ↔ its tagged JSONable tree."""

    def serialize(self, value: SliceTemplate) -> dict[str, JsonTypes]:
        """Encode for entity CBOR (byte canonicalization happens there)."""
        return _slice_to_json(value)

    def deserialize(self, value: object) -> SliceTemplate:
        """Decode the (container-tolerant) tagged tree back to the model."""
        return _slice_from_json(value)


@dataclasses.dataclass(frozen=True)
class GridConfigEntity(DataclassEntity, fxtr_type=GRID_CONFIG_TYPE_ID):
    """Canonical entity form of a grid config; its id is the fingerprint.

    The runtime attribute is the actual model object; the wire form is the
    tagged JSONable tree, serialized by fxtr's canonical entity CBOR.
    """

    config: GridConfig = dataclasses.field(
        metadata={"serialization_strategy": _GridConfigStrategy()}
    )


@dataclasses.dataclass(frozen=True)
class SliceTemplateEntity(DataclassEntity, fxtr_type=SLICE_TEMPLATE_TYPE_ID):
    """Canonical entity form of a slice template (targets' ``slice_id``)."""

    template: SliceTemplate = dataclasses.field(
        metadata={"serialization_strategy": _SliceTemplateStrategy()}
    )


def store_config(config: GridConfig, scope: FxtrScope | None = None) -> FxtrID:
    """Store the canonical config entity; returns its fingerprint.

    Also the enforcement funnel for the column-name law
    (:func:`validate_frame_schema`): every open path — the ops engine's
    and the user capability's alike — computes its proposed config's
    fingerprint here before the grid can be created or compared, so no
    invalid schema ever gets one.

    Args:
        config: The model config to encode.
        scope: Scope to stage the entity into. ``None`` uses the active
            scope — the scope-discipline user-boundary case (this helper
            doubles as the fingerprint calculator in user/test code);
            library callers pass their scope explicitly.

    Returns:
        The config's fingerprint (the entity id). Canonical entity CBOR +
        content addressing make this deterministic: equal configs always
        yield equal ids.

    Raises:
        EncodingError: A schema in the config violates the column-name
            law (dataframe grids and task ``FrameOutput`` declarations).
    """
    if scope is None:
        scope = get_active_scope()
    if isinstance(config, DataFrameGridConfig):
        validate_frame_schema(config.schema)
    elif isinstance(config, TaskGridConfig):
        for spec in config.outputs.values():
            if isinstance(spec, FrameOutput):
                validate_frame_schema(spec.schema)
    return GridConfigEntity(config=config).store(scope=scope).entity_id


def store_slice(s: SliceTemplate, scope: FxtrScope | None = None) -> FxtrID:
    """Store the canonical slice-template entity (targets' ``slice_id``).

    ``scope`` semantics match :func:`store_config` (``None`` = active
    scope, the user-boundary case)."""
    if scope is None:
        scope = get_active_scope()
    return SliceTemplateEntity(template=s).store(scope=scope).entity_id


# -- Step payloads (invocation args + checkpointed results) -------------------
#
# Step payloads must be durable and identity-bearing: the args fingerprint
# is the replay-compared half of a checkpoint's recorded invocation, and
# the result must round-trip when a hit serves it. Entity-encoding (rather
# than an opaque blob) is what pins the closures of any entity ids the
# payload references: each embedded id becomes a *strong* CBOR ref, so
# persisting the payload persists everything it points at (the
# complete-subgraph invariant does the rest).

STEP_PAYLOAD_TYPE_ID = define_type(WORKFLOWS3_NS, "step_payload_v0")


def service_data_to_frozen_cbor(value: "FxtrServiceData") -> FrozenFxtrCbor:
    """Encode durable service data as frozen fxtr CBOR (pure).

    The durable subset of the service vocabulary: primitives pass
    through; ``str``-keyed mappings → frozendicts; sequences → tuples;
    ``FxtrID`` / ``EntityRef`` → strong raw-id refs (``EntityRef``
    collapses to its bare id, matching the untyped wire).

    Raises:
        StepPayloadError: ``ServiceRef`` (not durable), a non-``str``
            mapping key, or a value outside the vocabulary.
    """
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    if isinstance(value, FxtrID):
        return RawStrongFxtrID(value)
    if isinstance(value, EntityRef):
        return RawStrongFxtrID(value.entity_id)
    if isinstance(value, ServiceRef):
        raise StepPayloadError(
            "ServiceRefs are not durable; step payloads must be plain data"
            " (services cross into steps as handle keys, via connect)"
        )
    if isinstance(value, AbcMapping):
        items: dict[str, FrozenFxtrCbor] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise StepPayloadError(
                    f"step payload mapping keys must be str, got {type(key).__name__}"
                )
            items[key] = service_data_to_frozen_cbor(item)
        return frozendict(items)
    if isinstance(value, AbcSequence):
        return tuple(service_data_to_frozen_cbor(item) for item in value)
    raise StepPayloadError(f"{type(value).__name__} is not durable step-payload data")


def frozen_cbor_to_service_data(cbor: FrozenFxtrCbor) -> "FxtrServiceData":
    """Decode a stored step payload back to service data (pure).

    The inverse of :func:`service_data_to_frozen_cbor` up to the wire's
    canonical forms: strong refs come back as bare ``FxtrID``\\ s,
    mappings as dicts, sequences as lists. ``run_step`` serves this
    canonical round-trip on *both* the miss and hit paths, so a replayed
    result is indistinguishable from a first execution.

    Raises:
        StepPayloadError: A wrapper no payload should contain (weak /
            marker raw ids) — corrupt storage.
    """
    if isinstance(cbor, RawStrongFxtrID):
        return cbor.inner_id
    if isinstance(cbor, (RawWeakFxtrID, RawFxtrEntityMarkerID)):
        raise StepPayloadError(f"corrupt step payload: unexpected {type(cbor).__name__}")
    if cbor is None or isinstance(cbor, (bool, int, float, str, bytes)):
        return cbor
    if isinstance(cbor, AbcMapping):
        return {key: frozen_cbor_to_service_data(item) for key, item in cbor.items()}
    if isinstance(cbor, AbcSequence):
        return [frozen_cbor_to_service_data(item) for item in cbor]
    raise StepPayloadError(f"corrupt step payload: unexpected {type(cbor).__name__}")


@dataclasses.dataclass(frozen=True)
class StepPayloadEntity(DataclassEntity, fxtr_type=STEP_PAYLOAD_TYPE_ID):
    """Canonical entity form of one step payload (args mapping or result).

    The entity id doubles as the payload's fingerprint (canonical entity
    CBOR: equal payloads yield equal ids — the invocation-comparison
    half); embedded ids are strong children, so pinning this entity pins
    their closures.
    """

    payload: FrozenFxtrCbor


def store_step_payload(value: "FxtrServiceData", scope: FxtrScope | None = None) -> FxtrID:
    """Encode + store one step payload; returns its id (the fingerprint).

    ``scope`` semantics match :func:`store_config` (``None`` = active
    scope, the user-boundary case; library callers pass explicitly).

    Raises:
        StepPayloadError: Non-durable payload data (see
            :func:`service_data_to_frozen_cbor`).
    """
    if scope is None:
        scope = get_active_scope()
    entity = StepPayloadEntity(payload=service_data_to_frozen_cbor(value))
    return entity.store(scope=scope).entity_id


# -- Generation records (the at-rest form of one generation) ------------------
#
# The canonical entity form of `model.GenerationRecord`
# (``notes/at_rest_format.md``): stored at flip time, referenced by
# ``task_execs.record_id`` and the export layout's ``generations.tsv``.
# Embedded ids are strong refs, so persisting a record pins the closure of
# everything it references (results, observed fingerprints, fn handles,
# checkpoint payloads, step type entities). The entity form is
# **canonically ordered**: the set-valued fields are sorted on both
# serialize and deserialize (``failures`` keeps insertion order — it is
# meaningful), so equal fact sets yield equal fingerprints regardless of
# construction order, and a decoded record is always in canonical order.

GENERATION_RECORD_TYPE_ID = define_type(WORKFLOWS3_NS, "generation_record_v0")

_TERMINAL_STATUSES = ("complete", "errored", "interrupted")
_OBS_KIND_NAMES = ("cell_value", "wrote_cell", "grid_config")
_REQ_KIND_NAMES = ("task_output", "data")
_FAILURE_KIND_NAMES = ("error", "fault")


def canonical_generation_record(record: GenerationRecord) -> GenerationRecord:
    """The record with its set-valued fields in canonical sort order.

    ``observations`` sort by subject location then kind, ``assertions``
    by (path, config id), ``request_edges`` by location then kind,
    ``checkpoints`` by location, ``results`` by output name;
    ``failures`` are untouched (insertion order is display order).
    """
    return dataclasses.replace(
        record,
        results=frozendict(sorted(record.results.items())),
        observations=tuple(
            sorted(record.observations, key=lambda o: (o.grid_path, o.coords, o.output, o.kind))
        ),
        assertions=tuple(
            sorted(record.assertions, key=lambda a: (a.grid_path, a.config_id.id_string()))
        ),
        request_edges=tuple(
            sorted(record.request_edges, key=lambda r: (r.grid_path, r.coords, r.kind))
        ),
        checkpoints=tuple(sorted(record.checkpoints, key=lambda c: (c.grid_path, c.coords))),
    )


def _record_to_tree(record: GenerationRecord) -> dict[str, FxtrCbor]:
    """The record's tagged CBOR tree (ids as strong refs)."""
    r = canonical_generation_record(record)
    return {
        "generation": r.generation,
        "status": r.status,
        "current_fn": RawStrongFxtrID(r.current_fn),
        "executed_fn": RawStrongFxtrID(r.executed_fn),
        "results": {name: RawStrongFxtrID(vid) for name, vid in r.results.items()},
        "failures": [[f.kind, f.error_class, f.message, f.ts] for f in r.failures],
        "observations": [
            [o.kind, o.grid_path, o.coords, o.output, RawStrongFxtrID(o.fingerprint)]
            for o in r.observations
        ],
        "assertions": [[a.grid_path, RawStrongFxtrID(a.config_id)] for a in r.assertions],
        "request_edges": [[e.kind, e.grid_path, e.coords] for e in r.request_edges],
        "checkpoints": [
            [
                c.grid_path,
                c.coords,
                RawStrongFxtrID(c.step_type_id),
                RawStrongFxtrID(c.args_id),
                RawStrongFxtrID(c.value_id),
            ]
            for c in r.checkpoints
        ],
    }


def _tree_str(v: object, what: str) -> str:
    if not isinstance(v, str):
        raise EncodingError(f"bad generation record: {what} must be str, got {v!r}")
    return v


def _tree_id(v: object, what: str) -> FxtrID:
    if not isinstance(v, RawStrongFxtrID):
        raise EncodingError(f"bad generation record: {what} must be a strong id, got {v!r}")
    return v.inner_id


def _tree_row(v: object, n: int, what: str) -> AbcSequence:
    if not _is_seq(v) or len(v) != n:
        raise EncodingError(f"bad generation record: {what} row {v!r}")
    return v


def _tree_rows(v: object, what: str) -> AbcSequence:
    if not _is_seq(v):
        raise EncodingError(f"bad generation record: {what} must be a sequence, got {v!r}")
    return v


def _record_from_tree(v: object) -> GenerationRecord:
    """Decode the tagged tree; EncodingError on any bad shape/tag."""
    if not _is_map(v):
        raise EncodingError(f"bad generation record: {v!r}")
    status = _tree_str(v.get("status"), "status")
    if status not in _TERMINAL_STATUSES:
        raise EncodingError(f"bad generation record: status {status!r}")
    results_v = v.get("results")
    if not _is_map(results_v):
        raise EncodingError(f"bad generation record: results {results_v!r}")
    failures = []
    for row in _tree_rows(v.get("failures"), "failures"):
        row = _tree_row(row, 4, "failure")
        if row[0] not in _FAILURE_KIND_NAMES:
            raise EncodingError(f"bad generation record: failure kind {row[0]!r}")
        failures.append(
            FailureFact(
                kind=row[0],
                error_class=_tree_str(row[1], "failure class"),
                message=_tree_str(row[2], "failure message"),
                ts=_tree_str(row[3], "failure ts"),
            )
        )
    observations = []
    for row in _tree_rows(v.get("observations"), "observations"):
        row = _tree_row(row, 5, "observation")
        if row[0] not in _OBS_KIND_NAMES:
            raise EncodingError(f"bad generation record: observation kind {row[0]!r}")
        observations.append(
            ObservationFact(
                kind=row[0],
                grid_path=_tree_str(row[1], "observation path"),
                coords=_tree_str(row[2], "observation coords"),
                output=_tree_str(row[3], "observation output"),
                fingerprint=_tree_id(row[4], "observation fingerprint"),
            )
        )
    assertions = []
    for row in _tree_rows(v.get("assertions"), "assertions"):
        row = _tree_row(row, 2, "assertion")
        assertions.append(
            AssertionFact(
                grid_path=_tree_str(row[0], "assertion path"),
                config_id=_tree_id(row[1], "assertion config"),
            )
        )
    request_edges = []
    for row in _tree_rows(v.get("request_edges"), "request_edges"):
        row = _tree_row(row, 3, "request edge")
        if row[0] not in _REQ_KIND_NAMES:
            raise EncodingError(f"bad generation record: request kind {row[0]!r}")
        request_edges.append(
            RequestFact(
                kind=row[0],
                grid_path=_tree_str(row[1], "request path"),
                coords=_tree_str(row[2], "request coords"),
            )
        )
    checkpoints = []
    for row in _tree_rows(v.get("checkpoints"), "checkpoints"):
        row = _tree_row(row, 5, "checkpoint")
        checkpoints.append(
            CheckpointFact(
                grid_path=_tree_str(row[0], "checkpoint path"),
                coords=_tree_str(row[1], "checkpoint coords"),
                step_type_id=_tree_id(row[2], "checkpoint step type"),
                args_id=_tree_id(row[3], "checkpoint args"),
                value_id=_tree_id(row[4], "checkpoint value"),
            )
        )
    return canonical_generation_record(
        GenerationRecord(
            generation=_tree_str(v.get("generation"), "generation"),
            status=status,
            current_fn=_tree_id(v.get("current_fn"), "current_fn"),
            executed_fn=_tree_id(v.get("executed_fn"), "executed_fn"),
            results=frozendict(
                {
                    _tree_str(name, "result name"): _tree_id(vid, "result id")
                    for name, vid in results_v.items()
                }
            ),
            failures=tuple(failures),
            observations=tuple(observations),
            assertions=tuple(assertions),
            request_edges=tuple(request_edges),
            checkpoints=tuple(checkpoints),
        )
    )


class _GenerationRecordStrategy(SerializationStrategy):
    """Field strategy: GenerationRecord ↔ its tagged CBOR tree."""

    def serialize(self, value: GenerationRecord) -> dict[str, FxtrCbor]:
        """Encode for entity CBOR (canonical order applied here)."""
        return _record_to_tree(value)

    def deserialize(self, value: object) -> GenerationRecord:
        """Decode (and re-canonicalize) the tagged tree."""
        return _record_from_tree(value)


@dataclasses.dataclass(frozen=True)
class GenerationRecordEntity(DataclassEntity, fxtr_type=GENERATION_RECORD_TYPE_ID):
    """Canonical entity form of one generation record.

    The entity id is the record's fingerprint (canonical entity CBOR over
    the canonically-ordered tree: equal fact sets yield equal ids);
    embedded ids are strong children, so pinning this entity pins the
    closure of everything the generation references.
    """

    record: GenerationRecord = dataclasses.field(
        metadata={"serialization_strategy": _GenerationRecordStrategy()}
    )


def store_generation_record(record: GenerationRecord, scope: FxtrScope | None = None) -> FxtrID:
    """Store the canonical record entity; returns its id (the fingerprint).

    ``scope`` semantics match :func:`store_config` (``None`` = active
    scope, the user-boundary case; library callers pass explicitly).
    """
    if scope is None:
        scope = get_active_scope()
    return GenerationRecordEntity(record=record).store(scope=scope).entity_id


def generation_record_ids(record: GenerationRecord) -> tuple[FxtrID, ...]:
    """Every id a record embeds — its strong-ref set, in field order.

    The extraction-contract view of one record (``notes/at_rest_format.md``):
    persisting the record entity pins exactly these ids' closures, and the
    flip-time choreography re-attaches each before storing (they are all
    individually pinned by the liveness invariant — recorded ids are
    persisted first — so the re-attach is total).
    """
    ids = [record.current_fn, record.executed_fn, *record.results.values()]
    ids.extend(o.fingerprint for o in record.observations)
    ids.extend(a.config_id for a in record.assertions)
    for c in record.checkpoints:
        ids.extend((c.step_type_id, c.args_id, c.value_id))
    return tuple(ids)
