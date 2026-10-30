"""Tests for the namespace read view (manager tier, no nix).

These exercise :class:`NamespaceReadAdapter` directly over an in-process
``RepoEntityStore`` + a hand-built manifest — the nix half
(:func:`build_namespace_manifest`) rides the part-2 nix E2E. The headline
behaviors: ``list_pins`` mirrors the manifest; ``retrieve`` resolves a pin's
bytes (and transitive closure) through the store; and the store is registered on
the view's **own** scope, so a caller scope only gains the requested entity.
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
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.data.system_entities import define_type
from fxtr_nix_runner.namespaces import NamespaceReadAdapter

pytestmark = pytest.mark.anyio

_TEST_NS = uuid.UUID("9d2e7c14-3a6b-4e21-8f70-1b5c0a9d4e62")


@dataclasses.dataclass(frozen=True)
class Leaf(DataclassEntity, fxtr_type=define_type(_TEST_NS, "namespaces_test_leaf")):
    value: str


@dataclasses.dataclass(frozen=True)
class Parent(DataclassEntity, fxtr_type=define_type(_TEST_NS, "namespaces_test_parent")):
    child: ChildRef[Leaf]


async def _create_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.name", "T"], cwd=path, check=True)


async def _seed(store: RepoEntityStore) -> tuple[FxtrID, FxtrID, FxtrID]:
    """Write into the store: a parent A->leaf B, and an unrelated leaf C.

    Returns ``(a_id, b_id, c_id)``. A's closure (A + B) is reachable by id; C is
    independent (used to check caller-scope isolation).
    """
    async with FxtrScope(name="seed") as seed:
        b_ref = seed.store(Leaf(value="deep_value"))
        a_ref = seed.store(Parent(child=b_ref))
        c_ref = seed.store(Leaf(value="unrelated"))
        lease = seed._current_auth_lease()
        await store.write_from_arena(a_ref.entity_id, seed._main_arena, lease.clone())
        await store.write_from_arena(c_ref.entity_id, seed._main_arena, lease.clone())
        return a_ref.entity_id, b_ref.entity_id, c_ref.entity_id


async def test_list_pins_and_retrieve_through_store(tmp_path: Path):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    async with RepoEntityStore.open(repo, create=True) as store:
        a_id, _b_id, _c_id = await _seed(store)
        manifest = {"alpha": a_id, "data/x:beta": _c_id}

        async with NamespaceReadAdapter(manifest, store, store.get_lease()) as ns:
            # list_pins mirrors the manifest, filterable by prefix.
            assert await ns.list_pins() == manifest
            assert await ns.list_pins("data/") == {"data/x:beta": _c_id}

            async with FxtrScope(name="caller") as caller:
                ref = await ns.retrieve("alpha", Parent, into_scope=caller, copy_into_scope=True)
                assert isinstance(ref, EntityRef)
                assert ref.entity_id == a_id
                # Bytes + transitive closure resolve through the store.
                loaded_a = await caller.load(ref)
                loaded_b = await loaded_a.child.load(caller)
                assert loaded_b.value == "deep_value"


async def test_retrieve_without_into_scope_returns_id(tmp_path: Path):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    async with RepoEntityStore.open(repo, create=True) as store:
        a_id, _b_id, _c_id = await _seed(store)
        async with NamespaceReadAdapter({"alpha": a_id}, store, store.get_lease()) as ns:
            result = await ns.retrieve("alpha")
            assert result == a_id
            # Loadable from the view's own scope (resolved through the store).
            loaded = await ns.scope.load(EntityRef(a_id, Parent))
            assert (await loaded.child.load(ns.scope)).value == "deep_value"


async def test_missing_pin(tmp_path: Path):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    async with RepoEntityStore.open(repo, create=True) as store:
        a_id, _b_id, _c_id = await _seed(store)
        async with NamespaceReadAdapter({"alpha": a_id}, store, store.get_lease()) as ns:
            assert await ns.try_retrieve("nope") is None
            with pytest.raises(KeyError):
                await ns.retrieve("nope")


async def test_caller_scope_does_not_gain_store_access(tmp_path: Path):
    """Side-effect-freeness: retrieving ``alpha`` into a caller scope must not
    register the store on the caller — an unrelated store-only id (C) stays
    unresolvable there."""
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    async with RepoEntityStore.open(repo, create=True) as store:
        a_id, _b_id, c_id = await _seed(store)
        async with NamespaceReadAdapter({"alpha": a_id}, store, store.get_lease()) as ns:
            async with FxtrScope(name="caller") as caller:
                await ns.retrieve("alpha", Parent, into_scope=caller, copy_into_scope=True)
                # C was never retrieved and the caller has no store source, so it
                # is unresolvable there (the store stayed on the view's scope).
                with pytest.raises(ValueError, match="not found in any parent scope"):
                    await caller.load(EntityRef(c_id, Leaf))
