"""Provenanced pin-mounting through the flake manager (no nix).

A provenanced layer stores its pin as a bare provenance-node id;
``FlakeManager.mount_pin`` must auto-detect the layer's ``provenanced`` flag
and re-embed, returning a layered ``(data, prov)`` id. Builds the layer on disk
and lets the building scopes close before mounting, so the mount genuinely
reconstructs provenance from disk.

The v2-level reader/writer mechanics are covered in
``v2/tests/test_layered_file_store/test_provenanced_round_trip.py``; here we
test the manager wiring.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import anyio
import pytest
from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from fxtr2.data.system_entities import define_type
from fxtr_nix_runner import HostBackend
from fxtr_nix_runner.manager import FlakeManager

pytestmark = pytest.mark.anyio

_TEST_NS = uuid.UUID("b2e6d4c1-9a3f-4d77-8b2c-1f0e5a9d6c42")
GROUND = FxtrID.from_tag_and_payload(".", b"")


@dataclasses.dataclass(frozen=True)
class Leaf(DataclassEntity, fxtr_type=define_type(_TEST_NS, "prov_mount_leaf")):
    value: str


@dataclasses.dataclass(frozen=True)
class Parent(DataclassEntity, fxtr_type=define_type(_TEST_NS, "prov_mount_parent")):
    child: ChildRef[Leaf]


async def _create_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.name", "T"], cwd=path, check=True)


def _make_c_node(scope: FxtrScope) -> FxtrID:
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": "prov_mount",
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), data)


def _bare(fxtr_id: FxtrID) -> FxtrID:
    return FxtrID(raw_data_id=fxtr_id._raw_data_id)


async def _build_provenanced_layer(layer_dir: Path) -> FxtrID:
    """Build a provenanced layer pinning a nested provenanced Parent. Returns
    the original layered id."""
    async with FxtrScope(name="builder") as builder:
        c_node = _make_c_node(builder)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=builder,
            name="seed",
        ) as seed:
            leaf_ref = seed.store(Leaf(value="prov_value"))
            parent_ref = seed.store(Parent(child=leaf_ref))
            layered = parent_ref.entity_id
            assert layered._provenance_parent is not None
            async with LayeredFileStoreIsolatedWriter(layer_dir, parents=[], provenanced=True) as w:
                await w.pin_to_this_layer("A", layered, from_scope=seed)
            return layered


async def test_mount_pin_provenanced_returns_layered_id(tmp_path: Path):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    layer_dir = tmp_path / "layer"
    layered = await _build_provenanced_layer(layer_dir)
    assert layered._provenance_parent is not None
    expected_prov = _bare(layered._provenance_parent)

    async with FlakeManager(repo, backend=HostBackend()) as mgr:
        mounted = mgr.mount_pin(layer_dir, "A", remap=None)
        # mount_pin re-embeds: a layered (data, prov) id, not the bare prov node.
        assert mounted._provenance_parent is not None
        assert mounted._raw_data_id == layered._raw_data_id
        assert mounted._provenance_parent == expected_prov

        loaded = await mgr.scope.load(EntityRef(mounted, Parent))
        # Nested provenance preserved through the load.
        assert loaded.child.entity_id._provenance_parent is not None
        child = await loaded.child.load(mgr.scope)
        assert child.value == "prov_value"


async def test_mount_pin_bare_layer_returns_bare(tmp_path: Path):
    """A bare layer (default) mounts back bare — mount_pin only embeds when the
    layer's ``provenanced`` flag is set."""
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    layer_dir = tmp_path / "layer"
    async with FxtrScope(name="seed") as seed:
        leaf_ref = seed.store(Leaf(value="bare"))
        bare_id = leaf_ref.entity_id
        async with LayeredFileStoreIsolatedWriter(layer_dir, parents=[]) as w:
            await w.pin_to_this_layer("A", bare_id, from_scope=seed)

    async with FlakeManager(repo, backend=HostBackend()) as mgr:
        mounted = mgr.mount_pin(layer_dir, "A", remap=None)
        assert mounted._provenance_parent is None
        assert mounted == bare_id
