"""Tests for the local environment against the real tools: pinning a checkout with git (clean,
unstaged, untracked, staged with and without the flag, the pin ref surviving a rewrite of the
original commit) and with jj (colocated and plain: an empty or non-empty working copy, merges and
conflicts, and git and jj pinning the same content to the same commit), and the venv checks with
uv on a throwaway project."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from fxtr.project.config import Project, ProjectError
from fxtr.project.environment import (
    PIN_REF_PREFIX,
    LocalEnvironment,
    PinRefusedError,
    pin_ref,
)

pytestmark = pytest.mark.anyio

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")

PYPROJECT = '[project]\nname = "p"\n[tool.fxtr]\n'


def sh(*argv: str, cwd: Path) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def git(*argv: str, cwd: Path) -> str:
    return sh("git", "-c", "user.name=t", "-c", "user.email=t@t", *argv, cwd=cwd)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository holding a fxtr project in ``proj/``, with one commit."""
    monkeypatch.setenv("FXTR_DATABASE_URL", "postgresql+asyncpg://x/db")
    root = tmp_path / "repo"
    (root / "proj").mkdir(parents=True)
    (root / "proj" / "pyproject.toml").write_text(PYPROJECT)
    (root / "proj" / "code.py").write_text("x = 1\n")
    git("init", "-q", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-qm", "one", cwd=root)
    return root


def project_in(repo: Path) -> Project:
    return LocalEnvironment().locate_project(repo / "proj")


# ---- pinning ----


@needs_git
async def test_a_clean_checkout_pins_head(repo: Path) -> None:
    pin = await LocalEnvironment().pin_code(project_in(repo), allow_staged=False)
    head = git("rev-parse", "HEAD", cwd=repo)
    assert pin.commit == head
    assert pin.tree == git("rev-parse", "HEAD^{tree}", cwd=repo)
    assert not pin.ephemeral
    assert pin.ref == pin_ref(head) == PIN_REF_PREFIX + head
    assert git("rev-parse", pin.ref, cwd=repo) == head
    assert pin.repo_root == repo.resolve()
    assert pin.project_path == "proj"


@needs_git
async def test_a_project_at_the_repo_root_has_path_dot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FXTR_DATABASE_URL", "postgresql+asyncpg://x/db")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    git("init", "-q", cwd=tmp_path)
    git("add", "-A", cwd=tmp_path)
    git("commit", "-qm", "one", cwd=tmp_path)
    pin = await LocalEnvironment().pin_code(
        LocalEnvironment().locate_project(tmp_path), allow_staged=False
    )
    assert pin.project_path == "."


@needs_git
async def test_unstaged_and_untracked_changes_are_refused_even_with_the_flag(repo: Path) -> None:
    env = LocalEnvironment()
    # Edits change the file's size: git's stat cache can miss a same-size edit made within the
    # same second as the commit, as git status itself would.
    (repo / "proj" / "code.py").write_text("x = 22\n")
    with pytest.raises(PinRefusedError, match="unstaged or untracked.*code.py.*allow_staged"):
        await env.pin_code(project_in(repo), allow_staged=True)
    git("checkout", "--", "proj/code.py", cwd=repo)
    (repo / "proj" / "new.py").write_text("")
    with pytest.raises(PinRefusedError, match="unstaged or untracked.*new.py"):
        await env.pin_code(project_in(repo), allow_staged=True)
    # Staging one change while another stays unstaged is still refused.
    git("add", "proj/new.py", cwd=repo)
    (repo / "proj" / "code.py").write_text("x = 333\n")
    with pytest.raises(PinRefusedError, match="unstaged or untracked.*code.py"):
        await env.pin_code(project_in(repo), allow_staged=True)


@needs_git
async def test_staged_changes_need_the_flag_and_pin_a_commit_of_fxtrs_own(repo: Path) -> None:
    env = LocalEnvironment()
    head = git("rev-parse", "HEAD", cwd=repo)
    (repo / "proj" / "code.py").write_text("x = 22\n")
    git("add", "-A", cwd=repo)
    with pytest.raises(PinRefusedError, match="staged but uncommitted.*allow_staged"):
        await env.pin_code(project_in(repo), allow_staged=False)

    pin = await env.pin_code(project_in(repo), allow_staged=True)
    assert pin.ephemeral
    assert pin.commit != head
    assert pin.tree == git("write-tree", cwd=repo)
    assert git("rev-parse", f"{pin.commit}^", cwd=repo) == head
    assert git("rev-parse", pin.ref, cwd=repo) == pin.commit
    # Nothing of the user's moved: HEAD, the branch, and the index are as they were.
    assert git("rev-parse", "HEAD", cwd=repo) == head
    assert git("diff", "--cached", "--name-only", cwd=repo) == "proj/code.py"
    assert "fxtr" in git("log", "-1", "--format=%an %s", pin.commit, cwd=repo)
    # The same staged content pins the same commit again.
    again = await env.pin_code(project_in(repo), allow_staged=True)
    assert again == pin


@needs_git
async def test_the_pin_survives_rewriting_the_original_commit(repo: Path) -> None:
    pin = await LocalEnvironment().pin_code(project_in(repo), allow_staged=False)
    git("commit", "-q", "--amend", "-m", "one, amended", cwd=repo)
    git("reflog", "expire", "--expire=now", "--all", cwd=repo)
    git("gc", "-q", "--prune=now", cwd=repo)
    assert git("rev-parse", "HEAD", cwd=repo) != pin.commit
    assert git("cat-file", "-t", pin.commit, cwd=repo) == "commit"


# ---- pin refs on a git remote ----


def a_receiving_repo(tmp_path: Path, remote: Path) -> Path:
    """Another git repository holding the same project, with ``remote`` as its ``origin``, and
    none of the pinned commits."""
    root = tmp_path / "other"
    (root / "proj").mkdir(parents=True)
    (root / "proj" / "pyproject.toml").write_text(PYPROJECT)
    git("init", "-q", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-qm", "elsewhere", cwd=root)
    git("remote", "add", "origin", str(remote), cwd=root)
    return root


async def a_pin_travels(tmp_path: Path, here: Project, commit: str, remote: Path) -> None:
    """Push ``commit`` from ``here`` to ``remote`` under fxtr's pin ref, and fetch it into
    another repository that lacks it."""
    env = LocalEnvironment()
    await env.push_commits(here, "origin", [commit])
    assert git("rev-parse", pin_ref(commit), cwd=remote) == commit
    await env.push_commits(here, "origin", [commit])  # pushing again changes nothing
    there = project_in(a_receiving_repo(tmp_path, remote))
    assert await env.missing_commits(there, [commit]) == [commit]
    await env.fetch_commits(there, "origin", [commit])
    assert await env.missing_commits(there, [commit]) == []
    assert git("rev-parse", pin_ref(commit), cwd=there.root) == commit
    with pytest.raises(ProjectError, match="fetch"):
        await env.fetch_commits(there, "origin", ["0" * 40])


@needs_git
@pytest.mark.parametrize("winner_writes", ["the same commit", "another commit"])
async def test_a_push_that_loses_a_race_for_its_pin_ref(
    repo: Path, tmp_path: Path, winner_writes: str
) -> None:
    """Two pushes of one commit race for its pin ref: the remote writes the ref for one, and
    refuses the other ("reference already exists"). The losing push succeeds if the ref holds
    its commit, and fails if the ref holds another commit.

    The remote's pre-receive hook plays the winning push: it writes the ref after the remote
    advertised its refs, and before the remote writes them for this push."""
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=repo)
    env = LocalEnvironment()
    here = project_in(repo)
    commit = (await env.pin_code(here, allow_staged=False)).commit
    other = git("commit-tree", f"{commit}^{{tree}}", "-p", commit, "-m", "other", cwd=repo)
    # The hook writes refs to objects the remote has outside the push's quarantine.
    git("push", "-q", "origin", f"{commit}:refs/heads/main", f"{other}:refs/heads/other", cwd=repo)
    target = '"$new"' if winner_writes == "the same commit" else other
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\nunset GIT_QUARANTINE_PATH\n"
        f'while read old new ref; do git update-ref "$ref" {target}; done\n'
    )
    hook.chmod(0o755)
    if winner_writes == "the same commit":
        await env.push_commits(here, "origin", [commit])
    else:
        with pytest.raises(ProjectError, match="push"):
            await env.push_commits(here, "origin", [commit])
    expected = commit if winner_writes == "the same commit" else other
    assert git("rev-parse", pin_ref(commit), cwd=remote) == expected


