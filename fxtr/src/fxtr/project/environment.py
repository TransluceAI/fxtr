"""The environment: fxtr's view of the checkout it is running in.

A launch has to answer three questions about its surroundings before a job exists: *which project
is this*, *which code is checked out*, and *does the interpreter running me match what the project
declares*. :class:`Environment` is the one object that answers them. Only clients use it (the CLI
through them); the runner, the execution policy, and the ledger never see it, and know about code
only through the :class:`~fxtr.project.code_version.CodeVersion` they are handed to record on the
job attempt a process takes.

An import asks one more: *does this repository have the code a record's results were produced at*
(:meth:`Environment.missing_commits`), so that every imported result names code that can be found
here.

A local client asks all three. A remote client (later) would ask the first two through the same
object and then send the pin to a service, which builds its own environment from the pinned commit
and runs the job there; that executor has no ``Environment`` of its own to ask.

**Pinning the code.** fxtr must be able to say exactly what code ran, so a launch pins a commit and
keeps it alive under a ref of fxtr's own, ``refs/fxtr/pins/<commit>``, which survives the user
rebasing or abandoning the original. There is one ref per pinned commit, however many jobs ran at
it: the commit is what a job attempt's :class:`CodeVersion` names, so the ref is found from the
job through it. The rules are the same for git and jj, phrased in each one's terms:

* A clean checkout pins what is checked out: git's ``HEAD``, or jj's ``@-`` when ``@`` is empty
  with one parent and no conflict.
* Unstaged or untracked changes are refused, always, with an error naming both fixes.
* Staged changes (jj: a non-empty ``@``) are refused unless ``allow_staged``, in which case fxtr
  makes its own commit of that content (git: from the index; jj: from ``@``'s tree, with ``@-`` as
  parent) without touching the user's branch, index, or jj change graph, and pins that. Such a
  commit is *ephemeral*: fxtr's ref is what keeps it. It is made with a fixed author, date, and
  message, so it is a function of its content and parent alone: two launches from the same staged
  state pin the same commit.

The :class:`Pin` says the commit, its tree hash, and whether it is ephemeral; the client records
them in the :class:`CodeVersion` with the rest of what the process ran.
``verify_environment=False`` at a launch skips the venv checks below; pinning is never skipped.

**Checking the venv.** A footgun check delegated to uv, not a proof of provenance: the lockfile is
current with the project's ``pyproject.toml``, the venv is synced to the lockfile, and the running
interpreter is that venv's. Each problem found says what is wrong and the command that fixes it;
the caller decides whether problems refuse the launch.

:class:`LocalEnvironment` answers by running git, jj, and uv in the checkout. It requires the
project to be managed by uv: a ``pyproject.toml`` uv recognizes with a ``uv.lock`` (at the project
root or, for a workspace member, the workspace root) and a virtual environment uv created. A
project managed some other way cannot be checked, which is an error, not a clean report. Tests
substitute an ``Environment`` of their own with canned answers.
"""

from __future__ import annotations

import abc
import dataclasses
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import anyio

from fxtr.project import config
from fxtr.project.config import Project, ProjectError

PIN_REF_PREFIX = "refs/fxtr/pins/"
"""Where fxtr keeps the commits it pinned, in the repository's own refs: one ref per commit, named
by the commit's hash (:func:`pin_ref`)."""


def pin_ref(commit: str) -> str:
    """The ref that keeps ``commit`` alive: ``refs/fxtr/pins/<commit>``."""
    return PIN_REF_PREFIX + commit


class PinRefusedError(ProjectError):
    """The checkout cannot be pinned as it is: it has unstaged or untracked changes, or staged
    changes without ``allow_staged``. The message names what to do."""


@dataclasses.dataclass(frozen=True)
class Pin:
    """What pinning a project's checkout produced.

    The client records it as part of a :class:`~fxtr.project.code_version.CodeVersion`, together
    with what only the client knows: whether the environment was checked, and which functions the
    process can run.

    Attributes:
        commit: The pinned commit's hash, as hex.
        tree: The hash of its tree, as hex.
        ephemeral: Whether fxtr made the commit from staged changes, so that only fxtr's ref keeps
            it.
        ref: The ref the commit is kept under (:func:`pin_ref` of the commit).
        repo_root: The repository's top-level directory.
        project_path: The project root relative to ``repo_root``, as a POSIX path (``"."`` when
            the project is the repository root). Recorded with the job, so a job says which
            project within the repository it ran.
    """

    commit: str
    tree: str
    ephemeral: bool
    ref: str
    repo_root: Path
    project_path: str


