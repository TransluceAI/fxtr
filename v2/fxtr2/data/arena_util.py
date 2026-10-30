"""Utility functions for arena operations.

Contains shared visitor functions for traversing and transforming entity data. These functions
are used by arena implementations to:

- Collect all IDs referenced by an entity (for tracking dependencies)
- Transform IDs when unwrapping provenance nodes or wrapping data with provenance
- Ensure operations happen in canonical CBOR order (important for p-node index paths)

The "canonical CBOR order" versions are needed because p-node paths use indices into the
serialization order, so we must process IDs in the exact order they appear in the wire format.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypedDict

import cbor2

from fxtr2.data import entity_core
from fxtr2.data.abstract_arena import DepKind, FxtrEntityFlatData
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawFxtrEntityMissingMarkerSentinel,
    RawStrongFxtrID,
    RawWeakFxtrID,
)

if TYPE_CHECKING:
    from fxtr2.data.abstract_arena import ReadableArena
    from fxtr2.leases.lease_types import Lease


def visit_ids(
    root: FxtrCbor,
    callback: Callable[[RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID], Any],
) -> None:
    """Visit all IDs in an entity.

    Args:
        root: The entity data to traverse.
        callback: Function called for each ID found.
    """
    match root:
        case RawWeakFxtrID() | RawStrongFxtrID() | RawFxtrEntityMarkerID():
            callback(root)
        case RawFxtrEntityMissingMarkerSentinel():
            # Sentinel default values are transient placeholders for an
            # entity's $fxtr.ctx that haven't been replaced yet. They
            # carry no IDs and are rejected by the byte-level encoder
            # if they survive past the storage path. Skip here.
            pass
        case list():
            for item in root:
                visit_ids(item, callback)
        case dict():
            for value in root.values():
                visit_ids(value, callback)
        case None | bool() | int() | float() | str() | bytes():
            pass
        case _:
            raise ValueError(f"Unsupported type: {type(root)}")


def visit_and_project_ids(
    root: FxtrCbor,
    callback: Callable[[RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID], FxtrID],
) -> FxtrCbor:
    """Visit all IDs in an entity and project them to new ID values.

    Args:
        root: The entity data to traverse.
        callback: Function that takes a typed ID and returns a new FxtrID.

    Returns:
        The transformed entity data with new IDs.
    """
    match root:
        case RawWeakFxtrID() | RawStrongFxtrID() | RawFxtrEntityMarkerID():
            proj = callback(root)
            return type(root)(proj)
        case RawFxtrEntityMissingMarkerSentinel():
            # See visit_ids: sentinels are transient leaves with no IDs
            # to project. Pass through untouched.
            return root
        case list():
            return [visit_and_project_ids(item, callback) for item in root]
        case dict():
            return {k: visit_and_project_ids(v, callback) for k, v in root.items()}
        case None | bool() | int() | float() | str() | bytes():
            return root
        case _:
            raise ValueError(f"Unsupported type: {type(root)}")


def visit_and_project_ids_in_canonical_cbor_order(
    root: FxtrCbor,
    callback: Callable[[RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID], FxtrID],
) -> FxtrCbor:
    """Visit all IDs in the order they would be serialized in canonical CBOR, and project them.

    Primarily used to unwrap p-nodes where child index depends on serialization order.

    Args:
        root: The entity data to traverse.
        callback: Function that takes a typed ID and returns a new FxtrID.

    Returns:
        The transformed entity data with new IDs.
    """
    as_bytes = entity_core.fxtr_cbor_dumps(root)

    def proj_decode_hook(decoder: Any, tag: Any, shareable_index: Any = None) -> Any:
        assert shareable_index is None
        match tag.tag:
            case 33000:
                assert isinstance(tag.value, bytes)
                inner_id = FxtrID(raw_data_id=tag.value, provenance_parent=None)
                return RawFxtrEntityMarkerID(callback(RawFxtrEntityMarkerID(inner_id=inner_id)))
            case 33001:
                assert isinstance(tag.value, bytes)
                inner_id = FxtrID(raw_data_id=tag.value, provenance_parent=None)
                return RawWeakFxtrID(callback(RawWeakFxtrID(inner_id=inner_id)))
            case 33002:
                assert isinstance(tag.value, bytes)
                inner_id = FxtrID(raw_data_id=tag.value, provenance_parent=None)
                return RawStrongFxtrID(callback(RawStrongFxtrID(inner_id=inner_id)))
            case _:
                raise TypeError(f"Unknown tag: {tag.tag}")

    return cbor2.loads(as_bytes, tag_hook=proj_decode_hook)


async def async_visit_and_project_ids_in_canonical_cbor_order(
    root: FxtrCbor,
    callback: Callable[
        [RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID], Awaitable[FxtrID]
    ],
) -> FxtrCbor:
    """Async version: Visit all IDs in canonical CBOR order and project them.

    Used for p/u node loading where child creation may require async DB operations.

    Args:
        root: The entity data to traverse.
        callback: Async function that takes a typed ID and returns a new FxtrID.

    Returns:
        The transformed entity data with new IDs.
    """
    # Serialize to get canonical order
    as_bytes = entity_core.fxtr_cbor_dumps(root)

    # First pass: collect all IDs in canonical order
    ids_in_order: list[RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID] = []

    def collect_decode_hook(decoder: Any, tag: Any, shareable_index: Any = None) -> Any:
        assert shareable_index is None
        match tag.tag:
            case 33000:
                assert isinstance(tag.value, bytes)
                inner_id = FxtrID(raw_data_id=tag.value, provenance_parent=None)
                typed_id = RawFxtrEntityMarkerID(inner_id=inner_id)
                ids_in_order.append(typed_id)
                return typed_id
            case 33001:
                assert isinstance(tag.value, bytes)
                inner_id = FxtrID(raw_data_id=tag.value, provenance_parent=None)
                typed_id = RawWeakFxtrID(inner_id=inner_id)
                ids_in_order.append(typed_id)
                return typed_id
            case 33002:
                assert isinstance(tag.value, bytes)
                inner_id = FxtrID(raw_data_id=tag.value, provenance_parent=None)
                typed_id = RawStrongFxtrID(inner_id=inner_id)
                ids_in_order.append(typed_id)
                return typed_id
            case _:
                raise TypeError(f"Unknown tag: {tag.tag}")

    cbor2.loads(as_bytes, tag_hook=collect_decode_hook)

    # Second pass: call async callback for each ID (in order)
    projected_ids: list[FxtrID] = []
    for typed_id in ids_in_order:
        projected_ids.append(await callback(typed_id))

    # Third pass: reconstruct with projected IDs
    projection_iter = iter(projected_ids)

    def proj_decode_hook(decoder: Any, tag: Any, shareable_index: Any = None) -> Any:
        assert shareable_index is None
        proj_id = next(projection_iter)
        match tag.tag:
            case 33000:
                return RawFxtrEntityMarkerID(inner_id=proj_id)
            case 33001:
                return RawWeakFxtrID(inner_id=proj_id)
            case 33002:
                return RawStrongFxtrID(inner_id=proj_id)
            case _:
                raise TypeError(f"Unknown tag: {tag.tag}")

    return cbor2.loads(as_bytes, tag_hook=proj_decode_hook)


# ----------------------------------------------------------------------
# Precomp closure walk
# ----------------------------------------------------------------------


class PrecompEntry(TypedDict, total=False):
    """One entry in a precomp closure map.

    Each key holds the ``id_string()`` of the relationship's target.
    Keys are omitted when inapplicable to the node's tag (e.g. ``ctx_node``
    is only set for ``c`` and ``p`` nodes), and ``unwraps_to`` is omitted
    when the node has no known unwrap target.
    """

    unwraps_to: str
    ctx_node: str
    backstep: str


PrecompMap = dict[str, PrecompEntry]
"""Transitively-closed precomp closure: ``id_string`` → ``PrecompEntry``."""


def derive_precomp_entry_from_deps(flat: "FxtrEntityFlatData") -> PrecompEntry:
    """Project ``flat.deps`` into the corresponding ``PrecompEntry``.

    Maps the dep kinds that carry precomp relationships:

    - ``DepKind.UNWRAPS_TO`` → ``unwraps_to``
    - ``DepKind.CTX_NODE``   → ``ctx_node``
    - ``DepKind.BACKSTEP``   → ``backstep``

    Other dep kinds (``STRONG_REF`` / ``WEAK_REF``) are ignored. Returns
    an empty dict when the entity has no precomp-relevant deps (e.g.
    plain d-nodes).
    """
    entry: PrecompEntry = {}
    for dep_id, dep_kind in flat.deps:
        if dep_kind is DepKind.UNWRAPS_TO:
            entry["unwraps_to"] = dep_id.id_string()
        elif dep_kind is DepKind.CTX_NODE:
            entry["ctx_node"] = dep_id.id_string()
        elif dep_kind is DepKind.BACKSTEP:
            entry["backstep"] = dep_id.id_string()
    return entry


def compute_precomp_closure(
    arena: "ReadableArena[Any]",
    lease: "Lease[Any]",
    seed_ids: list[FxtrID],
) -> PrecompMap:
    """Compute transitively-closed precomp map for a set of seed IDs.

    For each seed ID (and any IDs discovered as values in precomp
    relationships), compute ``unwraps_to``, ``ctx_node``, and
    ``backstep`` against the supplied arena. The result is a dict
    mapping ``id_string`` to ``PrecompEntry``, with empty entries and
    inapplicable relationships omitted.

    Sync; the arena's leased precomp methods are sync. The lease is
    cloned for each arena access so the caller's lease is left
    untouched.
    """
    precomp: PrecompMap = {}
    frontier: deque[FxtrID] = deque(seed_ids)
    visited: set[FxtrID] = set()

    with lease.take() as lease:
        while frontier:
            node_id = frontier.popleft()
            if node_id in visited:
                continue
            visited.add(node_id)

            entry: PrecompEntry = {}
            tag = node_id.get_tag()

            # Only provenance nodes (c/p/u/n) can be unwrapped
            if tag in ("c", "p", "u", "n"):
                unwraps_to = arena.unwrap_provenance(lease.clone(), node_id)
                if unwraps_to is not None:
                    entry["unwraps_to"] = unwraps_to.id_string()
                    frontier.append(unwraps_to)

            # c nodes have ctx_node, p nodes have pathroot (both stored as ctx_node)
            if tag == "c":
                ctx = arena.c_node_to_context_data(lease.clone(), node_id)
                entry["ctx_node"] = ctx.id_string()
                frontier.append(ctx)
            elif tag == "p":
                ctx = arena.p_node_to_pathroot(lease.clone(), node_id)
                entry["ctx_node"] = ctx.id_string()
                frontier.append(ctx)

                backstep = arena.step_back_p_ctx(lease.clone(), node_id)
                if backstep is not None:
                    entry["backstep"] = backstep.id_string()
                    frontier.append(backstep)

            if entry:
                precomp[node_id.id_string()] = entry

    return precomp
