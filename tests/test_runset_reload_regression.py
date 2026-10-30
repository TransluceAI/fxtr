"""Stage 0 regression tests for runset / run-graph reload across session-manager reopens.

These tests pin the *observable* behavior of reopening a ``SessionManager`` against an existing
runset: prior results stay retrievable, dependency/dependent edges are reconstructed, workflows
rebuild, and drops behave consistently. They are intentionally written against behavior, not the
cache file format, so they guard both the current pickle cache and the planned durable sidecar
(see notes/2026_06_15_durable_cache.md).

Coverage focus (previously untested):
- cold rebuild (cache deleted) reconstructs a multi-node dependency graph correctly;
- workflows survive reopen, including the cold-rebuild path;
- dependency/dependent edges survive reopen (instantiating a downstream handle works);
- drop-with-dependents removes the transitive closure, within a session and after reopen.
"""

import shutil
import tempfile
from pathlib import Path
from typing import Generator

import pickle

import cbrrr
import pytest
import tomlkit
from sqlitedict import SqliteDict

from fxtr.api import api_lib, fxtrconfig, session_manager
from fxtr.api.session_manager import RunGraphCacheBuilder, RunNode
from fxtr.impl_plans import plan_helpers
from fxtr.object_model import core, locator


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def isolated_test_config() -> Generator[fxtrconfig.FxtrConfig, None, None]:
    """Function-scoped config with a fresh state directory, so each test can reopen the manager."""
    temp_dir = Path(tempfile.mkdtemp(prefix="fxtr_test_reload_"))

    try:
        config_data = {
            "author": {"name": "Test Author", "email": "test@example.com"},
            "blueprint": {"sources": {}},
            "data": {"active": str(temp_dir / "data")},
            "resolver": {"trust_impl_code": True, "default_py_provider": "in_process"},
            "provider": {
                "uv": {
                    "virtual_env_dir": str(temp_dir / "tmp_venvs"),
                    "dynamic_source_packages": [],
                    "local_source_packages": {},
                },
            },
        }

        temp_config_path = temp_dir / "fxtr.toml"
        with open(temp_config_path, "w") as f:
            f.write(tomlkit.dumps(config_data))

        config = fxtrconfig.parse_fxtr_config(temp_config_path)
        state_dir = Path(config.data.active)
        state_dir.mkdir(parents=True, exist_ok=True)

        yield config

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ----------------------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------------------


_CACHE_PREFIX = api_lib.RUN_GRAPH_CACHE_KEY_PREFIX


def _active_store_path(config: fxtrconfig.FxtrConfig) -> Path:
    """The runset SqliteDict, which also holds the co-located run-graph cache rows."""
    return api_lib.get_active_runset_path(config)


def _delete_run_graph_cache(config: fxtrconfig.FxtrConfig) -> None:
    """Delete the co-located cache rows (the ``_rg/`` keys) to force a cold rebuild on next load.

    Leaves the runset rows intact (they share the file).
    """
    path = _active_store_path(config)
    assert path.exists(), f"Expected active store at {path}"
    with SqliteDict(str(path), autocommit=True) as db:
        cache_keys = [k for k in db.keys() if k.startswith(_CACHE_PREFIX)]
        assert cache_keys, "Expected co-located cache rows to delete; found none."
        for key in cache_keys:
            del db[key]


def _active_runset_cids(config: fxtrconfig.FxtrConfig) -> set:
    """Return the set of task CIDs currently present in the active runset on disk."""
    return set(api_lib.load_active_runset(config).runs.keys())


def _read_run_graph_cache(config: fxtrconfig.FxtrConfig) -> dict:
    """Return the cache records keyed by base32 task CID (prefix stripped)."""
    path = _active_store_path(config)
    with SqliteDict(str(path)) as db:
        return {
            key[len(_CACHE_PREFIX):]: value
            for key, value in db.items()
            if key.startswith(_CACHE_PREFIX)
        }


