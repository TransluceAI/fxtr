"""Canonical frame-value encoding: the identity form of dataframe cells.

The whole dataframe design — the three representations (canonical entity /
arrow transport / the runtime ``polars.DataFrame``), the column model, and
the rationale for every rule — is ``notes/dataframes.md``; this module owns the
byte-level identity spec stated there:

- A **frame** is a schema plus a row multiset. Its canonical entity holds
  the tagged schema (`encoding.schema_to_json` — the entity is
  self-describing) and the rows as arrays in schema column order, **sorted
  lexicographically by their canonical per-row encoded bytes**. The sort
  key derives from the encoding itself, so no per-kind total order exists
  anywhere; the resulting order is semantically arbitrary and never
  carries meaning. An empty frame (zero rows) is a valid value.
- The per-row encoding (:func:`encode_row`) is one canonical-CBOR array
  per row, produced by ``entity_core.fxtr_cbor_dumps`` — the *same*
  encoder the entity layer runs — so the standalone sort-key bytes and
  the in-entity row representation agree by construction (canonical CBOR
  is compositional; pinned by tests).
- Scalar values are validated **exact-type** (``type(v) is int`` — bool
  is rejected as an int, matching the coordinate convention in
  `encoding.py`): ``str``/``bytes``/``bool`` verbatim; ``int`` pinned to
  int64 range (arrow interop); ``float`` encoded per RFC 8949
  shortest-form with every NaN normalized to the single canonical NaN
  and ``-0.0`` preserved as distinct from ``0.0`` (cbor2's canonical
  mode implements this; the conformance tests make it spec, never codec
  behavior); ``None`` legal only in declared-nullable columns.

Frames are **leaf entities**: scalar-only columns mean no FxtrID can
appear anywhere in the tree, so closures are trivial and frame payloads
move through the system as opaque bytes. :class:`FrameEntity` is
deliberately a dumb carrier over pre-canonicalized raw CBOR (the
``StepPayloadEntity`` shape), not a model-object wrapper: the encoding is
*normalizing* (the row sort), so canonicalization lives in the plain
builders here (validate → sort → construct). Schema *name* validation is
`model.validate_frame_schema`, with the schema types; this module
validates *values*.

The one place the runtime table object does meet this module is
:func:`canonical_frame_from_ipc` — the funnel **every** identity-minting
path shares (cell writes on both capabilities, the executor's
frame-output canonicalization). It lives here, with the identity spec,
rather than in the frame runtime: minting identity is this module's job,
and `tables.py` is task-side (a guest never canonicalizes — the declared
schema is the host's authority; see `task_adapter.py`'s module
docstring). Its `tables.py` import is the only polars reach in this
module, and is function-local like every other frame-op call site, so
importing this module stays polars-free.
"""

from __future__ import annotations

import dataclasses
from typing import Iterable, Sequence, cast

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID, fxtr_cbor_dumps
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, get_active_scope
from fxtr2.data.system_entities import define_type

from fxtr_experiment_task._ns import WORKFLOWS3_NS
from fxtr_experiment.encoding import schema_from_json, schema_to_json
from fxtr_experiment_task.errors import EncodingError
from fxtr_experiment_task.model import (
    ColumnSpec,
    FrameRow,
    FrameSchema,
    TableValue,
    validate_frame_schema,
)

FRAME_TYPE_ID = define_type(WORKFLOWS3_NS, "frame_v0")

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_KIND_TYPES = {"str": str, "int": int, "float": float, "bool": bool, "bytes": bytes}


def _check_value(col: ColumnSpec, v: TableValue) -> None:
    """Validate one value against its column (exact-type; null legality).

    Raises:
        EncodingError: On a type mismatch, an illegal null, or an
            out-of-int64-range int.
    """
    if v is None:
        if not col.nullable:
            raise EncodingError(f"null in non-nullable column {col.name!r}")
        return
    # Exact type — bool is an int subclass, so identity code compares
    # types, never isinstance (the coordinate convention).
    if type(v) is not _KIND_TYPES[col.kind]:
        raise EncodingError(f"column {col.name!r} ({col.kind}) got {type(v).__name__} value {v!r}")
    if col.kind == "int":
        assert type(v) is int
        if not (INT64_MIN <= v <= INT64_MAX):
            raise EncodingError(f"column {col.name!r}: int {v!r} outside int64 range")


