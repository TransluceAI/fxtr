"""Transitive remote: a layer marks one entity as remote; its deps
(unmarked, no precomp files) are implicitly remote-resolvable through
the known-remote set, since the remote that supplied the parent must
also supply the children.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.errors import LayerEntityMissingError
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent, StubDeepFetchable


@pytest.mark.anyio
async def test_marked_remote_id_pulls_unmarked_deps_transitively(tmp_path: Path):
    """Mark only the *parent* of a 2-entity chain as remote. The
    child has no precomp file in any layer. At read time, loading the
    parent fetches it from the remote; load of the child must then
    succeed (the child is added to ``_known_remote`` by the parent's
    load propagation)."""
    layer = tmp_path / "L"

    async with FxtrScope(name="src") as remote_scope:
        leaf_ref = remote_scope.store(LfsLeaf(name="leaf", value=42))
        parent_ref = remote_scope.store(LfsParent(name="p", child=leaf_ref))
        stub = StubDeepFetchable(remote_scope)

        # Only the parent is marked remote.
        async with LayeredFileStoreIsolatedWriter(
            layer,
            parents=[],
            remote=(stub, stub.lease()),
            validate_on_close=False,
            # parent's deps include the leaf which we explicitly
            # don't mark; strict validation would catch it.
        ) as w:
            await w.mark_as_remote(parent_ref.entity_id)
            w._pins["p"] = parent_ref.entity_id

        # Sanity: only the parent has a precomp file.
        assert fmt.precomp_file_exists(layer, parent_ref.entity_id)
        assert not fmt.precomp_file_exists(layer, leaf_ref.entity_id)
        assert not fmt.data_file_exists(layer, leaf_ref.entity_id)

        async with LayeredFileStoreReader(layer, remote=(stub, stub.lease())) as reader:
            parent_e = await reader.retrieve("p", LfsParent)
            loaded_parent = await parent_e.load()
            # Loading the parent fetches it from remote, which
            # propagates the leaf id into _known_remote. Now loading
            # the leaf should also succeed through the remote — even
            # though no precomp file exists for it.
            loaded_leaf = await loaded_parent.child.load()
            assert loaded_leaf.name == "leaf"
            assert loaded_leaf.value == 42


@pytest.mark.anyio
async def test_unknown_id_raises_entity_missing(tmp_path: Path):
    """Sanity: loading an id with no local data, no precomp file
    anywhere, and not reached via a remote-load propagation raises
    ``LayerEntityMissingError`` — even when a remote is configured.

    The arena enforces this as an internal invariant: a load must
    correspond to either local data or a layer-declared / propagated
    known-remote id.
    """
    from fxtr2.data.entity_core import FxtrID
    from fxtr2.data.layered_file_store.arena import LayeredFileArena
    from fxtr2.data.layered_file_store.search import _LayerSearchSet

    layer = tmp_path / "L"

    async with FxtrScope(name="src") as remote_scope:
        ref = remote_scope.store(LfsLeaf(name="x", value=1))
        stub = StubDeepFetchable(remote_scope)

        async with LayeredFileStoreIsolatedWriter(
            layer, parents=[], remote=(stub, stub.lease())
        ) as w:
            await w.pin_to_this_layer("x", ref.entity_id, remote_scope)

        # Build an arena directly so we can drive `load_flat` with a known-bogus
        # id. Brand it on the stub (the source), so the incoming lease the arena
        # would thread to the remote is brand-consistent.
        search_set = _LayerSearchSet([layer])
        search_set.expand_parents_of(layer)
        brand_lease = stub.lease()
        arena = LayeredFileArena(
            brand_lease.grant,
            search_set,
            stub,
        )
        unknown = FxtrID(raw_data_id=b"d" + b"\xab" * 32)
        arena._tracked.add(unknown)
        with pytest.raises(LayerEntityMissingError):
            await arena.load_flat(stub.lease(), unknown)
        arena.close()
        brand_lease.discard()
