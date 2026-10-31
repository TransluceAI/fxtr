"""Shared execution and caller-owned launchers: live storage, flags, and optional dependencies."""

from __future__ import annotations

import dataclasses
import os
import runpy
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from fxtr.core.entities import BoundID, FxtrEntityStorage
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.project import running
from fxtr.project.client import JobHandle, JobNotFinishedError, ProjectClient

if TYPE_CHECKING:
    from fxtr.runner.postgres.database import Database

REPO = Path(__file__).resolve().parents[2]
EXAMPLE_LAUNCHERS = (
    "launch_rollout_experiment.py",
    "launch_bio_refinement.py",
    "launch_model_harness_sweep.py",
)
CONFIGURABLE_LAUNCHERS = ("launch_slow_mapped.py", "launch_huge_grid.py")


@dataclasses.dataclass(frozen=True)
class Note(DataclassEntity, fxtr_type="test.project_running.Note"):
    text: str


@step(name="project_running.note")
async def note(context: StepContext, text: str) -> BoundID[Note]:
    if text == "fail":
        raise ValueError("cannot write note")
    return await context.store(Note(text))


@workflow(name="project_running.write", inputs={"text": str}, output=BoundID[Note])
async def write(context: WorkflowContext, text: ArrayHandle[str]) -> ArrayHandle[BoundID[Note]]:
    return context.run_step("note", note, {"text": text})


def test_launch_cli_uses_direct_options_without_parsing_the_callers_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["user-script", "--custom-option", "x"])
    inputs = {"text": Array.scalar("hello", str)}
    client = object()
    calls = []

    @asynccontextmanager
    async def open_client(
        anchor: str | Path, *, verify_environment: bool, max_concurrency: int | None
    ) -> AsyncIterator[object]:
        assert anchor == __file__ and not verify_environment
        assert max_concurrency == 3
        yield client

    async def on_result(storage: FxtrEntityStorage, result: Array[Any]) -> None:
        pass

    async def run_job(*args: Any, **kwargs: Any) -> Array[Any]:
        assert args == (client, write, inputs)
        calls.append(kwargs)
        return Array.scalar("hello", str)

    monkeypatch.setattr(running, "open_local_client", open_client)
    monkeypatch.setattr(running, "run_job", run_job)
    running.launch_cli(
        write,
        inputs,
        anchor=__file__,
        allow_staged=True,
        verify_environment=False,
        max_concurrency=3,
        overwrite_cache_conflicts=True,
        on_result=on_result,
        open_browser=False,
    )
    assert calls == [
        {
            "allow_staged": True,
            "overwrite_cache_conflicts": True,
            "on_result": on_result,
            "open_browser": False,
        }
    ]


@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("launcher", EXAMPLE_LAUNCHERS)
def test_example_launchers_use_postgres_defaults_and_close_the_client(
    launcher: str, stopped: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = REPO / "example_projects" / "basic_project" / launcher
    calls: list[dict[str, Any]] = []
    events: list[str] = []
    client = object()

    @asynccontextmanager
    async def open_client(
        anchor: str | Path, *, verify_environment: bool, max_concurrency: int | None
    ) -> AsyncIterator[object]:
        assert anchor == str(script)
        assert verify_environment
        assert max_concurrency is None
        events.append("open")
        try:
            yield client
        finally:
            events.append("close")

    async def run_job(*args: Any, on_result: running.ResultHandler, **kwargs: Any) -> Array[Any]:
        assert args[0] is client
        assert events == ["open"]
        events.append("run")
        calls.append(kwargs)
        if stopped:
            raise JobNotFinishedError("the job stopped")
        return Array.scalar("hello", str)

    monkeypatch.setattr(running, "open_local_client", open_client)
    monkeypatch.setattr(running, "run_job", run_job)
    monkeypatch.setattr(sys, "argv", [str(script)])

    if stopped:
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(script), run_name="__main__")
        assert exc.value.code == 1
    else:
        runpy.run_path(str(script), run_name="__main__")
    assert calls == [
        {"allow_staged": False, "overwrite_cache_conflicts": False, "open_browser": True}
    ]
    assert events == ["open", "run", "close"]


