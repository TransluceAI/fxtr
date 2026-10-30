"""A child layer can preload an id that was marked-remote by a parent
layer, without re-marking it locally. The wiretap attach is lazy
(happens inside ``preload``), so any id known to the layer's stack
can be preloaded regardless of where the mark was recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent, StubDeepFetchable


@pytest.mark.anyio
async def test_child_preloads_parent_marked_id_without_remarking(tmp_path: Path):
    """Parent layer marks the root as remote and pins it. Child layer
    declares parent as parent, pins the *same* id, and preloads it
    deeper (writes data here) — without ever calling mark_as_remote
    in the child. The preload's lazy wiretap-attach makes this work.

    End-to-end: read the child layer with a remote configured and
    verify the full chain loads (with `leaf` reached transitively
    through the child's now-local `parent` data and the wiretap's
    propagation into the leaf)."""
    base = tmp_path / "base"
    child = tmp_path / "child"

    async with FxtrScope(name="src") as remote_scope:
        leaf_ref = remote_scope.store(LfsLeaf(name="leaf", value=99))
        parent_ref = remote_scope.store(LfsParent(name="p", child=leaf_ref))
        stub = StubDeepFetchable(remote_scope)

        # base: mark parent as remote and pin it. No data files.
        async with LayeredFileStoreIsolatedWriter(
            base,
            parents=[],
            remote=(stub, stub.lease()),
        ) as w:
            await w.mark_as_remote(parent_ref.entity_id)
            await w.pin_to_this_layer("p", parent_ref.entity_id)

        assert fmt.precomp_file_exists(base, parent_ref.entity_id)
        assert not fmt.data_file_exists(base, parent_ref.entity_id)

        # child: declares base as parent. Pins the *same* id and
        # preloads it (writes data in child). Does NOT call
        # mark_as_remote on parent_ref — the parent's precomp file is
        # enough.
        async with LayeredFileStoreIsolatedWriter(
            child,
            parents=[base],
            remote=(stub, stub.lease()),
            validate_on_close=False,  # no pins for the leaf in child
        ) as w:
            await w.pin_to_this_layer("p", parent_ref.entity_id)
            # Fast-path pin: parent's precomp covers it; from_scope
            # not needed.
            loaded = await w.preload(EntityRef(parent_ref.entity_id, LfsParent))
            assert loaded.name == "p"

        # Child has data for parent_ref; leaf's precomp is in child
        # (written by the preload's uncovered-dep handling).
        assert fmt.data_file_exists(child, parent_ref.entity_id)
        assert fmt.precomp_file_exists(child, leaf_ref.entity_id)

        # End-to-end: read the child with a remote, walk the chain.
        async with LayeredFileStoreReader(child, remote=(stub, stub.lease())) as reader:
            pin_e = await reader.retrieve("p", LfsParent)
            loaded_parent = await pin_e.load()
            assert loaded_parent.name == "p"
            leaf = await loaded_parent.child.load()
            assert leaf.name == "leaf"
            assert leaf.value == 99


@pytest.mark.anyio
async def test_mark_as_remote_is_noop_when_covered_by_parent(tmp_path: Path):
    """If a parent already has a precomp file for the id, child's
    mark_as_remote is a no-op (no precomp re-write, no extra remote
    round-trip)."""
    base = tmp_path / "base"
    child = tmp_path / "child"

    async with FxtrScope(name="src") as remote_scope:
        ref = remote_scope.store(LfsLeaf(name="x", value=1))
        stub = StubDeepFetchable(remote_scope)

        async with LayeredFileStoreIsolatedWriter(
            base,
            parents=[],
            remote=(stub, stub.lease()),
        ) as w:
            await w.mark_as_remote(ref.entity_id)

        async with LayeredFileStoreIsolatedWriter(
            child,
            parents=[base],
            remote=(stub, stub.lease()),
            validate_on_close=False,
        ) as w:
            await w.mark_as_remote(ref.entity_id)

        # No precomp file in child — base's is enough.
        assert not fmt.precomp_file_exists(child, ref.entity_id)
        assert not fmt.data_file_exists(child, ref.entity_id)
        assert fmt.precomp_file_exists(base, ref.entity_id)
