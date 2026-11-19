"""The commits a project's jobs ran at, as a small graph for the viewer's job picker.

:func:`read_history` reads the git repository enclosing the project and answers which commit each
job is shown under, and how those commits connect. It reads git and nothing else: the jobs' launch
versions (:class:`Launch`) are handed in, so it is tested against a temporary repository without a
database. Every call reads the repository afresh; nothing is kept between calls.

**Which commits are shown.** Only commits a job is shown under. A run of commits between two of
them is left out, and each shown commit's parents are the nearest shown commits its real parents
lead to (:func:`shown_parents`), so the graph keeps its shape.

**The project's commits.** Where the project is a directory inside a larger repository, only the
commits that changed something in that directory count (:class:`ProjectFiles`): each job is shown
under the newest such commit at or before the one the rules below find (following first parents),
so the commit a job is shown under says what last changed in the project before it ran. The
directory is the one the launch recorded (:attr:`Launch.project_path`), so a job launched before
the project moved is placed by the commits that changed the project where it was then. The commit
the job launched at is still recorded, and answered beside where it is shown when the two differ.
Where the project is the whole repository, every commit counts.

**Which commit a job is shown under.** A launch records a commit, the hash of its file tree, and
whether fxtr made the commit from staged changes (:mod:`fxtr.project.environment`); fxtr keeps
every such commit, so it outlives the branch it was on. In order:

1. Its own commit, when a branch contains it.
2. A commit on a branch with the same files: the same code under another hash (staged changes
   committed afterwards, an amended message, a squash merge of an up-to-date branch). For a
   project inside a larger repository, the same files in the project's directory, compared
   among the commits that changed it; so a launch from staged changes elsewhere in the repository
   lands on the project's commit, unmarked.
3. For a launch from staged changes, the commit that was checked out (the commit's parent), marked
   ``uncommitted``.
4. Its own commit, on no branch (a rebased, amended, or deleted branch's): it is still there, and
   still leads back to where it forked.
5. Nowhere, when the repository does not have the commit (a fresh clone, another machine's
   launch): the job is only counted (``hidden``).

**Branches.** A commit belongs to at most one branch for display (:func:`branch_owners`): the
default branch first, then the others from the oldest tip, so a branch stacked on another does not
claim the other's commits. Only local branches count.
"""

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio

DEFAULT_BRANCH_NAMES = ("main", "master", "trunk")
"""The branches treated as the default, the first that exists; else the checked-out one."""


@dataclasses.dataclass(frozen=True)
class Launch:
    """One job and the code its first job attempt ran.

    Attributes:
        job_id: The job.
        workflow_name: Its root workflow's name.
        created_at: When it was created.
        commit: The commit it launched at, as hex.
        tree: That commit's tree hash.
        ephemeral: Whether fxtr made the commit from staged changes.
        project_path: The project's directory relative to the repository's root at that commit,
            as a POSIX path (``"."`` for the root itself).
    """

    job_id: str
    workflow_name: str
    created_at: datetime
    commit: str
    tree: str
    ephemeral: bool
    project_path: str


@dataclasses.dataclass(frozen=True)
class Branch:
    """A local branch: its name, the commit it points at, and that commit's time."""

    name: str
    tip: str
    time: int


@dataclasses.dataclass(frozen=True)
class Walk:
    """The commits reachable from some tips, children before parents.

    Attributes:
        order: Every commit, newest first, a commit before each of its parents.
        parents: Each commit's parents.
        trees: Each commit's tree hash.
    """

    order: Sequence[str]
    parents: Mapping[str, Sequence[str]]
    trees: Mapping[str, str]


@dataclasses.dataclass(frozen=True)
class ProjectFiles:
    """Where a project is a directory inside a larger repository, which commits changed that
    directory.

    Attributes:
        nearest: For each commit walked, the newest commit at or before it, following first
            parents, that changed the project's directory; absent where none did.
        files: The hash of the project directory's tree at each commit that changed it on a
            branch, and at each launch's commit the repository has.
    """

    nearest: Mapping[str, str]
    files: Mapping[str, str]