@pytest.mark.parametrize("launcher", EXAMPLE_LAUNCHERS + CONFIGURABLE_LAUNCHERS)
def test_importing_launchers_and_cli_help_need_no_runner_extra(launcher: str) -> None:
    project = REPO / "example_projects" / "basic_project"
    script = """
import importlib.abc
import runpy
import sys

class NoPostgres(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('sqlalchemy', 'asyncpg'):
            raise AssertionError(f'unexpected optional import: {fullname}')

sys.meta_path.insert(0, NoPostgres())
runpy.run_path(sys.argv[1])
if sys.argv[1].endswith(('launch_slow_mapped.py', 'launch_huge_grid.py')):
    script = sys.argv[1]
    sys.argv = [script, '--help']
    try:
        runpy.run_path(script, run_name='__main__')
    except SystemExit as exc:
        assert exc.code == 0
import anyio
from fxtr.project import cli
anyio.run(lambda: cli.main(['--help']))
"""
    env = os.environ | {
        "PYTHONPATH": os.pathsep.join([str(project / "src"), os.environ.get("PYTHONPATH", "")])
    }
    completed = subprocess.run(
        [sys.executable, "-c", script, str(project / launcher)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout


@pytest.mark.parametrize("launcher", CONFIGURABLE_LAUNCHERS)
@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_configurable_launchers_preserve_options_and_close_the_client(
    launcher: str, stopped: bool, custom: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("sqlalchemy")
    from fxtr.runner.postgres.client import LocalProjectClient
    from fxtr.runner.postgres.runner import RunLimits

    script = REPO / "example_projects" / "basic_project" / launcher
    huge = launcher == "launch_huge_grid.py"
    concurrency = 3 if custom else 16 if huge else 8
    client = object()
    events: list[str] = []

    @asynccontextmanager
    async def open_client(
        anchor: str, *, verify_environment: bool, limits: RunLimits
    ) -> AsyncIterator[object]:
        assert anchor == str(script)
        assert verify_environment is not custom
        assert limits.steps == concurrency
        events.append("open")
        try:
            yield client
        finally:
            events.append("close")

    async def run_job(
        actual_client: object,
        workflow: Any,
        inputs: dict[str, Array[Any]],
        *,
        allow_staged: bool,
        overwrite_cache_conflicts: bool,
        on_result: running.ResultHandler,
        open_browser: bool,
    ) -> Array[Any]:
        assert actual_client is client
        assert open_browser
        assert workflow.name == ("huge_grid_experiment" if huge else "slow_mapped_experiment")
        assert allow_staged is custom
        assert not overwrite_cache_conflicts
        assert events == ["open"]
        assert inputs["delay_scale"].item() == (0.0 if custom else 1.0)
        assert len(inputs["run_id"].item()) == 32
        if huge:
            assert inputs["tasks"].item() == (2 if custom else 25)
            assert inputs["seeds"].item() == (3 if custom else 5)
        else:
            assert inputs["items"].item() == (2 if custom else 40)
        events.append("run")
        if stopped:
            raise JobNotFinishedError("the job stopped")
        return Array.scalar("done", str)

    monkeypatch.setattr(LocalProjectClient, "from_project", open_client)
    monkeypatch.setattr(running, "run_job", run_job)
    options = ["--tasks", "2", "--seeds", "3"] if huge else ["--items", "2"]
    argv = [str(script)]
    if custom:
        argv += ["--allow-staged", "--no-verify", "--concurrency", "3", "--delay-scale", "0"]
        argv += options
    monkeypatch.setattr(sys, "argv", argv)
    if stopped:
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(script), run_name="__main__")
        assert exc.value.code == 1
    else:
        runpy.run_path(str(script), run_name="__main__")
    assert events == ["open", "run", "close"]


@pytest.mark.parametrize("max_concurrency", [0, -1])
def test_local_client_rejects_nonpositive_concurrency(max_concurrency: int) -> None:
    with pytest.raises(ValueError, match="max_concurrency must be positive"):
        running.open_local_client(__file__, max_concurrency=max_concurrency)


@pytest.mark.anyio
async def test_viewer_link_is_printed_before_waiting(capsys: pytest.CaptureFixture[str]) -> None:
    async def wait() -> None:
        assert "http://127.0.0.1:8123/#job=live-job" in capsys.readouterr().out
        raise RuntimeError("still running")

    async def viewer_url() -> str:
        return "http://127.0.0.1:8123/"

    client = cast(ProjectClient, SimpleNamespace(viewer_url=viewer_url))
    handle = cast(JobHandle, SimpleNamespace(job_id="live-job", wait=wait))
    with pytest.raises(RuntimeError, match="still running"):
        await running.wait_for_job(client, handle)


@pytest.mark.anyio
@pytest.mark.parametrize("fail", [False, True])
async def test_postgres_result_callback_uses_the_open_client(
    database: Database,
    capsys: pytest.CaptureFixture[str],
    fail: bool,
) -> None:
    from postgres_testing import FakeEnvironment, a_client

    environment = FakeEnvironment()
    client = a_client(database, write, note, environment=environment, verify_environment=False)
    notes: list[Note] = []

    async def read_note(storage: FxtrEntityStorage, result: Array[Any]) -> None:
        assert storage is client
        notes.append(await storage.load(result.item(), as_type=Note))

    async def run() -> Array[Any]:
        return await running.run_job(
            client,
            write,
            {"text": Array.scalar("fail" if fail else "hello", str)},
            allow_staged=True,
            on_result=read_note,
        )

    async with client:
        if fail:
            with pytest.raises(JobNotFinishedError, match="stopped"):
                await run()
            assert notes == []
        else:
            result = await run()
            assert notes == [Note("hello")]
            assert await client.load(result.item(), as_type=Note) == Note("hello")
    assert environment.pins == [True]
    assert environment.checks == 0
    out = capsys.readouterr().out
    assert (": stopped" if fail else ": succeeded") in out
    if fail:
        assert "step root/note failed: ValueError: cannot write note" in out
    # No viewer runs for the test's project.
    assert "no viewer is running for this project: start one with `uv run fxtr view`" in out
