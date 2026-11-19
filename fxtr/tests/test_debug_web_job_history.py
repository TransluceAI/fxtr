"""The job picker's commit view and starred jobs: where each job's launch is shown in a real
temporary git repository (on a branch, after its staged changes were committed, launched with
uncommitted changes, on a deleted branch, missing, or from before the project moved), the graph
left once the commits between are dropped, which branch a commit is shown on, and the starred
jobs' file and routes."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from asgi_testing import PAGE_ORIGIN, a_request
from postgres_testing import a_client

pytest.importorskip("sqlalchemy")
pytest.importorskip("ariadne")

from fxtr.project.config import Project  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.debug_web import starred  # noqa: E402
from fxtr.runner.postgres.debug_web.app import create_app  # noqa: E402
from fxtr.runner.postgres.debug_web.git_history import (  # noqa: E402
    Branch,
    Launch,
    Walk,
    branch_owners,
    read_history,
    shown_parents,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
]


def git(*argv: str, cwd: Path) -> str:
    return subprocess.run(
        ("git", "-c", "user.name=t", "-c", "user.email=t@t", *argv),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, message: str, content: str | None = None) -> str:
    """A commit changing ``file.txt`` (to ``content``, the message by default); its hash."""
    (repo / "file.txt").write_text(content if content is not None else message)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty git repository on ``main``."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    return root


def launch(
    repo: Path, job: str, at: str, *, ephemeral: bool = False, project_path: str = "."
) -> Launch:
    """A job launched at commit ``at`` of ``repo``, by a project at ``project_path`` in it."""
    return Launch(
        job_id=job,
        workflow_name="wf",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        commit=at,
        tree=git("rev-parse", f"{at}^{{tree}}", cwd=repo),
        ephemeral=ephemeral,
        project_path=project_path,
    )


def staged_commit(repo: Path, content: str) -> str:
    """The commit fxtr makes of staged changes: a child of HEAD on no branch, kept by a ref."""
    (repo / "file.txt").write_text(content)
    git("add", "-A", cwd=repo)
    made = git("commit-tree", git("write-tree", cwd=repo), "-p", "HEAD", "-m", "pin", cwd=repo)
    git("update-ref", f"refs/fxtr/pins/{made}", made, cwd=repo)
    return made


async def test_only_commits_with_jobs_are_shown(repo: Path) -> None:
    first = commit(repo, "one")
    commit(repo, "two")
    third = commit(repo, "three")
    commit(repo, "four")
    commit(repo, "five")
    history = await read_history(repo, [launch(repo, "a", first), launch(repo, "b", third)])
    assert history is not None
    assert [c["subject"] for c in history["commits"]] == ["three", "one"]
    assert [c["parents"] for c in history["commits"]] == [[first], []]
    assert {c["branch"] for c in history["commits"]} == {"main"}
    assert history["branches"] == [{"name": "main", "default": True}]
    assert [(j["id"], j["commit"], j["uncommitted"]) for j in history["jobs"]] == [
        ("a", first, False),
        ("b", third, False),
    ]
    assert history["hidden"] == 0


async def test_only_branches_with_a_shown_commit_are_listed(repo: Path) -> None:
    base = commit(repo, "base")
    commit(repo, "more on main")
    git("checkout", "-qb", "feature", base, cwd=repo)
    with_job = commit(repo, "feature work")
    commit(repo, "more feature work")
    git("checkout", "-qb", "idle", base, cwd=repo)
    commit(repo, "idle work")
    git("checkout", "-q", "main", cwd=repo)
    history = await read_history(repo, [launch(repo, "a", base), launch(repo, "b", with_job)])
    assert history is not None
    assert history["branches"] == [
        {"name": "main", "default": True},
        {"name": "feature", "default": False},
    ]
    assert {c["hash"] for c in history["commits"]} == {base, with_job}


async def test_a_staged_launch_is_shown_under_the_checked_out_commit_until_committed(
    repo: Path,
) -> None:
    base = commit(repo, "one")
    pinned = staged_commit(repo, "staged work")
    job = launch(repo, "a", pinned, ephemeral=True)
    history = await read_history(repo, [job])
    assert history is not None
    assert [(j["commit"], j["uncommitted"]) for j in history["jobs"]] == [(base, True)]
    assert [c["hash"] for c in history["commits"]] == [base]

    # Committing exactly what was staged gives a commit with the same files: the job moves there.
    committed = commit(repo, "two", "staged work")
    history = await read_history(repo, [job])
    assert history is not None
    assert [(j["commit"], j["uncommitted"]) for j in history["jobs"]] == [(committed, False)]


async def test_a_project_inside_a_repository_shows_only_the_commits_that_changed_it(
    repo: Path,
) -> None:
    """Jobs are shown under the newest commit that changed the project's directory, with the
    commit they launched at beside; staged changes elsewhere in the repository are not marked."""
    project = repo / "project"
    project.mkdir()
    (project / "experiment.py").write_text("v1")
    changed = commit(repo, "change the project")
    elsewhere = commit(repo, "change something else")
    outside = staged_commit(repo, "staged elsewhere")
    git("reset", "-q", cwd=repo)
    (repo / "file.txt").write_text("change something else")
    (project / "experiment.py").write_text("v2")
    git("add", "-A", cwd=repo)
    made = git("commit-tree", git("write-tree", cwd=repo), "-p", "HEAD", "-m", "pin", cwd=repo)
    git("update-ref", f"refs/fxtr/pins/{made}", made, cwd=repo)
    git("reset", "-q", "--hard", cwd=repo)
    jobs = [
        launch(repo, "at-elsewhere", elsewhere, project_path="project"),
        launch(repo, "at-changed", changed, project_path="project"),
        launch(repo, "staged-outside", outside, ephemeral=True, project_path="project"),
        launch(repo, "staged-inside", made, ephemeral=True, project_path="project"),
    ]
    history = await read_history(project, jobs)
    assert history is not None
    assert [c["hash"] for c in history["commits"]] == [changed]
    assert [(j["id"], j["commit"], j["uncommitted"]) for j in history["jobs"]] == [
        ("at-elsewhere", changed, False),
        ("at-changed", changed, False),
        ("staged-outside", changed, False),
        ("staged-inside", changed, True),
    ]
    assert [j["launchedAt"] for j in history["jobs"]] == [
        {"hash": elsewhere, "subject": "change something else"},
        None,
        None,
        None,
    ]

    # A merge changes the project only where it differs from its first parent: merging in a
    # branch that changed it does; merging in one that forked before the project last changed,
    # and changed only other files, does not (though it differs from that branch).
    git("checkout", "-qb", "side", changed, cwd=repo)
    (repo / "notes.txt").write_text("side notes")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "change other files on a side branch", cwd=repo)
    git("checkout", "-q", "main", cwd=repo)
    (project / "experiment.py").write_text("v3")
    again = commit(repo, "change the project again", "change something else")
    git("merge", "-q", "--no-ff", "-m", "merge side", "side", cwd=repo)
    merged_other = git("rev-parse", "HEAD", cwd=repo)
    git("checkout", "-qb", "side2", changed, cwd=repo)
    (project / "extra.py").write_text("side")
    commit(repo, "change the project on a side branch", "change the project")
    git("checkout", "-q", "main", cwd=repo)
    git("merge", "-q", "--no-ff", "-m", "merge side2", "side2", cwd=repo)
    merged_project = git("rev-parse", "HEAD", cwd=repo)
    history = await read_history(
        project,
        [
            launch(repo, "after-other", merged_other, project_path="project"),
            launch(repo, "after-project", merged_project, project_path="project"),
        ],
    )
    assert history is not None
    assert [j["commit"] for j in history["jobs"]] == [again, merged_project]

    # Where the project is the whole repository, every commit counts.
    whole = await read_history(repo, [dataclasses.replace(jobs[0], project_path=".")])
    assert whole is not None
    assert [(j["commit"], j["launchedAt"]) for j in whole["jobs"]] == [(elsewhere, None)]


async def test_a_job_from_before_the_project_moved_is_placed_where_the_project_was(
    repo: Path,
) -> None:
    """The commits that count for a job are those that changed the directory it recorded, even
    where the project lives elsewhere now."""
    old = repo / "old"
    old.mkdir()
    (old / "experiment.py").write_text("v1")
    changed_old = commit(repo, "change the project where it was")
    elsewhere = commit(repo, "change something else")
    git("mv", "old", "new", cwd=repo)
    moved = commit(repo, "move the project", "change something else")
    after = commit(repo, "change something else again")
    jobs = [
        launch(repo, "before", elsewhere, project_path="old"),
        launch(repo, "after", after, project_path="new"),
    ]
    history = await read_history(repo / "new", jobs)
    assert history is not None
    assert [(j["id"], j["commit"]) for j in history["jobs"]] == [
        ("before", changed_old),
        ("after", moved),
    ]
    assert history["branches"] == [{"name": "main", "default": True}]
    assert [c["hash"] for c in history["commits"]] == [moved, changed_old]


async def test_a_deleted_branchs_commit_stays_where_it_forked(repo: Path) -> None:
    base = commit(repo, "one")
    git("checkout", "-qb", "feature", cwd=repo)
    side = commit(repo, "side")
    git("update-ref", f"refs/fxtr/pins/{side}", side, cwd=repo)
    git("checkout", "-q", "main", cwd=repo)
    commit(repo, "two")
    job = launch(repo, "a", side)
    history = await read_history(repo, [job, launch(repo, "b", base)])
    assert history is not None
    by_hash = {c["hash"]: c for c in history["commits"]}
    assert by_hash[side]["branch"] == "feature"
    assert [b["name"] for b in history["branches"]] == ["main", "feature"]

    git("branch", "-qD", "feature", cwd=repo)
    history = await read_history(repo, [job, launch(repo, "b", base)])
    assert history is not None
    by_hash = {c["hash"]: c for c in history["commits"]}
    assert set(by_hash) == {side, base}
    assert by_hash[side]["branch"] is None and by_hash[side]["parents"] == [base]
    assert [b["name"] for b in history["branches"]] == ["main"]


async def test_a_commit_the_repository_lacks_is_only_counted(repo: Path) -> None:
    base = commit(repo, "one")
    gone = Launch("a", "wf", datetime(2026, 1, 1, tzinfo=UTC), "f" * 40, "e" * 40, False, ".")
    history = await read_history(repo, [gone, launch(repo, "b", base)])
    assert history is not None
    assert [j["id"] for j in history["jobs"]] == ["b"] and history["hidden"] == 1


async def test_no_repository_is_no_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert await read_history(tmp_path, []) is None


def test_a_stacked_branch_does_not_claim_the_branch_below_it() -> None:
    # main: m; feature: m <- f; stacked (newer tip): f <- s.
    walk = Walk(
        ["s", "f", "m"], {"s": ["f"], "f": ["m"], "m": []}, {"s": "ts", "f": "tf", "m": "tm"}
    )
    branches = [Branch("stacked", "s", 30), Branch("feature", "f", 20), Branch("main", "m", 10)]
    assert branch_owners(walk, branches, "main") == {"m": "main", "f": "feature", "s": "stacked"}


def test_a_merge_left_out_does_not_draw_a_second_line_past_its_side_branch() -> None:
    # base <- side <- merge (also a child of base) <- top; the merge itself is not shown.
    walk = Walk(
        ["top", "merge", "side", "base"],
        {"top": ["merge"], "merge": ["base", "side"], "side": ["base"], "base": []},
        {},
    )
    assert shown_parents(walk, {"top", "side", "base"}) == {
        "base": [],
        "side": ["base"],
        "top": ["side"],
    }


# ---- starred jobs ----


def a_project(root: Path) -> Project:
    return Project(
        root=root,
        name="p",
        modules=(),
        stale_after=timedelta(seconds=1),
        database_url="postgresql+asyncpg://unused/db",
        schema="unused",
    )


JOB = "0b0e7c1e-6f3b-4a53-9d0f-0d6d6f0a7c11"


def test_stars_are_kept_named_and_removed(tmp_path: Path) -> None:
    project = a_project(tmp_path)
    assert starred.read_starred(project) == {}
    first = starred.star(project, JOB, "baseline")
    assert first[JOB]["name"] == "baseline"
    renamed = starred.star(project, JOB, "the baseline")
    assert renamed[JOB] == {"name": "the baseline", "starredAt": first[JOB]["starredAt"]}
    assert starred.read_starred(project) == renamed
    assert json.loads((tmp_path / starred.STARRED_FILE).read_text()) == {"jobs": renamed}
    assert starred.unstar(project, JOB) == {} and starred.read_starred(project) == {}


async def test_concurrent_stars_keep_every_job(tmp_path: Path) -> None:
    project = a_project(tmp_path)
    names = [f"job-{index}" for index in range(8)]
    starting = threading.Barrier(len(names))

    def add(name: str) -> None:
        starting.wait(timeout=5)
        starred.star(project, name, name)

    async with anyio.create_task_group() as tasks:
        for name in names:
            tasks.start_soon(anyio.to_thread.run_sync, add, name)
    assert {job: entry["name"] for job, entry in starred.read_starred(project).items()} == {
        name: name for name in names
    }


def test_a_star_names_a_job_and_a_short_name() -> None:
    assert starred.star_request(JOB.upper(), b'{"name": "  kept  "}') == (JOB, "kept")
    assert starred.star_request(JOB, b"") == (JOB, "")
    for job, body in (("nope", b"{}"), (JOB, b"[1]"), (JOB, b'{"name": 3}'), (JOB, b"{")):
        with pytest.raises(ValueError):
            starred.star_request(job, body)
    with pytest.raises(ValueError):
        starred.star_request(JOB, json.dumps({"name": "x" * 201}).encode())


async def test_only_the_viewers_pages_star_jobs(tmp_path: Path) -> None:
    async with a_client(Database("postgresql+asyncpg://unused", pg_schema="unused")) as client:
        client.project = a_project(tmp_path)
        app = create_app(client, frontend_dist=tmp_path)
        json_headers = {"content-type": "application/json", "origin": PAGE_ORIGIN}
        status, _, body = await a_request(
            app, f"/api/starred/{JOB}", method="PUT", body=b'{"name": "good"}', headers=json_headers
        )
        assert status == 200 and json.loads(body)["jobs"][JOB]["name"] == "good"
        status, _, body = await a_request(app, "/api/starred")
        assert status == 200 and list(json.loads(body)["jobs"]) == [JOB]

        elsewhere = {**json_headers, "origin": "http://evil.example"}
        refused = await a_request(app, f"/api/starred/{JOB}", method="DELETE", headers=elsewhere)
        assert refused[0] == 403
        assert (await a_request(app, "/api/starred/nope", method="DELETE"))[0] == 400
        not_json = await a_request(
            app, f"/api/starred/{JOB}", method="PUT", body=b"{}", headers={"origin": PAGE_ORIGIN}
        )
        assert not_json[0] == 415

        status, _, body = await a_request(
            app, f"/api/starred/{JOB}", method="DELETE", headers=json_headers
        )
        assert status == 200 and json.loads(body) == {"jobs": {}}