def nearest_changes(walk: Walk, changed: set[str]) -> dict[str, str]:
    """For each commit walked, the newest commit at or before it, following first parents, among
    ``changed``. A merge that changed nothing in a directory has the same files there as its
    first parent."""
    nearest: dict[str, str] = {}
    for commit in reversed(walk.order):  # parents before children
        if commit in changed:
            nearest[commit] = commit
        elif walk.parents[commit] and walk.parents[commit][0] in nearest:
            nearest[commit] = nearest[walk.parents[commit][0]]
    return nearest


def branch_owners(walk: Walk, branches: Sequence[Branch], default: str | None) -> dict[str, str]:
    """The branch each commit is shown as belonging to, for the commits some branch contains.

    The default branch claims everything it contains; then each other branch, from the oldest
    tip, claims what is left of what it contains.
    """
    ordered = sorted(branches, key=lambda b: (b.name != default, b.time, b.name))
    owner: dict[str, str] = {}
    for branch in ordered:
        pending = [branch.tip]
        while pending:
            commit = pending.pop()
            if commit in owner or commit not in walk.parents:
                continue
            owner[commit] = branch.name
            pending.extend(walk.parents[commit])
    return owner


def place_launches(
    walk: Walk,
    owner: Mapping[str, str],
    launches: Iterable[Launch],
    projects: Mapping[str, ProjectFiles] | None = None,
) -> dict[str, tuple[str, bool]]:
    """Where each job is shown: its job id to the commit and whether it is marked as launched
    with uncommitted changes. A job the repository cannot place is absent. ``projects``, by
    project directory, limits each launch's commits to those that changed the directory it
    recorded (none for a launch whose directory is not there); the module docstring has the
    rules."""
    projects = projects or {}
    same_files: dict[str, str] = {}
    same_project: dict[tuple[str, str], str] = {}
    for commit in walk.order:  # newest first: the newest commit on a branch with the files wins
        if commit in owner:
            same_files.setdefault(walk.trees[commit], commit)
            for path, project in projects.items():
                if project.nearest.get(commit) == commit and commit in project.files:
                    same_project.setdefault((path, project.files[commit]), commit)
    placed: dict[str, tuple[str, bool]] = {}
    for launch in launches:
        project = projects.get(launch.project_path)

        def near(commit: str, project: ProjectFiles | None = project) -> str:
            return project.nearest.get(commit, commit) if project else commit

        here = launch.commit in walk.parents
        files = project.files.get(launch.commit) if project else None
        if here and launch.commit in owner:
            placed[launch.job_id] = (near(launch.commit), False)
        elif files is not None and (launch.project_path, files) in same_project:
            placed[launch.job_id] = (same_project[launch.project_path, files], False)
        elif launch.tree in same_files:
            placed[launch.job_id] = (near(same_files[launch.tree]), False)
        elif here and launch.ephemeral and walk.parents[launch.commit]:
            placed[launch.job_id] = (near(walk.parents[launch.commit][0]), True)
        elif here:
            placed[launch.job_id] = (near(launch.commit), False)
    return placed


def shown_parents(walk: Walk, shown: set[str]) -> dict[str, list[str]]:
    """Each shown commit's parents among the shown commits: the nearest shown commits its real
    parents lead to, less any that another of them already descends from (a merge left out
    between two shown commits would otherwise draw a second line past the commits on its side
    branch)."""
    nearest: dict[str, list[str]] = {}
    ancestors: dict[str, set[str]] = {}
    result: dict[str, list[str]] = {}

    def reduced(commits: list[str]) -> list[str]:
        unique = list(dict.fromkeys(commits))
        if len(unique) < 2:
            return unique
        return [c for c in unique if not any(c in ancestors[other] for other in unique)]

    for commit in reversed(walk.order):  # parents before children
        above = reduced([n for parent in walk.parents[commit] for n in nearest.get(parent, [])])
        if commit in shown:
            result[commit] = above
            ancestors[commit] = set(above).union(*(ancestors[a] for a in above))
            nearest[commit] = [commit]
        else:
            nearest[commit] = above
    return result


