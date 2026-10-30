"""Read-only git CLI wrappers used by the MCP's nix-entity path.

Two callers:

- :func:`head_commit` + the clean-tree guard (:func:`is_git_repo` /
  :func:`is_dirty`) back the sandbox ``current_commit()`` builtin, which
  the agent uses to construct ``NixWasmServiceDefn`` / ``set_preset`` args
  correct-by-default. nix git flakes only see *tracked* files, so pinning
  a commit while the tree is dirty (uncommitted changes OR untracked
  files) would silently ship a stale flake — hence the guard rejects any
  dirty tree.
- :func:`untracked_count` backs the untracked-files notice
  :func:`retrieve_nix_entity` appends: under git-form resolution new
  untracked files are invisible to nix until ``git add``.

All shell-outs are read-only (``rev-parse`` / ``status`` / ``ls-files``)
and use :func:`anyio.run_process` so the MCP supervisor's main task is
never blocked. None of these commit or mutate the working tree.

Failures map to typed exceptions so the caller can surface the right
agent-facing error (see
`projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage4_retrieve_nix_entity.md`).
"""

from __future__ import annotations

from pathlib import Path

import anyio


class GitError(Exception):
    """Base class for git CLI failures."""


class NotAGitRepo(GitError):
    """The MCP's working dir is not inside a git work tree.

    Raised by ``current_commit()`` (a commit can only be pinned from a
    repo). The data path (``retrieve_nix_entity``) does *not* require a
    repo — it works via the ``path:`` fallback — so it never raises this.
    """

    def __init__(self, working_dir: Path) -> None:
        self.working_dir = working_dir
        super().__init__(
            f"Working dir {working_dir} is not a git repository, so there is "
            "no commit to pin. Initialize a repo and commit your flake before "
            "spawning a service / setting a preset."
        )


class WorkingTreeDirty(GitError):
    """``current_commit()`` was called with a dirty working tree.

    Dirty means any uncommitted change, **including untracked files** — nix
    git flakes only see tracked files at the pinned commit, so pinning HEAD
    now would build something other than what's on disk.
    """

    def __init__(self, working_dir: Path) -> None:
        self.working_dir = working_dir
        super().__init__(
            f"Working tree at {working_dir} has uncommitted changes (including "
            "untracked files). nix builds git flakes from the committed tree, "
            "so commit (and `git add` new files) before pinning a commit."
        )


async def _run_git(working_dir: Path, args: list[str]) -> tuple[int, str]:
    """Run a git subcommand in ``working_dir``; return ``(returncode, stdout)``.

    stdout is decoded; stderr is discarded (these are read-only probes and
    the typed exceptions carry their own messages)."""
    result = await anyio.run_process(
        ["git", "-C", str(working_dir), *args],
        check=False,
    )
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


async def is_git_repo(working_dir: Path) -> bool:
    """True iff ``working_dir`` is inside a git work tree."""
    rc, out = await _run_git(working_dir, ["rev-parse", "--is-inside-work-tree"])
    return rc == 0 and out.strip() == "true"


async def is_dirty(working_dir: Path) -> bool:
    """True iff the working tree has any uncommitted change, including
    untracked files (``git status --porcelain`` non-empty)."""
    rc, out = await _run_git(working_dir, ["status", "--porcelain"])
    if rc != 0:
        # Defensive: a repo we already confirmed shouldn't fail status.
        raise GitError(f"`git status` failed in {working_dir} (rc={rc})")
    return bool(out.strip())


async def untracked_count(working_dir: Path) -> int:
    """Count of untracked, non-gitignored files
    (``git ls-files --others --exclude-standard``)."""
    rc, out = await _run_git(working_dir, ["ls-files", "--others", "--exclude-standard"])
    if rc != 0:
        raise GitError(f"`git ls-files` failed in {working_dir} (rc={rc})")
    return len([line for line in out.splitlines() if line.strip()])


async def head_commit(working_dir: Path) -> str:
    """Return HEAD's full commit sha, erroring if not a repo or dirty.

    Raises:
        NotAGitRepo: ``working_dir`` isn't a git work tree.
        WorkingTreeDirty: the tree has uncommitted changes / untracked files.
        GitError: ``git rev-parse HEAD`` otherwise failed (e.g. no commits).
    """
    if not await is_git_repo(working_dir):
        raise NotAGitRepo(working_dir)
    if await is_dirty(working_dir):
        raise WorkingTreeDirty(working_dir)
    rc, out = await _run_git(working_dir, ["rev-parse", "HEAD"])
    if rc != 0:
        raise GitError(
            f"`git rev-parse HEAD` failed in {working_dir} (rc={rc}) — "
            "is there at least one commit?"
        )
    return out.strip()
