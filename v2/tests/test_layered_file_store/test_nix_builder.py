"""Unit tests for the nix-agnostic core of ``nix_builder`` (``_run`` + ``ctx``).

Exercises the builder primitive without nix: a tmpdir for ``$out`` + parent
layers, an in-process ``RepoEntityStore`` as the FOD remote. Covers the pure
path (read a parent layer, derive + pin a new entity), the FOD path (``ctx.remote``
populated; mark/preload/pin a remote entity), spec passthrough with no parents,
and the ``"module:fn"`` resolver.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

import fxtr2.data.system_entities  # noqa: F401  (register fallback-ctx builtins early)
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawWeakFxtrID,
    freeze_fxtr_cbor,
)
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store import nix_builder
from fxtr2.data.layered_file_store.nix_build_provenance import (
    NixFixedOutputBuildProvenance,
    NixInputAddressedBuildProvenance,
)
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from fxtr2.data.repo_storage import RepoEntityStore
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent

assert fxtr2.data.system_entities is not None
_GROUND = FxtrID.from_tag_and_payload(".", b"")


def _make_c_node(scope: FxtrScope) -> FxtrID:
    """A c-node usable as a scope ``provenance_context`` (makes stores layered)."""
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=_GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=_GROUND),
        "ctx_key": "nix_builder_test",
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), data)


async def _create_git_repo(path: Path) -> None:
    """``RepoEntityStore.open`` discovers the VCS root, so the store dir must be
    a repo (matches ``tests/test_repo_storage.py``)."""
    path.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.name", "T"], cwd=path, check=True)


async def _build_leaf_layer(layer_dir: Path, *, name: str, value: int) -> None:
    """Build a single-layer store at ``layer_dir`` pinning one ``LfsLeaf``
    under ``"leaf"`` (the parent-layer input the builder reads)."""
    async with FxtrScope(name="seed") as scope:
        ref = scope.store(LfsLeaf(name=name, value=value))
        async with LayeredFileStoreIsolatedWriter(layer_dir, parents=[]) as writer:
            await writer.pin_to_this_layer("leaf", ref.entity_id, scope)


@pytest.mark.anyio
async def test_pure_builder_reads_parent_and_pins_derived(tmp_path: Path):
    """A pure builder: read the parent layer's ``leaf`` through
    ``ctx.readers``, derive an ``LfsParent`` on top in ``ctx.scope``, pin it.
    Read the output back (with the parent) and confirm the chain resolves —
    no remote anywhere."""
    base = tmp_path / "base"
    out = tmp_path / "out"
    await _build_leaf_layer(base, name="leaf", value=42)

    async def entry(ctx: nix_builder.BuilderContext) -> None:
        assert ctx.remote is None  # pure
        assert ctx.spec == {"label": "derived"}
        # Bare retrieve(into_scope=None) lands in the active ctx.scope (which
        # includes the parent reader's scope), so the ref is usable here.
        leaf_ref = await ctx.readers["base"].retrieve("leaf", LfsLeaf)
        parent_ref = ctx.scope.store(LfsParent(name=ctx.spec["label"], child=leaf_ref))
        await ctx.writer.pin_to_this_layer("parent", parent_ref.entity_id, ctx.scope)

    await nix_builder._run(
        entry, out_dir=out, parents={"base": base}, remote=None, spec={"label": "derived"}
    )

    async with LayeredFileStoreReader(out) as reader:
        parent = await (await reader.retrieve("parent", LfsParent)).load()
        assert parent.name == "derived"
        leaf = await parent.child.load()
        assert leaf.name == "leaf"
        assert leaf.value == 42


@pytest.mark.anyio
async def test_fod_builder_preloads_remote_entity(tmp_path: Path):
    """A FOD builder: ``ctx.remote`` is populated; mark + preload a remote
    entity's bytes into the output layer and pin it. Read it back (with the
    store as remote) and confirm the bytes are present."""
    repo = tmp_path / "repo"
    out = tmp_path / "out"
    await _create_git_repo(repo)

    async with RepoEntityStore.open(repo, create=True) as store:
        # Seed the remote store with one entity (by id).
        async with FxtrScope(name="seed") as seed:
            leaf_ref = seed.store(LfsLeaf(name="remote-leaf", value=7))
            leaf_id = leaf_ref.entity_id
            await store.write_from_arena(
                leaf_id, seed._main_arena, seed._current_auth_lease().clone()
            )

        captured: dict[str, object] = {}

        async def entry(ctx: nix_builder.BuilderContext) -> None:
            assert ctx.remote is not None  # FOD
            await ctx.writer.mark_as_remote(leaf_id)
            loaded = await ctx.writer.preload(leaf_ref)
            captured["loaded"] = loaded
            await ctx.writer.pin_to_this_layer("leaf", leaf_id)

        await nix_builder._run(
            entry,
            out_dir=out,
            parents={},
            remote=(store, store.get_lease()),
            spec={},
        )

        # preload returned the decoded entity to the entrypoint.
        assert isinstance(captured["loaded"], LfsLeaf)
        assert captured["loaded"].value == 7  # type: ignore[union-attr]

        # The bytes landed locally; reading back resolves without touching the
        # remote (the store is supplied anyway, mirroring production).
        async with LayeredFileStoreReader(out, remote=(store, store.get_lease())) as reader:
            leaf = await (await reader.retrieve("leaf", LfsLeaf)).load()
            assert leaf.name == "remote-leaf"
            assert leaf.value == 7


@pytest.mark.anyio
async def test_pure_no_parents_spec_passthrough(tmp_path: Path):
    """No parents, no remote: the entrypoint constructs a fresh entity from
    ``ctx.spec`` and pins it."""
    out = tmp_path / "out"

    async def entry(ctx: nix_builder.BuilderContext) -> None:
        assert ctx.remote is None
        assert ctx.parents == {}
        ref = ctx.scope.store(LfsLeaf(name=ctx.spec["name"], value=ctx.spec["value"]))
        await ctx.writer.pin_to_this_layer("leaf", ref.entity_id, ctx.scope)

    await nix_builder._run(
        entry, out_dir=out, parents={}, remote=None, spec={"name": "fresh", "value": 99}
    )

    async with LayeredFileStoreReader(out) as reader:
        leaf = await (await reader.retrieve("leaf", LfsLeaf)).load()
        assert leaf.name == "fresh"
        assert leaf.value == 99


async def _load_build_provenance(reader: LayeredFileStoreReader, n_node: FxtrID, entity_type: type):
    """From a layered entity's provenance parent (an n-node minted by the auto
    path), reach the build-step c-node it was stored under (the n-node's
    ``$fxtr.ctx`` marker) and load that c-node's d-node payload as ``entity_type``
    — the build-provenance entity the builder minted."""
    n_cbor = await reader.scope._main_arena.load(reader.scope.clone_authorization(), n_node)
    assert isinstance(n_cbor, dict)
    marker = n_cbor["$fxtr.ctx"]
    assert isinstance(marker, RawFxtrEntityMarkerID)
    c_node = marker.inner_id
    assert c_node.get_tag() == "c"
    d_id = reader.scope.c_node_to_context_data(c_node)
    return await reader.scope.load(EntityRef(d_id, entity_type))


@pytest.mark.anyio
async def test_provenanced_builder_mints_input_addressed_context(tmp_path: Path):
    """A pure (input-addressed) ``_run(provenanced=True)`` mints a default
    build-step ``provenance_context`` on ``ctx.scope``: a freshly-stored entity
    reads back **layered** with a ``NixInputAddressedBuildProvenance`` c-node
    that anchors on ``$out`` (== ``out_dir``). No per-script work."""
    out = tmp_path / "out"

    async def entry(ctx: nix_builder.BuilderContext) -> None:
        ref = ctx.scope.store(LfsLeaf(name="leaf", value=5))
        await ctx.writer.pin_to_this_layer("leaf", ref.entity_id, ctx.scope)

    await nix_builder._run(entry, out_dir=out, parents={}, remote=None, spec={}, provenanced=True)

    assert fmt.read_layer_format(out).provenanced is True
    async with LayeredFileStoreReader(out) as reader:
        ref = await reader.retrieve("leaf", LfsLeaf)
        n_node = ref.entity_id._provenance_parent
        assert n_node is not None and n_node.get_tag() == "n"
        prov = await _load_build_provenance(reader, n_node, NixInputAddressedBuildProvenance)
        assert prov.output_store_path == str(out)
        assert prov.provenance_info is None
        assert (await ref.load()) == LfsLeaf(name="leaf", value=5)


@pytest.mark.anyio
async def test_provenanced_builder_carries_provenance_info(tmp_path: Path):
    """``provenance_info`` rides into the input-addressed build-provenance
    entity verbatim (the author-declared supplement)."""
    out = tmp_path / "out"
    info = freeze_fxtr_cbor({"desc": "the demo report build"})

    async def entry(ctx: nix_builder.BuilderContext) -> None:
        ref = ctx.scope.store(LfsLeaf(name="leaf", value=5))
        await ctx.writer.pin_to_this_layer("leaf", ref.entity_id, ctx.scope)

    await nix_builder._run(
        entry, out_dir=out, parents={}, remote=None, spec={}, provenanced=True, provenance_info=info
    )

    async with LayeredFileStoreReader(out) as reader:
        ref = await reader.retrieve("leaf", LfsLeaf)
        n_node = ref.entity_id._provenance_parent
        assert n_node is not None
        prov = await _load_build_provenance(reader, n_node, NixInputAddressedBuildProvenance)
        assert prov.output_store_path == str(out)
        assert prov.provenance_info == info


@pytest.mark.anyio
async def test_provenanced_fod_builder_mints_fixed_output_context(tmp_path: Path):
    """A FOD provenanced builder (``remote is not None``) anchors on declared
    ``provenance_info`` instead of ``$out`` — a ``NixFixedOutputBuildProvenance``
    c-node, no store path. (Uses an in-process store as the remote; the entity is
    fresh, so no remote fetch is exercised — only the build-kind branch.)"""
    repo = tmp_path / "repo"
    out = tmp_path / "out"
    info = freeze_fxtr_cbor({"desc": "fixed-output build"})
    await _create_git_repo(repo)

    async def entry(ctx: nix_builder.BuilderContext) -> None:
        ref = ctx.scope.store(LfsLeaf(name="leaf", value=5))
        await ctx.writer.pin_to_this_layer("leaf", ref.entity_id, ctx.scope)

    async with RepoEntityStore.open(repo, create=True) as store:
        await nix_builder._run(
            entry,
            out_dir=out,
            parents={},
            remote=(store, store.get_lease()),
            spec={},
            provenanced=True,
            provenance_info=info,
        )

    async with LayeredFileStoreReader(out) as reader:
        ref = await reader.retrieve("leaf", LfsLeaf)
        n_node = ref.entity_id._provenance_parent
        assert n_node is not None and n_node.get_tag() == "n"
        prov = await _load_build_provenance(reader, n_node, NixFixedOutputBuildProvenance)
        assert prov.provenance_info == info


@pytest.mark.anyio
async def test_provenanced_fod_builder_requires_provenance_info(tmp_path: Path):
    """A FOD provenanced builder with no ``provenance_info`` raises — there is no
    safe ``$out`` anchor to fall back to (the ``_run`` defense for the nix-side
    eval throw)."""
    repo = tmp_path / "repo"
    out = tmp_path / "out"
    await _create_git_repo(repo)

    async def entry(ctx: nix_builder.BuilderContext) -> None:
        ref = ctx.scope.store(LfsLeaf(name="leaf", value=5))
        await ctx.writer.pin_to_this_layer("leaf", ref.entity_id, ctx.scope)

    with pytest.raises(ValueError, match="must declare provenanceInfo"):
        async with RepoEntityStore.open(repo, create=True) as store:
            await nix_builder._run(
                entry,
                out_dir=out,
                parents={},
                remote=(store, store.get_lease()),
                spec={},
                provenanced=True,
                provenance_info=None,
            )


@pytest.mark.anyio
async def test_unprovenanced_builder_stores_bare(tmp_path: Path):
    """``provenanced=False`` sets no ``provenance_context`` on the builder scope,
    so a freshly-stored entity stays bare (no build c-node)."""
    out = tmp_path / "out"

    async def entry(ctx: nix_builder.BuilderContext) -> None:
        ref = ctx.scope.store(LfsLeaf(name="leaf", value=5))
        await ctx.writer.pin_to_this_layer("leaf", ref.entity_id, ctx.scope)

    await nix_builder._run(entry, out_dir=out, parents={}, remote=None, spec={}, provenanced=False)

    async with LayeredFileStoreReader(out) as reader:
        ref = await reader.retrieve("leaf", LfsLeaf)
        assert ref.entity_id._provenance_parent is None
        assert (await ref.load()) == LfsLeaf(name="leaf", value=5)


@pytest.mark.anyio
async def test_provenanced_builder_composes_over_provenanced_parent(tmp_path: Path):
    """The report case: a provenanced builder reads a *genuinely-provenanced*
    (layered) input from a parent layer, derives an entity on top (which becomes
    layered via ``store_cbor``'s auto path), and pins it. Reading the output back
    reconstructs the layered parent with its nested child still provenanced."""
    base = tmp_path / "base"
    out = tmp_path / "out"
    # A provenanced base layer with a genuinely-provenanced leaf (stored as a
    # [data, prov] pair via its c-node).
    async with FxtrScope(name="builder") as builder:
        c_node = _make_c_node(builder)
        async with FxtrScope(
            provenance_context=c_node, provenance_context_from_scope=builder, name="seed"
        ) as seed:
            leaf_ref = seed.store(LfsLeaf(name="leaf", value=42))
            assert leaf_ref.entity_id._provenance_parent is not None
            async with LayeredFileStoreIsolatedWriter(base, parents=[], provenanced=True) as w:
                await w.pin_to_this_layer("leaf", leaf_ref.entity_id, seed)

    async def entry(ctx: nix_builder.BuilderContext) -> None:
        # The provenanced parent reader hands back a layered leaf id.
        layered_leaf = await ctx.readers["base"].retrieve("leaf", LfsLeaf)
        assert layered_leaf.entity_id._provenance_parent is not None
        # Deriving on top yields a layered parent (auto prov path, layered child).
        parent_ref = ctx.scope.store(LfsParent(name="p", child=layered_leaf))
        assert parent_ref.entity_id._provenance_parent is not None
        await ctx.writer.pin_to_this_layer("parent", parent_ref.entity_id, ctx.scope)

    await nix_builder._run(
        entry, out_dir=out, parents={"base": base}, remote=None, spec={}, provenanced=True
    )

    async with LayeredFileStoreReader(out) as reader:
        ref = await reader.retrieve("parent", LfsParent)
        assert ref.entity_id._provenance_parent is not None
        parent = await ref.load()
        assert parent.name == "p"
        assert parent.child.entity_id._provenance_parent is not None  # nested provenance
        leaf = await parent.child.load()
        assert leaf == LfsLeaf(name="leaf", value=42)


@pytest.mark.anyio
async def test_amain_reads_provenanced_from_builder_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The nix-coupled surface: ``amain`` reads ``builderSpec.provenanced`` from
    ``$NIX_ATTRS_JSON_FILE`` and opens the ``$out`` writer in preserve mode —
    proven by a *layered* entity reading back layered (a strip writer would have
    stored it bare). Fakes the build contract (no nix); pure (no ``FXTR_REMOTE_*``)."""
    out = tmp_path / "out"
    attrs = tmp_path / "attrs.json"
    attrs.write_text(json.dumps({"builderSpec": {"parents": {}, "spec": {}, "provenanced": True}}))
    monkeypatch.setenv("NIX_ATTRS_JSON_FILE", str(attrs))
    monkeypatch.setenv("out", str(out))
    monkeypatch.delenv(nix_builder.REMOTE_BASE_URL_ENV, raising=False)

    async def entry(ctx: nix_builder.BuilderContext) -> None:
        # Produce a genuinely-layered entity via a provenance_context sub-scope.
        c_node = _make_c_node(ctx.scope)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=ctx.scope,
            include=[ctx.scope],
            name="prov",
        ) as prov_scope:
            ref = prov_scope.store(LfsLeaf(name="leaf", value=11))
            assert ref.entity_id._provenance_parent is not None
            await ctx.writer.pin_to_this_layer("leaf", ref.entity_id, prov_scope)

    await nix_builder.amain(entry)

    assert fmt.read_layer_format(out).provenanced is True
    async with LayeredFileStoreReader(out) as reader:
        ref = await reader.retrieve("leaf", LfsLeaf)
        assert ref.entity_id._provenance_parent is not None  # preserved (layered)
        assert (await ref.load()) == LfsLeaf(name="leaf", value=11)


