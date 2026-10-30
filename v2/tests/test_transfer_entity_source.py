"""Tests for TransferEntitySource (lease-backed, two-set design under STATIC_LIFETIME)."""

import pytest

from fxtr2.data.abstract_arena import DepKind, FxtrEntityFlatData
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.remote.transfer_entity_source import TransferEntitySource
from fxtr2.leases.local_lease import STATIC_LIFETIME


def _lease():
    return STATIC_LIFETIME.lease()


# ---------------------------------------------------------------------------
# token / lease
# ---------------------------------------------------------------------------


def test_token_is_static_lifetime() -> None:
    arena = TransferEntitySource({})
    assert arena.token is STATIC_LIFETIME.token


# ---------------------------------------------------------------------------
# is_tracked
# ---------------------------------------------------------------------------


def test_default_tracked_set_is_all_entity_keys() -> None:
    eid = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    entity = FxtrEntityFlatData(entity_id=eid, entity_bytes=b"\x01", deps=[])
    arena = TransferEntitySource({eid: entity})
    assert arena.is_tracked(_lease(), eid)


def test_is_tracked_returns_false_for_unknown_id() -> None:
    arena = TransferEntitySource({})
    missing = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    assert not arena.is_tracked(_lease(), missing)


def test_explicit_tracked_set_can_be_narrower_than_entities() -> None:
    eid1 = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    eid2 = FxtrID.from_tag_and_payload("d", (2).to_bytes(20, "big"))
    e1 = FxtrEntityFlatData(entity_id=eid1, entity_bytes=b"\x01", deps=[])
    e2 = FxtrEntityFlatData(entity_id=eid2, entity_bytes=b"\x02", deps=[])
    arena = TransferEntitySource({eid1: e1, eid2: e2}, tracked=[eid1])
    assert arena.is_tracked(_lease(), eid1)
    assert not arena.is_tracked(_lease(), eid2)


def test_builtin_tracked_only_if_in_tracked_set() -> None:
    ground = FxtrID.from_tag_and_payload(".", (0).to_bytes(20, "big"))
    arena_without = TransferEntitySource({})
    assert not arena_without.is_tracked(_lease(), ground)
    arena_with = TransferEntitySource({}, tracked=[ground])
    assert arena_with.is_tracked(_lease(), ground)


# ---------------------------------------------------------------------------
# load_flat
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_load_flat_returns_entity() -> None:
    eid = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    entity = FxtrEntityFlatData(entity_id=eid, entity_bytes=b"\x01", deps=[])
    arena = TransferEntitySource({eid: entity})
    loaded = await arena.load_flat(_lease(), eid)
    assert loaded is entity


@pytest.mark.anyio
async def test_load_flat_requires_tracked() -> None:
    eid1 = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    eid2 = FxtrID.from_tag_and_payload("d", (2).to_bytes(20, "big"))
    e1 = FxtrEntityFlatData(entity_id=eid1, entity_bytes=b"\x01", deps=[])
    e2 = FxtrEntityFlatData(entity_id=eid2, entity_bytes=b"\x02", deps=[])
    # eid2 has data but is NOT tracked: load_flat must refuse.
    arena = TransferEntitySource({eid1: e1, eid2: e2}, tracked=[eid1])
    with pytest.raises(ValueError, match="not tracked"):
        await arena.load_flat(_lease(), eid2)


@pytest.mark.anyio
async def test_load_flat_raises_when_closed() -> None:
    eid = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    entity = FxtrEntityFlatData(entity_id=eid, entity_bytes=b"\x01", deps=[])
    arena = TransferEntitySource({eid: entity})
    arena.close()
    with pytest.raises(RuntimeError, match="closed"):
        await arena.load_flat(_lease(), eid)


@pytest.mark.anyio
async def test_load_flat_synthesizes_builtin_when_tracked() -> None:
    builtin_id = FxtrID.from_tag_and_payload(".", (0).to_bytes(20, "big"))
    arena = TransferEntitySource({}, tracked=[builtin_id])
    result = await arena.load_flat(_lease(), builtin_id)
    assert result.entity_id == builtin_id
    assert result.entity_bytes is None
    assert result.deps == []


@pytest.mark.anyio
async def test_load_flat_adds_deps_to_tracked_set() -> None:
    leaf_id = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    root_id = FxtrID.from_tag_and_payload("d", (2).to_bytes(20, "big"))
    leaf = FxtrEntityFlatData(entity_id=leaf_id, entity_bytes=b"\x01", deps=[])
    root = FxtrEntityFlatData(
        entity_id=root_id,
        entity_bytes=b"\x02",
        deps=[(leaf_id, DepKind.STRONG_REF)],
    )
    arena = TransferEntitySource({root_id: root, leaf_id: leaf}, tracked=[root_id])
    # Before loading, leaf is known in the data dict but not tracked.
    assert arena.is_tracked(_lease(), root_id)
    assert not arena.is_tracked(_lease(), leaf_id)
    await arena.load_flat(_lease(), root_id)
    # Loading the root tracks the leaf as a side effect.
    assert arena.is_tracked(_lease(), leaf_id)
    # Now load_flat on the leaf succeeds.
    await arena.load_flat(_lease(), leaf_id)


