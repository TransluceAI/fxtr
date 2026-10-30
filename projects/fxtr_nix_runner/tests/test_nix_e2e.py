"""Nix-marked end-to-end tests for the manager tier, over the backend sweep.

Builds a real flake fixture through `FlakeManager.build_span` on each available
backend (host + podman; the `nix_backend` fixture skips unavailable ones):

1. **FOD fetch** — the manager stands up the rehost over a populated repo store,
   wires `FXTR_REMOTE_*` into the build, and a fixed-output derivation fetches an
   entity's bytes from the rehost (bearer-authed). Proves build-time env delivery
   + networking + auth. The FOD's name is unique per run so it's never a nix
   store cache hit (a real fetch happens every run).
2. **Entities adapter (builtin)** — `mgr.entities(span).retrieve(...)` evaluates a
   `fxtr.entities.*` attr through real nix and resolves a builtin entity.

3. **Entity-from-TSV-pin (remote pointer)** — declare ``fxtr.entities.<name>``
   via ``lib.entityFromTsvPin`` (a preload FOD), build it through the manager
   (the FOD fetches the pinned entity's bytes from the rehost; its content dep
   is left remote), and read it back through ``mgr.entities(span)``, resolving
   that dep through the repo store. Exercises the documented v0 re-pin recipe
   (``lib.fakeHash`` → parse the mismatch → re-pin the live workdir).

4. **Provenanced builder** — a pure ``mkFxtrFnBuilder`` with ``provenanced =
   true`` builds a layer through real nix and is read back via
   ``mgr.entities(span)`` as a **layered** id, proving the provenance flag
   threads nix scaffold → ``builderSpec.provenanced`` → ``nix_builder`` →
   provenanced writer → ``mount_pin`` embed.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import shutil
import sys
import typing
import uuid
from pathlib import Path
from typing import Any

import pytest
from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
from fxtr2.data.memory_arena import MemoryArena, MemoryStore
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.data.system_entities import define_type
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from fxtr_nix_runner import HostBackend
from fxtr_nix_runner._backend import Backend
from fxtr_nix_runner._fod_lock import LOCK_FILENAME, read_lock
from fxtr_nix_runner.manager import FlakeManager
from fxtr_nix_runner.update_hashes import Mode, update_hashes
from fxtr_workdir_harness import prepare_workdir_repo

pytestmark = [pytest.mark.anyio, pytest.mark.slow]

_REHOST_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "rehost_fod"
_FOD_SENTINEL = b"fod-ok"

_TSV_PIN_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "tsv_pin_entity"
_PIN_KEY = "greeting"
_PIN_TEST_NS = uuid.UUID("b41d6e3a-9f02-47c8-8a5d-3e1c7b9046fa")

_BUILDER_FOD_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "builder_fod"
_PROVENANCED_BUILDER_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "provenanced_builder"


@dataclasses.dataclass(frozen=True)
class _PinLeaf(DataclassEntity, fxtr_type=define_type(_PIN_TEST_NS, "e2e_pin_leaf")):
    value: str


@dataclasses.dataclass(frozen=True)
class _PinParent(DataclassEntity, fxtr_type=define_type(_PIN_TEST_NS, "e2e_pin_parent")):
    child: ChildRef[_PinLeaf]


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


def _create_test_entity(memory_arena: MemoryArena, value: str) -> FxtrID:
    ground_context = FxtrID.from_tag_and_payload(".", b"")
    memory_arena.track_builtin(_mem_l(memory_arena), ground_context)
    type_id = FxtrID.builtin("nix_e2e_test_entity")
    memory_arena.track_builtin(_mem_l(memory_arena), type_id)
    entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        "value": value,
    }
    return memory_arena.store(_mem_l(memory_arena), entity)


def _fod_output_hash() -> str:
    """The flat-mode SRI hash of the FOD's fixed sentinel output."""
    return "sha256-" + base64.b64encode(hashlib.sha256(_FOD_SENTINEL).digest()).decode()


def _templated_fixture(src: Path, dest_template: Path, *, fod_name: str, entity_id: str) -> Path:
    """Copy the template and substitute the test-owned @...@ markers.

    (The fxtr-lib marker is left for ``prepare_workdir_repo``.)
    """
    shutil.copytree(src, dest_template)
    flake = dest_template / "flake.nix"
    text = (
        flake.read_text()
        .replace("@FOD_NAME@", fod_name)
        .replace("@FOD_ENTITY_ID@", entity_id)
        .replace("@FOD_OUTPUT_HASH@", _fod_output_hash())
    )
    flake.write_text(text)
    return dest_template