async def read_history(project_root: Path, launches: Sequence[Launch]) -> dict[str, Any] | None:
    """The viewer's commit view of ``launches``, read from the git repository enclosing
    ``project_root``; ``None`` when there is none (or git is not installed).

    The JSON-ready answer has ``branches`` (those some shown commit belongs to: ``name``,
    ``default``); ``commits``, newest first
    (``hash``, ``subject``, ``time``, ``parents`` among the shown commits, ``branch`` or null);
    ``jobs``, in the order given (``id``, ``workflowName``, ``createdAt``, ``commit``,
    ``uncommitted``, and ``launchedAt``: the commit it launched at, ``hash`` and ``subject``, where
    that is another commit the repository has and fxtr did not make; else null); and ``hidden``,
    how many jobs the repository could not place.
    """
    refs = await _git(
        project_root,
        "for-each-ref",
        "--format=%(refname:short)%00%(objectname)%00%(committerdate:unix)",
        "refs/heads",
    )
    if refs is None:
        return None
    branches = [
        Branch(name, tip, int(time))
        for name, tip, time in (line.split("\0") for line in refs.splitlines() if line)
    ]
    head_branch = (
        await _git(project_root, "symbolic-ref", "-q", "--short", "HEAD") or ""
    ).strip() or None
    names = {branch.name for branch in branches}
    default = next((name for name in DEFAULT_BRANCH_NAMES if name in names), head_branch)

    wanted = list(dict.fromkeys(launch.commit for launch in launches))
    present = await _present(project_root, wanted)
    tips: list[str] = list(dict.fromkeys([*present, *(b.tip for b in branches)]))
    walk = await _walk(project_root, tips)
    owner = branch_owners(walk, branches, default)
    here = set(present)
    projects: dict[str, ProjectFiles] = {}
    for path in dict.fromkeys(launch.project_path for launch in launches):
        if path == ".":
            continue
        launched = [
            launch.commit
            for launch in launches
            if launch.project_path == path and launch.commit in here
        ]
        projects[path] = await _project_files(project_root, path, tips, walk, owner, launched)
    placed = place_launches(walk, owner, launches, projects)
    shown = {commit for commit, _ in placed.values()}
    owning = {owner[commit] for commit in shown if commit in owner}
    parents = shown_parents(walk, shown)
    elsewhere = {
        launch.job_id: launch.commit
        for launch in launches
        if launch.job_id in placed
        and not launch.ephemeral
        and launch.commit in walk.parents
        and placed[launch.job_id][0] != launch.commit
    }
    details = await _details(
        project_root, [*(c for c in walk.order if c in shown), *set(elsewhere.values())]
    )
    commits = [
        {
            "hash": commit,
            "subject": details[commit][1],
            "time": datetime.fromtimestamp(details[commit][0], UTC).isoformat(),
            "parents": parents[commit],
            "branch": owner.get(commit),
        }
        for commit in walk.order
        if commit in shown
    ]
    return {
        "branches": [
            {"name": branch.name, "default": branch.name == default}
            for branch in sorted(branches, key=lambda b: (b.name != default, b.name))
            if branch.name in owning
        ],
        "commits": commits,
        "jobs": [
            {
                "id": launch.job_id,
                "workflowName": launch.workflow_name,
                "createdAt": launch.created_at.isoformat(),
                "commit": placed[launch.job_id][0],
                "uncommitted": placed[launch.job_id][1],
                "launchedAt": {"hash": at, "subject": details[at][1]}
                if (at := elsewhere.get(launch.job_id)) is not None and at in details
                else None,
            }
            for launch in launches
            if launch.job_id in placed
        ],
        "hidden": sum(1 for launch in launches if launch.job_id not in placed),
    }


