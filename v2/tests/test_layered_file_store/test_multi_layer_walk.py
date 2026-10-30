"""Multi-layer walk: writes split across parent/child/grandparent layers,
dynamic parent expansion, cycle handling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent


@pytest.mark.anyio
async def test_single_layer_load(tmp_path: Path):
    """Round-trip a single-layer pin through the reader."""
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        ref = scope.store(LfsLeaf(name="x", value=1))
        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("x", ref.entity_id, scope)

    async with LayeredFileStoreReader(layer) as reader:
        e_ref = await reader.retrieve("x", LfsLeaf)
        loaded = await e_ref.load()
        assert loaded.name == "x"
        assert loaded.value == 1


@pytest.mark.anyio
async def test_child_layer_reaches_into_parent(tmp_path: Path):
    """Two-layer split: parent holds the leaf, child holds an entity
    that references the leaf. Reading the child resolves the leaf via
    the parent's data folder."""
    base = tmp_path / "base"
    child = tmp_path / "child"

    async with FxtrScope(name="src") as scope:
        leaf_ref = scope.store(LfsLeaf(name="leaf", value=7))
        parent_ref = scope.store(LfsParent(name="p", child=leaf_ref))

        # base layer pins / stores the leaf only.
        async with LayeredFileStoreIsolatedWriter(base, parents=[]) as w:
            await w.pin_to_this_layer("leaf", leaf_ref.entity_id, scope)
        # child layer declares base as parent; pin the parent entity.
        async with LayeredFileStoreIsolatedWriter(child, parents=[base]) as w:
            await w.pin_to_this_layer("parent", parent_ref.entity_id, scope)

    # Read the child layer; the leaf data should resolve through `base`.
    async with LayeredFileStoreReader(child) as reader:
        e_ref = await reader.retrieve("parent", LfsParent)
        loaded = await e_ref.load()
        assert loaded.name == "p"
        # Walking into the leaf works because the base layer has its data.
        leaf = await loaded.child.load()
        assert leaf.name == "leaf"


@pytest.mark.anyio
async def test_dynamic_parent_expansion(tmp_path: Path):
    """child → mid → base, where mid declares base as parent. child's
    parents.json only lists mid (not base). The reader must pick up
    base lazily after first touching mid."""
    base = tmp_path / "base"
    mid = tmp_path / "mid"
    child = tmp_path / "child"

    async with FxtrScope(name="src") as scope:
        leaf_ref = scope.store(LfsLeaf(name="leaf", value=11))
        parent_ref = scope.store(LfsParent(name="p", child=leaf_ref))

        async with LayeredFileStoreIsolatedWriter(base, parents=[]) as w:
            await w.pin_to_this_layer("leaf", leaf_ref.entity_id, scope)
        async with LayeredFileStoreIsolatedWriter(mid, parents=[base]) as w:
            # mid is empty except for declaring base as parent. We
            # don't pin anything new here.
            _ = w
        async with LayeredFileStoreIsolatedWriter(child, parents=[mid]) as w:
            # child has only `mid` as parent (NOT base).
            await w.pin_to_this_layer("parent", parent_ref.entity_id, scope)

    async with LayeredFileStoreReader(child) as reader:
        e_ref = await reader.retrieve("parent", LfsParent)
        loaded = await e_ref.load()
        # Reaching the leaf required dynamic walk: child -> mid -> base.
        leaf = await loaded.child.load()
        assert leaf.name == "leaf"


@pytest.mark.anyio
async def test_parents_cycle_does_not_loop(tmp_path: Path):
    """Layers a and b each declare each other as parents. Reader walks
    finitely (search-set dedup handles the cycle)."""
    a = tmp_path / "a"
    b = tmp_path / "b"

    async with FxtrScope(name="src") as scope:
        leaf_ref = scope.store(LfsLeaf(name="leaf", value=3))

        # Create skeletal layers first so we can refer to b from a and
        # vice versa. The writer's atomic finalize writes the parents
        # file at the end, so we need to plant both directories and
        # then patch parents.json after the fact.
        a.mkdir()
        b.mkdir()
        (a / fmt.DATA_DIR).mkdir()
        (b / fmt.DATA_DIR).mkdir()
        (a / fmt.PRECOMP_DIR).mkdir()
        (b / fmt.PRECOMP_DIR).mkdir()

        # Write the leaf to a's data folder directly.
        flat = await scope._main_arena.load_flat(scope.clone_authorization(), leaf_ref.entity_id)
        fmt.write_data_file(a, flat)
        fmt.write_pins(a, {"leaf": leaf_ref.entity_id})
        fmt.write_pins(b, {})
        fmt.write_parents(a, [b])  # cycle: a -> b
        fmt.write_parents(b, [a])  # cycle: b -> a
        fmt.write_layer_format(a)
        fmt.write_layer_format(b)

    # Reading 'leaf' from a should work; reading from b should also
    # succeed via the dynamic parent walk into a.
    async with LayeredFileStoreReader(a) as reader:
        e_ref = await reader.retrieve("leaf", LfsLeaf)
        loaded = await e_ref.load()
        assert loaded.name == "leaf"

    async with LayeredFileStoreReader(b) as reader:
        # b has no pins, so list_pins is empty even though we can reach
        # a's data through the parent walk during loads.
        assert await reader.list_pins() == {}
