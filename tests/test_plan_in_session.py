"""Tests for make_task_from and make_plan_from running in a session manager."""

import shutil
import tempfile
from pathlib import Path
from typing import Generator

import pytest
import tomlkit

from fxtr.api import fxtrconfig, session_manager
from fxtr.impl_plans import plan_helpers
from fxtr.object_model import core


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def test_config() -> Generator[fxtrconfig.FxtrConfig, None]:
    """Create a test configuration with a temporary state directory.

    This fixture:
    - Creates a temporary directory for the test session
    - Creates a minimal config (provider.uv is required by structure but not used with in_process_python)
    - Sets up a temporary data directory
    - Cleans up after the session
    """
    # Create temporary directory for the test session
    temp_dir = Path(tempfile.mkdtemp(prefix="fxtr_test_plan_in_session_"))

    try:
        # Create a minimal config
        config_data = {
            "author": {
                "name": "Test Author",
                "email": "test@example.com",
            },
            "blueprint": {
                "sources": {},
            },
            "data": {
                "active": str(temp_dir / "data"),
            },
            "resolver": {
                "trust_impl_code": True,
                "default_py_provider": "in_process",
            },
            "provider": {
                "uv": {
                    "virtual_env_dir": str(temp_dir / "tmp_venvs"),
                    "dynamic_source_packages": [],
                    "local_source_packages": {},
                },
            },
        }

        # Write config to temp location
        temp_config_path = temp_dir / "fxtr.toml"
        with open(temp_config_path, "w") as f:
            f.write(tomlkit.dumps(config_data))

        # Load the config
        config = fxtrconfig.parse_fxtr_config(temp_config_path)

        # Ensure the state directory exists
        state_dir = Path(config.data.active)
        state_dir.mkdir(parents=True, exist_ok=True)

        yield config

    finally:
        # Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_make_task_from_with_bind_storage(
    test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """Test make_task_from with bind_storage to pass a storage handle."""
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    async with session_manager.SessionManager.create(test_config, in_process_python=True) as mgr:
        # Create a constant handle with a WrappedInt value
        storage_handle_plan = core.ConstantHandle(value=plan_fixture_module.WrappedInt(value=1))

        # Create a task using make_task_from with bind_storage
        dyn_task = plan_helpers.make_task_from(plan_fixture_module.add_delta_untasked)(
            plan_helpers.bind_storage(storage_handle_plan),
            delta=2,
        )

        # Run the task
        task_fn = await mgr.instantiate(dyn_task.function)
        result = await task_fn.run_task()

        # Verify the result
        assert isinstance(result, plan_fixture_module.WrappedInt)
        assert result.value == 3  # 1 + 2


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_make_task_from_with_bind_storage_and_bind_impl(
    test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """Test make_task_from with both bind_storage and bind_impl."""
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    async with session_manager.SessionManager.create(test_config, in_process_python=True) as mgr:
        # Create constant handles
        storage_handle_plan = core.ConstantHandle(value=plan_fixture_module.WrappedInt(value=2))
        impl_plan = core.ConstantHandle(value=plan_fixture_module.WrappedInt(value=3))

        # Create a task using make_task_from with both bind_storage and bind_impl
        dyn_task = plan_helpers.make_task_from(plan_fixture_module.add_stuff_untasked)(
            plan_helpers.bind_storage(storage_handle_plan),
            plan_helpers.bind_impl(impl_plan),
            delta=2,
        )

        # Run the task
        task_fn = await mgr.instantiate(dyn_task.function)
        result = await task_fn.run_task()

        # Verify the result: val1.value (3) + val2.value (2) + delta (2) = 7
        assert isinstance(result, plan_fixture_module.WrappedInt)
        assert result.value == 7


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_make_plan_from_with_bind_impl(
    test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """Test make_plan_from with bind_impl to pass a plan that gets instantiated."""
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    async with session_manager.SessionManager.create(test_config, in_process_python=True) as mgr:
        # Create a plan using build_plan
        foo_plan = plan_helpers.build_plan(plan_fixture_module.StaticFooImpl).bind(other="hello")

        # Create a plan using make_plan_from with bind_impl
        foo2_plan = plan_helpers.make_plan_from(plan_fixture_module.UnresolvingFooDoubler)(
            other=plan_helpers.bind_impl(foo_plan)
        )

        # Instantiate and use the plan
        foo2 = await mgr.instantiate(foo2_plan)
        result = await foo2.bar(1)

        # Verify the result: "hello" + "hello" = "hellohello"
        assert result == "hellohello"


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_repeatedly_add_workflow_materialization(
    test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """Test materializing a workflow that repeatedly adds 1 to a value."""
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    async with session_manager.SessionManager.create(test_config, in_process_python=True) as mgr:
        await mgr.drop_all_runs()
        wf0 = plan_fixture_module.repeatedly_add.bind(
            input_node=core.ConstantHandle(value=plan_fixture_module.WrappedInt(value=0)),
            num_times=3,
        )

        res = await mgr.materialize(core.freeze({"root": wf0}), message="Test materialization")
        assert len(res.completed) == 7
        assert len(res.failed) == 0
        assert len(res.cancelled) == 0

        # Retrieve the final result
        retriever = await mgr.instantiate(wf0.result_handle)
        final_result = await retriever.retrieve()
        assert final_result == plan_fixture_module.WrappedInt(value=3)


@pytest.fixture
def isolated_test_config() -> Generator[fxtrconfig.FxtrConfig, None, None]:
    """Create an isolated test configuration for tests that need to reopen the session manager.

    Unlike test_config which is session-scoped, this fixture is function-scoped
    so each test gets a fresh state directory.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="fxtr_test_isolated_"))

    try:
        config_data = {
            "author": {
                "name": "Test Author",
                "email": "test@example.com",
            },
            "blueprint": {
                "sources": {},
            },
            "data": {
                "active": str(temp_dir / "data"),
            },
            "resolver": {
                "trust_impl_code": True,
                "default_py_provider": "in_process",
            },
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


@pytest.mark.parametrize("use_stringized", [False, True])
@pytest.mark.anyio
async def test_run_graph_cache_persists_across_sessions(
    isolated_test_config: fxtrconfig.FxtrConfig,
    use_stringized: bool,
):
    """Test that the run graph cache allows reopening a session and accessing materialized results.

    This test:
    1. Creates a session manager
    2. Materializes a task using mgr.materialize
    3. Closes the session manager (which saves the run graph cache)
    4. Reopens a new session manager with the same config
    5. Instantiates the result handle of the previously materialized task
    """
    from fxtr_testdeps import plan_fixtures, plan_fixtures_stringized

    from fxtr.api import fxtrconfig as fxtrconfig_module

    plan_fixture_module = plan_fixtures_stringized if use_stringized else plan_fixtures

    # Create a task to materialize
    storage_handle_plan = core.ConstantHandle(value=plan_fixture_module.WrappedInt(value=10))
    dyn_task = plan_helpers.make_task_from(plan_fixture_module.add_delta_untasked)(
        plan_helpers.bind_storage(storage_handle_plan),
        delta=5,
    )

    # First session: materialize the task
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr:
        await mgr.drop_all_runs()

        # Materialize the task
        res = await mgr.materialize(core.freeze({"task": dyn_task}), message="Test materialization")
        assert len(res.completed) == 1
        assert len(res.failed) == 0
        assert len(res.cancelled) == 0

    # Verify the durable run-graph cache was created. It is co-located in the active runset's
    # SqliteDict under "_rg/"-prefixed keys (maintained incrementally during the session).
    from sqlitedict import SqliteDict

    from fxtr.api import api_lib

    active_store = api_lib.get_active_runset_path(isolated_test_config)
    assert active_store.exists(), "Active runset store should exist"
    with SqliteDict(str(active_store)) as db:
        cache_keys = [k for k in db.keys() if k.startswith(api_lib.RUN_GRAPH_CACHE_KEY_PREFIX)]
    assert cache_keys, "Run graph cache rows should have been written"

    # Second session: reopen and access the result
    async with session_manager.SessionManager.create(
        isolated_test_config, in_process_python=True
    ) as mgr2:
        # Instantiate the result handle - this should work because the cache was loaded
        retriever = await mgr2.instantiate(dyn_task.result_handle)
        result = await retriever.retrieve()

        # Verify the result
        assert isinstance(result, plan_fixture_module.WrappedInt)
        assert result.value == 15  # 10 + 5
