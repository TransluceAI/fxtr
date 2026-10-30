"""Lifetime tests for the entities adapter / flake manager mount path (no nix).

The headline case: a layer holds a **local** entity ``A`` whose data references a
**remote-only** entity ``B`` (B's bytes live only in the repo store, recorded in
the layer as a precomp marker — no data file). We mount ``A`` through the manager
*inside a build span*, then **let the span close** (the rehost + builder go away,
the store and the manager scope stay open). After teardown we load ``A`` from the
manager scope (local, from the layer on disk) and follow ``A -> B``, which must
fetch ``B`` from the repo store (the source threaded into the mount). This proves
the mounted entity survives the span and resolves lazily for the manager's
lifetime — no lease held by the arena.

The seam-level remote-pointer mechanics are unit-tested in
``v2/tests/test_layered_file_store/test_pin_arena.py``; here we test the runner
wiring (manager scope + ``mount_pin`` + build-span teardown).
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import anyio
import pytest
from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
from fxtr2.data.layered_file_store import LayeredFileStoreReader, LayerRemoteNotConfiguredError
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.data.system_entities import define_type
from fxtr_nix_runner import HostBackend
from fxtr_nix_runner.manager import FlakeManager

pytestmark = pytest.mark.anyio

_TEST_NS = uuid.UUID("3a7c1e90-5b2d-4f88-9c1a-6e0d2f4b8a31")


@dataclasses.dataclass(frozen=True)
class Leaf(DataclassEntity, fxtr_type=define_type(_TEST_NS, "entities_test_leaf")):
    value: str


@dataclasses.dataclass(frozen=True)
class Parent(DataclassEntity, fxtr_type=define_type(_TEST_NS, "entities_test_parent")):
    child: ChildRef[Leaf]


async def _create_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.name", "T"], cwd=path, check=True)


async def _seed_store_and_build_layer(repo: Path, layer_dir: Path) -> tuple[FxtrID, FxtrID]:
    """Seed the repo store with A (parent) + B (leaf), then build a layer with A
    local (data) and B remote-only (precomp). Returns ``(a_id, b_id)``."""
    async with FxtrScope(name="seed") as seed:
        leaf_ref = seed.store(Leaf(value="remote_value"))
        parent_ref = seed.store(Parent(child=leaf_ref))
        a_id, b_id = parent_ref.entity_id, leaf_ref.entity_id
        async with RepoEntityStore.open(repo, create=True) as store:
            await store.write_from_arena(a_id, seed._main_arena, seed._current_auth_lease().clone())
            async with LayeredFileStoreIsolatedWriter(
                layer_dir, parents=[], remote=(store, store.get_lease())
            ) as w:
                # Mark B remote first; the A walk stops at B and writes only A.
                await w.mark_as_remote(b_id)
                await w.pin_to_this_layer("A", a_id, from_scope=seed)
    return a_id, b_id


async def test_mounted_entity_resolves_remote_dep_after_build_span_closes(tmp_path: Path):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    layer_dir = tmp_path / "layer"
    a_id, _b_id = await _seed_store_and_build_layer(repo, layer_dir)

    async with FlakeManager(repo, backend=HostBackend()) as mgr:
        # Mount A through the manager *inside* a build span, then close the span.
        async with mgr.build_span():
            mounted = mgr.mount_pin(layer_dir, "A", remap=None)
            assert mounted == a_id
        # The span (rehost + builder) is gone; the store + manager scope remain.

        # A is local (layer on disk); following A -> B must fetch B from the repo
        # store (no local data) with the rehost long gone — lazily, no held lease.
        loaded_a = await mgr.scope.load(EntityRef(a_id, Parent))
        loaded_b = await loaded_a.child.load(mgr.scope)
        assert loaded_b.value == "remote_value"


async def test_layer_b_is_genuinely_remote(tmp_path: Path):
    """Sanity-check the fixture: with no remote configured, A (local) loads but
    following to B fails — confirming B really was left remote."""
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    layer_dir = tmp_path / "layer"
    a_id, _b_id = await _seed_store_and_build_layer(repo, layer_dir)

    async with LayeredFileStoreReader(layer_dir) as reader:  # no remote configured
        a_ref = await reader.retrieve("A", Parent, into_scope=reader.scope)
        assert a_ref.entity_id == a_id
        loaded_a = await a_ref.load()
        with pytest.raises(LayerRemoteNotConfiguredError):
            await loaded_a.child.load(reader.scope)