def _make_chain(plan_fixture_module, start: int):
    """Build a two-task dependency chain: task_b depends on task_a's result handle.

    task_a: add_one(start)        -> start + 1
    task_b: add_one(task_a)       -> start + 2
    """
    const = core.ConstantHandle(value=plan_fixture_module.WrappedInt(value=start))
    task_a = plan_fixture_module.add_one.bind(input_node=const)
    task_b = plan_fixture_module.add_one.bind(input_node=task_a.result_handle)
    return task_a, task_b


async def _retrieve(mgr: session_manager.SessionManager, handle) -> object:
    retriever = await mgr.instantiate(handle)
    return await retriever.retrieve()


# ----------------------------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_cold_rebuild_reconstructs_dependency_graph(
    isolated_test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """Deleting the cache forces a cold rebuild; a multi-node dependency graph still reloads.

    This is the path with zero prior coverage: ``_load_runset`` reconstructing edges from the
    entity store rather than from the cache.
    """
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    task_a, task_b = _make_chain(plan_fixture_module, start=0)

    # Session 1: materialize the chain.
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(
            core.freeze({"a": task_a, "b": task_b}), message="chain materialize"
        )
        assert len(res.completed) == 2
        assert len(res.failed) == 0
        assert len(res.cancelled) == 0

    # Force the cold path: remove the cache so session 2 must rebuild from the entity store.
    _delete_run_graph_cache(isolated_test_config)

    # The authoritative runset must still contain both tasks.
    runset_cids = _active_runset_cids(isolated_test_config)
    assert core.entity_cid(task_a) in runset_cids
    assert core.entity_cid(task_b) in runset_cids

    # Session 2: cold rebuild. Both handles must still resolve, including the downstream one whose
    # correctness depends on the reconstructed dependency edge.
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        result_a = await _retrieve(mgr2, task_a.result_handle)
        result_b = await _retrieve(mgr2, task_b.result_handle)
        assert isinstance(result_a, plan_fixture_module.WrappedInt)
        assert isinstance(result_b, plan_fixture_module.WrappedInt)
        assert result_a.value == 1  # 0 + 1
        assert result_b.value == 2  # 0 + 1 + 1


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_warm_reload_reconstructs_dependency_graph(
    isolated_test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """The warm path (cache present) also reloads a multi-node dependency graph correctly.

    Complements the existing single-task reopen test by exercising cross-task edges.
    """
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    task_a, task_b = _make_chain(plan_fixture_module, start=10)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"a": task_a, "b": task_b}))
        assert len(res.completed) == 2

    # No cache deletion here: exercise the warm reload path.
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        result_b = await _retrieve(mgr2, task_b.result_handle)
        assert isinstance(result_b, plan_fixture_module.WrappedInt)
        assert result_b.value == 12  # 10 + 1 + 1


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_workflow_survives_warm_reload(
    isolated_test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """A materialized workflow's result handle still resolves after reopen (warm path).

    Exercises ``workflow_contains`` / ``workflow_next`` reconstruction, which the in-session
    workflow test never reloads.
    """
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    wf0 = plan_fixture_module.repeatedly_add.bind(
        input_node=core.ConstantHandle(value=plan_fixture_module.WrappedInt(value=0)),
        num_times=3,
    )

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"root": wf0}), message="wf materialize")
        assert len(res.completed) == 7
        assert len(res.failed) == 0
        assert len(res.cancelled) == 0

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        final_result = await _retrieve(mgr2, wf0.result_handle)
        assert final_result == plan_fixture_module.WrappedInt(value=3)


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_workflow_survives_cold_rebuild(
    isolated_test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """A workflow rebuilds correctly from the entity store when the cache is missing.

    This is the highest-risk reconstruction for the planned change (workflow step graph), and it
    currently has no coverage at all.
    """
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    wf0 = plan_fixture_module.repeatedly_add.bind(
        input_node=core.ConstantHandle(value=plan_fixture_module.WrappedInt(value=0)),
        num_times=3,
    )

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"root": wf0}))
        assert len(res.completed) == 7

    _delete_run_graph_cache(isolated_test_config)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        final_result = await _retrieve(mgr2, wf0.result_handle)
        assert final_result == plan_fixture_module.WrappedInt(value=3)


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_drop_removes_dependent_closure(
    isolated_test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """Dropping an upstream task (with and_dependents) removes its downstream dependents too.

    Pins the dependent-edge semantics that the durable cache must keep correct (the reverse index
    in the plan). Within a single session.
    """
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    task_a, task_b = _make_chain(plan_fixture_module, start=0)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"a": task_a, "b": task_b}))
        assert len(res.completed) == 2

        cids = _active_runset_cids(isolated_test_config)
        assert core.entity_cid(task_a) in cids
        assert core.entity_cid(task_b) in cids

        # Drop the upstream task; the downstream dependent must go with it.
        await mgr.drop_runs(core.freeze({"a": task_a}), and_dependents=True)

        cids_after = _active_runset_cids(isolated_test_config)
        assert core.entity_cid(task_a) not in cids_after
        assert core.entity_cid(task_b) not in cids_after


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_drop_after_reload_removes_dependent_closure(
    isolated_test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """After a reopen, dropping an upstream task still removes its reloaded dependents.

    This is the case most sensitive to dependent-edge reconstruction: the dependent edge must be
    rebuilt on load for the drop to find the downstream node.
    """
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    task_a, task_b = _make_chain(plan_fixture_module, start=5)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"a": task_a, "b": task_b}))
        assert len(res.completed) == 2

    # Reopen, then drop the upstream task. The dependent must have been reconstructed so it is
    # dropped as part of the closure.
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        cids = _active_runset_cids(isolated_test_config)
        assert core.entity_cid(task_a) in cids
        assert core.entity_cid(task_b) in cids

        await mgr2.drop_runs(core.freeze({"a": task_a}), and_dependents=True)

        cids_after = _active_runset_cids(isolated_test_config)
        assert core.entity_cid(task_a) not in cids_after
        assert core.entity_cid(task_b) not in cids_after


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_run_graph_cache_self_heals_on_corruption(
    isolated_test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """A corrupt/stale sidecar is discarded and rebuilt from the entity store on the next load.

    Tampering so the cache key set no longer matches the runset should trigger a cold rebuild
    (results still resolve) and the cache should be repopulated to a consistent state.
    """
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    task_a, task_b = _make_chain(plan_fixture_module, start=0)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"a": task_a, "b": task_b}))
        assert len(res.completed) == 2

    # Corrupt the cache: drop one cache record (a _rg/ key) so the cache key set no longer matches
    # the runset. Leave runset rows intact.
    with SqliteDict(str(_active_store_path(isolated_test_config)), autocommit=True) as db:
        cache_key = next(k for k in db.keys() if k.startswith(_CACHE_PREFIX))
        del db[cache_key]

    # Reopen: the cache is rejected, a cold rebuild happens, and results still resolve.
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        result_b = await _retrieve(mgr2, task_b.result_handle)
        assert isinstance(result_b, plan_fixture_module.WrappedInt)
        assert result_b.value == 2

    # The cache should have been repopulated to match the runset again.
    assert set(_read_run_graph_cache(isolated_test_config).keys()) == {
        core.entity_cid(task_a).encode(base="base32"),
        core.entity_cid(task_b).encode(base="base32"),
    }


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_run_graph_cache_reverse_index_is_consistent(
    isolated_test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """The persisted dependent_cids are exactly the inverse of dependency_cids (Stage 2 readiness).

    The eager loader rebuilds edges from dependency_cids, but the reverse index must be maintained
    correctly so a future lazy loader can find a node's dependents without a full scan.
    """
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    task_a, task_b = _make_chain(plan_fixture_module, start=0)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"a": task_a, "b": task_b}))
        assert len(res.completed) == 2

    records = _read_run_graph_cache(isolated_test_config)

    # Build forward and reverse edge sets keyed by base32 CID and check they are inverses.
    forward: set[tuple[str, str]] = set()
    reverse: set[tuple[str, str]] = set()
    for key, record in records.items():
        for dep_cid in record["dependency_cids"]:
            forward.add((dep_cid.encode(base="base32"), key))
        for dependent_cid in record["dependent_cids"]:
            reverse.add((key, dependent_cid.encode(base="base32")))

    assert forward == reverse
    # Sanity: there is at least the a -> b edge.
    assert (
        core.entity_cid(task_a).encode(base="base32"),
        core.entity_cid(task_b).encode(base="base32"),
    ) in forward


