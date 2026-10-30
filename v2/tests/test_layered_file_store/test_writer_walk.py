"""``pin_to_this_layer`` walk semantics: transitive walks, dedup against
parent layers (data + precomp), unreachable roots error."""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent, StubDeepFetchable


@pytest.mark.anyio
async def test_walk_writes_transitive_deps(tmp_path: Path):
    """``pin_to_this_layer`` writes both the pinned entity and every
    referenced entity in this layer (since the parents list is empty)."""
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        leaf_ref = scope.store(LfsLeaf(name="leaf", value=7))
        parent_ref = scope.store(LfsParent(name="p", child=leaf_ref))
        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("parent", parent_ref.entity_id, scope)

    assert fmt.data_file_exists(layer, parent_ref.entity_id)
    assert fmt.data_file_exists(layer, leaf_ref.entity_id)


@pytest.mark.anyio
async def test_walk_skips_data_in_parent(tmp_path: Path):
    """A dep that already has a data file in a parent layer is not
    redundantly stored in the child."""
    base = tmp_path / "base"
    child = tmp_path / "child"
    async with FxtrScope(name="src") as scope:
        leaf_ref = scope.store(LfsLeaf(name="leaf", value=2))
        parent_ref = scope.store(LfsParent(name="p", child=leaf_ref))

        async with LayeredFileStoreIsolatedWriter(base, parents=[]) as w:
            await w.pin_to_this_layer("leaf", leaf_ref.entity_id, scope)
        async with LayeredFileStoreIsolatedWriter(child, parents=[base]) as w:
            await w.pin_to_this_layer("parent", parent_ref.entity_id, scope)

    # `parent` is in `child`'s data folder.
    assert fmt.data_file_exists(child, parent_ref.entity_id)
    # but the leaf is NOT — it was already in base.
    assert not fmt.data_file_exists(child, leaf_ref.entity_id)


@pytest.mark.anyio
async def test_walk_stops_at_parent_precomp_inherited(tmp_path: Path):
    """A dep that is in a parent's ``precomp/`` (marked remote there) is
    *not* re-materialized or re-marked locally — the walk stops at this
    node and future readers resolve the dep via the parent's precomp
    chain → remote."""
    base = tmp_path / "base"
    child = tmp_path / "child"

    async with FxtrScope(name="src") as remote_scope:
        leaf_ref = remote_scope.store(LfsLeaf(name="leaf", value=11))
        parent_ref = remote_scope.store(LfsParent(name="p", child=leaf_ref))
        stub = StubDeepFetchable(remote_scope)

        # base: marks the leaf as remote (only precomp).
        async with LayeredFileStoreIsolatedWriter(
            base, parents=[], remote=(stub, stub.lease())
        ) as w:
            await w.mark_as_remote(leaf_ref.entity_id)

        # child: pins parent (with no remote configured — base's precomp
        # covers the leaf). We disable validate_on_close because the
        # validation would check whether the leaf is reachable — which
        # it is, via base's precomp. So validation passes; let's keep
        # it on to confirm.
        async with LayeredFileStoreIsolatedWriter(child, parents=[base]) as w:
            await w.pin_to_this_layer("parent", parent_ref.entity_id, remote_scope)

    # The parent is stored in child as data.
    assert fmt.data_file_exists(child, parent_ref.entity_id)
    # The leaf is NOT in child's data folder.
    assert not fmt.data_file_exists(child, leaf_ref.entity_id)
    # The leaf is NOT re-marked in child's precomp folder either.
    assert not fmt.precomp_file_exists(child, leaf_ref.entity_id)
    # It remains in base's precomp folder.
    assert fmt.precomp_file_exists(base, leaf_ref.entity_id)


@pytest.mark.anyio
async def test_pin_unreachable_id_raises(tmp_path: Path):
    """If the root id isn't tracked in ``from_scope``, the
    ``copy_to_new_arena`` call raises a `ValueError`."""
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        # Don't store anything; the id is fabricated.
        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            fake = FxtrID(raw_data_id=b"d" + b"\x44" * 32)
            with pytest.raises(ValueError):
                await w.pin_to_this_layer("fake", fake, scope)


@pytest.mark.anyio
async def test_pin_same_id_under_two_names_dedups_writes(tmp_path: Path):
    """Pinning the same id under two different names is fine; the data
    file is written once, both pin names point at the same id."""
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        leaf_ref = scope.store(LfsLeaf(name="leaf", value=1))

        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("alpha", leaf_ref.entity_id, scope)
            # Second pin under different name — walk hits "already in
            # this layer" and skips. No error.
            await w.pin_to_this_layer("beta", leaf_ref.entity_id, scope)

    pins = fmt.read_pins(layer)
    assert pins["alpha"] == leaf_ref.entity_id
    assert pins["beta"] == leaf_ref.entity_id
