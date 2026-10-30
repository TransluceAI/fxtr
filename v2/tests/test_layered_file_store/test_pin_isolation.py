"""Pins are layer-local: a reader on a child layer only sees pins
declared in that layer, not pins from parents.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf


@pytest.mark.anyio
async def test_list_pins_layer_local(tmp_path: Path):
    base = tmp_path / "base"
    child = tmp_path / "child"

    async with FxtrScope(name="src") as scope:
        a = scope.store(LfsLeaf(name="a", value=1))
        b = scope.store(LfsLeaf(name="b", value=2))

        async with LayeredFileStoreIsolatedWriter(base, parents=[]) as w:
            await w.pin_to_this_layer("base_only", a.entity_id, scope)
        async with LayeredFileStoreIsolatedWriter(child, parents=[base]) as w:
            await w.pin_to_this_layer("child_only", b.entity_id, scope)

    # Reader on `child` sees ONLY `child_only`.
    async with LayeredFileStoreReader(child) as reader:
        pins = await reader.list_pins()
        assert "child_only" in pins
        assert "base_only" not in pins, "child reader must not see parent's pins"

    # Reader on `base` sees only its own pin.
    async with LayeredFileStoreReader(base) as reader:
        pins = await reader.list_pins()
        assert "base_only" in pins
        assert "child_only" not in pins


@pytest.mark.anyio
async def test_retrieve_unknown_pin_raises(tmp_path: Path):
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        ref = scope.store(LfsLeaf(name="x", value=1))
        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("x", ref.entity_id, scope)

    async with LayeredFileStoreReader(layer) as reader:
        with pytest.raises(KeyError):
            await reader.retrieve("nope")

        # try_retrieve returns None.
        assert await reader.try_retrieve("nope") is None


@pytest.mark.anyio
async def test_pin_to_this_layer_duplicate_name_errors(tmp_path: Path):
    """Same pin name twice in a single layer is an error."""
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        a = scope.store(LfsLeaf(name="a", value=1))
        b = scope.store(LfsLeaf(name="b", value=2))

        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("dup", a.entity_id, scope)
            with pytest.raises(ValueError):
                await w.pin_to_this_layer("dup", b.entity_id, scope)


@pytest.mark.anyio
async def test_list_pins_prefix(tmp_path: Path):
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        a = scope.store(LfsLeaf(name="a", value=1))
        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("ns/a", a.entity_id, scope)
            await w.pin_to_this_layer("other/b", a.entity_id, scope)

    async with LayeredFileStoreReader(layer) as reader:
        ns = await reader.list_pins("ns/")
        assert set(ns.keys()) == {"ns/a"}