@needs_git
async def test_a_failed_pin_push_reports_push_when_the_remote_cannot_be_inspected(
    repo: Path, tmp_path: Path
) -> None:
    git("remote", "add", "origin", str(tmp_path / "unavailable.git"), cwd=repo)
    environment = LocalEnvironment()
    project = project_in(repo)
    commit = (await environment.pin_code(project, allow_staged=False)).commit
    with pytest.raises(ProjectError) as failure:
        await environment.push_commits(project, "origin", [commit])
    assert "git push origin" in str(failure.value)
    assert "git ls-remote" not in str(failure.value)


@needs_git
async def test_an_ephemeral_pin_travels_through_a_git_remote(repo: Path, tmp_path: Path) -> None:
    """A commit made from staged changes exists only under fxtr's pin ref; pushing the commit
    itself takes it to the remote all the same."""
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=repo)
    (repo / "proj" / "code.py").write_text("x = 2\n")
    git("add", "-A", cwd=repo)
    pin = await LocalEnvironment().pin_code(project_in(repo), allow_staged=True)
    assert pin.ephemeral
    await a_pin_travels(tmp_path, project_in(repo), pin.commit, remote)


@needs_git
async def test_a_project_outside_a_repository_cannot_be_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FXTR_DATABASE_URL", "postgresql+asyncpg://x/db")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    with pytest.raises(ProjectError, match="rev-parse --show-toplevel"):
        await LocalEnvironment().pin_code(
            LocalEnvironment().locate_project(tmp_path), allow_staged=False
        )


