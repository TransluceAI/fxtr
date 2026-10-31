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
