"""Tests for the LMDB-backed entity store (`locator.LMDBEntityLocator`)."""

import pickle
from concurrent.futures import ProcessPoolExecutor
from typing import Literal

import cbrrr
import pytest

from fxtr.object_model import core, locator


class LMDBInner(
    core.DataEntityBase, namespace_id="!testing.test_lmdb", type_id="LMDBInner^1"
):
    """Inner test entity (a child, to exercise subgraph writes)."""

    value: int


class LMDBOuter(
    core.DataEntityBase, namespace_id="!testing.test_lmdb", type_id="LMDBOuter^1"
):
    """Outer test entity that nests an inner entity."""

    inner: LMDBInner
    name: str


def _make_outer() -> LMDBOuter:
    return LMDBOuter(inner=LMDBInner(value=42), name="hello")


def _make_config(
    active: str, *, entity_store_format: "Literal['cbor_files', 'lmdb']" = "cbor_files"
):
    """Build a minimal FxtrConfig with the given data.active and entity store format."""
    from pathlib import Path

    from fxtr.api import fxtrconfig

    return fxtrconfig.FxtrConfig(
        author=fxtrconfig.AuthorConfig(name="t", email="t@e.org"),
        blueprint=fxtrconfig.BlueprintConfig(sources={}),
        data=fxtrconfig.DataConfig(active=active, entity_store_format=entity_store_format),
        resolver=fxtrconfig.ResolverConfig(trust_impl_code=True, default_py_provider="uv"),
        provider=fxtrconfig.ProviderConfig(
            uv=fxtrconfig.UvProviderConfig(
                virtual_env_dir="venvs", dynamic_source_packages=[], local_source_packages={}
            )
        ),
        config_path=Path("/tmp/fxtr.toml"),
    )


def test_round_trip(tmp_path):
    """An entity saved to LMDB reloads to an equal object, with its children."""
    loc = locator.LMDBEntityLocator(tmp_path / "entities.lmdb")
    outer = _make_outer()
    loc.save_entity(outer)

    cid = core.entity_cid(outer)
    assert loc.has_entity(cid)
    assert loc.has_entity(core.entity_cid(outer.inner))

    loader = locator.CachingEntityLoader.from_locator(loc)
    reconstructed = loader(cid)
    assert reconstructed == outer
    assert isinstance(reconstructed, LMDBOuter)
    assert reconstructed.inner.value == 42


def test_missing_entity(tmp_path):
    """Absent entities are reported missing and retrieval raises / try_retrieve defaults."""
    loc = locator.LMDBEntityLocator(tmp_path / "entities.lmdb")
    cid = core.entity_cid(_make_outer())
    assert not loc.has_entity(cid)
    with pytest.raises(ValueError):
        loc.retrieve(cid)
    assert loc.try_retrieve(cid, default="nope") == "nope"


def test_cid_mismatch_rejected(tmp_path):
    """write_entity_data rejects a declared CID that does not match the content hash."""
    loc = locator.LMDBEntityLocator(tmp_path / "entities.lmdb")
    inner = LMDBInner(value=1)
    data = core.typespec(inner).entity_data_to_ipld(inner)
    wrong_cid = core.entity_cid(LMDBInner(value=999))
    with pytest.raises(ValueError, match="CID mismatch"):
        loc.write_entity_data(data, wrong_cid)


def test_idempotent_writes(tmp_path):
    """Saving the same entity twice is a no-op the second time (content-addressed)."""
    loc = locator.LMDBEntityLocator(tmp_path / "entities.lmdb")
    outer = _make_outer()
    loc.save_entity(outer)
    loc.save_entity(outer)  # must not raise
    loader = locator.CachingEntityLoader.from_locator(loc)
    assert loader(core.entity_cid(outer)) == outer


