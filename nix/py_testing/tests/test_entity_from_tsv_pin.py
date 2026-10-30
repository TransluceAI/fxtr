"""In-process tests for the ``entity_from_tsv_pin`` FOD writer (no nix, no HTTP).

The full env→rehost→fetch path is exercised by the part-2 nix E2E; here we test
the **core** (:func:`write_preload_layer`) against a direct in-process
``RepoEntityStore`` as the ``(DeepFetchable, Lease)`` remote: the entity's bytes
land locally, a pure reader (no remote) loads the pinned entity, and the output
is byte-stable for a fixed id (the FOD-hash determinism gate).
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import anyio
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.data.system_entities import define_type
from fxtr_nix_writer.writers import write_preload_layer

_TEST_NS = uuid.UUID("5f3b1a08-7c64-4d2e-9a11-2c8e6b4f0d93")


@dataclasses.dataclass(frozen=True)
class Note(DataclassEntity, fxtr_type=define_type(_TEST_NS, "preload_test_note")):
    text: str


async def _create_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.name", "T"], cwd=path, check=True)


async def _seed_store(store: RepoEntityStore, text: str) -> FxtrID:
    async with FxtrScope(name="seed") as seed:
        ref = seed.store(Note(text=text))
        await store.write_from_arena(
            ref.entity_id, seed._main_arena, seed._current_auth_lease().clone()
        )
        return ref.entity_id


def test_preload_layer_round_trip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    out = tmp_path / "layer"

    async def _run() -> None:
        await _create_git_repo(repo)
        async with RepoEntityStore.open(repo, create=True) as store:
            note_id = await _seed_store(store, "hello from the store")
            await write_preload_layer(str(out), "greeting", note_id, (store, store.get_lease()))

            # The pin is recorded and the entity's bytes are local (preloaded).
            assert layer_format.read_pins(out) == {"greeting": note_id}
            assert layer_format.data_file_exists(out, note_id)

            # A pure reader (no remote configured) loads the preloaded pin.
            async with LayeredFileStoreReader(out) as reader:
                ref = await reader.retrieve("greeting", Note, into_scope=reader.scope)
                assert ref.entity_id == note_id
                assert (await ref.load()).text == "hello from the store"

    anyio.run(_run)


def test_preload_layer_is_byte_stable(tmp_path: Path) -> None:
    """Building the same pin twice yields byte-identical layer contents (the
    FOD-hash determinism gate)."""
    repo = tmp_path / "repo"
    out1 = tmp_path / "layer1"
    out2 = tmp_path / "layer2"

    async def _run() -> None:
        await _create_git_repo(repo)
        async with RepoEntityStore.open(repo, create=True) as store:
            note_id = await _seed_store(store, "deterministic")
            await write_preload_layer(str(out1), "p", note_id, (store, store.get_lease()))
            await write_preload_layer(str(out2), "p", note_id, (store, store.get_lease()))

    anyio.run(_run)

    files1 = sorted(p.relative_to(out1).as_posix() for p in out1.rglob("*") if p.is_file())
    files2 = sorted(p.relative_to(out2).as_posix() for p in out2.rglob("*") if p.is_file())
    assert files1 == files2 and files1, files1
    for rel in files1:
        assert (out1 / rel).read_bytes() == (out2 / rel).read_bytes(), f"{rel} differs"


def test_missing_id_in_store_raises(tmp_path: Path) -> None:
    """A pin id absent from the remote fails loudly (mark_as_remote validates)."""
    repo = tmp_path / "repo"
    out = tmp_path / "layer"
    absent = FxtrID.from_tag_and_payload("d", b"\x00" * 32)

    async def _run() -> None:
        await _create_git_repo(repo)
        async with RepoEntityStore.open(repo, create=True) as store:
            try:
                await write_preload_layer(str(out), "p", absent, (store, store.get_lease()))
            except Exception as exc:  # LayerEntityMissingError
                assert "not" in str(exc).lower() or absent.id_string() in str(exc)
                return
            raise AssertionError("expected a missing-entity error")

    anyio.run(_run)
