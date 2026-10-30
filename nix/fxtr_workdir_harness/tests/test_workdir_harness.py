"""Mechanics tests for `prepare_workdir_repo`.

These exercise copy + marker substitution + git mechanics with
``relock=False``, so they need neither nix/uv nor a builder. The
end-to-end git-ref build (real `uv lock` / `nix flake lock`) is covered by
``test_sandbox_import.py``. Plan:
``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage3_bundle_and_spawner.md``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fxtr_workdir_harness import (
    LibRef,
    PreparedWorkdir,
    prepare_workdir_repo,
    resolve_lib_ref,
)

_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "minimal"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_lib_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit, standing in for the fxtr repo."""
    repo = tmp_path / "lib"
    repo.mkdir()
    (repo / "flake.nix").write_text("{ outputs = _: {}; }\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-m", "init")
    return repo


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def prepared(tmp_path: Path) -> PreparedWorkdir:
    lib_repo = _make_lib_repo(tmp_path)
    return await prepare_workdir_repo(
        _TEMPLATE,
        tmp_path / "workdir",
        lib_repo=lib_repo,
        lib_rev="deadbeefcafe",
        relock=False,
    )


@pytest.mark.anyio
async def test_flake_input_substituted(prepared: PreparedWorkdir) -> None:
    flake = (prepared.path / "flake.nix").read_text()
    assert "@FXTR_LIB_FLAKE@" not in flake
    assert prepared.lib_ref.flake_url in flake
    # Marker comment is left in place (harmless, aids re-inspection).
    assert "fxtr-lib-sub: flake-input" in flake


@pytest.mark.anyio
async def test_uv_source_comment_flip(prepared: PreparedWorkdir) -> None:
    pyproject = (prepared.path / "packages" / "foo" / "pyproject.toml").read_text()
    lines = pyproject.splitlines()

    template_line = next(ln for ln in lines if "active-in-template" in ln)
    workdir_line = next(ln for ln in lines if "active-in-workdir" in ln)

    # The relative-path (template) form is now commented out...
    assert template_line.lstrip().startswith("#")
    assert 'path = "../../../v2/portable"' in template_line
    # ...and the git-source (workdir) form is now active + substituted.
    assert not workdir_line.lstrip().startswith("#")
    assert "@FXTR_LIB_URL@" not in workdir_line
    assert "@FXTR_LIB_REV@" not in workdir_line
    assert 'git = "file://' in workdir_line
    assert 'rev = "deadbeefcafe"' in workdir_line
    # The subdirectory (and other non-ref bits) survive untouched.
    assert 'subdirectory = "v2/portable"' in workdir_line


@pytest.mark.anyio
async def test_head_committed_and_on_branch(prepared: PreparedWorkdir) -> None:
    assert prepared.head_commit == _git(prepared.path, "rev-parse", "HEAD")
    # Default on_branch=True → `main` exists and points at HEAD.
    assert _git(prepared.path, "rev-parse", "main") == prepared.head_commit
    # The substituted files were committed (clean tree).
    assert _git(prepared.path, "status", "--porcelain") == ""


@pytest.mark.anyio
async def test_off_branch_commit(tmp_path: Path) -> None:
    lib_repo = _make_lib_repo(tmp_path)
    prepared = await prepare_workdir_repo(
        _TEMPLATE,
        tmp_path / "workdir",
        lib_repo=lib_repo,
        lib_rev="cafef00d",
        on_branch=False,
        relock=False,
    )
    # No branch points at the commit, but it's still a reachable object.
    assert _git(prepared.path, "for-each-ref", "--format=%(refname)", "refs/heads/") == ""
    assert _git(prepared.path, "cat-file", "-t", prepared.head_commit) == "commit"


@pytest.mark.anyio
async def test_dest_must_not_exist(tmp_path: Path) -> None:
    lib_repo = _make_lib_repo(tmp_path)
    dest = tmp_path / "workdir"
    dest.mkdir()
    with pytest.raises(FileExistsError):
        await prepare_workdir_repo(_TEMPLATE, dest, lib_repo=lib_repo, lib_rev="x", relock=False)


