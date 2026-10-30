"""Remote resolution: ``mark_as_remote`` + reader-side fallback through
the configured ``DeepFetchable``, and ``preload_data`` promotion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.errors import (
    LayerEntityMissingError,
    LayerRemoteNotConfiguredError,
)
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent, StubDeepFetchable


@pytest.mark.anyio
async def test_mark_as_remote_then_read_via_remote(tmp_path: Path):
    """Writer marks an id as remote; reader resolves it through the
    configured stub remote at read time."""
    layer = tmp_path / "L"

    async with FxtrScope(name="src") as remote_scope:
        ref = remote_scope.store(LfsLeaf(name="rem", value=99))
        stub = StubDeepFetchable(remote_scope)

        async with LayeredFileStoreIsolatedWriter(
            layer,
            parents=[],
            remote=(stub, stub.lease()),
        ) as w:
            await w.mark_as_remote(ref.entity_id)
            # Mark the remote id as this layer's pin so the reader can
            # resolve it by name. (Writers in real usage would
            # presumably reach this via a sibling pin_to_this_layer
            # call after preload_data; for this focused test we
            # set the pin directly.)
            w._pins["remote_leaf"] = ref.entity_id

        async with LayeredFileStoreReader(layer, remote=(stub, stub.lease())) as reader:
            e_ref = await reader.retrieve("remote_leaf", LfsLeaf)
            loaded = await e_ref.load()
            assert loaded.name == "rem"
            assert loaded.value == 99


@pytest.mark.anyio
async def test_read_without_remote_raises(tmp_path: Path):
    """If a pin is precomp-only and no remote is configured on the
    reader, attempting to load raises ``LayerRemoteNotConfiguredError``."""
    layer = tmp_path / "L"

    async with FxtrScope(name="src") as remote_scope:
        ref = remote_scope.store(LfsLeaf(name="rem", value=1))
        stub = StubDeepFetchable(remote_scope)

        async with LayeredFileStoreIsolatedWriter(
            layer,
            parents=[],
            remote=(stub, stub.lease()),
        ) as w:
            await w.mark_as_remote(ref.entity_id)
            w._pins["remote_leaf"] = ref.entity_id

        async with LayeredFileStoreReader(layer) as reader:
            e_ref = await reader.retrieve("remote_leaf", LfsLeaf)
            with pytest.raises(LayerRemoteNotConfiguredError):
                await e_ref.load()


@pytest.mark.anyio
async def test_mark_as_remote_missing_in_remote_raises(tmp_path: Path):
    """``mark_as_remote`` fails if the remote doesn't have the id."""
    layer = tmp_path / "L"

    async with FxtrScope(name="empty_remote") as remote_scope:
        stub = StubDeepFetchable(remote_scope)
        async with LayeredFileStoreIsolatedWriter(
            layer,
            parents=[],
            remote=(stub, stub.lease()),
        ) as w:
            fake = FxtrID(raw_data_id=b"d" + b"\xab" * 32)
            with pytest.raises(LayerEntityMissingError):
                await w.mark_as_remote(fake)


@pytest.mark.anyio
async def test_preload_promotes_remote(tmp_path: Path):
    """``preload(ref)`` turns a marked-remote entity into a data entry
    in this layer, writes precomp for newly-exposed deps, and returns
    the loaded entity. Across layers: marking in parent + preloading
    in child works (mark_as_remote skips the precomp-write when the
    id is already covered upstream)."""
    base = tmp_path / "base"
    new = tmp_path / "new"

    async with FxtrScope(name="src") as remote_scope:
        leaf_ref = remote_scope.store(LfsLeaf(name="leaf", value=42))
        parent_ref = remote_scope.store(LfsParent(name="p", child=leaf_ref))
        stub = StubDeepFetchable(remote_scope)

        # base: only the parent id is marked remote.
        async with LayeredFileStoreIsolatedWriter(
            base,
            parents=[],
            remote=(stub, stub.lease()),
        ) as w:
            await w.mark_as_remote(parent_ref.entity_id)

        assert fmt.precomp_file_exists(base, parent_ref.entity_id)

        # new: declares base as parent. mark_as_remote registers
        # parent in new's wiretap (no precomp re-write because base
        # already has it); preload promotes data into `new`.
        async with LayeredFileStoreIsolatedWriter(
            new,
            parents=[base],
            remote=(stub, stub.lease()),
            validate_on_close=False,  # no pins; closure trivially holds
        ) as w:
            await w.mark_as_remote(parent_ref.entity_id)
            loaded_parent = await w.preload(EntityRef(parent_ref.entity_id, LfsParent))
            # Returned entity is the decoded LfsParent.
            assert loaded_parent.name == "p"

    assert fmt.data_file_exists(new, parent_ref.entity_id)
    # leaf is a dep of parent; preload wrote precomp for it (uncovered
    # in new and not in base).
    assert fmt.precomp_file_exists(new, leaf_ref.entity_id)


@pytest.mark.anyio
async def test_preload_for_unknown_id_errors(tmp_path: Path):
    """``preload(ref)`` errors if the id is not in the writer's
    known-remote set (no prior mark_as_remote, not a transitively
    discovered dep)."""
    layer = tmp_path / "L"

    async with FxtrScope(name="empty") as scope:
        stub = StubDeepFetchable(scope)
        async with LayeredFileStoreIsolatedWriter(
            layer,
            parents=[],
            remote=(stub, stub.lease()),
        ) as w:
            fake = FxtrID(raw_data_id=b"d" + b"\xcc" * 32)
            with pytest.raises(LayerEntityMissingError):
                await w.preload(EntityRef(fake, LfsLeaf))