def _write_legacy_pickle_cache_from_colocated(config: fxtrconfig.FxtrConfig) -> Path:
    """Synthesize a legacy monolithic pickle cache from the current co-located rows, then remove
    the co-located rows so only the legacy pickle holds the graph. Returns the pickle path."""
    records = _read_run_graph_cache(config)  # {base32 cid: record}
    nodes: dict = {}
    for b32, record in records.items():
        cid = cbrrr.CID.decode(b32)
        workflow_contains = record["workflow_contains"]
        nodes[cid] = RunNode(
            task_cid=cid,
            run_or_reservation=record["run"],
            dependencies={},
            dependents={},
            workers={},
            workflow_contains=set(workflow_contains) if workflow_contains is not None else None,
            workflow_next=record["workflow_next"],
        )
    for b32, record in records.items():
        node = nodes[cbrrr.CID.decode(b32)]
        for dep_cid in record["dependency_cids"]:
            dep = nodes[dep_cid]
            node.dependencies[id(dep)] = dep
            dep.dependents[id(node)] = node

    state_store = fxtrconfig.get_active_state_store(config)
    state_store.mkdir(parents=True, exist_ok=True)
    pickle_path = state_store / "_run_graph_cache.pickle"
    with open(pickle_path, "wb") as f:
        pickle.dump({"version": 1, "nodes": nodes}, f)

    # Remove the co-located cache rows so the only graph cache is the legacy pickle.
    with SqliteDict(str(_active_store_path(config)), autocommit=True) as db:
        for key in [k for k in db.keys() if k.startswith(_CACHE_PREFIX)]:
            del db[key]
    return pickle_path


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_legacy_pickle_cache_is_migrated(
    isolated_test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """A legacy monolithic pickle cache is migrated into co-located rows on load (no cold rebuild).

    After migration the pickle is removed, the co-located rows are present, and results resolve.
    """
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    task_a, task_b = _make_chain(plan_fixture_module, start=7)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"a": task_a, "b": task_b}))
        assert len(res.completed) == 2

    # Turn the co-located cache into a legacy pickle (and drop the co-located rows).
    pickle_path = _write_legacy_pickle_cache_from_colocated(isolated_test_config)
    assert pickle_path.exists()
    assert _read_run_graph_cache(isolated_test_config) == {}  # co-located rows gone

    # Reopen: the legacy pickle should be migrated into co-located rows and then used.
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        result_b = await _retrieve(mgr2, task_b.result_handle)
        assert isinstance(result_b, plan_fixture_module.WrappedInt)
        assert result_b.value == 9  # 7 + 1 + 1

    # The pickle is consumed, and the co-located cache now matches the runset.
    assert not pickle_path.exists()
    assert set(_read_run_graph_cache(isolated_test_config).keys()) == {
        core.entity_cid(task_a).encode(base="base32"),
        core.entity_cid(task_b).encode(base="base32"),
    }