# --- tests ------------------------------------------------------------------


async def test_fod_fetch_through_build_span(
    tmp_path: Path, nix_backend: Backend, memory_arena: MemoryArena
) -> None:
    if isinstance(nix_backend, HostBackend) and sys.platform == "darwin":
        # On macOS the host backend offloads the linux FOD build to a remote
        # builder (e.g. ssh-ng://…-vm), which can't reach the mac host's
        # 127.0.0.1 rehost — so expose_host_endpoint's loopback URL is
        # unreachable from the build. The podman backend is the validated path
        # on macOS (host.containers.internal routes to the host); the host
        # backend works for this only where builds run locally (e.g. linux CI).
        pytest.skip("host backend offloads linux builds on macOS; rehost unreachable (use podman)")

    entity_id = _create_test_entity(memory_arena, f"fod-payload-{uuid.uuid4().hex}")
    tmpl = _templated_fixture(
        _REHOST_TEMPLATE,
        tmp_path / "template",
        fod_name=f"rehost-fod-{uuid.uuid4().hex}",
        entity_id=entity_id.id_string(),
    )
    prepared = await prepare_workdir_repo(tmpl, tmp_path / "wd")

    # Seed the repo store the manager will rehost (gitignored, local).
    async with RepoEntityStore.open(prepared.path, create=True) as store:
        await store.write_from_arena(entity_id, memory_arena, _mem_l(memory_arena))

    async with FlakeManager(prepared.path, backend=nix_backend) as mgr:
        async with mgr.build_span() as span:
            ref = f"{span.handle.flake_ref_root()}#fodFetch"
            result = await span.run_nix(["build", "--no-link", "--print-out-paths", ref])
            assert result.returncode == 0, result.stderr
            store_path = result.stdout.strip().splitlines()[-1]
            mat = await span.handle.materialize(store_path, closure=False)
            # The FOD only succeeds if it reached the rehost, authed, and got
            # real bytes — so a clean build already proves the path. Reading the
            # sentinel back also exercises materialize on both backends.
            assert Path(mat.host_path).read_bytes() == _FOD_SENTINEL


async def test_entities_adapter_builtin_retrieve(tmp_path: Path, nix_backend: Backend) -> None:
    tmpl = _templated_fixture(
        _REHOST_TEMPLATE,
        tmp_path / "template",
        fod_name=f"rehost-fod-{uuid.uuid4().hex}",
        entity_id="unused",  # the builtin path builds/fetches nothing
    )
    prepared = await prepare_workdir_repo(tmpl, tmp_path / "wd")

    async with FlakeManager(prepared.path, backend=nix_backend) as mgr:
        async with mgr.build_span() as span:
            async with FxtrScope(name="consumer", isolated=True) as scope:
                fid = await mgr.entities(span).retrieve("demo", into_scope=scope)
    assert fid == FxtrID.from_id_string("!basic_type")


async def test_provenanced_builder_through_manager(tmp_path: Path, nix_backend: Backend) -> None:
    """A pure ``mkFxtrFnBuilder`` with ``provenanced = true``, building a
    genuinely-provenanced entity, read back via the entities adapter through real
    nix: the whole chain (nix scaffold → ``builderSpec.provenanced`` →
    ``nix_builder`` → preserve-mode writer → ``mount_pin`` embed) yields a
    **layered** ``(data, prov)`` id."""
    tmpl = tmp_path / "template"
    shutil.copytree(_PROVENANCED_BUILDER_TEMPLATE, tmpl)
    prepared = await prepare_workdir_repo(tmpl, tmp_path / "wd")

    async with FlakeManager(prepared.path, backend=nix_backend) as mgr:
        async with mgr.build_span() as span:
            async with FxtrScope(name="consumer", isolated=True) as scope:
                fid = await mgr.entities(span).retrieve("thePin", into_scope=scope)
    # The entities adapter mounted a provenanced layer → a layered (data, prov)
    # id (the entity carried a c-node, so the prov half is an n-node).
    assert isinstance(fid, FxtrID)
    assert fid._provenance_parent is not None