def encode_row(schema: FrameSchema, row: Sequence[TableValue]) -> bytes:
    """Canonically encode one row: the sort key and the identity unit.

    One canonical-CBOR array, values in schema column order, produced by
    the entity layer's own encoder (``fxtr_cbor_dumps``) so the
    standalone bytes match the row's in-entity representation exactly.

    Args:
        schema: The declared schema the row must match.
        row: One value per column, in schema order.

    Returns:
        The row's canonical bytes.

    Raises:
        EncodingError: On an arity mismatch or an invalid value
            (:func:`_check_value`).
    """
    if len(row) != len(schema):
        raise EncodingError(f"row has {len(row)} values; schema has {len(schema)} columns")
    for col, v in zip(schema, row):
        _check_value(col, v)
    return fxtr_cbor_dumps(list(row))


@dataclasses.dataclass(frozen=True)
class FrameEntity(DataclassEntity, fxtr_type=FRAME_TYPE_ID):
    """Canonical entity form of one frame value; its id is the fingerprint.

    A dumb carrier (the ``StepPayloadEntity`` shape): both fields are raw
    frozen CBOR, already canonicalized by :func:`build_frame_entity` —
    ``schema`` the tagged schema tree, ``rows`` the row arrays in
    canonical (sorted) order. Never construct directly: the builders own
    validation and the sort, so a hand-built instance can violate the
    canonical-order invariant and fingerprint as a different value.
    """

    schema: FrozenFxtrCbor
    rows: FrozenFxtrCbor


def build_frame_entity(schema: FrameSchema, rows: Iterable[FrameRow]) -> FrameEntity:
    """Validate and canonicalize a frame into its entity form (pure).

    Args:
        schema: The frame's schema (validated against the column-name
            law here too — belt over `encoding.store_config`'s braces).
        rows: The row multiset, in any order (duplicates preserved).

    Returns:
        The canonical entity: rows sorted lexicographically by their
        :func:`encode_row` bytes. Store it (or hand it to a persist
        path) to obtain the frame's fingerprint id.

    Raises:
        EncodingError: On an invalid schema, row arity mismatch, or
            invalid value.
    """
    validate_frame_schema(schema)
    keyed = [(encode_row(schema, row), tuple(row)) for row in rows]
    # Sort on the encoded bytes ALONE. The bytes determine the row (up to
    # NaN payloads, which the stored form deliberately collapses), so
    # byte-equal rows are interchangeable and no tie-break exists; and
    # identity code must not lean on Python value ordering anyway (None
    # and mixed types don't have one — the spec's no-semantic-total-order
    # principle, applied to the implementation).
    keyed.sort(key=lambda kr: kr[0])
    # The tagged column form is a list of scalars by construction
    # (_column_to_json); the cast just re-states that for the freeze.
    tagged = cast("list[list[FrozenFxtrCbor]]", schema_to_json(schema))
    return FrameEntity(
        schema=tuple(tuple(col) for col in tagged),
        rows=tuple(row for _, row in keyed),
    )


