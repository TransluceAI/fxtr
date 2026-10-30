"""Tests for the build-time rehost (manager tier — exercises fxtr2).

No nix needed: stand up an in-process ``RepoEntityStore``, serve it, and hit
``/data`` over HTTP. Covers the bearer-token gate (accept/reject), the
ephemeral bind, and a by-id round trip.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Any

import anyio
import cbor2
import httpx
import pytest
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawWeakFxtrID,
    fxtr_cbor_loads,
)
from fxtr2.data.memory_arena import MemoryArena, MemoryStore
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.experimental.http.client import http_deep_fetchable
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from fxtr_nix_runner.rehost import REMOTE_BASE_URL_ENV, REMOTE_BEARER_TOKEN_ENV, rehost_context

pytestmark = pytest.mark.anyio


# --- helpers (mirror v2/tests/test_repo_storage.py) -------------------------


@pytest.fixture
def memory_arena():
    store = MemoryStore()
    ull = UnconditionalLocalLifetime("test")
    store.register_token(ull.token, name="test")
    root_lease = ull.root_lease.take()
    arena = store.new_arena(root_lease.clone())
    typing.cast(Any, arena)._test_lease = root_lease
    try:
        yield arena
    finally:
        arena.close()
        root_lease.discard()


def _mem_l(memory_arena: MemoryArena) -> Any:
    return typing.cast(Any, memory_arena)._test_lease.clone()


async def _create_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.name", "T"], cwd=path, check=True)


def _create_test_entity(memory_arena: MemoryArena, value: str) -> FxtrID:
    ground_context = FxtrID.from_tag_and_payload(".", b"")
    memory_arena.track_builtin(_mem_l(memory_arena), ground_context)
    type_id = FxtrID.builtin("rehost_test_entity")
    memory_arena.track_builtin(_mem_l(memory_arena), type_id)
    entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        "value": value,
    }
    return memory_arena.store(_mem_l(memory_arena), entity)


# --- tests ------------------------------------------------------------------


async def test_rehost_data_roundtrip(tmp_path: Path, memory_arena: MemoryArena):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    entity_id = _create_test_entity(memory_arena, "hello")

    async with RepoEntityStore.open(repo, create=True) as store:
        await store.write_from_arena(entity_id, memory_arena, _mem_l(memory_arena))
        async with rehost_context(store) as info:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{info.base_url}/data/{entity_id.id_string()}",
                    headers={"Authorization": f"Bearer {info.token}"},
                )
    assert r.status_code == 200
    result = typing.cast(Any, cbor2.loads(r.content))
    decoded = typing.cast(Any, fxtr_cbor_loads(result["bytes"]))
    assert decoded["value"] == "hello"


async def test_rehost_rejects_missing_token(tmp_path: Path):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    fake_id = FxtrID.from_tag_and_payload("d", b"\x00" * 32)

    async with RepoEntityStore.open(repo, create=True) as store:
        async with rehost_context(store) as info:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{info.base_url}/data/{fake_id.id_string()}")
    assert r.status_code == 401


async def test_rehost_rejects_wrong_token(tmp_path: Path):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    fake_id = FxtrID.from_tag_and_payload("d", b"\x00" * 32)

    async with RepoEntityStore.open(repo, create=True) as store:
        async with rehost_context(store) as info:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{info.base_url}/data/{fake_id.id_string()}",
                    headers={"Authorization": "Bearer not-the-token"},
                )
    assert r.status_code == 401


async def test_http_deep_fetchable_round_trip(tmp_path: Path, memory_arena: MemoryArena):
    """The fxtr2 helper builds a working ``(DeepFetchable, Lease)`` from the
    rehost's URL + token — the remote the FOD writer fetches through."""
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    entity_id = _create_test_entity(memory_arena, "from_helper")

    async with RepoEntityStore.open(repo, create=True) as store:
        await store.write_from_arena(entity_id, memory_arena, _mem_l(memory_arena))
        async with rehost_context(store) as info:
            async with http_deep_fetchable(info.base_url, token=info.token) as (deep, lease):
                arena = await deep.try_lookup(entity_id)
                assert arena is not None
                flat = await arena.load_flat(lease.clone(), entity_id)
                assert flat.entity_bytes is not None
                decoded = typing.cast(Any, fxtr_cbor_loads(flat.entity_bytes))
                assert decoded["value"] == "from_helper"
                arena.close()


async def test_http_deep_fetchable_without_token_is_unauthorized(tmp_path: Path):
    """No token -> the rehost 401s -> ``try_lookup`` returns None."""
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    fake_id = FxtrID.from_tag_and_payload("d", b"\x00" * 32)

    async with RepoEntityStore.open(repo, create=True) as store:
        async with rehost_context(store) as info:
            async with http_deep_fetchable(info.base_url) as (deep, _lease):
                assert await deep.try_lookup(fake_id) is None


async def test_rehost_ephemeral_port_and_build_env(tmp_path: Path):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)

    async with RepoEntityStore.open(repo, create=True) as store:
        async with rehost_context(store) as info:
            assert info.port != 0
            assert info.base_url == f"http://127.0.0.1:{info.port}"
            # build_env defaults to the host-local URL...
            assert info.build_env() == {
                REMOTE_BASE_URL_ENV: info.base_url,
                REMOTE_BEARER_TOKEN_ENV: info.token,
            }
            # ...and substitutes a backend's build-visible URL when given.
            built = info.build_env("http://host.containers.internal:9999")
            assert built[REMOTE_BASE_URL_ENV] == "http://host.containers.internal:9999"
            assert built[REMOTE_BEARER_TOKEN_ENV] == info.token