@pytest.mark.anyio
async def test_copy_excludes_junk(tmp_path: Path) -> None:
    lib_repo = _make_lib_repo(tmp_path)
    # Seed junk in a copied-from template clone so we can assert exclusion.
    dirty_template = tmp_path / "dirty_template"
    import shutil

    shutil.copytree(_TEMPLATE, dirty_template)
    (dirty_template / ".venv").mkdir()
    (dirty_template / ".venv" / "x").write_text("junk")
    (dirty_template / "__pycache__").mkdir()

    prepared = await prepare_workdir_repo(
        dirty_template,
        tmp_path / "workdir",
        lib_repo=lib_repo,
        lib_rev="x",
        relock=False,
    )
    assert not (prepared.path / ".venv").exists()
    assert not (prepared.path / "__pycache__").exists()


@pytest.mark.anyio
async def test_uncommitted_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lib_repo = _make_lib_repo(tmp_path)
    (lib_repo / "flake.nix").write_text("{ outputs = _: { changed = true; }; }\n")  # dirty
    # rev resolved from HEAD (no explicit rev) + dirty repo → warning.
    await prepare_workdir_repo(
        _TEMPLATE,
        tmp_path / "workdir",
        lib_repo=lib_repo,
        relock=False,
    )
    assert "uncommitted changes" in capsys.readouterr().err