def test_parity_with_file_locator(tmp_path):
    """LMDB and CBOR-file stores of the same entity reload identically."""
    outer = _make_outer()

    lmdb_loc = locator.LMDBEntityLocator(tmp_path / "entities.lmdb")
    file_loc = locator.ClosedBinaryCBOROnDiskLocator(tmp_path / "entities")
    lmdb_loc.save_entity(outer)
    file_loc.save_entity(outer)

    cid = core.entity_cid(outer)
    # Same membership and byte-identical stored values.
    assert lmdb_loc.has_entity(cid) and file_loc.has_entity(cid)
    lmdb_data, _ = lmdb_loc.retrieve(cid)
    file_data, _ = file_loc.retrieve(cid)
    assert cbrrr.encode_dag_cbor(lmdb_data) == cbrrr.encode_dag_cbor(file_data)

    # Same reconstructed Python object.
    from_lmdb = locator.CachingEntityLoader.from_locator(lmdb_loc)(cid)
    from_file = locator.CachingEntityLoader.from_locator(file_loc)(cid)
    assert from_lmdb == from_file == outer


def test_pickle_round_trip(tmp_path):
    """A pickled locator (env dropped) unpickles and can read the same store."""
    loc = locator.LMDBEntityLocator(tmp_path / "entities.lmdb")
    outer = _make_outer()
    loc.save_entity(outer)

    revived = pickle.loads(pickle.dumps(loc))
    assert revived.path == loc.path
    cid = core.entity_cid(outer)
    data, _ = revived.retrieve(cid)
    assert data is not None


def test_factory_selects_backend(tmp_path):
    """get_active_data_locator picks the backend from config and writes to the right place."""
    from fxtr.api import api_lib

    outer = _make_outer()
    cid = core.entity_cid(outer)

    # Default: CBOR files.
    cbor_cfg = _make_config(str(tmp_path / "cbor_active"), entity_store_format="cbor_files")
    cbor_loc = api_lib.get_active_data_locator(cbor_cfg)
    assert isinstance(cbor_loc, locator.ClosedBinaryCBOROnDiskLocator)
    cbor_loc.save_entity(outer)
    assert (tmp_path / "cbor_active" / "entities").is_dir()

    # lmdb format.
    lmdb_cfg = _make_config(str(tmp_path / "lmdb_active"), entity_store_format="lmdb")
    lmdb_loc = api_lib.get_active_data_locator(lmdb_cfg)
    assert isinstance(lmdb_loc, locator.LMDBEntityLocator)
    lmdb_loc.save_entity(outer)
    assert (tmp_path / "lmdb_active" / "entities.lmdb" / "data.mdb").exists()
    assert locator.load_entity_from_locator(lmdb_loc, cid) == outer


def _load_in_worker(loc: locator.LMDBEntityLocator, cid_bytes: bytes):
    """Top-level worker: reopen the env in a separate process and load an entity."""
    cid = cbrrr.CID(cid_bytes)
    return locator.load_entity_from_locator(loc, cid)


def test_process_pool_read(tmp_path):
    """The locator survives being shipped to a ProcessPoolExecutor (per-PID lazy env).

    This mirrors the run-graph cache build, which pickles a loader/locator to worker processes.
    """
    loc = locator.LMDBEntityLocator(tmp_path / "entities.lmdb")
    outer = _make_outer()
    loc.save_entity(outer)
    cid = core.entity_cid(outer)

    with ProcessPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(_load_in_worker, [loc, loc], [bytes(cid), bytes(cid)]))
    assert all(r == outer for r in results)


def test_process_pool_read_after_fork(tmp_path):
    """Forked workers can reopen a store the parent already opened (at-fork hook clears inherited).

    Reproduces the LMDB "already open in this process" trap: the parent opens the env (via the write
    below), then a fork-based pool reopens the same path in each child.
    """
    import multiprocessing as mp

    loc = locator.LMDBEntityLocator(tmp_path / "entities.lmdb")
    outer = _make_outer()
    loc.save_entity(outer)  # opens the env in this (parent) process
    cid = core.entity_cid(outer)

    ctx = mp.get_context("fork")
    with ProcessPoolExecutor(max_workers=2, mp_context=ctx) as ex:
        results = list(ex.map(_load_in_worker, [loc, loc], [bytes(cid), bytes(cid)]))
    assert all(r == outer for r in results)


def _load_migration_script():
    """Import scripts/migrate_entities_to_lmdb.py by path (it's a script, not a package)."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_entities_to_lmdb.py"
    spec = importlib.util.spec_from_file_location("migrate_entities_to_lmdb", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MIGRATION_CONFIG_TOML = """
[author]
name = "t"
email = "t@e.org"

[blueprint.sources.default]
module = "nonexistent"