# ---- the venv ----


@pytest.fixture(scope="module")
def uv_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway uv project with no dependencies, locked and synced (a couple of seconds once
    an interpreter is cached), marked as a fxtr project."""
    root = tmp_path_factory.mktemp("uvp")
    sh("uv", "init", "--bare", "--no-workspace", cwd=root)
    (root / "pyproject.toml").write_text((root / "pyproject.toml").read_text() + "\n[tool.fxtr]\n")
    sh("uv", "lock", cwd=root)
    sh("uv", "sync", cwd=root)
    return root


def in_venv(monkeypatch: pytest.MonkeyPatch, venv: Path) -> None:
    """Pretend this process runs in ``venv``.

    Also hides the environment running the tests from ``uv python find``: drops ``VIRTUAL_ENV``,
    which ``uv run`` sets and uv honors over the project's own ``.venv``, and takes that
    environment's scripts directory off ``PATH``, where uv would find its interpreter for a project
    without a ``.venv`` when no interpreter it prefers (a managed one) matches.
    """
    scripts = Path(sysconfig.get_path("scripts")).resolve()
    path = [p for p in os.environ.get("PATH", "").split(os.pathsep) if Path(p).resolve() != scripts]
    monkeypatch.setenv("PATH", os.pathsep.join(path))
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


@needs_uv
async def test_a_synced_project_run_from_its_venv_has_no_problems(
    uv_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FXTR_DATABASE_URL", "postgresql+asyncpg://x/db")
    in_venv(monkeypatch, uv_project / ".venv")
    env = LocalEnvironment()
    assert await env.check_synced(env.locate_project(uv_project)) == []


@needs_uv
async def test_refreshing_the_environment_answers_an_edit_to_the_projects_settings(
    tmp_path: Path,
) -> None:
    """uv reinstalls a project that is a package when its ``pyproject.toml`` changes, so without
    the refresh, a process that does not start with ``uv run`` is refused its next launch."""
    root = tmp_path / "package"
    (root / "src" / "package").mkdir(parents=True)
    (root / "src" / "package" / "__init__.py").touch()
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "package"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        "[tool.fxtr]\n"
    )
    sh("uv", "sync", cwd=root)
    lock = (root / "uv.lock").read_text()
    pyproject.write_text(pyproject.read_text() + 'default_remote = "hub"\n')
    is_current = ("uv", "sync", "--check")
    assert subprocess.run(is_current, cwd=root, capture_output=True, check=False).returncode == 1

    await LocalEnvironment().refresh_environment(root)

    assert subprocess.run(is_current, cwd=root, capture_output=True, check=False).returncode == 0
    assert (root / "uv.lock").read_text() == lock


@needs_uv
async def test_refreshing_a_project_with_no_lockfile_changes_nothing(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    with pytest.raises(ProjectError, match="uv.lock"):
        await LocalEnvironment().refresh_environment(tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == ["pyproject.toml"]


@needs_uv
async def test_running_outside_the_projects_venv_is_a_problem(
    uv_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FXTR_DATABASE_URL", "postgresql+asyncpg://x/db")
    in_venv(monkeypatch, uv_project.parent)  # anywhere but the project's venv
    env = LocalEnvironment()
    (problem,) = await env.check_synced(env.locate_project(uv_project))
    assert "project's virtual environment" in problem.what and "fxtr expects" in problem.what
    assert "uv run" in problem.fix


@needs_uv
async def test_a_stale_lock_and_a_missing_venv_are_reported_with_their_fixes(
    uv_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FXTR_DATABASE_URL", "postgresql+asyncpg://x/db")
    root = tmp_path / "copy"
    shutil.copytree(uv_project, root, ignore=shutil.ignore_patterns(".venv"))
    in_venv(monkeypatch, root / ".venv")
    env = LocalEnvironment()
    project = env.locate_project(root)
    # With nothing to install, uv's sync check passes even without a venv: the third check is
    # what catches the missing environment.
    (problem,) = await env.check_synced(project)
    assert "no virtual environment" in problem.what
    assert problem.fix == f"run `uv sync` in {root.resolve()}"

    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace("dependencies = []", 'dependencies = ["six"]')
    )
    problems = await env.check_synced(project)
    assert [p.fix for p in problems] == [
        f"run `uv lock` in {root.resolve()}",
        f"run `uv sync` in {root.resolve()}",
        f"run `uv sync` in {root.resolve()}",
    ]
    assert problems[0].what.startswith("uv.lock is out of date")
    assert "not match uv.lock" in problems[1].what


@needs_uv
async def test_a_project_uv_does_not_manage_is_an_error_not_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FXTR_DATABASE_URL", "postgresql+asyncpg://x/db")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)  # no uv.lock, nothing uv recognizes
    env = LocalEnvironment()
    with pytest.raises(ProjectError, match="managed by uv"):
        await env.check_synced(env.locate_project(tmp_path))


# ---- pinning, jj ----

needs_jj = pytest.mark.skipif(shutil.which("jj") is None, reason="jj is not installed")


def jj(*argv: str, cwd: Path) -> str:
    return sh("jj", *argv, cwd=cwd)


def jj_commit(revset: str, cwd: Path) -> str:
    return jj("log", "-r", revset, "--no-graph", "-T", "commit_id", cwd=cwd)


@pytest.fixture(params=["colocated", "plain"])
def jj_repo(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A jj repository (colocated with git, or not) holding a fxtr project in ``proj/``, with one
    described change and an empty working-copy change on top of it."""
    monkeypatch.setenv("FXTR_DATABASE_URL", "postgresql+asyncpg://x/db")
    root = tmp_path / "repo"
    (root / "proj").mkdir(parents=True)
    jj("git", "init", *(["--colocate"] if request.param == "colocated" else []), cwd=root)
    (root / "proj" / "pyproject.toml").write_text(PYPROJECT)
    (root / "proj" / "code.py").write_text("x = 1\n")
    jj("describe", "-m", "one", cwd=root)
    jj("new", cwd=root)
    return root


