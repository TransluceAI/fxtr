"""Integration tests for task materialization and backend runner functionality.

This test module validates that:
1. Tasks can be materialized and their results stored in the active runset
2. Backend runner can provision secrets, judges, and task result handles
3. Both in_process and uv Python providers work correctly
"""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Generator

import anyio
import pytest
from frozendict import frozendict

from fxtr.api import api_lib, backend_runner, fxtr_cli, fxtrconfig
from fxtr.object_model import core


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def test_config() -> Generator[fxtrconfig.FxtrConfig, None]:
    """Create a test configuration with a temporary state directory.

    This fixture:
    - Creates a temporary directory for the test session
    - Loads the demo_09_15_fake config
    - Overrides the data.active path to use the temp directory
    - Writes a modified fxtr.toml file with the overridden path
    - Sets up fake environment variables for OpenAI API access
    - Cleans up after the session
    """
    import tomlkit

    # Set up fake environment variables for the test
    original_env = {}
    test_env_vars = {
        "OPENAI_API_KEY": "fake-test-api-key",
        "OPENAI_API_ORG": "fake-test-org",
    }

    for key, value in test_env_vars.items():
        original_env[key] = os.environ.get(key)
        os.environ[key] = value

    # Create temporary directory for the test session
    temp_dir = Path(tempfile.mkdtemp(prefix="fxtr_test_"))

    try:
        # Load the demo config
        demo_config_path = (
            Path(__file__).parent.parent / "examples" / "demo_09_15_fake" / "user" / "fxtr.toml"
        )
        config = fxtrconfig.parse_fxtr_config(demo_config_path)

        # Read the original TOML file
        with open(demo_config_path, "r") as f:
            toml_data = tomlkit.parse(f.read())

        # Override paths to be absolute (since we're writing to a different location)
        demo_config_dir = demo_config_path.parent

        # Override the data.active path
        toml_data["data"]["active"] = str(temp_dir / "data")  # type: ignore

        # Make virtual_env_dir absolute
        toml_data["provider"]["uv"]["virtual_env_dir"] = str(temp_dir / "tmp_venvs")  # type: ignore

        # Make local_source_packages paths absolute
        local_packages = toml_data["provider"]["uv"]["local_source_packages"]  # type: ignore
        for pkg_name, rel_path in local_packages.items():  # type: ignore
            abs_path = (demo_config_dir / rel_path).resolve()
            local_packages[pkg_name] = str(abs_path)  # type: ignore

        # Write modified config to temp location
        temp_config_path = temp_dir / "fxtr.toml"
        with open(temp_config_path, "w") as f:
            f.write(tomlkit.dumps(toml_data))

        # Load the modified config
        modified_config = fxtrconfig.parse_fxtr_config(temp_config_path)

        # Ensure the state directory exists
        state_dir = Path(modified_config.data.active)
        state_dir.mkdir(parents=True, exist_ok=True)

        yield modified_config

    finally:
        # Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)

        # Restore original environment variables
        for key, original_value in original_env.items():
            if original_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original_value


@pytest.fixture(scope="session")
async def materialized_env(
    test_config: fxtrconfig.FxtrConfig,
) -> tuple[fxtrconfig.FxtrConfig, dict]:
    """Materialize all tasks in the demo collection.

    This fixture:
    - Clears the active runset
    - Materializes all tasks using materialize_impl
    - Returns the config and run states
    """
    # Clear the active runset (equivalent to: fxtr runset drop --all)
    empty_runset = core.RunSet(runs=frozendict())
    api_lib.save_active_runset(test_config, empty_runset)

    # Materialize all tasks
    with anyio.fail_after(30):
        run_states = await fxtr_cli.materialize_impl(
            config=test_config,
            namespec="**",
            and_previous=False,
            collection=None,  # Use default collection
            module=None,
            message="Test materialization",
        )

    return (test_config, run_states)


