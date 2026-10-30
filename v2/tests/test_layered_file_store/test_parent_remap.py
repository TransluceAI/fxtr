"""Parent-path remap hook for reading relocated layer closures.

``parents.json`` records *absolute* parent-layer paths. When a layer
closure is built in one place and read in another — the motivating case
is the podman nix backend, which builds layers at in-container
``/nix/store/...`` paths then *materializes* (closure-copies) them into a
host bind-mount store at ``<root>/nix/store/...`` — the recorded parent
paths don't exist at the read site. ``ParentPathRemap`` rewrites them on
the fly with a prefix swap, so the closure copy can stay verbatim.

We simulate materialization without nix: build the layers under one dir,
copy the tree to a second dir (``parents.json`` still points at the
*original* paths), delete the original, then read the copy with a remap
``<orig> -> <copy>``. Without the remap the original paths are gone and
the read fails — that's the negative control.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store.errors import LayerMalformedError
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.search import ParentPathRemap
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent


def test_parent_path_remap_apply():
    """Unit: prefix swap when under ``old_prefix``, passthrough otherwise."""
    remap = ParentPathRemap(old_prefix=Path("/nix/store"), new_prefix=Path("/mat/nix/store"))
    assert remap.apply(Path("/nix/store/abc-layer")) == Path("/mat/nix/store/abc-layer")
    # Deeper paths keep their tail.
    assert remap.apply(Path("/nix/store/abc/data/x.cbor")) == Path("/mat/nix/store/abc/data/x.cbor")
    # The prefix itself maps to the new prefix.
    assert remap.apply(Path("/nix/store")) == Path("/mat/nix/store")
    # Not under old_prefix -> unchanged.
    assert remap.apply(Path("/elsewhere/layer")) == Path("/elsewhere/layer")


async def _build_two_layers(orig: Path) -> None:
    """base holds the leaf; child declares base as parent + pins a parent
    entity that references the leaf."""
    base = orig / "base"
    child = orig / "child"
    async with FxtrScope(name="src") as scope:
        leaf_ref = scope.store(LfsLeaf(name="leaf", value=7))
        parent_ref = scope.store(LfsParent(name="p", child=leaf_ref))
        async with LayeredFileStoreIsolatedWriter(base, parents=[]) as w:
            await w.pin_to_this_layer("leaf", leaf_ref.entity_id, scope)
        async with LayeredFileStoreIsolatedWriter(child, parents=[base]) as w:
            await w.pin_to_this_layer("parent", parent_ref.entity_id, scope)


@pytest.mark.anyio
async def test_remap_resolves_relocated_parent(tmp_path: Path):
    """A materialized child reads its relocated parent through the remap."""
    orig = tmp_path / "orig"
    mat = tmp_path / "mat"
    await _build_two_layers(orig)

    # Compute prefixes before deleting (resolve is lexical, but keep it
    # honest), then "materialize": copy verbatim and drop the original so
    # the recorded parent paths genuinely don't exist at the read site.
    old_prefix = orig.resolve()
    new_prefix = mat.resolve()
    shutil.copytree(orig, mat)
    shutil.rmtree(orig)

    remap = ParentPathRemap(old_prefix=old_prefix, new_prefix=new_prefix)
    async with LayeredFileStoreReader(mat / "child", parent_path_remap=remap) as reader:
        e_ref = await reader.retrieve("parent", LfsParent)
        loaded = await e_ref.load()
        assert loaded.name == "p"
        # The leaf lives only in the (relocated) base layer; reaching it
        # exercises the remapped parent path.
        leaf = await loaded.child.load()
        assert leaf.name == "leaf"
        assert leaf.value == 7


@pytest.mark.anyio
async def test_without_remap_relocated_parent_is_unreachable(tmp_path: Path):
    """Negative control: same materialized layout, no remap -> the
    parent's recorded path is gone, so the reader fails on it.

    The reader eagerly expands the root layer's parents on enter, so the
    missing parent dir surfaces as a malformed-layer error right there.
    """
    orig = tmp_path / "orig"
    mat = tmp_path / "mat"
    await _build_two_layers(orig)
    shutil.copytree(orig, mat)
    shutil.rmtree(orig)

    with pytest.raises(LayerMalformedError):
        async with LayeredFileStoreReader(mat / "child") as reader:
            await reader.retrieve("parent", LfsParent)


@pytest.mark.anyio
async def test_remap_applies_transitively(tmp_path: Path):
    """child -> mid -> base, all relocated. The remap must apply at every
    depth (mid's parents.json points at the original base too)."""
    orig = tmp_path / "orig"
    mat = tmp_path / "mat"
    base = orig / "base"
    mid = orig / "mid"
    child = orig / "child"

    async with FxtrScope(name="src") as scope:
        leaf_ref = scope.store(LfsLeaf(name="leaf", value=11))
        parent_ref = scope.store(LfsParent(name="p", child=leaf_ref))
        async with LayeredFileStoreIsolatedWriter(base, parents=[]) as w:
            await w.pin_to_this_layer("leaf", leaf_ref.entity_id, scope)
        async with LayeredFileStoreIsolatedWriter(mid, parents=[base]) as w:
            _ = w  # empty layer, just declares base as parent
        async with LayeredFileStoreIsolatedWriter(child, parents=[mid]) as w:
            await w.pin_to_this_layer("parent", parent_ref.entity_id, scope)

    old_prefix = orig.resolve()
    new_prefix = mat.resolve()
    shutil.copytree(orig, mat)
    shutil.rmtree(orig)

    remap = ParentPathRemap(old_prefix=old_prefix, new_prefix=new_prefix)
    async with LayeredFileStoreReader(mat / "child", parent_path_remap=remap) as reader:
        e_ref = await reader.retrieve("parent", LfsParent)
        loaded = await e_ref.load()
        # Reaching the leaf required child -> mid -> base, each remapped.
        leaf = await loaded.child.load()
        assert leaf.name == "leaf"
        assert leaf.value == 11


@pytest.mark.anyio
async def test_no_remap_is_unchanged_behavior(tmp_path: Path):
    """A reader with no remap (the default) behaves exactly as before:
    in-place layers read fine."""
    orig = tmp_path / "orig"
    await _build_two_layers(orig)

    async with LayeredFileStoreReader(orig / "child") as reader:
        e_ref = await reader.retrieve("parent", LfsParent)
        loaded = await e_ref.load()
        leaf = await loaded.child.load()
        assert leaf.name == "leaf"