async def _git(cwd: Path, *args: str, stdin: str | None = None) -> str | None:
    """git's output in ``cwd``, or ``None`` when it fails (no repository there, no such object)
    or is not installed."""
    try:
        result = await anyio.run_process(
            ("git", *args),
            cwd=cwd,
            check=False,
            input=None if stdin is None else stdin.encode(),
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    return result.stdout.decode(errors="replace") if result.returncode == 0 else None


async def _present(cwd: Path, commits: Sequence[str]) -> list[str]:
    """Those of ``commits`` the repository has, in order."""
    if not commits:
        return []
    out = await _git(cwd, "cat-file", "--batch-check", stdin="".join(f"{c}\n" for c in commits))
    found = {
        fields[0]
        for line in (out or "").splitlines()
        if (fields := line.split())[1:2] == ["commit"]
    }
    return [commit for commit in commits if commit in found]


async def _project_files(
    cwd: Path,
    prefix: str,
    tips: Sequence[str],
    walk: Walk,
    owner: Mapping[str, str],
    launched: Sequence[str],
) -> ProjectFiles:
    """Which commits changed the directory ``prefix`` (from the repository's root), and its files
    at them and at the commits ``launched``."""
    if not tips:
        return ProjectFiles({}, {})
    out = await _git(
        cwd,
        "log",
        "--full-history",
        "--format=%H %P",
        "--stdin",
        "--",
        f":(top){prefix}",
        stdin="".join(f"{tip}\n" for tip in tips),
    )
    # git lists a merge whose directory differs from any parent's; it changed the directory only
    # where it differs from the first parent's (a merge of main into a branch changes nothing).
    listed = [line.split() for line in (out or "").splitlines() if line]
    merges = [(commit, parents[0]) for commit, *parents in listed if len(parents) > 1]
    before = await _directory_trees(cwd, prefix, [c for merge in merges for c in merge])
    changed = {
        commit
        for commit, *parents in listed
        if len(parents) < 2 or before.get(commit) != before.get(parents[0])
    }
    nearest = nearest_changes(walk, changed)
    wanted = [*(c for c in walk.order if c in owner and nearest.get(c) == c), *launched]
    return ProjectFiles(nearest, await _directory_trees(cwd, prefix, wanted))


async def _directory_trees(cwd: Path, prefix: str, commits: Sequence[str]) -> dict[str, str]:
    """The hash of the directory ``prefix`` (from the repository's root) at each of ``commits``
    that has it."""
    wanted = list(dict.fromkeys(commits))
    if not wanted:
        return {}
    found = await _git(
        cwd, "cat-file", "--batch-check", stdin="".join(f"{c}:{prefix}\n" for c in wanted)
    )
    trees: dict[str, str] = {}
    for commit, line in zip(wanted, (found or "").splitlines(), strict=False):
        fields = line.split()
        if fields[1:2] == ["tree"]:
            trees[commit] = fields[0]
    return trees


async def _walk(cwd: Path, tips: Sequence[str]) -> Walk:
    """Every commit reachable from ``tips``, with its tree and parents."""
    if not tips:
        return Walk((), {}, {})
    out = await _git(
        cwd,
        "log",
        "--date-order",
        "--format=%H %T %P",
        "--stdin",
        stdin="".join(f"{tip}\n" for tip in tips),
    )
    order: list[str] = []
    parents: dict[str, Sequence[str]] = {}
    trees: dict[str, str] = {}
    for line in (out or "").splitlines():
        commit, tree, *above = line.split()
        order.append(commit)
        parents[commit] = above
        trees[commit] = tree
    return Walk(order, parents, trees)


async def _details(cwd: Path, commits: Sequence[str]) -> dict[str, tuple[int, str]]:
    """Each commit's time (the committer's, as seconds) and subject line."""
    if not commits:
        return {}
    out = await _git(
        cwd,
        "log",
        "--no-walk=unsorted",
        "--format=%H%x00%ct%x00%s",
        "--stdin",
        stdin="".join(f"{commit}\n" for commit in commits),
    )
    details: dict[str, tuple[int, str]] = {}
    for line in (out or "").splitlines():
        commit, time, subject = line.split("\0", 2)
        details[commit] = (int(time), subject)
    return details