@pytest.mark.anyio
async def test_materialization_succeeds(materialized_env):
    """Test that materialization completes successfully for all tasks."""
    config, run_states = materialized_env

    # Assert all tasks completed successfully
    for task_cid, (status, _run_cid, status_message) in run_states.items():
        assert status == "completed", (
            f"Task {task_cid.encode('base32')} failed with state: {status}\n"
            f"Status message: {status_message}"
        )

    # Reload the active runset and verify all tasks are present
    active_runset = api_lib.load_active_runset(config)

    for task_cid in run_states.keys():
        assert (
            task_cid in active_runset.runs
        ), f"Task {task_cid.encode('base32')} not found in active runset"


@pytest.mark.anyio
@pytest.mark.parametrize("python_provider", ["in_process", "uv"])
async def test_backend_runner_secrets(materialized_env, python_provider):
    """Test that BackendRunner can provision secrets."""
    config, _ = materialized_env

    # Import the experiment definition
    from demo_09_15_fake_defns import index as defns

    # Get the secrets plan
    openai_api_key_handle = defns.openai_api_key.handle

    # Create BackendRunner and test secrets provision
    async with backend_runner.BackendRunner.create(config) as runner:
        # Activate the default collection
        await runner.activate_collection("default")

        # Request the secrets implementation
        secret_impl = await runner.request(openai_api_key_handle)

        # Verify we got a valid implementation back
        assert secret_impl is not None


@pytest.mark.anyio
async def test_backend_runner_constant(materialized_env):
    """Test that BackendRunner can provision constants."""
    config, _ = materialized_env

    # Create BackendRunner and test constant provision
    async with backend_runner.BackendRunner.create(config) as runner:
        # Note: Constants don't need collection activation
        constant_plan = core.ConstantHandle(value=core.AdHocMetadata(metadata=2))
        # Request the constant implementation
        constant_impl = await runner.request(constant_plan)
        # Verify the implementation is a FxtrCallTarget
        assert isinstance(constant_impl, core.FxtrCallTarget)
        assert core.implements_interface(constant_impl, core.StorageHandle[core.AdHocMetadata])
        val = await constant_impl.retrieve()
        assert val == core.AdHocMetadata(metadata=2)


@pytest.mark.anyio
@pytest.mark.parametrize("python_provider", ["in_process", "uv"])
async def test_backend_runner_judge(materialized_env, python_provider):
    """Test that BackendRunner can provision a judge with secret dependencies."""
    config, _ = materialized_env

    # Import the experiment definition
    import demo_09_15_fake_user
    from demo_09_15_fake_defns import index as defns

    # Get the judge and secrets
    openai_api_key_handle = defns.openai_api_key.handle
    judge = defns.setup_openai_weird_behavior_judge(
        demo_09_15_fake_user.behavior,
        model_name="gpt-5-mini-2025-08-07",
        oai_requires="openai>=1.108.0",
    )

    # Create BackendRunner and test judge provision
    async with backend_runner.BackendRunner.create(config) as runner:
        # Activate the default collection
        await runner.activate_collection("default")

        # Request the judge implementation (runner handles dependencies automatically)
        judge_impl = await runner.request(judge)

        # Verify we got a valid implementation back
        assert judge_impl is not None

        # Verify the judge is a FxtrCallTarget
        assert isinstance(judge_impl, core.FxtrCallTarget)


@pytest.mark.anyio
@pytest.mark.parametrize("python_provider", ["in_process", "uv"])
async def test_backend_runner_handle(materialized_env, python_provider):
    """Test that BackendRunner can provision a TaskResultHandle for materialized tasks."""
    config, _ = materialized_env

    # Import the experiment definition
    import demo_09_15_fake_user

    # Get the task result handle
    experiment = demo_09_15_fake_user.experiment
    idea_gen_task = experiment.idea_gen_task
    idea_gen_handle = idea_gen_task.result_handle

    # Create BackendRunner and test handle provision
    async with backend_runner.BackendRunner.create(config) as runner:
        # Activate the default collection
        await runner.activate_collection("default")

        # Request the task result handle implementation
        handle_impl = await runner.request(idea_gen_handle)

        # Verify we got a valid implementation back
        assert handle_impl is not None
