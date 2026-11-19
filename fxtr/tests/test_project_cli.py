"""The ``fxtr`` command line against a real database, through a local client with a fake
environment: what each command prints and exits with, and the refusals."""

from __future__ import annotations

import dataclasses
import os
import pty
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest
from postgres_testing import FakeEnvironment, a_client, a_project, a_runner
from project_remotes_testing import STORE

from fxtr.core.car import read_car
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.project import cli, running
from fxtr.project.client import JobNotFinishedError, JobSummary, ProjectClient
from fxtr.project.config import LOCAL_CONFIG_FILENAME, Project, RemoteConfig, RemotesConfig
from fxtr.project.environment import Pin, PinRefusedError, Problem

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres.client import LocalProjectClient  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402

pytestmark = pytest.mark.anyio


@step(name="cli_test.double")
async def double(context: StepContext, x: int) -> int:
    if x < 0:
        raise ValueError(f"cannot double {x}")
    return 2 * x


@workflow(name="cli_test.doubled", inputs={"x": int}, output=int)
async def doubled(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("double", double, {"x": x})


@workflow(name="cli_test.doubled_each", inputs={"xs": ("[i: int]", int)}, output=("[i: int]", int))
async def doubled_each(context: WorkflowContext, xs: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("double", double, {"x": xs}, map_over=["i"])


FUNCTIONS = (double, doubled, doubled_each)


class Opener:
    """An :class:`~fxtr.project.cli.OpenClient` over the test database, recording what each
    command asked for."""

    def __init__(
        self,
        database: Database,
        environment: FakeEnvironment | None = None,
        project: Project | None = None,
    ) -> None:
        self.database = database
        self.environment = FakeEnvironment() if environment is None else environment
        self.project = project
        self.opened: list[tuple[Path | None, bool]] = []

    def __call__(
        self, project: Path | None, *, verify_environment: bool
    ) -> AbstractAsyncContextManager[ProjectClient]:
        self.opened.append((project, verify_environment))
        return a_client(
            self.database,
            *FUNCTIONS,
            environment=self.environment,
            verify_environment=verify_environment,
            project=self.project,
        )


async def fxtr(
    opener: Opener, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    """Run the command line; its exit status, stdout, and stderr."""
    status = await cli.main(argv, open_client=opener)
    out, err = capsys.readouterr()
    return status, out, err


def job_id_in(out: str) -> str:
    """The job id of a ``job <id>: running`` line."""
    (line,) = [line for line in out.splitlines() if line.endswith(": running")]
    return line.removeprefix("job ").removesuffix(": running")


# ---- run, resume, and the job commands ----


@pytest.mark.parametrize("flags, opens", [((), True), (("--no-open",), False)])
async def test_run_opens_the_job_unless_told_not_to(
    database: Database,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    flags: tuple[str, ...],
    opens: bool,
) -> None:
    shown: list[bool] = []

    async def show_job(client: object, job_id: str, *, open_browser: bool = True) -> None:
        shown.append(open_browser)

    monkeypatch.setattr(running, "show_job", show_job)
    status, _, _ = await fxtr(
        Opener(database), capsys, "run", doubled.name, "--input", "x=21", *flags
    )
    assert status == cli.EXIT_OK and shown == [opens]


async def test_run_waits_for_the_job_and_prints_its_status(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = Opener(database)
    status, out, _ = await fxtr(opener, capsys, "run", doubled.name, "--input", "x=21")
    assert status == cli.EXIT_OK
    job = job_id_in(out)
    assert f"job {job}: succeeded" in out and "result: " in out
    assert "no viewer is running for this project" in out
    assert out.rstrip().endswith("42")
    assert opener.opened == [(None, True)]
    assert opener.environment.pins == [False]

    status, out, _ = await fxtr(opener, capsys, "jobs", "list")
    assert status == cli.EXIT_OK
    assert out.startswith(f"{job}  succeeded ") and out.rstrip().endswith(f"{doubled.name} (root)")
    status, out, _ = await fxtr(opener, capsys, "jobs", "status", job)
    assert (status, out.splitlines()[0]) == (cli.EXIT_OK, f"job {job}: succeeded")


@pytest.mark.parametrize("x", [21, -1])
async def test_cli_and_python_api_report_the_same_job(
    database: Database,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    x: int,
) -> None:
    opener = Opener(database)
    status, cli_out, _ = await fxtr(
        opener,
        capsys,
        "run",
        doubled.name,
        "--input",
        f"x={x}",
        "--root",
        "comparison",
        "--allow-staged",
        "--no-verify",
    )

    async def launch() -> Array[Any]:
        async with opener(tmp_path, verify_environment=False) as client:
            return await running.run_job(
                client,
                doubled,
                {"x": Array.scalar(x, int)},
                root="comparison",
                allow_staged=True,
            )

    if x < 0:
        assert status == cli.EXIT_STOPPED
        with pytest.raises(JobNotFinishedError, match="stopped"):
            await launch()
    else:
        assert status == cli.EXIT_OK
        assert (await launch()).item() == 2 * x
    python_out = capsys.readouterr().out
    assert cli_out.replace(job_id_in(cli_out), "JOB") == python_out.replace(
        job_id_in(python_out), "JOB"
    )
    assert opener.opened == [(None, False), (tmp_path, False)]
    assert opener.environment.pins == [True, True]
    assert opener.environment.checks == 0


async def test_run_passes_the_launch_flags_and_the_project(
    database: Database, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    opener = Opener(database)
    status, _, _ = await fxtr(
        opener,
        capsys,
        "--project",
        str(tmp_path),
        "run",
        doubled.name,
        "--input",
        "x=1",
        "--allow-staged",
        "--no-verify",
    )
    assert status == cli.EXIT_OK
    assert opener.opened == [(tmp_path, False)]
    assert opener.environment.pins == [True]
    assert opener.environment.checks == 0


async def test_a_run_whose_job_stops_exits_1_with_the_errors(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = await fxtr(Opener(database), capsys, "run", doubled.name, "--input", "x=-1")
    assert status == cli.EXIT_STOPPED
    assert "stopped" in out
    assert "step root/double failed: ValueError: cannot double -1" in out


async def test_an_input_names_a_project_array_to_snapshot(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = Opener(database)
    rows = Array.from_items([((1,), 1), ((2,), 5)], ("[i: int]", int))
    async with opener(None, verify_environment=True) as client:
        await client.create_array("xs", rows.type)
        await client.replace_array("xs", rows)
    status, out, _ = await fxtr(opener, capsys, "run", doubled_each.name, "--input", "xs=@xs")
    assert status == cli.EXIT_OK, out
    async with opener(None, verify_environment=True) as client:
        handle = await client.job(cli._job_id(job_id_in(out)))  # pyright: ignore[reportPrivateUsage]
        assert await handle.result() == Array.from_items([((1,), 2), ((2,), 10)], rows.type)


async def test_resume_runs_a_job_nothing_has_taken(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = Opener(database)
    async with a_client(database, *FUNCTIONS) as client:
        job = await client.jobs.create(doubled, {"x": Array.scalar(4, int)})
    status, out, _ = await fxtr(opener, capsys, "resume", str(job))
    assert status == cli.EXIT_OK
    assert f"job {job}: succeeded" in out
    assert "no viewer is running for this project" in out
    assert out.rstrip().endswith("8")

    status, _, err = await fxtr(opener, capsys, "resume", str(job))
    assert status == cli.EXIT_REFUSED
    assert "nothing to resume" in err


async def test_a_resumed_job_that_fails_exits_1_with_the_errors(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = Opener(database)
    async with a_client(database, *FUNCTIONS) as client:
        job = await client.jobs.create(doubled, {"x": Array.scalar(-1, int)})
    status, out, err = await fxtr(opener, capsys, "resume", str(job))
    assert status == cli.EXIT_STOPPED
    assert "step root/double failed: ValueError: cannot double -1" in out
    assert "no viewer is running for this project" in out
    assert err == ""


async def test_cancel_asks_only_a_job_that_is_not_finished(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = Opener(database)
    async with a_client(database, *FUNCTIONS) as client:
        untaken = await client.jobs.create(doubled, {"x": Array.scalar(4, int)})
        taken = await client.jobs.create(doubled, {"x": Array.scalar(5, int)})
        await a_runner(database, taken, *FUNCTIONS)
    status, out, _ = await fxtr(opener, capsys, "jobs", "cancel", str(untaken))
    assert (status, out) == (cli.EXIT_OK, f"job {untaken}: stopped, nothing to cancel\n")
    status, out, _ = await fxtr(opener, capsys, "jobs", "cancel", str(taken), "--hard")
    assert (status, out) == (cli.EXIT_OK, f"job {taken}: hard cancel requested\n")

    _, out, _ = await fxtr(opener, capsys, "run", doubled.name, "--input", "x=1")
    done = job_id_in(out)
    status, out, _ = await fxtr(opener, capsys, "jobs", "cancel", done)
    assert (status, out) == (cli.EXIT_OK, f"job {done}: succeeded, nothing to cancel\n")


async def test_export_writes_a_jobs_record(
    database: Database, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    opener = Opener(database)
    _, out, _ = await fxtr(opener, capsys, "run", doubled.name, "--input", "x=1")
    done = job_id_in(out)
    path = tmp_path / "job.car"
    status, out, _ = await fxtr(opener, capsys, "jobs", "export", done, str(path))
    (root,), _ = read_car(path)
    record = EntityID(cid=root).cid_str()
    assert (status, out) == (cli.EXIT_OK, f"job {done}: exported to {path} (record {record})\n")
    # This project has the job already, so importing it back is refused, unless it updates the
    # job, which here changes nothing.
    status, _, err = await fxtr(opener, capsys, "jobs", "import", str(path))
    assert status == cli.EXIT_REFUSED and "--update" in err
    status, out, _ = await fxtr(opener, capsys, "jobs", "import", "--update", str(path))
    assert (status, out) == (cli.EXIT_OK, f"job {done}: imported from {path}\n")
    status, out, _ = await fxtr(opener, capsys, "jobs", "import", "--replace", str(path))
    assert (status, out) == (cli.EXIT_OK, f"job {done}: imported from {path}\n")


# ---- push and pull ----


async def test_push_sends_a_job_and_pull_brings_it_back(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    STORE.clear()
    remotes = {"team": RemoteConfig("team", "memory", {})}
    project = dataclasses.replace(a_project(database), remotes=RemotesConfig(remotes))
    opener = Opener(database, project=project)
    _, out, _ = await fxtr(opener, capsys, "run", doubled.name, "--input", "x=1")
    done = job_id_in(out)
    status, out, _ = await fxtr(opener, capsys, "push", done, "--tag", "latest")
    (pushed, hint) = out.splitlines()
    assert status == cli.EXIT_OK
    assert pushed.startswith(f"job {done}: pushed to team as latest (sha256:")
    assert hint == "pull it with: fxtr pull latest"
    # This project has the job, so a pull needs --update.
    status, _, err = await fxtr(opener, capsys, "pull", "latest")
    assert status == cli.EXIT_REFUSED and "--update" in err
    status, out, _ = await fxtr(opener, capsys, "pull", "latest", "--update")
    assert (status, out) == (cli.EXIT_OK, f"job {done}: pulled from team (latest)\n")
    status, out, _ = await fxtr(opener, capsys, "pull", "latest", "--replace")
    assert (status, out) == (cli.EXIT_OK, f"job {done}: pulled from team (latest)\n")
    # A pull can adopt the job's results into the step cache, as an import can.
    await fxtr(opener, capsys, "cache", "clear", "--all")
    status, _, err = await fxtr(
        opener, capsys, "pull", "latest", "--update", "--overwrite-cache-conflicts"
    )
    assert status == cli.EXIT_REFUSED and "needs --adopt-cache" in err
    _, out, _ = await fxtr(opener, capsys, "pull", "latest", "--update", "--adopt-cache")
    assert out.splitlines() == [
        f"job {done}: pulled from team (latest)",
        f"job {done}: adopted 1 cache entry",
    ]
    status, _, err = await fxtr(opener, capsys, "push", done, "--remote", "elsewhere")
    assert status == cli.EXIT_REFUSED and "no remote named 'elsewhere'" in err
    # Several tags, on a record the remote holds already; and without any, the kind's default
    # tag, which the pull command names.
    _, out, _ = await fxtr(opener, capsys, "push", done, "--tag", "a", "--tag", "b")
    assert out.startswith(f"job {done}: already on team as a, b (sha256:")
    _, out, _ = await fxtr(opener, capsys, "push", done)
    assert out.splitlines()[1] == f"pull it with: fxtr pull job-{done}"


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[bytes], None]]:
    """Makes standard input a real terminal with these keys typed into it."""
    typist, screen = pty.openpty()
    monkeypatch.setattr("sys.stdin", os.fdopen(screen))

    def type_keys(keys: bytes) -> None:
        os.write(typist, keys)

    yield type_keys
    sys.stdin.close()
    os.close(typist)


async def two_jobs_with_a_remote(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> tuple[Opener, str, str]:
    """An opener whose project has one remote, and two finished jobs: the older, the newer."""
    STORE.clear()
    remotes = {"team": RemoteConfig("team", "memory", {})}
    project = dataclasses.replace(a_project(database), remotes=RemotesConfig(remotes))
    opener = Opener(database, project=project)
    _, out, _ = await fxtr(opener, capsys, "run", doubled.name, "--input", "x=1")
    older = job_id_in(out)
    _, out, _ = await fxtr(opener, capsys, "run", doubled.name, "--input", "x=2", "--root", "other")
    return opener, older, job_id_in(out)


@pytest.mark.parametrize(
    "typed, chosen",
    [(b"\n", "newer"), (b"1\n", "newer"), (b"2\n", "older"), (b"7\nnope\n2\n", "older")],
)
async def test_a_push_with_no_job_asks_which_at_a_terminal(
    database: Database,
    capsys: pytest.CaptureFixture[str],
    terminal: Callable[[bytes], None],
    typed: bytes,
    chosen: str,
) -> None:
    opener, older, newer = await two_jobs_with_a_remote(database, capsys)
    terminal(typed)
    status, out, err = await fxtr(opener, capsys, "push")
    assert status == cli.EXIT_OK
    assert out.startswith(f"job {newer if chosen == 'newer' else older}: pushed to team")
    first, second = err.splitlines()[:2]
    assert first.split()[0] == "1" and first.rstrip().endswith(newer[:8])
    assert second.split()[0] == "2" and second.rstrip().endswith(older[:8])
    assert f"{doubled.name} (other)" in first and "succeeded" in first and "just now" in first
    assert "Push which job to team? [1]: " in err
    assert ("Choose a number from 1 to 2." in err) == typed.startswith(b"7")


async def test_a_running_job_is_shown_but_cannot_be_chosen(
    database: Database,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    terminal: Callable[[bytes], None],
) -> None:
    opener, older, newer = await two_jobs_with_a_remote(database, capsys)
    running_id = uuid.uuid4()

    list_jobs = LocalProjectClient.list_jobs

    async def listed(self: LocalProjectClient, *, limit: int | None = None) -> list[JobSummary]:
        jobs = await list_jobs(self, limit=limit)
        return [dataclasses.replace(jobs[0], job_id=running_id, state="running"), *jobs]

    monkeypatch.setattr(LocalProjectClient, "list_jobs", listed)
    terminal(b"2\n")
    status, out, err = await fxtr(opener, capsys, "push")
    assert status == cli.EXIT_OK and out.startswith(f"job {older}: pushed to team")
    assert err.splitlines()[0].split()[0] == "-"
    assert str(running_id)[:8] in err.splitlines()[0]
    assert newer[:8] in err.splitlines()[1]


async def test_a_push_with_no_job_refuses_with_nobody_at_a_terminal(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    opener, older, newer = await two_jobs_with_a_remote(database, capsys)
    # pytest's standard input is not a terminal.
    status, out, err = await fxtr(opener, capsys, "push")
    assert (status, out) == (cli.EXIT_REFUSED, "")
    assert "fxtr push JOB" in err
    assert [line.split()[0] for line in err.splitlines()[1:]] == [newer, older]


async def test_ending_a_terminals_input_chooses_no_job(
    database: Database, capsys: pytest.CaptureFixture[str], terminal: Callable[[bytes], None]
) -> None:
    opener, _, _ = await two_jobs_with_a_remote(database, capsys)
    terminal(b"\x04")  # Ctrl-D
    status, out, err = await fxtr(opener, capsys, "push")
    assert (status, out) == (cli.EXIT_REFUSED, "")
    assert "no job chosen" in err


async def test_a_push_with_no_job_and_no_jobs_says_to_run_one(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    remotes = {"team": RemoteConfig("team", "memory", {})}
    project = dataclasses.replace(a_project(database), remotes=RemotesConfig(remotes))
    status, _, err = await fxtr(Opener(database, project=project), capsys, "push")
    assert status == cli.EXIT_REFUSED and "run one first" in err


async def test_jobs_list_can_show_only_the_newest(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    opener, older, newer = await two_jobs_with_a_remote(database, capsys)
    _, out, _ = await fxtr(opener, capsys, "jobs", "list", "--limit", "1")
    assert [line.split()[0] for line in out.splitlines()] == [newer]
    _, out, _ = await fxtr(opener, capsys, "jobs", "list")
    assert [line.split()[0] for line in out.splitlines()] == [newer, older]
    with pytest.raises(SystemExit):
        await fxtr(opener, capsys, "jobs", "list", "--limit", "0")


# ---- the cache ----


async def test_the_cache_is_listed_and_cleared_by_selection(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = Opener(database)
    await fxtr(opener, capsys, "run", doubled.name, "--input", "x=1", "--root", "a")
    await fxtr(opener, capsys, "run", doubled.name, "--input", "x=2", "--root", "b")
    status, out, _ = await fxtr(opener, capsys, "cache", "list")
    assert status == cli.EXIT_OK
    assert out.splitlines() == [
        f"completed  a/double  ({double.name})",
        f"completed  b/double  ({double.name})",
    ]
    _, out, _ = await fxtr(opener, capsys, "cache", "list", "a", "--state", "completed")
    assert out.splitlines() == [f"completed  a/double  ({double.name})"]

    status, _, err = await fxtr(opener, capsys, "cache", "clear")
    assert status == cli.EXIT_REFUSED and "exactly one of" in err
    status, _, err = await fxtr(opener, capsys, "cache", "list", "a/*")
    assert status == cli.EXIT_REFUSED and "a * stands only for a whole mapped value" in err
    _, out, _ = await fxtr(opener, capsys, "cache", "clear", "--address", "a/double")
    assert out == "cleared 1 cache entry\n"
    _, out, _ = await fxtr(opener, capsys, "cache", "clear", "--all")
    assert out == "cleared 1 cache entry\n"
    _, out, _ = await fxtr(opener, capsys, "cache", "list")
    assert out == ""


async def test_a_jobs_cache_conflicts_are_shown_listed_and_cleared(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = Opener(database)
    await fxtr(opener, capsys, "run", doubled.name, "--input", "x=1", "--root", "a")
    await fxtr(opener, capsys, "run", doubled.name, "--input", "x=1", "--root", "b")
    # The same root with a different input: the job stops on a cache conflict at a/double.
    status, out, _ = await fxtr(
        opener, capsys, "run", doubled.name, "--input", "x=2", "--root", "a"
    )
    assert status == cli.EXIT_STOPPED
    job = out.split(":", 1)[0].removeprefix("job ")
    assert "  cache conflicts at 1 address:\n" in out
    assert f"    a/double  1 address  {double.name}\n" in out
    assert f"  to list them:          fxtr cache list --conflicts-in {job}\n" in out
    clear = f"fxtr cache clear --conflicts-in {job}, then fxtr resume {job}"
    assert f"  to clear them:         {clear}\n" in out
    assert f"  to rerun overwriting:  fxtr resume {job} --overwrite-cache-conflicts\n" in out
    status, out, _ = await fxtr(opener, capsys, "jobs", "status", job)
    assert (status, out.splitlines()[0]) == (cli.EXIT_OK, f"job {job}: stopped")
    assert f"    a/double  1 address  {double.name}\n" in out
    _, out, _ = await fxtr(opener, capsys, "cache", "list", "--conflicts-in", job)
    assert out.splitlines() == [f"completed  a/double  ({double.name})"]
    # Clearing the job's conflicts leaves the entries it does not conflict with.
    status, _, err = await fxtr(opener, capsys, "cache", "clear", "a", "--conflicts-in", job)
    assert status == cli.EXIT_REFUSED and "exactly one of" in err
    _, out, _ = await fxtr(opener, capsys, "cache", "clear", "--conflicts-in", job)
    assert out == "cleared 1 cache entry\n"
    _, out, _ = await fxtr(opener, capsys, "cache", "list")
    assert out.splitlines() == [f"completed  b/double  ({double.name})"]
    status, _, _ = await fxtr(opener, capsys, "resume", job)
    assert status == cli.EXIT_OK
    _, out, _ = await fxtr(opener, capsys, "cache", "list", "--conflicts-in", job)
    assert out == ""


async def test_a_jobs_results_are_adopted_into_the_cache(
    database: Database, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    opener = Opener(database)
    _, out, _ = await fxtr(opener, capsys, "run", doubled.name, "--input", "x=1", "--root", "a")
    first = out.split(":", 1)[0].removeprefix("job ")
    await fxtr(opener, capsys, "cache", "clear", "--all")
    status, out, _ = await fxtr(opener, capsys, "cache", "adopt", first)
    assert (status, out) == (cli.EXIT_OK, f"job {first}: adopted 1 cache entry\n")
    _, out, _ = await fxtr(opener, capsys, "cache", "adopt", first, "b")
    assert out == f"job {first}: adopted 0 cache entries\n"
    _, out, _ = await fxtr(opener, capsys, "cache", "adopt", first)
    assert out.splitlines() == [
        f"job {first}: adopted 0 cache entries",
        "  skipped 1: the same result is there already",
    ]
    # Another request's result at the key is a conflict, replaced only when asked.
    await fxtr(opener, capsys, "cache", "clear", "--all")
    await fxtr(opener, capsys, "run", doubled.name, "--input", "x=2", "--root", "a")
    _, out, _ = await fxtr(opener, capsys, "cache", "adopt", first)
    assert out.splitlines() == [
        f"job {first}: adopted 0 cache entries",
        "  skipped 1: a different request's result is there (--overwrite-cache-conflicts"
        " replaces it)",
        "    a/double  (1 address)",
    ]
    _, out, _ = await fxtr(opener, capsys, "cache", "adopt", first, "--overwrite-cache-conflicts")
    assert out == f"job {first}: adopted 1 cache entry\n"
    # An import can adopt the job's results too.
    path = tmp_path / "job.car"
    await fxtr(opener, capsys, "jobs", "export", first, str(path))
    await fxtr(opener, capsys, "cache", "clear", "--all")
    status, _, err = await fxtr(
        opener, capsys, "jobs", "import", "--update", "--overwrite-cache-conflicts", str(path)
    )
    assert status == cli.EXIT_REFUSED and "needs --adopt-cache" in err
    _, out, _ = await fxtr(opener, capsys, "jobs", "import", "--update", "--adopt-cache", str(path))
    assert out.splitlines() == [
        f"job {first}: imported from {path}",
        f"job {first}: adopted 1 cache entry",
    ]


# ---- init ----


async def test_init_writes_the_local_config_once(
    database: Database, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\n\n[tool.fxtr]\n')
    opener = Opener(database)
    argv = ("--project", str(tmp_path), "init", "--database-url", "postgresql://h/d")
    status, out, _ = await fxtr(opener, capsys, *argv)
    assert status == cli.EXIT_OK and "the project's database is ready" in out
    written = (tmp_path / LOCAL_CONFIG_FILENAME).read_text()
    assert 'database_url = "postgresql://h/d"' in written

    status, _, err = await fxtr(opener, capsys, *argv)
    assert status == cli.EXIT_REFUSED and "--force" in err
    status, _, _ = await fxtr(opener, capsys, *argv, "--schema", "s", "--force")
    assert status == cli.EXIT_OK
    assert 'schema = "s"' in (tmp_path / LOCAL_CONFIG_FILENAME).read_text()


async def test_init_needs_an_fxtr_project(
    database: Database, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\n')
    status, _, err = await fxtr(
        Opener(database), capsys, "--project", str(tmp_path), "init", "--database-url", "u"
    )
    assert status == cli.EXIT_REFUSED and "[tool.fxtr]" in err
    assert not (tmp_path / LOCAL_CONFIG_FILENAME).exists()


# ---- refusals ----


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (("jobs", "status", "nope"), "not a job id: 'nope'"),
        (("jobs", "status", "00000000-0000-0000-0000-000000000000"), "No job 00000000"),
        (("run", "cli_test.missing"), "No function named 'cli_test.missing'"),
        (("run", "cli_test.doubled", "--input", "x"), "an input is NAME=VALUE"),
        (("run", "cli_test.doubled", "--input", "x=[1]"), "write a launcher script"),
        (("run", "cli_test.doubled", "--input", "x=1.5"), "Input 'x'"),
    ],
)
async def test_refusals_print_a_message_and_exit_2(
    database: Database, capsys: pytest.CaptureFixture[str], argv: Sequence[str], says: str
) -> None:
    status, out, err = await fxtr(Opener(database), capsys, *argv)
    assert status == cli.EXIT_REFUSED
    assert err.startswith("fxtr: ") and says in err and "Traceback" not in err
    assert out == ""


class RefusingEnvironment(FakeEnvironment):
    """A fake environment whose checkout cannot be pinned."""

    async def pin_code(self, project: Project, *, allow_staged: bool) -> Pin:
        raise PinRefusedError("The checkout holds changes. Commit them, or pass allow_staged=True.")


async def test_the_environment_refusals_say_the_command_line_flag(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    run_argv = ("run", doubled.name, "--input", "x=1")
    status, _, err = await fxtr(Opener(database, RefusingEnvironment()), capsys, *run_argv)
    assert status == cli.EXIT_REFUSED
    assert err.endswith("(On the command line, allow_staged=True is --allow-staged.)\n")

    unsynced = FakeEnvironment(problems=[Problem(what="The lock is stale.", fix="uv lock")])
    status, _, err = await fxtr(Opener(database, unsynced), capsys, *run_argv)
    assert status == cli.EXIT_REFUSED
    assert "The lock is stale." in err
    assert err.endswith("(On the command line, verify_environment=False is --no-verify.)\n")


# ---- parsing ----


@pytest.mark.parametrize(
    "argv",
    [
        ("--project", "p", "-v", "jobs", "list"),
        ("jobs", "--project", "p", "-v", "list"),
        ("jobs", "list", "--project", "p", "-v"),
    ],
)
def test_the_shared_options_go_before_or_after_a_command(argv: Sequence[str]) -> None:
    args: Any = cli._parse(argv)  # pyright: ignore[reportPrivateUsage]
    assert (args.project, args.verbosity) == (Path("p"), 1)


def test_the_shared_options_default() -> None:
    args: Any = cli._parse(["jobs", "list"])  # pyright: ignore[reportPrivateUsage]
    assert (args.project, args.verbosity) == (None, 0)