def _template_tsv_pin_fixture(dest: Path, *, entity_id: str) -> Path:
    """Copy the tsv_pin_entity template, substituting the pin key + id (the
    fxtr-lib marker stays for prepare_workdir_repo; the FOD hash comes from
    fod_lock.toml, which `update_hashes` writes — no hash marker)."""
    shutil.copytree(_TSV_PIN_TEMPLATE, dest)
    for rel in ("flake.nix", "root.fxtr.tsv"):
        p = dest / rel
        p.write_text(p.read_text().replace("@PIN_KEY@", _PIN_KEY).replace("@ENTITY_ID@", entity_id))
    return dest


def _set_pin_id(repo: Path, entity_id: str) -> None:
    """Rewrite the committed TSV so `greeting` resolves to a different id."""
    tsv = repo / "root.fxtr.tsv"
    tsv.write_text(f"{_PIN_KEY}\t{entity_id}\n")


async def test_entity_from_tsv_pin_remote_pointer(tmp_path: Path, nix_backend: Backend) -> None:
    if isinstance(nix_backend, HostBackend) and sys.platform == "darwin":
        pytest.skip("host backend offloads linux builds on macOS; rehost unreachable (use podman)")

    # Build the entity (parent -> leaf) to learn its id, then seed it into the
    # repo store. preload(parent) writes the parent's bytes into the FOD layer
    # but leaves the leaf remote (precomp) — so reading the parent back exercises
    # remote-dep resolution through the repo store.
    async with FxtrScope(name="seed") as seed:
        leaf_ref = seed.store(_PinLeaf(value="deep_e2e"))
        parent_ref = seed.store(_PinParent(child=leaf_ref))
        entity_id = parent_ref.entity_id

        tmpl = _template_tsv_pin_fixture(tmp_path / "template", entity_id=entity_id.id_string())
        prepared = await prepare_workdir_repo(tmpl, tmp_path / "wd")

        async with RepoEntityStore.open(prepared.path, create=True) as store:
            await store.write_from_arena(
                entity_id, seed._main_arena, seed._current_auth_lease().clone()
            )

    # Populate the lock with the real FOD hash via the runner-owned tool
    # (replaces the manual fakeHash -> parse-mismatch -> re-pin recipe). The
    # fixture commits an empty lock; update-hashes discovers `thePin` as a new
    # slot, builds it, and records its hash.
    plan = await update_hashes(prepared.path, backend=nix_backend)
    assert plan.relock == ["thePin"] and plan.reasons["thePin"] == "new"
    lock = read_lock(prepared.path / LOCK_FILENAME)
    assert lock.hashes["thePin"].startswith("sha256-")

    # rev=None: read the live (now-locked) workdir.
    async with FlakeManager(prepared.path, backend=nix_backend) as mgr:
        async with mgr.build_span() as span:
            # Read it back through the entities adapter: builds the now-pinned FOD
            # layer, mounts the pin into the manager scope; loading the parent and
            # following its child resolves the (remote) leaf through the repo store.
            ref = await mgr.entities(span).retrieve("thePin", _PinParent)
            assert isinstance(ref, EntityRef)
            assert ref.entity_id == entity_id
            loaded = await mgr.scope.load(ref)
            leaf = await loaded.child.load(mgr.scope)
            assert leaf.value == "deep_e2e"


def _template_builder_fod_fixture(dest: Path, *, entity_id: str) -> Path:
    """Copy the builder_fod template, substituting the spec entity id (the
    fxtr-lib marker stays for prepare_workdir_repo; the FOD hash comes from
    fod_lock.toml, which `update_hashes` writes)."""
    shutil.copytree(_BUILDER_FOD_TEMPLATE, dest)
    flake = dest / "flake.nix"
    flake.write_text(flake.read_text().replace("@ENTITY_ID@", entity_id))
    return dest