[data]
active = "{active}"

[resolver]
trust_impl_code = true
default_py_provider = "uv"

[provider.uv]
virtual_env_dir = "venvs"
dynamic_source_packages = []
local_source_packages = {{}}
"""


def test_migration_round_trip(tmp_path, monkeypatch):
    """A file store migrates to LMDB; every entity reloads equal, and re-running is a no-op."""
    active = tmp_path / "active"
    active.mkdir()
    config_path = tmp_path / "fxtr.toml"
    config_path.write_text(_MIGRATION_CONFIG_TOML.format(active=active.as_posix()))

    # Populate a CBOR-file store with many entities (spread across shards).
    file_loc = locator.ClosedBinaryCBOROnDiskLocator(active / "entities")
    entities = [LMDBOuter(inner=LMDBInner(value=i), name=f"e{i}") for i in range(50)]
    for e in entities:
        file_loc.save_entity(e)
    all_cids = set()
    for e in entities:
        all_cids.add(core.entity_cid(e))
        all_cids.add(core.entity_cid(e.inner))

    mod = _load_migration_script()
    monkeypatch.setattr("sys.argv", ["migrate", str(config_path)])
    assert mod.main() == 0

    # The LMDB store exists and reloads every entity (parents + children) identically.
    lmdb_loc = locator.LMDBEntityLocator(active / "entities.lmdb")
    assert (active / "entities.lmdb" / "data.mdb").exists()
    for cid in all_cids:
        assert lmdb_loc.has_entity(cid)
    loader = locator.CachingEntityLoader.from_locator(lmdb_loc)
    for e in entities:
        assert loader(core.entity_cid(e)) == e
    lmdb_loc.close()

    # Re-running is a no-op (all shards already in the progress sidecar).
    monkeypatch.setattr("sys.argv", ["migrate", str(config_path)])
    assert mod.main() == 0


def _setup_file_store(tmp_path, n=60):
    """Create an `active` dir with a CBOR-file store of n entities + a config; return paths/cids."""
    active = tmp_path / "active"
    active.mkdir()
    config_path = tmp_path / "fxtr.toml"
    config_path.write_text(_MIGRATION_CONFIG_TOML.format(active=active.as_posix()))
    file_loc = locator.ClosedBinaryCBOROnDiskLocator(active / "entities")
    entities = [LMDBOuter(inner=LMDBInner(value=i), name=f"e{i}") for i in range(n)]
    for e in entities:
        file_loc.save_entity(e)
    return active, config_path, entities


def _assert_all_present(active, entities):
    lmdb_loc = locator.LMDBEntityLocator(active / "entities.lmdb")
    loader = locator.CachingEntityLoader.from_locator(lmdb_loc)
    for e in entities:
        assert loader(core.entity_cid(e)) == e
        assert lmdb_loc.has_entity(core.entity_cid(e.inner))
    lmdb_loc.close()


def test_migration_small_batches(tmp_path, monkeypatch):
    """A tiny commit-batch-size (forcing many multi-shard transactions) still round-trips."""
    active, config_path, entities = _setup_file_store(tmp_path)
    mod = _load_migration_script()
    monkeypatch.setattr(
        "sys.argv", ["migrate", str(config_path), "--commit-batch-size", "3"]
    )
    assert mod.main() == 0
    _assert_all_present(active, entities)


def test_migration_resume_with_stale_sidecar(tmp_path, monkeypatch):
    """Resume is correct even if the progress sidecar is lost after data was committed.

    Simulates a crash between a transaction commit and the progress save: the LMDB data is present
    but the sidecar doesn't list those shards. Re-running must self-heal (re-put idempotently) rather
    than abort, since append silently skips already-present keys.
    """
    active, config_path, entities = _setup_file_store(tmp_path)
    mod = _load_migration_script()
    monkeypatch.setattr("sys.argv", ["migrate", str(config_path), "--commit-batch-size", "5"])
    assert mod.main() == 0

    # Drop the sidecar entirely -> next run re-reads every shard over already-committed data.
    (active / "entities.lmdb.progress.json").unlink()
    monkeypatch.setattr("sys.argv", ["migrate", str(config_path), "--commit-batch-size", "5"])
    assert mod.main() == 0
    _assert_all_present(active, entities)