@pytest.mark.anyio
async def test_no_warning_with_explicit_rev(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lib_repo = _make_lib_repo(tmp_path)
    (lib_repo / "flake.nix").write_text("{ outputs = _: { changed = true; }; }\n")  # dirty
    # Explicit rev means the caller knows what they want → no warning.
    await prepare_workdir_repo(
        _TEMPLATE,
        tmp_path / "workdir",
        lib_repo=lib_repo,
        lib_rev="explicit",
        relock=False,
    )
    assert "uncommitted changes" not in capsys.readouterr().err


def test_resolve_lib_ref_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib_repo = _make_lib_repo(tmp_path)
    monkeypatch.setenv("FXTR_TEST_LIB_REPO", str(lib_repo))
    monkeypatch.setenv("FXTR_TEST_LIB_REV", "envrev")
    # validate=False: this is a mechanics test with a placeholder rev.
    ref = resolve_lib_ref(validate=False)
    assert ref == LibRef(repo_path=lib_repo.resolve(), rev="envrev")
    assert ref.flake_url == f"git+file://{lib_repo.resolve()}?rev=envrev"
    assert ref.uv_git_url == f"file://{lib_repo.resolve()}"


def test_resolve_lib_ref_validates_rev_exists(tmp_path: Path) -> None:
    lib_repo = _make_lib_repo(tmp_path)
    with pytest.raises(RuntimeError, match="not a commit"):
        resolve_lib_ref(lib_repo=lib_repo, lib_rev="deadbeef" * 5)


def test_resolve_lib_ref_validates_rev_reachable(tmp_path: Path) -> None:
    lib_repo = _make_lib_repo(tmp_path)
    head = _git(lib_repo, "rev-parse", "HEAD")
    # A reachable rev passes.
    assert resolve_lib_ref(lib_repo=lib_repo, lib_rev=head).rev == head
    # An object-present but ref-unreachable commit (the jj-workspace
    # failure mode) is rejected with the bookmark hint. `commit-tree`
    # mints a commit no ref points at.
    tree = _git(lib_repo, "rev-parse", "HEAD^{tree}")
    orphan = subprocess.run(
        ["git", "-C", str(lib_repo), "commit-tree", tree, "-m", "orphan"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    with pytest.raises(RuntimeError, match="jj bookmark create"):
        resolve_lib_ref(lib_repo=lib_repo, lib_rev=orphan)


# ---------------------------------------------------------------------------
# jj-workspace mode (non-colocated workspace → mirror)
# ---------------------------------------------------------------------------


def _jj(cwd: Path, *args: str) -> str:
    import os

    env = {**os.environ, "JJ_USER": "t", "JJ_EMAIL": "t@e"}
    return subprocess.run(
        ["jj", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


def _make_jj_main_and_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """A colocated jj main repo plus a non-colocated secondary workspace —
    the layout where plain-git resolution fails (the workspace has ``.jj``
    but no ``.git``) and the harness must go through the backing store."""
    main = tmp_path / "main"
    main.mkdir()
    _jj(main, "git", "init", "--colocate")
    (main / "flake.nix").write_text("{ outputs = _: {}; }\n")
    (main / "lib.txt").write_text("v-main\n")
    _jj(main, "commit", "-m", "init")
    _jj(main, "workspace", "add", "../ws")
    ws = tmp_path / "ws"
    assert (ws / ".jj").exists() and not (ws / ".git").exists()
    assert (ws / "flake.nix").is_file()
    return main, ws


@pytest.fixture
def jj_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """``(workspace, mirror)`` with the mirror env pointed into tmp."""
    _main, ws = _make_jj_main_and_workspace(tmp_path)
    mirror = tmp_path / "mirror.git"
    monkeypatch.setenv("FXTR_TEST_LIB_MIRROR", str(mirror))
    monkeypatch.delenv("FXTR_TEST_LIB_REPO", raising=False)
    monkeypatch.delenv("FXTR_TEST_LIB_REV", raising=False)
    return ws, mirror


def test_jj_workspace_resolves_working_copy_via_mirror(
    jj_workspace: tuple[Path, Path],
) -> None:
    ws, mirror = jj_workspace
    # Uncommitted working-tree edit: must be captured (jj snapshots @).
    (ws / "lib.txt").write_text("edited in ws\n")
    ref = resolve_lib_ref(lib_repo=ws)  # validate=True: full push + checks
    assert ref.repo_path == mirror
    # The rev is the workspace's @ commit, branch-reachable in the mirror,
    # and its tree contains the uncommitted edit.
    assert _git(mirror, "cat-file", "-t", ref.rev) == "commit"
    assert _git(mirror, "show", f"{ref.rev}:lib.txt") == "edited in ws"
    # Idempotent: no new edits → same rev, no re-push trouble.
    assert resolve_lib_ref(lib_repo=ws) == ref


def test_jj_workspace_edits_move_the_rev(jj_workspace: tuple[Path, Path]) -> None:
    ws, mirror = jj_workspace
    ref1 = resolve_lib_ref(lib_repo=ws)
    (ws / "lib.txt").write_text("v2\n")
    ref2 = resolve_lib_ref(lib_repo=ws)
    assert ref1.rev != ref2.rev
    # Both revs stay reachable in the accumulating mirror.
    for ref in (ref1, ref2):
        assert _git(mirror, "cat-file", "-t", ref.rev) == "commit"


def test_jj_workspace_rev_override(jj_workspace: tuple[Path, Path]) -> None:
    ws, _mirror = jj_workspace
    ref1 = resolve_lib_ref(lib_repo=ws)
    (ws / "lib.txt").write_text("v2\n")
    # Pin the earlier @ commit explicitly; it's object-present in the
    # backing store, so the mirror push transports it the same way.
    ref2 = resolve_lib_ref(lib_repo=ws, lib_rev=ref1.rev)
    assert ref2 == ref1
    # A rev that isn't in the backing store fails the push, loudly.
    with pytest.raises(RuntimeError, match="failed to push"):
        resolve_lib_ref(lib_repo=ws, lib_rev="deadbeef" * 5)


def test_jj_wins_over_decoy_git_dir(jj_workspace: tuple[Path, Path]) -> None:
    """A decoy `.git` (kept around so gitignore-respecting tools behave in
    non-colocated workspaces) must not fool the dispatch into git mode."""
    ws, mirror = jj_workspace
    (ws / ".git").mkdir()
    (ws / ".git" / "README.md").write_text("decoy for fd & friends\n")
    ref = resolve_lib_ref(lib_repo=ws)
    assert ref.repo_path == mirror


def test_jj_wins_in_colocated_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A colocated checkout (real `.git` + `.jj`) also takes the jj path:
    the rev is the working-copy commit (edits included), not git HEAD."""
    main, _ws = _make_jj_main_and_workspace(tmp_path)
    mirror = tmp_path / "mirror.git"
    monkeypatch.setenv("FXTR_TEST_LIB_MIRROR", str(mirror))
    monkeypatch.delenv("FXTR_TEST_LIB_REPO", raising=False)
    monkeypatch.delenv("FXTR_TEST_LIB_REV", raising=False)
    (main / "lib.txt").write_text("uncommitted edit in main\n")
    ref = resolve_lib_ref(lib_repo=main)
    assert ref.repo_path == mirror
    assert ref.rev != _git(main, "rev-parse", "HEAD")
    assert _git(mirror, "show", f"{ref.rev}:lib.txt") == "uncommitted edit in main"


def test_flocked_reentry_raises_instead_of_deadlocking(tmp_path: Path) -> None:
    from fxtr_workdir_harness import _flocked

    lock = tmp_path / "x.lock"
    with _flocked(lock):
        # Distinct paths nest fine.
        with _flocked(tmp_path / "y.lock"):
            pass
        # Same path on the same thread: loud error, not a silent deadlock.
        with pytest.raises(RuntimeError, match="re-entered lock"):
            with _flocked(lock):
                pass
    # Released on exit → re-acquirable.
    with _flocked(lock):
        pass


@pytest.mark.anyio
async def test_prepare_workdir_from_jj_workspace(jj_workspace: tuple[Path, Path]) -> None:
    ws, mirror = jj_workspace
    prepared = await prepare_workdir_repo(
        _TEMPLATE,
        ws.parent / "workdir",
        lib_repo=ws,
        relock=False,
    )
    assert prepared.lib_ref.repo_path == mirror
    flake = (prepared.path / "flake.nix").read_text()
    assert f"git+file://{mirror}?rev={prepared.lib_ref.rev}" in flake


# --- repin_workdir mechanics (relock=False, like the scaffold tests) ---


@pytest.mark.anyio
async def test_repin_flake_only(prepared: PreparedWorkdir) -> None:
    """Default repin rewrites the flake input, leaves uv sources alone,
    and commits; a same-rev repin on a clean tree is a no-op."""
    from fxtr_workdir_harness import repin_workdir

    lib_repo = prepared.lib_ref.repo_path
    out = await repin_workdir(
        prepared.path, lib_repo=lib_repo, lib_rev="feedfacef00d", relock=False
    )
    assert out.changed
    assert out.head_commit != prepared.head_commit
    flake = (prepared.path / "flake.nix").read_text()
    assert f"git+file://{lib_repo}?rev=feedfacef00d" in flake
    assert "deadbeefcafe" not in flake
    # The marker survives the rewrite (repin stays repeatable).
    assert "# fxtr-lib-sub: flake-input" in flake
    # uv sources untouched by default.
    pyproject = (prepared.path / "packages" / "foo" / "pyproject.toml").read_text()
    assert 'rev = "deadbeefcafe"' in pyproject
    assert _git(prepared.path, "log", "-1", "--format=%s") == "repin fxtr lib to feedfacef00d"

    # Idempotence: same rev, clean tree → no commit.
    again = await repin_workdir(
        prepared.path, lib_repo=lib_repo, lib_rev="feedfacef00d", relock=False
    )
    assert not again.changed
    assert again.head_commit == out.head_commit


@pytest.mark.anyio
async def test_repin_uv_sources(prepared: PreparedWorkdir) -> None:
    """--uv also re-pins the active-in-workdir git sources (and leaves
    the commented template line commented)."""
    from fxtr_workdir_harness import repin_workdir

    lib_repo = prepared.lib_ref.repo_path
    out = await repin_workdir(
        prepared.path, lib_repo=lib_repo, lib_rev="feedfacef00d", uv=True, relock=False
    )
    assert out.changed
    pyproject = (prepared.path / "packages" / "foo" / "pyproject.toml").read_text()
    assert 'rev = "feedfacef00d"' in pyproject
    assert "deadbeefcafe" not in pyproject
    # The template-form line stays commented out.
    for line in pyproject.splitlines():
        if "fxtr-lib-sub: active-in-template" in line:
            assert line.lstrip().startswith("#")


@pytest.mark.anyio
async def test_repin_sweeps_pending_edits(prepared: PreparedWorkdir) -> None:
    """Pending workdir-local edits ride the repin commit (the documented
    'publish the workdir's current state' gesture) — including when the
    pin itself is unchanged."""
    from fxtr_workdir_harness import repin_workdir

    (prepared.path / "renderers_note.txt").write_text("local renderer edit\n")
    out = await repin_workdir(
        prepared.path,
        lib_repo=prepared.lib_ref.repo_path,
        lib_rev=prepared.lib_ref.rev,
        relock=False,
    )
    assert out.changed  # the edit was committed even though the pin matched
    assert _git(prepared.path, "status", "--porcelain") == ""
    committed = _git(prepared.path, "show", "--name-only", "--format=", "HEAD")
    assert "renderers_note.txt" in committed


@pytest.mark.anyio
async def test_repin_refuses_non_workdir(tmp_path: Path) -> None:
    from fxtr_workdir_harness import repin_workdir

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError, match="not a git repository"):
        await repin_workdir(plain, lib_rev="x", relock=False)
    _git(plain, "init", "-b", "main")
    with pytest.raises(RuntimeError, match="no marked flake.nix"):
        await repin_workdir(plain, lib_repo=plain, lib_rev="x", relock=False)