def git_in_store(repo: Path, *argv: str) -> str:
    """Git against jj's store, wherever ``git_target`` says it is."""
    store = repo / ".jj" / "repo" / "store"
    git_dir = (store / (store / "git_target").read_text().strip()).resolve()
    return sh("git", f"--git-dir={git_dir}", *argv, cwd=repo)


@needs_jj
async def test_an_empty_working_copy_pins_its_parent(jj_repo: Path) -> None:
    parent = jj_commit("@-", cwd=jj_repo)
    pin = await LocalEnvironment().pin_code(project_in(jj_repo), allow_staged=False)
    assert pin.commit == parent
    assert pin.tree == git_in_store(jj_repo, "rev-parse", f"{parent}^{{tree}}")
    assert not pin.ephemeral
    assert git_in_store(jj_repo, "rev-parse", pin.ref) == parent
    assert pin.repo_root == jj_repo.resolve() and pin.project_path == "proj"


@needs_jj
async def test_a_non_empty_working_copy_needs_the_flag_and_pins_its_tree(jj_repo: Path) -> None:
    env = LocalEnvironment()
    parent = jj_commit("@-", cwd=jj_repo)
    (jj_repo / "proj" / "code.py").write_text("x = 22\n")
    with pytest.raises(
        PinRefusedError, match=r"working-copy change \(@\).*holds changes.*allow_staged"
    ):
        await env.pin_code(project_in(jj_repo), allow_staged=False)

    pin = await env.pin_code(project_in(jj_repo), allow_staged=True)
    at = jj_commit("@", cwd=jj_repo)  # snapshotted by the pin's own jj log
    assert pin.ephemeral
    assert pin.commit not in (at, parent)
    assert pin.tree == git_in_store(jj_repo, "rev-parse", f"{at}^{{tree}}")
    assert git_in_store(jj_repo, "rev-parse", f"{pin.commit}^") == parent
    assert git_in_store(jj_repo, "rev-parse", pin.ref) == pin.commit
    # jj's graph is untouched: @ is the same change, and jj does not know the pin commit.
    assert jj_commit("@", cwd=jj_repo) == at
    assert (
        jj("log", "-r", "all()", "--no-graph", "-T", 'commit_id ++ "\n"', cwd=jj_repo).count(
            pin.commit
        )
        == 0
    )
    again = await env.pin_code(project_in(jj_repo), allow_staged=True)
    assert (again.commit, again.tree, again.ephemeral) == (pin.commit, pin.tree, pin.ephemeral)