@dataclasses.dataclass(frozen=True)
class Problem:
    """One way the environment does not match the project.

    Written for the person who ran a launcher and was refused: ``what`` says what is wrong and,
    where it is not obvious, what fxtr expected and why, on its own; ``fix`` says what to do.

    Attributes:
        what: What is wrong, in a sentence or two.
        fix: What to do about it, as the command to run.
    """

    what: str
    fix: str

    def __str__(self) -> str:
        return f"{self.what} (fix: {self.fix})"


class Environment(abc.ABC):
    """fxtr's view of the checkout it is running in.

    An implementation answers, for the process it runs in, which project a path belongs to, what
    code the project's repository has checked out (pinning it), and whether the interpreter and
    its packages match what the project declares. The module docstring states the rules every
    implementation follows; the methods below state their contracts. The methods that run tools
    are async: an implementation runs them as subprocesses through anyio without blocking the
    event loop.
    """

    @property
    @abc.abstractmethod
    def cwd(self) -> Path:
        """The process's working directory, resolved: the CLI's anchor for locating a project."""

    @abc.abstractmethod
    def locate_project(self, anchor: Path | None = None) -> Project:
        """Find and read the project a path belongs to.

        Walks up from the anchor to the nearest directory holding a ``pyproject.toml``, requires
        it to have a ``[tool.fxtr]`` table, and reads that project's configuration, including its
        local ``fxtr.local.toml`` and the ``FXTR_DATABASE_URL`` override (the rules of
        :mod:`fxtr.project.config`, which an implementation applies through
        :func:`fxtr.project.config.locate_project`). Reads files only; runs no tool.

        Args:
            anchor: A file or directory inside the project; :attr:`cwd` when ``None``. A launch
                passes the root workflow's source file, the CLI passes nothing.

        Returns:
            The project, with its configuration read.

        Raises:
            ProjectNotFoundError: If no ``pyproject.toml`` lies at or above the anchor, or the
                nearest one has no ``[tool.fxtr]`` table.
            ConfigError: If a configuration file is malformed or holds an invalid setting, or
                nothing says where the project's database is.
        """

    @abc.abstractmethod
    async def pin_code(self, project: Project, *, allow_staged: bool) -> Pin:
        """Pin the code checked out in the project's repository, so what ran can be found again.

        The repository is the one enclosing ``project.root`` (git, or jj when the repository root
        holds a ``.jj`` directory). The implementation classifies the checkout and then pins one
        commit under ``refs/fxtr/pins/<commit>`` (:func:`pin_ref`), writing that ref and nothing
        else of the user's: not the branch, not ``HEAD``, not the index, not jj's change graph.
        Pinning a commit that is pinned already changes nothing.

        * A *clean* checkout (git: no staged, unstaged, or untracked changes; jj: ``@`` is empty,
          has one parent, and has no conflict) pins the checked-out commit, git's ``HEAD`` or
          jj's ``@-``. The result is not ephemeral.
        * A checkout with *unstaged or untracked* changes (git only; jj has no such state, since
          it snapshots the working copy into ``@``) is refused, whatever ``allow_staged`` says.
        * A checkout with *staged* changes and nothing unstaged (jj: a non-empty ``@``) is refused
          unless ``allow_staged``. With it, the implementation makes a commit of exactly that
          content, git's index or jj's ``@`` tree, whose parent is the checked-out commit, and
          pins that. The result is ephemeral: fxtr's ref is what keeps the commit alive. The
          commit's author, date, and message are fixed, so its hash depends on its content and
          parent alone, and launching twice from the same staged state pins the same commit.

        The refusal message names both fixes, committing the changes or staging them and passing
        ``allow_staged``, and says why fxtr must pin the code that runs.

        Args:
            project: The project whose repository is pinned.
            allow_staged: Whether staged-but-uncommitted changes may be pinned as a commit of
                fxtr's own.

        Returns:
            The pin: the commit, its tree, whether it is ephemeral, the ref
            written, the repository's root, and the project's path within it.

        Raises:
            PinRefusedError: If the checkout has unstaged or untracked changes, or staged changes
                without ``allow_staged``.
            ProjectError: If ``project.root`` is not inside a repository, or the tool fails.
        """

    @abc.abstractmethod
    async def missing_commits(self, project: Project, commits: Iterable[str]) -> list[str]:
        """The ones among ``commits`` the project's repository does not contain, in the order
        given. What an import checks before taking a job's record: the code that produced its
        results must be in this repository, else it names code nobody here can find.

        Raises:
            ProjectError: If ``project.root`` is not inside a repository, or the tool fails.
        """

    @abc.abstractmethod
    async def committed_file(self, project: Project, commit: str, path: str) -> bytes | None:
        """The contents of ``path`` in ``commit`` of the project's repository, or ``None`` when the
        repository does not contain the commit or the commit has no file there. What the viewer
        reads a job's code from, as the job's pinned commit had it rather than as the working tree
        has it now.

        Args:
            project: The project whose repository is read.
            commit: The commit's hash, as hex.
            path: The file, relative to the repository root, as a POSIX path.

        Raises:
            ProjectError: If ``project.root`` is not inside a repository, or the tool cannot run.
        """

    @abc.abstractmethod
    async def check_synced(self, project: Project) -> list[Problem]:
        """Report the ways this process's environment does not match the project's declaration.

        A footgun check, delegated to the project's tooling, and not a proof: it catches the
        common ways of running code that is not what the checkout declares. Three things are
        checked, in the project root:

        1. The lockfile is current with ``pyproject.toml`` (uv: ``uv lock --check``).
        2. The virtual environment is synced to the lockfile (uv: ``uv sync --check``): every
           locked package is installed at its locked version and nothing else is.
        3. This process runs in the project's virtual environment (uv: the environment ``uv
           python find`` reports is this process's ``sys.prefix``, both resolved), so the code
           running is the code in that environment. The environment, not the interpreter
           binary, is what identifies where code runs: a venv's ``python`` is a symlink to the
           base interpreter.

        Each check that fails contributes one :class:`Problem` saying what is wrong and the
        command that fixes it (``uv lock``, ``uv sync``, running under ``uv run`` or activating
        the venv). The check never repairs anything and never raises for a mismatch: the caller
        decides whether problems refuse a launch (``verify_environment``) and reports them.

        Args:
            project: The project whose declaration the environment is checked against.

        Returns:
            The problems found, in the order above; empty when the environment matches.

        Raises:
            ProjectError: If the tooling cannot answer, as distinct from a mismatch it reports: uv
                is not installed, or the project is not one uv manages (no lockfile, no
                pyproject uv recognizes).
        """


