"""Tests for remotes: their configuration, choosing one, and pushing and pulling
jobs through an in-memory kind registered here (the real kinds have tests of their own)."""

from __future__ import annotations

import dataclasses
import shlex
import subprocess
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import anyio
import pytest
from postgres_testing import STALE_AFTER, FakeEnvironment, a_client, a_project
from project_remotes_testing import STORE, MemoryRemote

from fxtr.entity_defns.array import Array
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.project.cli import _parser
from fxtr.project.client import ImportRefusedError, JobID
from fxtr.project.config import (
    LOCAL_CONFIG_FILENAME,
    REQUIRED,
    ConfigError,
    Project,
    RemoteConfig,
    RemotesConfig,
    Setting,
    load_remotes,
)
from fxtr.project.environment import Environment
from fxtr.project.remotes import (
    PushedJob,
    Remote,
    RemoteError,
    implied_remote,
    pull_command,
    pull_job,
    push_job,
    register_kind,
    remote_for,
)

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres.database import Database  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def empty_store() -> None:
    STORE.clear()


@pytest.fixture
async def elsewhere(database: Database) -> AsyncIterator[Database]:
    """A second project's database, on its own schema of the same server."""
    async with Database(database.url, pg_schema=f"test_{uuid.uuid4().hex}") as other:
        await other.create_schema()
        try:
            yield other
        finally:
            with anyio.fail_after(30):
                await other.drop_schema(lock_timeout="5s")


def with_remotes(database: Database, default: str | None = None, **kinds: str) -> Project:
    """The test project with remotes of the given kinds, by name."""
    remotes = {name: RemoteConfig(name, kind, {}) for name, kind in kinds.items()}
    return dataclasses.replace(a_project(database), remotes=RemotesConfig(remotes, default))


@step(inputs={"x": int}, output=int)
async def double(context: StepContext, x: int) -> int:
    return 2 * x