@needs_jj
async def test_the_same_staged_content_pins_the_same_commit_from_git_and_jj(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin commit depends on its tree and parent only, not on which tool described the
    checkout: a git repo and a jj repo with the same history and the same staged content agree."""
    monkeypatch.setenv("FXTR_DATABASE_URL", "postgresql+asyncpg://x/db")
    env = LocalEnvironment()
    # The two histories must share the first commit: make it once in git with a fixed identity
    # and date, then check the same tree out under jj as a plain change.
    identity = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    for name in ("g", "j"):
        root = tmp_path / name
        (root / "proj").mkdir(parents=True)
        (root / "proj" / "pyproject.toml").write_text(PYPROJECT)
        (root / "proj" / "code.py").write_text("x = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "one"],
            cwd=root,
            check=True,
            env={**dict(os.environ), **identity},
        )
        (root / "proj" / "code.py").write_text("x = 22\n")
    assert git("rev-parse", "HEAD", cwd=tmp_path / "g") == git(
        "rev-parse", "HEAD", cwd=tmp_path / "j"
    )
    git("add", "-A", cwd=tmp_path / "g")
    jj(
        "git", "init", "--colocate", cwd=tmp_path / "j"
    )  # imports HEAD; the edit becomes @'s content
    from_git = await env.pin_code(env.locate_project(tmp_path / "g" / "proj"), allow_staged=True)
    from_jj = await env.pin_code(env.locate_project(tmp_path / "j" / "proj"), allow_staged=True)
    assert (from_git.commit, from_git.tree) == (from_jj.commit, from_jj.tree)
    assert from_git.ephemeral and from_jj.ephemeral


@needs_jj
async def test_a_merge_or_conflicted_working_copy_is_refused(jj_repo: Path) -> None:
    env = LocalEnvironment()
    base = jj_commit("@-", cwd=jj_repo)
    # Two changes off the base touching different files: their merge is clean but has two
    # parents.
    (jj_repo / "proj" / "code.py").write_text("x = 2\n")
    jj("describe", "-m", "left", cwd=jj_repo)
    left = jj_commit("@", cwd=jj_repo)
    jj("new", base, cwd=jj_repo)
    (jj_repo / "other.txt").write_text("o\n")
    jj("describe", "-m", "right", cwd=jj_repo)
    right = jj_commit("@", cwd=jj_repo)
    jj("new", left, right, cwd=jj_repo)
    with pytest.raises(PinRefusedError, match="2 parents"):
        await env.pin_code(project_in(jj_repo), allow_staged=True)
    # A third change edits the same line as "left"; a child of its merge with "left" has one
    # parent but inherits the conflict.
    jj("new", base, cwd=jj_repo)
    (jj_repo / "proj" / "code.py").write_text("x = 3\n")
    jj("describe", "-m", "clash", cwd=jj_repo)
    clash = jj_commit("@", cwd=jj_repo)
    jj("new", left, clash, cwd=jj_repo)
    jj("new", cwd=jj_repo)
    with pytest.raises(PinRefusedError, match="unresolved conflicts"):
        await env.pin_code(project_in(jj_repo), allow_staged=True)


@needs_jj
async def test_a_pin_travels_from_a_jj_repository(jj_repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    jj("git", "remote", "add", "origin", str(remote), cwd=jj_repo)
    pin = await LocalEnvironment().pin_code(project_in(jj_repo), allow_staged=False)
    await a_pin_travels(tmp_path, project_in(jj_repo), pin.commit, remote)


@needs_git
async def test_repository_binding_uses_the_project_directory_in_git_worktrees(
    repo: Path, tmp_path: Path
) -> None:
    git("remote", "add", "origin", "git@github.com:acme/research.git", cwd=repo)
    environment = LocalEnvironment()
    assert await environment.repository_binding(project_in(repo).root, "origin") == (
        "git@github.com:acme/research.git",
        "proj",
    )
    checkout = tmp_path / "linked"
    git("worktree", "add", "-b", "other", str(checkout), cwd=repo)
    assert await environment.repository_binding(project_in(checkout).root, "origin") == (
        "git@github.com:acme/research.git",
        "proj",
    )


async def test_a_git_remote_that_git_would_read_as_an_option_is_refused(repo: Path) -> None:
    """A project's committed configuration names the git remote, and ``--upload-pack=...`` would
    run a command on whoever pulls in that checkout."""
    environment = LocalEnvironment()
    project = project_in(repo)
    remote = "--upload-pack=touch ran"
    with pytest.raises(ProjectError, match="cannot be a git remote"):
        await environment.fetch_commits(project, remote, ["a" * 40])
    with pytest.raises(ProjectError, match="cannot be a git remote"):
        await environment.push_commits(project, remote, ["a" * 40])
    with pytest.raises(ProjectError, match="cannot be a git remote"):
        await environment.repository_binding(repo, remote)
