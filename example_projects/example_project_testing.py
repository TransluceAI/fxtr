"""Postgres fixtures shared by the independent example projects.

The production client locates and imports each project's configured modules. Only the code pin
and environment check are faked, using fxtr's test environment, so tests never pin this checkout.
"""

import importlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace

import anyio
import pytest

from fxtr.project.client import ProjectClient
from fxtr.project.config import locate_project

# Jobs the tests launch never open a browser on a viewer someone has running.
os.environ["FXTR_NO_BROWSER"] = "1"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def project_client(request: pytest.FixtureRequest) -> AsyncIterator[ProjectClient]:
    url = os.environ.get("FXTR_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FXTR_TEST_DATABASE_URL is not set")
    from postgres_testing import FakeEnvironment

    from fxtr.runner.postgres.client import LocalProjectClient
    from fxtr.runner.postgres.database import Database
    from fxtr.runner.postgres.runner import RunnerSettings

    project = replace(
        locate_project(request.path, env={"FXTR_DATABASE_URL": url}),
        schema=f"example_test_{uuid.uuid4().hex}",
    )
    for module in project.modules:
        importlib.import_module(module)
    database = Database(url, pg_schema=project.schema)
    await database.create_schema()
    try:
        async with LocalProjectClient(
            database,
            project=project,
            environment=FakeEnvironment(),
            handle_sigint=False,
            settings=RunnerSettings(poll_interval=0.02, reconcile_interval=0.1),
        ) as client:
            with anyio.fail_after(120):
                yield client
    finally:
        with anyio.fail_after(30):
            await database.drop_schema()
        await database.dispose()


@pytest.fixture
def launcher(
    request: pytest.FixtureRequest,
    project_client: ProjectClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Load a project's launcher with its client opener connected to the test database."""
    from contextlib import asynccontextmanager
    from importlib.util import module_from_spec, spec_from_file_location

    path = request.path.parents[1] / "launch.py"
    spec = spec_from_file_location(f"{path.parent.name}_launcher", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    @asynccontextmanager
    async def open_client(*args, **kwargs) -> AsyncIterator[ProjectClient]:
        yield project_client

    monkeypatch.setattr(module, "open_local_client", open_client)
    return module
