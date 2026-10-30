"""Provenanced-mode writer: in preserve mode each pin is recorded in its
natural form (layered → ``[data, prov]`` pair, bare → plain id — no u-wrap),
``layer_format.json`` records the flag, and ``mark_as_remote`` / ``preload``
cover both levels of a layered id.

These tests inspect the on-disk layer directly (pins / format / data /
precomp); the read-side round-trip (embed on retrieve) lives in
``test_provenanced_round_trip``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Eager import so the fallback-ctx builtin registrations don't trip the
# global-scope leak detector mid-test (see test_provenance_auto_fallback).
import fxtr2.data.system_entities  # noqa: F401
from fxtr2.data.abstract_arena import DepKind
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent, StubDeepFetchable

assert fxtr2.data.system_entities is not None

GROUND = FxtrID.from_tag_and_payload(".", b"")


def _make_c_node(scope: FxtrScope, ctx_key: str = "writer_test") -> FxtrID:
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": ctx_key,
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), data)


def _bare(fxtr_id: FxtrID) -> FxtrID:
    """The bare (provenance-stripped) form of an id."""
    return FxtrID(raw_data_id=fxtr_id._raw_data_id)


@pytest.mark.anyio
async def test_provenanced_pin_records_prov_node_and_sets_flag(tmp_path: Path):
    """Pinning a genuinely-provenanced (layered) entity records the bare
    prov-node id as the pin value, sets the ``provenanced`` flag, and walks
    both the prov node and its unwrapped data node into ``data/``."""
    layer = tmp_path / "L"
    async with FxtrScope(name="builder") as builder:
        c_node = _make_c_node(builder)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=builder,
            name="src",
        ) as scope:
            leaf_ref = scope.store(LfsLeaf(name="leaf", value=7))
            parent_ref = scope.store(LfsParent(name="p", child=leaf_ref))
            # The provenanced scope yields a layered id.
            assert parent_ref.entity_id._provenance_parent is not None

            async with LayeredFileStoreIsolatedWriter(layer, parents=[], provenanced=True) as w:
                await w.pin_to_this_layer("parent", parent_ref.entity_id, scope)

            prov_node = _bare(parent_ref.entity_id._provenance_parent)
            data_node = _bare(parent_ref.entity_id)

    # The on-disk format records the flag.
    assert fmt.read_layer_format(layer).provenanced is True
    # The pin reads back as the layered (data, prov) id (stored as a
    # [data, prov] pair). FxtrID equality is provenance-blind, so check both
    # halves explicitly.
    pins = fmt.read_pins(layer)
    assert pins["parent"]._raw_data_id == data_node._raw_data_id
    assert pins["parent"]._provenance_parent == prov_node
    assert prov_node._provenance_parent is None
    assert prov_node.get_tag() == "n"
    # Both the prov node and its unwrapped data node are present.
    assert fmt.data_file_exists(layer, prov_node)
    assert fmt.data_file_exists(layer, data_node)
    # The on-disk prov node genuinely encodes its unwrap to the data node —
    # i.e. a fresh reader would reconstruct the right (data, prov) pair. This
    # is a pure on-disk check, decoupled from the in-process memory store.
    prov_flat = fmt.read_data_file_flat(layer, prov_node)
    assert prov_flat is not None
    assert (data_node, DepKind.UNWRAPS_TO) in prov_flat.deps


@pytest.mark.anyio
async def test_provenanced_pin_bare_entity_stored_bare(tmp_path: Path):
    """A genuinely-bare entity pinned into a provenanced (preserve) layer is
    stored **bare** (a plain string), not ``u``-wrapped — so a provenanced
    layer can hold mixed pins and carries no meaningless trivial-``u`` nodes."""
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        leaf_ref = scope.store(LfsLeaf(name="leaf", value=3))
        assert leaf_ref.entity_id._provenance_parent is None
        # The u-node the writer would have minted under the old behavior — it
        # must NOT appear on disk now.
        u_node = scope.create_u_node(leaf_ref.entity_id)

        async with LayeredFileStoreIsolatedWriter(layer, parents=[], provenanced=True) as w:
            await w.pin_to_this_layer("leaf", leaf_ref.entity_id, scope)

    pins = fmt.read_pins(layer)
    # Stored bare: a plain id, no provenance parent, equal to what was pinned.
    assert pins["leaf"] == leaf_ref.entity_id
    assert pins["leaf"]._provenance_parent is None
    assert fmt.data_file_exists(layer, leaf_ref.entity_id)
    # No u-node was minted/written.
    assert not fmt.data_file_exists(layer, u_node)


@pytest.mark.anyio
async def test_bare_writer_omits_flag(tmp_path: Path):
    """A non-provenanced writer leaves the flag off; the format reads back
    as bare (back-compat)."""
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        ref = scope.store(LfsLeaf(name="leaf", value=1))
        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("leaf", ref.entity_id, scope)
        # Bare layer: pin value is the bare data id (plain string, no pair).
        bare_pin = fmt.read_pins(layer)["leaf"]
        assert bare_pin == ref.entity_id
        assert bare_pin._provenance_parent is None
    assert fmt.read_layer_format(layer).provenanced is False


@pytest.mark.anyio
async def test_bare_writer_strips_provenance_from_layered_id(tmp_path: Path):
    """A non-provenanced (strip) writer handed a *layered* id stores the bare
    data id — it must NOT silently keep provenance as a [data, prov] pair. The
    prov node's data file is not written, only the data closure."""
    layer = tmp_path / "L"
    async with FxtrScope(name="builder") as builder:
        c_node = _make_c_node(builder)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=builder,
            name="src",
        ) as scope:
            ref = scope.store(LfsLeaf(name="leaf", value=2))
            layered = ref.entity_id
            assert layered._provenance_parent is not None
            prov_node = _bare(layered._provenance_parent)
            data_node = _bare(layered)
            async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
                await w.pin_to_this_layer("leaf", layered, scope)

    # Stored bare (a plain string, no _provenance_parent) — provenance stripped.
    pin = fmt.read_pins(layer)["leaf"]
    assert pin._raw_data_id == data_node._raw_data_id
    assert pin._provenance_parent is None
    # The data node is on disk; the prov node is not (it was never walked).
    assert fmt.data_file_exists(layer, data_node)
    assert not fmt.data_file_exists(layer, prov_node)


