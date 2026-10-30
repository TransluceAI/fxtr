"""End-to-end provenance round-trip through a provenanced layer.

Writes a genuinely-provenanced (nested) entity into a provenanced layer,
then reads it back with a **fresh** reader after every writing scope has
closed — so the retrieve genuinely reconstructs the layered id from the
on-disk layer (the reader auto-detects the ``provenanced`` flag and
``embed``s), rather than leaning on the in-process store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fxtr2.data.system_entities  # noqa: F401
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent

assert fxtr2.data.system_entities is not None

GROUND = FxtrID.from_tag_and_payload(".", b"")


def _make_c_node(scope: FxtrScope, ctx_key: str = "round_trip") -> FxtrID:
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": ctx_key,
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), data)


def _bare(fxtr_id: FxtrID) -> FxtrID:
    return FxtrID(raw_data_id=fxtr_id._raw_data_id)


@pytest.mark.anyio
async def test_provenanced_round_trip_reconstructs_layered_id(tmp_path: Path):
    """Write a nested provenanced entity → read it back layered, load it, and
    confirm the nested child still carries provenance."""
    layer = tmp_path / "L"

    # --- write (all writing scopes close before we read) ---
    async with FxtrScope(name="builder") as builder:
        c_node = _make_c_node(builder)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=builder,
            name="src",
        ) as scope:
            leaf_ref = scope.store(LfsLeaf(name="leaf", value=7))
            parent_ref = scope.store(LfsParent(name="p", child=leaf_ref))
            layered_parent = parent_ref.entity_id
            assert layered_parent._provenance_parent is not None
            expected_prov = _bare(layered_parent._provenance_parent)
            expected_data = _bare(layered_parent)
            async with LayeredFileStoreIsolatedWriter(layer, parents=[], provenanced=True) as w:
                await w.pin_to_this_layer("parent", layered_parent, scope)

    # --- read back with a fresh reader ---
    async with LayeredFileStoreReader(layer) as reader:
        ref = await reader.retrieve("parent", LfsParent)
        rid = ref.entity_id
        # Reconstructed layered id: data half + prov half match what was pinned.
        assert rid._provenance_parent is not None
        assert rid._raw_data_id == expected_data._raw_data_id
        assert rid._provenance_parent == expected_prov

        parent = await ref.load()
        assert parent.name == "p"
        # Nested provenance preserved: the child ref is itself layered.
        assert parent.child.entity_id._provenance_parent is not None
        child = await parent.child.load()
        assert child == LfsLeaf(name="leaf", value=7)

        # list_pins returns the SAME layered id as retrieve (pure read of the
        # stored [data, prov] pair) — the consistency the pair format buys.
        listed = (await reader.list_pins())["parent"]
        assert listed._raw_data_id == rid._raw_data_id
        assert listed._provenance_parent == rid._provenance_parent


@pytest.mark.anyio
async def test_bare_layer_reader_returns_bare(tmp_path: Path):
    """A bare layer (default) reads back bare — the reader only embeds when the
    ``provenanced`` flag is set."""
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        ref = scope.store(LfsLeaf(name="leaf", value=1))
        bare_id = ref.entity_id
        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("leaf", bare_id, scope)

    assert fmt.read_layer_format(layer).provenanced is False
    async with LayeredFileStoreReader(layer) as reader:
        got = await reader.retrieve("leaf")
        assert got._provenance_parent is None
        assert got == bare_id