async def test_fod_builder_through_rehost(tmp_path: Path, nix_backend: Backend) -> None:
    """A part-3 FOD builder (`mkFxtrFnBuilder`, outputHash + impureEnvVars) run
    through the manager's build span: its entrypoint builds `ctx.remote` from the
    delivered `FXTR_REMOTE_*`, fetches the pinned entity via the rehost, and pins
    it (leaving the content dep remote). The net-new seam over the other FOD E2Es
    is `nix_builder.amain`'s env → `http_deep_fetchable` path inside a real nix
    FOD; downstream (preload, remote pointers, read-back) mirrors the
    entity-from-tsv-pin test.
    """
    if isinstance(nix_backend, HostBackend) and sys.platform == "darwin":
        pytest.skip("host backend offloads linux builds on macOS; rehost unreachable (use podman)")

    async with FxtrScope(name="seed") as seed:
        leaf_ref = seed.store(_PinLeaf(value="builder_deep"))
        parent_ref = seed.store(_PinParent(child=leaf_ref))
        entity_id = parent_ref.entity_id

        tmpl = _template_builder_fod_fixture(tmp_path / "template", entity_id=entity_id.id_string())
        prepared = await prepare_workdir_repo(tmpl, tmp_path / "wd")

        async with RepoEntityStore.open(prepared.path, create=True) as store:
            await store.write_from_arena(
                entity_id, seed._main_arena, seed._current_auth_lease().clone()
            )

    # Lock the builder FOD's hash via the runner-owned tool (it discovers `thePin`
    # as a new preload slot, builds it through the rehost, records its hash).
    plan = await update_hashes(prepared.path, backend=nix_backend)
    assert plan.relock == ["thePin"] and plan.reasons["thePin"] == "new"
    lock = read_lock(prepared.path / LOCK_FILENAME)
    assert lock.hashes["thePin"].startswith("sha256-")

    async with FlakeManager(prepared.path, backend=nix_backend) as mgr:
        async with mgr.build_span() as span:
            ref = await mgr.entities(span).retrieve("thePin", _PinParent)
            assert isinstance(ref, EntityRef)
            assert ref.entity_id == entity_id
            loaded = await mgr.scope.load(ref)
            # The builder preloaded the parent's bytes but left the leaf remote;
            # following the child resolves it through the repo store.
            leaf = await loaded.child.load(mgr.scope)
            assert leaf.value == "builder_deep"


async def test_update_hashes_incremental_and_explicit_relock(
    tmp_path: Path, nix_backend: Backend
) -> None:
    """new -> relock, re-run -> no-op (default leaves locked slots), `-k` re-locks.

    Seeds two entities, then drives `update_hashes`: a fresh lock relocks
    `thePin` as *new*; an immediate re-run is a no-op (default never touches an
    already-locked slot); repointing the committed TSV at the other entity does
    *not* trigger a default relock (in-place input edits aren't auto-detected),
    but an explicit `-k thePin` re-locks it to the new content's hash.
    """
    if isinstance(nix_backend, HostBackend) and sys.platform == "darwin":
        pytest.skip("host backend offloads linux builds on macOS; rehost unreachable (use podman)")

    async with FxtrScope(name="seed") as seed:
        id_a = seed.store(_PinLeaf(value="alpha")).entity_id
        id_b = seed.store(_PinLeaf(value="bravo")).entity_id

        tmpl = _template_tsv_pin_fixture(tmp_path / "template", entity_id=id_a.id_string())
        prepared = await prepare_workdir_repo(tmpl, tmp_path / "wd")

        async with RepoEntityStore.open(prepared.path, create=True) as store:
            lease = seed._current_auth_lease()
            await store.write_from_arena(id_a, seed._main_arena, lease.clone())
            await store.write_from_arena(id_b, seed._main_arena, lease.clone())

    lock_path = prepared.path / LOCK_FILENAME

    # 1. fresh lock -> relock `thePin` as new.
    p1 = await update_hashes(prepared.path, backend=nix_backend)
    assert p1.relock == ["thePin"] and p1.reasons["thePin"] == "new"
    hash_a = read_lock(lock_path).hashes["thePin"]
    assert hash_a.startswith("sha256-")

    # 2. nothing new/stale -> default no-op, lock untouched.
    p2 = await update_hashes(prepared.path, backend=nix_backend)
    assert p2.relock == [] and p2.unchanged == ["thePin"] and not p2.changed
    assert read_lock(lock_path).hashes["thePin"] == hash_a

    # 3. repoint the pin in place. The default still won't touch the locked slot
    #    (in-place edits aren't auto-detected); an explicit `-k` re-locks it.
    _set_pin_id(prepared.path, id_b.id_string())
    p3 = await update_hashes(prepared.path, backend=nix_backend)
    assert p3.relock == [] and read_lock(lock_path).hashes["thePin"] == hash_a

    p4 = await update_hashes(prepared.path, mode=Mode.KEYS, keys=["thePin"], backend=nix_backend)
    assert p4.relock == ["thePin"] and p4.reasons["thePin"] == "requested"
    assert read_lock(lock_path).hashes["thePin"] != hash_a