@workflow(inputs={"x": int}, output=int)
async def doubled(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("double", double, {"x": x})


async def test_a_pushed_job_is_pulled_into_another_project(
    database: Database, elsewhere: Database
) -> None:
    here = with_remotes(database, team="memory")
    async with a_client(database, double, doubled, project=here) as client:
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        pushed = await push_job(client, job, tags=["latest"])
    assert pushed.remote == "team" and pushed.tags == ("latest",)
    assert pull_command(here, pushed) == "fxtr pull latest"
    there = with_remotes(elsewhere, team="memory")
    async with a_client(elsewhere, double, doubled, project=there) as client:
        assert await pull_job(client, "latest") == job
        assert await client.jobs.result(job) == Array.scalar(6, int)


# ---- choosing and building a remote ----


class StrictRemote(MemoryRemote):
    """A kind with a setting of its own, to check that a kind's settings are read strictly."""


def build_strict(
    config: RemoteConfig, settings: Mapping[str, Any], project: Project, env: Environment
) -> Remote:
    assert settings["repository"] == "r"
    return StrictRemote(config, project=project, environment=env, git_remote="origin")


register_kind("strict", (Setting("repository", str, REQUIRED, "a repository"),), build_strict)


def remotes_of(tmp_path: Path, default: str | None = None, **remotes: RemoteConfig) -> Project:
    return Project(
        root=tmp_path,
        name="test",
        modules=(),
        stale_after=STALE_AFTER,
        database_url="unused",
        schema="unused",
        remotes=RemotesConfig(remotes, default),
    )


def test_a_remote_is_the_named_one_else_the_default_else_the_only_one(tmp_path: Path) -> None:
    environment = FakeEnvironment()
    one = RemoteConfig("one", "memory", {})
    two = RemoteConfig("two", "memory", {})
    assert remote_for(remotes_of(tmp_path, one=one), environment).name == "one"
    both = remotes_of(tmp_path, one=one, two=two)
    assert remote_for(both, environment, "two").name == "two"
    assert remote_for(remotes_of(tmp_path, "two", one=one, two=two), environment).name == "two"
    with pytest.raises(RemoteError, match="Name a remote"):
        remote_for(both, environment)
    with pytest.raises(RemoteError, match="no remote named 'three'"):
        remote_for(both, environment, "three")


def test_a_default_remote_that_names_nothing_is_refused_only_when_it_is_needed(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n[tool.fxtr]\nmodules = []\n')
    (tmp_path / LOCAL_CONFIG_FILENAME).write_text('database_url = "u"\ndefault_remote = "gone"\n')
    assert load_remotes(tmp_path).default == "gone"
    project = remotes_of(tmp_path, default="gone")
    with pytest.raises(RemoteError, match="default_remote 'gone' names no configured remote"):
        implied_remote(project)


def test_a_project_with_no_remote_has_nowhere_to_push(tmp_path: Path) -> None:
    """As git does, a push goes only where a remote says: no hub is assumed, and the message
    says how to add one. A name that is no configured remote is refused too, including "hub"."""
    environment = FakeEnvironment()
    with pytest.raises(RemoteError, match="fxtr remote add"):
        remote_for(remotes_of(tmp_path), environment)
    with pytest.raises(RemoteError, match="no remote named 'hub'"):
        remote_for(remotes_of(tmp_path), environment, "hub")


def test_a_remote_needs_a_known_kind_and_its_settings(tmp_path: Path) -> None:
    environment = FakeEnvironment()
    unknown = RemoteConfig("team", "nfs", {})
    with pytest.raises(RemoteError, match="kind 'nfs'"):
        remote_for(remotes_of(tmp_path, team=unknown), environment)
    built = RemoteConfig("team", "strict", {"repository": "r"})
    assert isinstance(remote_for(remotes_of(tmp_path, team=built), environment), StrictRemote)
    for settings, message in (
        ({}, "repository is required"),
        ({"repository": "r", "tag": "x"}, "unknown setting"),
        ({"repository": 1}, "repository must be"),
    ):
        config = RemoteConfig("team", "strict", settings)
        with pytest.raises(ConfigError, match=message):
            remote_for(remotes_of(tmp_path, team=config), environment)


def test_the_pull_command_names_the_remote_only_when_a_pull_would_not_pick_it(
    tmp_path: Path,
) -> None:
    one, two = RemoteConfig("one", "memory", {}), RemoteConfig("two", "memory", {})
    pushed = PushedJob("two", JobID(uuid.uuid4()), ("t",), "t", "sha256:0")
    assert pull_command(remotes_of(tmp_path, two=two), pushed) == "fxtr pull t"
    assert pull_command(remotes_of(tmp_path, "two", one=one, two=two), pushed) == "fxtr pull t"
    assert pull_command(remotes_of(tmp_path, one=one, two=two), pushed) == (
        "fxtr pull t --remote two"
    )


@pytest.mark.parametrize(
    "tag", ["release/東京 v1", "x; printf injected", "$(printf expanded)", "-n"]
)
@pytest.mark.parametrize("remote", ["two words", "-team", "team"])
def test_printed_pull_command_preserves_tag_and_remote_in_a_shell(
    tmp_path: Path, tag: str, remote: str
) -> None:
    pushed = PushedJob(remote, JobID(uuid.uuid4()), (tag,), tag, "sha256:0")
    project = remotes_of(tmp_path, other=RemoteConfig("other", "memory", {}))
    command = pull_command(project, pushed)
    # A shell function stands in for the executable, recording exactly the arguments a pasted
    # command would deliver. The values deliberately include harmless shell syntax.
    result = subprocess.run(
        ["sh", "-c", 'fxtr() { printf "%s\\n" "$@"; }\n' + command],
        capture_output=True,
        text=True,
        check=True,
    )
    args = shlex.split(command)[1:]
    assert result.stdout.splitlines() == args
    parsed = _parser().parse_args(args)
    assert parsed.ref == tag and parsed.remote == remote


# ---- pulling ----


async def test_a_pull_of_a_job_here_needs_update_and_an_unknown_tag_is_refused(
    database: Database,
) -> None:
    project = with_remotes(database, team="memory")
    async with a_client(database, double, doubled, project=project) as client:
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        pushed = await push_job(client, job)
        with pytest.raises(ImportRefusedError, match="--update"):
            await pull_job(client, pushed.tags[0])
        assert await pull_job(client, pushed.tags[0], update=True) == job
        assert await pull_job(client, "@" + pushed.ref, update=True) == job
        with pytest.raises(RemoteError, match="names nothing"):
            await pull_job(client, "missing")