@pytest.mark.anyio
async def test_provenanced_mark_as_remote_marks_both_levels(tmp_path: Path):
    """``mark_as_remote`` on a layered id records precomp for **both** the
    bare prov-node id and the bare data id."""
    layer = tmp_path / "L"
    async with FxtrScope(name="builder") as builder:
        c_node = _make_c_node(builder)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=builder,
            name="remote",
        ) as remote_scope:
            ref = remote_scope.store(LfsLeaf(name="leaf", value=5))
            assert ref.entity_id._provenance_parent is not None
            stub = StubDeepFetchable(remote_scope)
            prov_node = _bare(ref.entity_id._provenance_parent)
            data_node = _bare(ref.entity_id)

            async with LayeredFileStoreIsolatedWriter(
                layer, parents=[], remote=(stub, stub.lease()), provenanced=True
            ) as w:
                await w.mark_as_remote(ref.entity_id)

    assert fmt.precomp_file_exists(layer, prov_node)
    assert fmt.precomp_file_exists(layer, data_node)


@pytest.mark.anyio
async def test_provenanced_preload_fetches_both_levels(tmp_path: Path):
    """``preload`` on a layered id pulls **both** the prov-node and bare-data
    closures into ``data/`` (so a later reader can serve either form offline),
    and returns the decoded entity."""
    layer = tmp_path / "L"
    async with FxtrScope(name="builder") as builder:
        c_node = _make_c_node(builder)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=builder,
            name="remote",
        ) as remote_scope:
            ref = remote_scope.store(LfsLeaf(name="leaf", value=9))
            assert ref.entity_id._provenance_parent is not None
            stub = StubDeepFetchable(remote_scope)
            prov_node = _bare(ref.entity_id._provenance_parent)
            data_node = _bare(ref.entity_id)

            async with LayeredFileStoreIsolatedWriter(
                layer,
                parents=[],
                remote=(stub, stub.lease()),
                provenanced=True,
                validate_on_close=False,
            ) as w:
                await w.mark_as_remote(ref.entity_id)
                loaded = await w.preload(EntityRef(ref.entity_id, LfsLeaf))

    assert isinstance(loaded, LfsLeaf)
    assert loaded == LfsLeaf(name="leaf", value=9)
    # Both levels are now local data files (not just remote precomp markers).
    assert fmt.data_file_exists(layer, prov_node)
    assert fmt.data_file_exists(layer, data_node)
    # The on-disk prov node encodes its unwrap to the data node.
    prov_flat = fmt.read_data_file_flat(layer, prov_node)
    assert prov_flat is not None
    assert (data_node, DepKind.UNWRAPS_TO) in prov_flat.deps


@pytest.mark.anyio
async def test_provenanced_pin_bare_remote_input_stored_bare(tmp_path: Path):
    """The realistic provenanced-FOD path: a **bare** remote input is
    ``mark_as_remote`` + ``preload``ed, then pinned **without** ``from_scope``.

    A bare entity is stored bare in preserve mode (no u-wrap); the pin's data is
    already local from ``preload`` so the fast path records it without a walk."""
    layer = tmp_path / "L"
    async with FxtrScope(name="remote") as remote_scope:
        ref = remote_scope.store(LfsLeaf(name="leaf", value=4))
        bare_id = ref.entity_id
        assert bare_id._provenance_parent is None
        stub = StubDeepFetchable(remote_scope)
        async with LayeredFileStoreIsolatedWriter(
            layer,
            parents=[],
            remote=(stub, stub.lease()),
            provenanced=True,
            validate_on_close=False,
        ) as w:
            await w.mark_as_remote(bare_id)
            await w.preload(EntityRef(bare_id, LfsLeaf))
            await w.pin_to_this_layer("leaf", bare_id)

    pins = fmt.read_pins(layer)
    assert pins["leaf"] == bare_id
    assert pins["leaf"]._provenance_parent is None
    assert fmt.data_file_exists(layer, bare_id)
