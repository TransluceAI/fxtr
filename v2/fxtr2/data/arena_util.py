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

import enum
from typing import Any, Awaitable, Callable

import cbor2

from fxtr2.data import entity_core
from fxtr2.data.abstract_arena import ReadableArena
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)


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


class DepKind(enum.Enum):
    """Kind of dependency relationship between entities."""

    STRONG_REF = "strong_ref"
    WEAK_REF = "weak_ref"
    CTX_NODE = "ctx_node"
    BACKSTEP = "backstep"
    UNWRAPS_TO = "unwraps_to"


async def fetch_data_and_deps_from_arena(
    fxtr_id: FxtrID, source: ReadableArena
) -> tuple[bytes | None, list[tuple[FxtrID, DepKind]]]:
    """Fetch data and dependencies from an external arena.

    Args:
        fxtr_id: The FxtrID of the entity to fetch data and dependencies from.
        source: The arena to fetch the entity from.

    Returns:
        (cbor_bytes, deps) where:
        - cbor_bytes: Serialized CBOR data for d/n nodes, or None for derived nodes
        - deps: List of (FxtrID, DepKind) tuples. Deduplicated so each (id, kind) pair
          appears at most once, even if the entity contains multiple references to
          the same child with the same link type.
    """
    tag = fxtr_id.get_tag()
    deps: list[tuple[FxtrID, DepKind]] = []
    seen: set[tuple[FxtrID, DepKind]] = set()

    def add_dep(dep_id: FxtrID, kind: DepKind) -> None:
        """Add a dependency if not already seen (deduplicates by (id, kind) pair)."""
        key = (dep_id, kind)
        if key not in seen:
            seen.add(key)
            deps.append(key)

    match tag:
        case "." | "!":
            # Builtin nodes have no data and no deps
            return None, []

        case "d":
            # Data node: load, serialize, extract child refs
            data = await source.load(fxtr_id)
            cbor_bytes = entity_core.fxtr_cbor_dumps_with_marker_check(data)

            def collect_deps(
                typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
            ):
                if isinstance(typed_id, RawStrongFxtrID):
                    add_dep(typed_id.inner_id, DepKind.STRONG_REF)
                else:
                    add_dep(typed_id.inner_id, DepKind.WEAK_REF)

            visit_ids(data, collect_deps)
            return cbor_bytes, deps

        case "n":
            # Normal provenance node: like d-node + UNWRAPS_TO
            data = await source.load(fxtr_id)
            cbor_bytes = entity_core.fxtr_cbor_dumps_with_marker_check(data)

            def collect_deps(
                typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
            ):
                if isinstance(typed_id, RawStrongFxtrID):
                    add_dep(typed_id.inner_id, DepKind.STRONG_REF)
                else:
                    add_dep(typed_id.inner_id, DepKind.WEAK_REF)

            visit_ids(data, collect_deps)

            # Add UNWRAPS_TO dep
            unwraps_to = source.unwrap_provenance(fxtr_id)
            assert unwraps_to is not None
            add_dep(unwraps_to, DepKind.UNWRAPS_TO)
            return cbor_bytes, deps

        case "c":
            # Context provenance node: CTX_NODE + UNWRAPS_TO
            ctx_data_id = source.c_node_to_context_data(fxtr_id)
            add_dep(ctx_data_id, DepKind.CTX_NODE)

            unwraps_to = source.unwrap_provenance(fxtr_id)
            assert unwraps_to is not None
            add_dep(unwraps_to, DepKind.UNWRAPS_TO)
            return None, deps

        case "p":
            # Path provenance node: CTX_NODE + UNWRAPS_TO + optional BACKSTEP
            pathroot_id = source.p_node_to_pathroot(fxtr_id)
            add_dep(pathroot_id, DepKind.CTX_NODE)

            unwraps_to = source.unwrap_provenance(fxtr_id)
            assert unwraps_to is not None
            add_dep(unwraps_to, DepKind.UNWRAPS_TO)

            backstep_id = source.step_back_p_ctx(fxtr_id)
            if backstep_id is not None:
                add_dep(backstep_id, DepKind.BACKSTEP)
            return None, deps

        case "u":
            # Untracked provenance node: just UNWRAPS_TO
            unwraps_to = source.unwrap_provenance(fxtr_id)
            assert unwraps_to is not None
            add_dep(unwraps_to, DepKind.UNWRAPS_TO)
            return None, deps

        case _:
            raise ValueError(f"Unknown node tag: {tag}")