# ----------------------------------------------------------------------------------------------
# Stage 2 — lazy loading property tests (white-box: assert on the manager's internals)
# ----------------------------------------------------------------------------------------------


def _independent_task(plan_fixture_module, value: int):
    """A single task that depends only on a constant (no task dependencies)."""
    const = core.ConstantHandle(value=plan_fixture_module.WrappedInt(value=value))
    return plan_fixture_module.add_one.bind(input_node=const)


@pytest.mark.anyio
async def test_lazy_warm_reload_loads_nodes_on_demand(isolated_test_config: fxtrconfig.FxtrConfig):
    """A complete cache → lazy mode: nothing is built until accessed, and only touched nodes load."""
    from fxtr_testdeps import plan_fixtures as pf

    task_x = _independent_task(pf, 100)
    task_a, task_b = _make_chain(pf, start=0)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"x": task_x, "a": task_a, "b": task_b}))
        assert len(res.completed) == 3

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        arm = mgr2._active_run_manager
        assert arm._fully_loaded is False, "complete cache should enter lazy mode"
        assert len(arm.nodes) == 0, "lazy startup should build no nodes"

        result_b = await _retrieve(mgr2, task_b.result_handle)
        assert isinstance(result_b, pf.WrappedInt)
        assert result_b.value == 2

        # Only task_b was touched: its dependency (task_a) and the unrelated task_x stay unloaded.
        assert core.entity_cid(task_b) in arm.nodes
        assert core.entity_cid(task_a) not in arm.nodes
        assert core.entity_cid(task_x) not in arm.nodes


