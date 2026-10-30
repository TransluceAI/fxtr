"""``preload(ref)`` returns the loaded entity (via the writer's
internal scope + wiretap), and transitive preloads work — the deps
of a preloaded entity become known-remote, so ``preload(a.next)``
succeeds without an explicit ``mark_as_remote`` on the dep."""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent, StubDeepFetchable


@pytest.mark.anyio
async def test_preload_returns_decoded_entity(tmp_path: Path):
    """`mark_as_remote(parent_id)` + `preload(parent_ref)` returns
    the decoded `LfsParent` entity, writes its data file, and writes
    a precomp file for the leaf dep."""
    layer = tmp_path / "L"

    async with FxtrScope(name="src") as remote_scope:
        leaf_ref = remote_scope.store(LfsLeaf(name="leaf", value=7))
        parent_ref = remote_scope.store(LfsParent(name="p", child=leaf_ref))
        stub = StubDeepFetchable(remote_scope)

        async with LayeredFileStoreIsolatedWriter(
            layer,
            parents=[],
            remote=(stub, stub.lease()),
            validate_on_close=False,  # no pins
        ) as w:
            await w.mark_as_remote(parent_ref.entity_id)
            loaded = await w.preload(EntityRef(parent_ref.entity_id, LfsParent))

            # The returned value is the decoded LfsParent.
            assert loaded.name == "p"
            # The data file is on disk.
            assert fmt.data_file_exists(layer, parent_ref.entity_id)
            # The leaf got precomp for the uncovered-dep case.
            assert fmt.precomp_file_exists(layer, leaf_ref.entity_id)


@pytest.mark.anyio
async def test_preload_chain_transitive_known_remote(tmp_path: Path):
    """After preload(parent), the leaf dep enters the wiretap's
    known-remote set automatically (via load_flat's dep expansion).
    So preload(leaf_ref) succeeds without an explicit mark_as_remote
    on the leaf."""
    layer = tmp_path / "L"

    async with FxtrScope(name="src") as remote_scope:
        leaf_ref = remote_scope.store(LfsLeaf(name="leaf", value=11))
        parent_ref = remote_scope.store(LfsParent(name="p", child=leaf_ref))
        stub = StubDeepFetchable(remote_scope)

        async with LayeredFileStoreIsolatedWriter(
            layer,
            parents=[],
            remote=(stub, stub.lease()),
            validate_on_close=False,
        ) as w:
            await w.mark_as_remote(parent_ref.entity_id)
            parent = await w.preload(EntityRef(parent_ref.entity_id, LfsParent))
            # leaf isn't explicitly marked, but parent's preload added
            # it to the wiretap's known_remote.
            leaf = await w.preload(parent.child)
            assert leaf.name == "leaf"
            assert leaf.value == 11
            assert fmt.data_file_exists(layer, leaf_ref.entity_id)
            # After preloading leaf, its (now-resolved) precomp file
            # in this layer should be gone — the data file supersedes.
            assert not fmt.precomp_file_exists(layer, leaf_ref.entity_id)


@pytest.mark.anyio
async def test_pin_to_this_layer_fast_path_after_mark(tmp_path: Path):
    """`pin_to_this_layer(name, id)` works without `from_scope` when
    the id is already known via `mark_as_remote`."""
    layer = tmp_path / "L"

    async with FxtrScope(name="src") as remote_scope:
        leaf_ref = remote_scope.store(LfsLeaf(name="x", value=1))
        stub = StubDeepFetchable(remote_scope)

        async with LayeredFileStoreIsolatedWriter(
            layer,
            parents=[],
            remote=(stub, stub.lease()),
        ) as w:
            await w.mark_as_remote(leaf_ref.entity_id)
            # No from_scope; relies on the wiretap's known_remote.
            await w.pin_to_this_layer("x", leaf_ref.entity_id)

        # Pin landed.
        pins = fmt.read_pins(layer)
        assert pins == {"x": leaf_ref.entity_id}


@pytest.mark.anyio
async def test_pin_to_this_layer_errors_without_scope_for_unknown(tmp_path: Path):
    """`pin_to_this_layer(name, id)` errors if id isn't known and no
    `from_scope` is provided."""
    from fxtr2.data.entity_core import FxtrID

    layer = tmp_path / "L"
    async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
        fake = FxtrID(raw_data_id=b"d" + b"\x99" * 32)
        with pytest.raises(ValueError, match="id is not known"):
            await w.pin_to_this_layer("nope", fake)