@pytest.mark.anyio
async def test_amain_reads_provenance_info_from_builder_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``amain`` reads ``builderSpec.provenanceInfo`` (plain JSON), freezes it,
    and threads it into the minted input-addressed build-provenance entity —
    proven by reading the declared info back off the c-node. Pure build."""
    out = tmp_path / "out"
    attrs = tmp_path / "attrs.json"
    attrs.write_text(
        json.dumps(
            {
                "builderSpec": {
                    "parents": {},
                    "spec": {},
                    "provenanced": True,
                    "provenanceInfo": {"desc": "via amain", "n": 3},
                }
            }
        )
    )
    monkeypatch.setenv("NIX_ATTRS_JSON_FILE", str(attrs))
    monkeypatch.setenv("out", str(out))
    monkeypatch.delenv(nix_builder.REMOTE_BASE_URL_ENV, raising=False)

    async def entry(ctx: nix_builder.BuilderContext) -> None:
        ref = ctx.scope.store(LfsLeaf(name="leaf", value=11))
        await ctx.writer.pin_to_this_layer("leaf", ref.entity_id, ctx.scope)

    await nix_builder.amain(entry)

    async with LayeredFileStoreReader(out) as reader:
        ref = await reader.retrieve("leaf", LfsLeaf)
        n_node = ref.entity_id._provenance_parent
        assert n_node is not None
        prov = await _load_build_provenance(reader, n_node, NixInputAddressedBuildProvenance)
        assert prov.output_store_path == str(out)
        assert prov.provenance_info == freeze_fxtr_cbor({"desc": "via amain", "n": 3})


def test_resolve_entrypoint_roundtrip():
    """``_resolve_entrypoint`` resolves ``"module:attr"`` and rejects a bare
    module."""
    assert (
        nix_builder._resolve_entrypoint("fxtr2.data.layered_file_store.nix_builder:_run")
        is nix_builder._run
    )
    with pytest.raises(SystemExit):
        nix_builder._resolve_entrypoint("no_colon_here")