@pytest.mark.anyio
async def test_lazy_drop_loads_only_closure(isolated_test_config: fxtrconfig.FxtrConfig):
    """Dropping a->b after a lazy reopen loads only that closure, not unrelated nodes."""
    from fxtr_testdeps import plan_fixtures as pf

    task_x = _independent_task(pf, 100)
    task_a, task_b = _make_chain(pf, start=0)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"x": task_x, "a": task_a, "b": task_b}))
        assert len(res.completed) == 3

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        arm = mgr2._active_run_manager
        assert len(arm.nodes) == 0

        await mgr2.drop_runs(core.freeze({"a": task_a}), and_dependents=True)

        # The unrelated task was never loaded into memory ...
        assert core.entity_cid(task_x) not in arm.nodes
        # ... and only a + b were dropped from the runset.
        cids = _active_runset_cids(isolated_test_config)
        assert core.entity_cid(task_a) not in cids
        assert core.entity_cid(task_b) not in cids
        assert core.entity_cid(task_x) in cids


@pytest.mark.anyio
async def test_incomplete_cache_triggers_rebuild(isolated_test_config: fxtrconfig.FxtrConfig):
    """Missing cache rows (count != runset) → one-time resumable rebuild, then serve lazily.

    The rebuild repopulates the co-located cache by building a fresh store and swapping it over the
    active store; afterwards the manager is in lazy mode (nothing resident) with a complete cache.
    """
    from fxtr_testdeps import plan_fixtures as pf

    task_a, task_b = _make_chain(pf, start=0)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"a": task_a, "b": task_b}))
        assert len(res.completed) == 2

    _delete_run_graph_cache(isolated_test_config)  # cache rows gone -> incomplete

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        arm = mgr2._active_run_manager
        # After the rebuild we serve lazily: nothing resident, complete cache on disk.
        assert arm._fully_loaded is False, "rebuild should leave the manager in lazy mode"
        assert len(arm.nodes) == 0, "lazy mode builds no nodes until accessed"
        # The rebuild repopulated the cache to match the runset.
        assert set(_read_run_graph_cache(isolated_test_config).keys()) == {
            core.entity_cid(task_a).encode(base="base32"),
            core.entity_cid(task_b).encode(base="base32"),
        }
        # ... and results still resolve through the rebuilt (lazy) cache.
        result_b = await _retrieve(mgr2, task_b.result_handle)
        assert isinstance(result_b, pf.WrappedInt)
        assert result_b.value == 2


@pytest.mark.anyio
async def test_lazy_miss_on_corrupt_cache_raises(isolated_test_config: fxtrconfig.FxtrConfig):
    """In lazy mode, a runset row whose cache row is missing is corruption → clear error."""
    from fxtr_testdeps import plan_fixtures as pf

    task = _independent_task(pf, 5)

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"t": task}))
        assert len(res.completed) == 1

    # Corrupt while keeping the cache row count equal to the runset count (so startup still goes
    # lazy): drop the real cache row and add a bogus one.
    with SqliteDict(str(_active_store_path(isolated_test_config)), autocommit=True) as db:
        real_key = next(k for k in db.keys() if k.startswith(_CACHE_PREFIX))
        value = db[real_key]
        del db[real_key]
        db[_CACHE_PREFIX + "bogusbogusbogusbogus"] = value

    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        arm = mgr2._active_run_manager
        assert arm._fully_loaded is False, "equal counts should still enter lazy mode"
        with pytest.raises(RuntimeError):
            arm._get_node(core.entity_cid(task))


# ----------------------------------------------------------------------------------------------
# RunGraphCacheBuilder — fast / resumable / syncable cold build (source -> staging -> dest)
# ----------------------------------------------------------------------------------------------