def canonical_frame_from_ipc(data: bytes, schema: FrameSchema) -> FrameEntity:
    """The whole write boundary in one body: wire bytes → canonical entity.

    Every path that mints frame identity funnels here — the run
    capability's ``write_cell_frame``, the user capability's, and the
    executor's frame-output canonicalization (results loop and safety
    pass) — so the validation story and the identity derivation cannot
    drift between surfaces: parse the arrow IPC stream, validate/align
    against the *declared* schema (`tables.validate_and_align`), and build
    the canonical sorted-rows entity (:func:`build_frame_entity`).

    Host-side only, by doctrine: a guest never canonicalizes (module
    docstring). Hence the local `tables.py` import — the frame runtime is
    task-side, and this direction (identity → runtime) is the legal one.

    Args:
        data: The wire frame form (arrow IPC stream bytes, any row order).
        schema: The target's declared schema (grid config / ``FrameOutput``)
            — the identity-bearing schema the entity embeds.

    Returns:
        The canonical (unstored) frame entity; ``.store(...)`` yields the
        fingerprint id.

    Raises:
        EncodingError: On any validation failure (unparseable payload,
            name set, exact dtypes, null legality, value checks) — the one
            exception type callers translate to their surface's error.
    """
    from fxtr_experiment_task import tables

    try:
        df = tables.ipc_to_frame(data)
    except Exception as exc:
        # polars raises its own hierarchy on garbage bytes; the wire payload
        # is caller-supplied data, so normalize to the validation error.
        raise EncodingError(f"unparseable arrow IPC frame payload: {exc}") from exc
    return build_frame_entity(schema, tables.frame_to_rows(tables.validate_and_align(df, schema)))


def store_frame(
    schema: FrameSchema, rows: Iterable[FrameRow], scope: FxtrScope | None = None
) -> FxtrID:
    """Build and store one frame; returns its fingerprint id.

    ``scope`` semantics match `encoding.store_config` (``None`` = active
    scope, the user-boundary case; library callers pass explicitly).

    Raises:
        EncodingError: As :func:`build_frame_entity`.
    """
    if scope is None:
        scope = get_active_scope()
    return build_frame_entity(schema, rows).store(scope=scope).entity_id


async def load_frame(
    frame_id: FxtrID, scope: FxtrScope | None = None, *, verify: bool = True
) -> tuple[FrameSchema, tuple[FrameRow, ...]]:
    """Load one frame entity back to (schema, rows).

    The read half of the identity spec. With ``verify`` (the default), it
    validates every row (arity, exact types, null legality) and **verifies
    canonical row order** by re-encoding — a stored frame whose rows are
    unsorted (or otherwise invalid) is refused rather than silently served.
    That is the right posture at trust boundaries (tests, tooling,
    verifying a *foreign* store), and the wrong tax on the hot path:
    ``verify=False`` is the trusting decode for serving paths over the
    session's own store (projection rebuilds) — every frame id recorded in
    the state db was minted host-side by :func:`build_frame_entity`, and
    the system trusts its own store on read everywhere else (configs, step
    payloads, entity cells).

    Args:
        frame_id: The frame entity's id. Must be loadable in ``scope``.
        scope: Scope the entity is loadable in (``None`` = active scope,
            the user-boundary case).
        verify: Fully validate values and canonical order (see above).

    Returns:
        The declared schema and the rows in canonical order.

    Raises:
        EncodingError: Wrong entity type or a malformed schema/row tree;
            with ``verify``, also invalid values or non-canonical order.
    """
    if scope is None:
        scope = get_active_scope()
    # Loadable-in-scope is the caller's precondition (docstring above);
    # typed load enforces the "$fxtr.type" tag for us.
    entity = await scope.load(EntityRef(frame_id, FrameEntity))
    schema = schema_from_json(entity.schema)
    rows_raw = entity.rows
    if not isinstance(rows_raw, Sequence) or isinstance(rows_raw, (str, bytes)):
        raise EncodingError(f"corrupt frame entity: rows is {type(rows_raw).__name__}")
    rows: list[FrameRow] = []
    prev_key: bytes | None = None
    for r in rows_raw:
        if not isinstance(r, Sequence) or isinstance(r, (str, bytes)):
            raise EncodingError(f"corrupt frame entity: row is {type(r).__name__}")
        typed_row = cast(FrameRow, tuple(r))
        if verify:
            for v in typed_row:
                if v is not None and type(v) not in (str, int, float, bool, bytes):
                    raise EncodingError(f"corrupt frame entity: non-scalar value {v!r}")
            key = encode_row(schema, typed_row)  # also validates types vs the schema
            if prev_key is not None and key < prev_key:
                raise EncodingError("corrupt frame entity: rows not in canonical order")
            prev_key = key
        rows.append(typed_row)
    return schema, tuple(rows)