# ---- the local implementation ----

_PIN_IDENTITY = {
    # A pin commit is a function of its content and parent, not of who or when: two launches of
    # the same staged content at the same base pin the same commit.
    "GIT_AUTHOR_NAME": "fxtr-autocommit",
    "GIT_AUTHOR_EMAIL": "fxtr-autocommit@localhost",
    "GIT_COMMITTER_NAME": "fxtr-autocommit",
    "GIT_COMMITTER_EMAIL": "fxtr-autocommit@localhost",
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
}
_OBJECT_HASH = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")  # SHA-1, or SHA-256 repositories

_PIN_MESSAGE = """fxtr: pin of staged, uncommitted changes

fxtr made this commit automatically when a job was launched with allow_staged=True from a
checkout with staged changes in the working copy that were not yet part of a finished commit.
It holds exactly that content on top of the commit that was checked out. This commit is usually
only referenced by refs/fxtr/pins/<its hash>, which keeps it from being garbage collected so that
fxtr can track the version of code that was executed for a given job.
"""
_JJ_WORKING_COPY_TEMPLATE = (
    'commit_id ++ "\\n" ++ empty ++ "\\n" ++ conflict ++ "\\n" ++ parents.len() ++ "\\n"'
    ' ++ parents.map(|c| c.commit_id()).join("\\n")'
)
"""What is asked of ``@``, one field per line: its commit, whether it is empty, whether it has a
conflict, how many parents it has, and their commits."""