def _loader_for(config: fxtrconfig.FxtrConfig) -> locator.CachingEntityLoader:
    """A fresh entity loader over the config's (read-only) entity store."""
    return locator.CachingEntityLoader.from_locator(api_lib.get_active_data_locator(config))


def _read_store(path: Path) -> tuple[set, dict]:
    """Return (runset task-cid base32 keys, cache records keyed by base32 task cid) of a store."""
    runset_keys: set = set()
    cache: dict = {}
    with SqliteDict(str(path)) as db:
        for key, value in db.items():
            if key.startswith(_CACHE_PREFIX):
                cache[key[len(_CACHE_PREFIX):]] = value
            else:
                runset_keys.add(key)
    return runset_keys, cache


def _assert_inverse(cache: dict) -> None:
    """Assert the persisted dependent_cids are exactly the inverse of dependency_cids."""
    forward: set = set()
    reverse: set = set()
    for key, record in cache.items():
        for dep in record["dependency_cids"]:
            forward.add((dep.encode(base="base32"), key))
        for dependent in record["dependent_cids"]:
            reverse.add((key, dependent.encode(base="base32")))
    assert forward == reverse


def _bogus_run_cid(salt: str) -> cbrrr.CID:
    """A validly-formatted CID whose entity is absent from any store (loads will fail)."""
    return cbrrr.CID.cidv1_dag_cbor_sha256_32_from(cbrrr.encode_dag_cbor(f"absent::{salt}"))


@pytest.mark.anyio
async def test_builder_copies_source_to_separate_target(
    isolated_test_config: fxtrconfig.FxtrConfig, tmp_path: Path
):
    """A source->dest build produces a valid cache in a *separate* file, leaving source untouched."""
    from fxtr_testdeps import plan_fixtures as pf

    task_a, task_b = _make_chain(pf, start=0)
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        assert len((await mgr.materialize(core.freeze({"a": task_a, "b": task_b}))).completed) == 2

    source_path = _active_store_path(isolated_test_config)
    source_before = _read_store(source_path)
    source_runs = api_lib.load_active_runset(isolated_test_config).runs

    dest = tmp_path / "dest.sqlite"
    staging = tmp_path / "dest.sqlite.staging"
    built, skipped = RunGraphCacheBuilder(
        source_runs=source_runs,
        loader=_loader_for(isolated_test_config),
        staging_path=staging,
        dest_path=dest,
    ).run()

    assert (built, skipped) == (2, 0)
    assert not staging.exists(), "staging should be deleted on success"
    # Source file is untouched (still has its own runset + cache rows).
    assert _read_store(source_path) == source_before

    runset_keys, cache = _read_store(dest)
    a32, b32 = (core.entity_cid(t).encode(base="base32") for t in (task_a, task_b))
    assert runset_keys == {a32, b32}, "dest carries a copy of the runset rows"
    assert set(cache.keys()) == {a32, b32}
    _assert_inverse(cache)
    assert cache[a32]["dependent_cids"][0].encode(base="base32") == b32  # a -> b edge


