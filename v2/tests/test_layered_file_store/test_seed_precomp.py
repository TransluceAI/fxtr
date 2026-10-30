"""Sync precomp queries must work on a freshly-retrieved pin without
the caller first loading the entity. The HTTP namespace pre-ingests
precomp into the per-pin arena before ``lazily_attach_from_arena``;
this test verifies the layered reader does the same — currently
**failing** until we implement seed-precomp population.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf


@pytest.mark.anyio
async def test_seed_precomp_for_u_node_pin(tmp_path: Path):
    """Pin a ``u``-node; after retrieve, sync ``unwrap_provenance``
    on the scope's main arena should return the wrapped data node id
    *without* first calling ``load`` on the pin."""
    layer = tmp_path / "L"

    async with FxtrScope(name="src") as scope:
        # Underlying data node.
        data_ref = scope.store(LfsLeaf(name="x", value=1))
        # Wrap it in a u-node — a provenance node whose unwraps_to is
        # the data node.
        u_id = scope._main_arena.create_u_node(scope.clone_authorization(), data_ref.entity_id)
        assert u_id.get_tag() == "u"

        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("u_pin", u_id, scope)

    async with LayeredFileStoreReader(layer) as reader:
        pin_id = await reader.retrieve("u_pin")
        assert pin_id == u_id

        # Sync precomp query BEFORE any load. The scope's memory store
        # delegates ``unwrap_provenance`` to the attached external
        # arena (our LayeredFileArena), which should answer with the
        # data node id.
        unwrapped = reader.scope._main_arena.unwrap_provenance(
            reader.scope.clone_authorization(), pin_id
        )
        assert unwrapped == data_ref.entity_id, (
            f"Expected unwrap_provenance({pin_id}) -> {data_ref.entity_id}," f" got {unwrapped}"
        )