_WHY_PIN = (
    "fxtr records exactly which code each job ran, so it must be able to pin the checkout as a"
    " commit."
)


class LocalEnvironment(Environment):
    """The environment of the process this runs in, answered by running git and uv in the checkout
    (see the module docstring for what it requires of the project). Reads the process's working
    directory, ``sys.prefix``, and environment variables when asked, never earlier."""

    @property
    def cwd(self) -> Path:
        return Path.cwd().resolve()

    def locate_project(self, anchor: Path | None = None) -> Project:
        return config.locate_project(self.cwd if anchor is None else anchor)

    # -- pinning --

    async def pin_code(self, project: Project, *, allow_staged: bool) -> Pin:
        root = project.root.resolve()
        # A jj repository (colocated with git or not) is jj's to describe: its working copy is a
        # commit, and git's view of the same files is not the source of truth.
        jj_root = next((d for d in (root, *root.parents) if (d / ".jj").is_dir()), None)
        if jj_root is not None:
            return await self._pin_jj(jj_root, root, allow_staged=allow_staged)
        toplevel = await self._git(root, "rev-parse", "--show-toplevel")
        return await self._pin_git(Path(toplevel.rstrip("\n")).resolve(), root, allow_staged)

    async def missing_commits(self, project: Project, commits: Iterable[str]) -> list[str]:
        root = project.root.resolve()
        jj_root = next((d for d in (root, *root.parents) if (d / ".jj").is_dir()), None)
        git_args = () if jj_root is None else (f"--git-dir={_jj_git_dir(jj_root)}",)
        missing: list[str] = []
        for commit in commits:
            found = await self._run(
                ("git", *git_args, "cat-file", "-e", f"{commit}^{{commit}}"), root
            )
            if found.returncode != 0:
                missing.append(commit)
        return missing

    async def committed_file(self, project: Project, commit: str, path: str) -> bytes | None:
        root = project.root.resolve()
        jj_root = next((d for d in (root, *root.parents) if (d / ".jj").is_dir()), None)
        git_args = () if jj_root is None else (f"--git-dir={_jj_git_dir(jj_root)}",)
        found = await self._run(("git", *git_args, "cat-file", "blob", f"{commit}:{path}"), root)
        return found.stdout if found.returncode == 0 else None

    async def _pin_git(self, repo_root: Path, root: Path, allow_staged: bool) -> Pin:
        project_path = root.relative_to(repo_root).as_posix()
        staged, unstaged = self._staged_and_unstaged_paths(
            await self._git(repo_root, "status", "--porcelain", "--untracked-files=all")
        )
        if unstaged:
            raise PinRefusedError(
                f"The checkout at {repo_root} has unstaged or untracked changes"
                f" ({', '.join(unstaged[:5])}{', ...' if len(unstaged) > 5 else ''})."
                f" {_WHY_PIN} Commit the changes, or stage them (git add) and pass"
                " allow_staged=True to pin them as a commit of fxtr's own."
            )
        if staged and not allow_staged:
            raise PinRefusedError(
                f"The checkout at {repo_root} has staged but uncommitted changes"
                f" ({', '.join(staged[:5])}{', ...' if len(staged) > 5 else ''})."
                f" {_WHY_PIN} Commit them, or pass allow_staged=True to pin them as a commit of"
                " fxtr's own that leaves your branch untouched."
            )

        head = await self._git_hash(repo_root, "rev-parse", "--verify", "HEAD")
        if staged:
            tree = await self._git_hash(repo_root, "write-tree")
            commit = await self._git_hash(
                repo_root, "commit-tree", tree, "-p", head, "-m", _PIN_MESSAGE, env=_PIN_IDENTITY
            )
        else:
            tree = await self._git_hash(repo_root, "rev-parse", "HEAD^{tree}")
            commit = head
        ref = pin_ref(commit)
        await self._git(repo_root, "update-ref", ref, commit)
        return Pin(
            commit=commit,
            tree=tree,
            ephemeral=bool(staged),
            ref=ref,
            repo_root=repo_root,
            project_path=project_path,
        )

    async def _pin_jj(self, repo_root: Path, root: Path, *, allow_staged: bool) -> Pin:
        """Pin a jj checkout: ``@-`` when ``@`` is empty, else a commit of ``@``'s tree. Git
        plumbing runs against jj's git store, which the store's ``git_target`` file locates (the
        repository's ``.git`` when colocated)."""
        project_path = root.relative_to(repo_root).as_posix()
        # ``jj log`` snapshots the working copy first, so @ is exactly what is on disk.
        fields = await self._jj(
            repo_root, "log", "-r", "@", "--no-graph", "-T", _JJ_WORKING_COPY_TEMPLATE
        )
        at, empty, conflict, parent_count, *parents = fields.rstrip("\n").split("\n")
        if conflict == "true":
            raise PinRefusedError(
                f"The working-copy change (@) of the jj checkout at {repo_root} has unresolved"
                f" conflicts. {_WHY_PIN} Resolve them first."
            )
        if parent_count != "1":
            raise PinRefusedError(
                f"The working-copy change (@) of the jj checkout at {repo_root} has"
                f" {parent_count} parents. {_WHY_PIN} Pin from a change with a single parent"
                " (squash the merge, or `jj new` onto one commit)."
            )
        (parent,) = parents
        git_args = (f"--git-dir={_jj_git_dir(repo_root)}",)

        if empty == "true":
            commit = parent
            tree = await self._git_hash(repo_root, *git_args, "rev-parse", f"{parent}^{{tree}}")
        elif not allow_staged:
            raise PinRefusedError(
                f"The working-copy change (@) of the jj checkout at {repo_root} holds changes."
                f" {_WHY_PIN} Finish the change (`jj commit`, or `jj new` to leave it behind), or"
                " pass allow_staged=True to pin @'s content as a commit of fxtr's own outside jj's"
                " change graph."
            )
        else:
            tree = await self._git_hash(repo_root, *git_args, "rev-parse", f"{at}^{{tree}}")
            commit = await self._git_hash(
                repo_root,
                *git_args,
                "commit-tree",
                tree,
                "-p",
                parent,
                "-m",
                _PIN_MESSAGE,
                env=_PIN_IDENTITY,
            )
        ref = pin_ref(commit)
        await self._git(repo_root, *git_args, "update-ref", ref, commit)
        return Pin(
            commit=commit,
            tree=tree,
            ephemeral=empty != "true",
            ref=ref,
            repo_root=repo_root,
            project_path=project_path,
        )

    @staticmethod
    def _staged_and_unstaged_paths(porcelain: str) -> tuple[list[str], list[str]]:
        """Split ``git status --porcelain`` into the paths with staged changes and the paths with
        unstaged or untracked ones (a path may be in both). Each line is ``XY path``: ``X`` the
        index's status, ``Y`` the worktree's, ``??`` untracked; a leading space is significant."""
        staged: list[str] = []
        unstaged: list[str] = []
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            index, worktree, path = line[0], line[1], line[3:]
            if index == "?" or worktree != " ":
                unstaged.append(path)
            if index not in " ?":
                staged.append(path)
        return staged, unstaged

    # -- the venv --

    async def check_synced(self, project: Project) -> list[Problem]:
        # uv's checks exit 1 when the check fails and 2 when they cannot check at all (no project
        # or lockfile uv recognizes): the first is an answer, the second an error.
        root = project.root
        problems: list[Problem] = []
        if await self._uv_check(root, "lock", "--check"):
            problems.append(
                Problem(
                    "uv.lock is out of date with pyproject.toml, so the locked dependencies are"
                    " not the ones the project declares",
                    f"run `uv lock` in {root}",
                )
            )
        if await self._uv_check(root, "sync", "--check"):
            problems.append(
                Problem(
                    "The project's virtual environment does not match uv.lock: packages are"
                    " missing, extra, or at other versions than locked",
                    f"run `uv sync` in {root}",
                )
            )
        found = await self._uv(root, "python", "find")
        if found.returncode != 0:
            raise ProjectError(
                f"`uv python find` failed in {root}: {found.stderr.decode().strip()}"
            )
        # uv reports the interpreter it would run the project with: the venv's python when there
        # is a venv, else a base interpreter it would build one from.
        interpreter = Path(found.stdout.decode().strip())
        venv = _venv_root(interpreter)
        prefix = Path(sys.prefix).resolve()
        if venv is None:
            problems.append(
                Problem(
                    "The project has no virtual environment yet, so nothing has been installed"
                    f" from uv.lock (uv would fall back to {interpreter})",
                    f"run `uv sync` in {root}",
                )
            )
        elif venv != prefix:
            problems.append(
                Problem(
                    f"This process is running under {prefix}, but the project's virtual"
                    f" environment is {venv}. fxtr expects a launch to run inside the project's"
                    " own environment, so that the packages it checked against uv.lock are the"
                    " ones the job imports",
                    f"start the launcher with `uv run <script>` from {root}, or activate {venv}"
                    " first",
                )
            )
        return problems

    async def _uv_check(self, cwd: Path, *args: str) -> bool:
        """Run one of uv's ``--check`` commands: ``True`` if the check failed (exit 1), ``False``
        if it passed, and a ProjectError if uv could not check (any other exit)."""
        result = await self._uv(cwd, *args)
        if result.returncode in (0, 1):
            return result.returncode == 1
        raise ProjectError(
            f"`uv {' '.join(args)}` could not check the project at {cwd}:"
            f" {result.stderr.decode().strip()}. The local environment requires a project"
            " managed by uv (a pyproject.toml uv recognizes, with a uv.lock)."
        )

    # -- running tools --

    async def _git(self, cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
        """Run git in ``cwd`` and return its stdout exactly as printed (porcelain output has
        significant leading spaces); any failure is a ProjectError."""
        result = await self._run(("git", *args), cwd, extra_env=env)
        if result.returncode != 0:
            raise ProjectError(
                f"`git {' '.join(args)}` failed in {cwd}: {result.stderr.decode().strip()}"
            )
        return result.stdout.decode()

    async def _jj(self, cwd: Path, *args: str) -> str:
        """Run jj in ``cwd`` and return its stdout exactly as printed; any failure is a
        ProjectError."""
        result = await self._run(("jj", *args), cwd)
        if result.returncode != 0:
            raise ProjectError(
                f"`jj {' '.join(args)}` failed in {cwd}: {result.stderr.decode().strip()}"
            )
        return result.stdout.decode()

    async def _git_hash(self, cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
        """Run a git command that prints exactly one object hash, and return it."""
        out = await self._git(cwd, *args, env=env)
        if not _OBJECT_HASH.fullmatch(out.rstrip("\n")):
            raise ProjectError(f"`git {' '.join(args)}` printed {out!r}, not an object hash")
        return out.rstrip("\n")

    async def _uv(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        """Run uv in ``cwd`` and return the completed process, whatever its exit code: a nonzero
        code is the answer to a check, not a failure of the tool."""
        return await self._run(("uv", *args), cwd)

    async def _run(
        self, argv: Sequence[str], cwd: Path, *, extra_env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        env = None if extra_env is None else {**os.environ, **extra_env}
        try:
            return await anyio.run_process(argv, cwd=cwd, env=env, check=False)
        except FileNotFoundError:
            raise ProjectError(f"{argv[0]} is not installed or not on PATH") from None


def _jj_git_dir(repo_root: Path) -> Path:
    """The git store of the jj repository at ``repo_root``, which its store's ``git_target`` file
    locates (the repository's ``.git`` when colocated)."""
    store = repo_root / ".jj" / "repo" / "store"
    return (store / (store / "git_target").read_text().strip()).resolve()


def _venv_root(interpreter: Path) -> Path | None:
    """The virtual environment ``interpreter`` belongs to, resolved, or ``None`` for a base
    interpreter. Python's own rule (PEP 405): a venv is marked by a ``pyvenv.cfg`` in the
    interpreter's directory or its parent (``<venv>/bin/python`` on POSIX,
    ``<venv>\\Scripts\\python.exe`` on Windows). The interpreter path itself is not resolved
    first, since a venv's python is a symlink to the base interpreter."""
    for directory in (interpreter.parent, interpreter.parent.parent):
        if (directory / "pyvenv.cfg").is_file():
            return directory.resolve()
    return None


__all__ = [
    "PIN_REF_PREFIX",
    "Environment",
    "LocalEnvironment",
    "Pin",
    "PinRefusedError",
    "Problem",
    "pin_ref",
]