@pytest.mark.anyio
async def test_builder_resume_and_sync(
    isolated_test_config: fxtrconfig.FxtrConfig, tmp_path: Path
):
    """Stage 1 picks up runs *added* to the source; stage 2 skips dest rows already written."""
    from fxtr_testdeps import plan_fixtures as pf

    task_a, task_b = _make_chain(pf, start=0)
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        assert len((await mgr.materialize(core.freeze({"a": task_a, "b": task_b}))).completed) == 2

    dest = tmp_path / "dest.sqlite"
    staging = tmp_path / "dest.sqlite.staging"

    # Build the first two nodes, driving the stages directly so staging persists between calls.
    b1 = RunGraphCacheBuilder(
        source_runs=api_lib.load_active_runset(isolated_test_config).runs,
        loader=_loader_for(isolated_test_config),
        staging_path=staging,
        dest_path=dest,
    )
    b1._run_stage1()
    assert b1._staging_cids() == set(api_lib.load_active_runset(isolated_test_config).runs.keys())
    b1._run_stage1()  # idempotent: re-running skips everything already staged
    assert b1._staging_cids() == set(api_lib.load_active_runset(isolated_test_config).runs.keys())
    b1._run_stage2()

    _, cache_after_first = _read_store(dest)
    assert len(cache_after_first) == 2

    # Stage-2 resume: drop a dest cache row, rerun stage 2 (staging still present) -> restored.
    with SqliteDict(str(dest), autocommit=True) as db:
        del db[next(k for k in db.keys() if k.startswith(_CACHE_PREFIX))]
    b1._run_stage2()
    _, cache_resumed = _read_store(dest)
    assert set(cache_resumed.keys()) == set(cache_after_first.keys())
    _assert_inverse(cache_resumed)

    # Sync: add a new independent run to the source, then build into the same dest.
    task_x = _independent_task(pf, 99)
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        assert len((await mgr2.materialize(core.freeze({"x": task_x}))).completed) == 1

    built, _ = RunGraphCacheBuilder(
        source_runs=api_lib.load_active_runset(isolated_test_config).runs,
        loader=_loader_for(isolated_test_config),
        staging_path=staging,
        dest_path=dest,
    ).run()
    assert built == 1, "only the newly-added run is processed into dest"
    runset_keys, cache = _read_store(dest)
    x32 = core.entity_cid(task_x).encode(base="base32")
    assert x32 in cache and x32 in runset_keys
    assert len(cache) == 3
    _assert_inverse(cache)


@pytest.mark.anyio
async def test_builder_aborts_on_unbuildable(
    isolated_test_config: fxtrconfig.FxtrConfig, tmp_path: Path
):
    """An unloadable runset entry aborts the build by default (fail-loud, no silent skip)."""
    from fxtr_testdeps import plan_fixtures as pf

    task = _independent_task(pf, 1)
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        assert len((await mgr.materialize(core.freeze({"t": task}))).completed) == 1

    runs = dict(api_lib.load_active_runset(isolated_test_config).runs)
    runs[_bogus_run_cid("task")] = _bogus_run_cid("run")  # a runset entry with no entities

    with pytest.raises(RuntimeError, match="Failed to extract node"):
        RunGraphCacheBuilder(
            source_runs=runs,
            loader=_loader_for(isolated_test_config),
            staging_path=tmp_path / "d.sqlite.staging",
            dest_path=tmp_path / "d.sqlite",
        ).run()


@pytest.mark.anyio
async def test_builder_skip_unbuildable_excludes_closure(
    isolated_test_config: fxtrconfig.FxtrConfig, tmp_path: Path
):
    """With skip_unbuildable, an unbuildable node AND its dependent closure are dropped together;
    unrelated nodes are kept, and no kept node references a skipped cid (never an edge-drop)."""
    from fxtr_testdeps import plan_fixtures as pf

    task_x = _independent_task(pf, 7)  # unrelated, should survive
    task_a, task_b = _make_chain(pf, start=0)  # b depends on a
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()
        res = await mgr.materialize(core.freeze({"x": task_x, "a": task_a, "b": task_b}))
        assert len(res.completed) == 3

    # Corrupt a's run pointer so a is unbuildable; b (which depends on a) must cascade out, x stays.
    runs = dict(api_lib.load_active_runset(isolated_test_config).runs)
    a_cid = core.entity_cid(task_a)
    runs[a_cid] = _bogus_run_cid("a-run")

    dest = tmp_path / "d.sqlite"
    built, skipped = RunGraphCacheBuilder(
        source_runs=runs,
        loader=_loader_for(isolated_test_config),
        staging_path=tmp_path / "d.sqlite.staging",
        dest_path=dest,
        skip_unbuildable=True,
    ).run()

    runset_keys, cache = _read_store(dest)
    x32 = core.entity_cid(task_x).encode(base="base32")
    a32 = a_cid.encode(base="base32")
    b32 = core.entity_cid(task_b).encode(base="base32")
    assert set(cache.keys()) == {x32}, "only the unrelated node survives"
    assert built == 1 and skipped == 2  # a (failed) + b (cascaded)
    # No kept node references a skipped cid (dependency-closed kept set).
    referenced = {d.encode(base="base32") for r in cache.values() for d in r["dependency_cids"]}
    assert a32 not in referenced and b32 not in referenced
    _assert_inverse(cache)