# ---------------------------------------------------------------------------
# copy_to_new_arena
# ---------------------------------------------------------------------------


def test_copy_shares_data_dict_and_narrows_tracked_set() -> None:
    eid1 = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    eid2 = FxtrID.from_tag_and_payload("d", (2).to_bytes(20, "big"))
    e1 = FxtrEntityFlatData(entity_id=eid1, entity_bytes=b"\x01", deps=[])
    e2 = FxtrEntityFlatData(entity_id=eid2, entity_bytes=b"\x02", deps=[])
    arena = TransferEntitySource({eid1: e1, eid2: e2})
    sub = arena.copy_to_new_arena(_lease(), [eid1])
    assert sub.is_tracked(_lease(), eid1)
    assert not sub.is_tracked(_lease(), eid2)


@pytest.mark.anyio
async def test_copy_can_load_deps_via_shared_data() -> None:
    """A narrowed copy can reach deps because the data dict is shared — loading
    the root adds deps to the copy's tracked set."""
    leaf_id = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    root_id = FxtrID.from_tag_and_payload("d", (2).to_bytes(20, "big"))
    leaf = FxtrEntityFlatData(entity_id=leaf_id, entity_bytes=b"\x01", deps=[])
    root = FxtrEntityFlatData(
        entity_id=root_id,
        entity_bytes=b"\x02",
        deps=[(leaf_id, DepKind.STRONG_REF)],
    )
    arena = TransferEntitySource({root_id: root, leaf_id: leaf})
    sub = arena.copy_to_new_arena(_lease(), [root_id])
    # Leaf is in the shared data dict but not yet in sub's tracked set.
    assert not sub.is_tracked(_lease(), leaf_id)
    loaded_root = await sub.load_flat(_lease(), root_id)
    for dep_id, _ in loaded_root.deps:
        loaded_dep = await sub.load_flat(_lease(), dep_id)
        assert loaded_dep.entity_id == dep_id


def test_copy_is_cheap_over_transitive_closure() -> None:
    """Previously copy_to_new_arena walked the transitive closure; now it's O(|ids|)
    because the data dict is shared by reference."""
    # Populate a small graph; the test merely asserts copy_to_new_arena doesn't
    # raise when the requested ids reference deps that aren't on its tracked set.
    leaf_id = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    root_id = FxtrID.from_tag_and_payload("d", (2).to_bytes(20, "big"))
    leaf = FxtrEntityFlatData(entity_id=leaf_id, entity_bytes=b"\x01", deps=[])
    root = FxtrEntityFlatData(
        entity_id=root_id,
        entity_bytes=b"\x02",
        deps=[(leaf_id, DepKind.STRONG_REF)],
    )
    arena = TransferEntitySource({root_id: root, leaf_id: leaf})
    sub = arena.copy_to_new_arena(_lease(), [root_id])
    # Sub only tracks root initially; the data dict still contains leaf for when
    # a subsequent load_flat(root) adds leaf to the tracked set.
    assert sub.is_tracked(_lease(), root_id)
    assert not sub.is_tracked(_lease(), leaf_id)


def test_copy_multiple_roots() -> None:
    eid_a = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    eid_b = FxtrID.from_tag_and_payload("d", (2).to_bytes(20, "big"))
    e_a = FxtrEntityFlatData(entity_id=eid_a, entity_bytes=b"\x01", deps=[])
    e_b = FxtrEntityFlatData(entity_id=eid_b, entity_bytes=b"\x02", deps=[])
    arena = TransferEntitySource({eid_a: e_a, eid_b: e_b})
    sub = arena.copy_to_new_arena(_lease(), [eid_a, eid_b])
    assert sub.is_tracked(_lease(), eid_a)
    assert sub.is_tracked(_lease(), eid_b)


def test_sibling_copies_are_independent_after_close() -> None:
    eid = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    entity = FxtrEntityFlatData(entity_id=eid, entity_bytes=b"\x01", deps=[])
    arena = TransferEntitySource({eid: entity})
    sub = arena.copy_to_new_arena(_lease(), [eid])
    arena.close()
    # sub still has access to the shared data dict (it holds its own reference).
    assert sub.is_tracked(_lease(), eid)


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def test_close_clears_tracked_set() -> None:
    eid = FxtrID.from_tag_and_payload("d", (1).to_bytes(20, "big"))
    entity = FxtrEntityFlatData(entity_id=eid, entity_bytes=b"\x01", deps=[])
    arena = TransferEntitySource({eid: entity})
    arena.close()
    assert not arena.is_tracked(_lease(), eid)
