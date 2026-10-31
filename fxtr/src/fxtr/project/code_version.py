"""The code version: which code a job attempt's process ran.

A :class:`CodeVersion` is what a process records about the code it runs when it takes a job: the
commit of the project's repository it pinned (see :mod:`fxtr.project.environment`), which project
within the repository it ran, whether it checked its environment against the project, and where
every function it could run is implemented. It is content-addressed, so every job attempt run by
processes alike in all of these shares one entity.

**Where functions are.** :func:`function_sources` maps each name the process's registry can run to
the source of the function registered there, as of the pin. A function implemented inside the
repository is a :class:`RepoSource`: a path relative to the repository root, so the commit's tree
has it at that path. The interpreter's own environment does not count as inside, even when its
venv lives in the checkout: what is installed there is a package. Anything else (an installed
package, an editable install of another checkout) is a :class:`ModuleSource`, identified the way
Python found it, by its module's dotted name, with the distribution that provides the module when
the process's metadata says. No path is recorded
outside the repository: where a module's file was is a fact about one machine.

Line numbers are the body's, decorators included, read when the code version is built. They match
the commit because the pin refuses a working tree that differs from the pinned tree.

Note: If a module is imported but then edited and committed before the code version is pinned, the
code version will still refer to line numbers from the imported state. This should not happen during
normal operation. To prevent issues in edge cases, launch scripts should be sure to pin their code
version as soon as possible after importing their modules.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import inspect
import sys
from collections.abc import Mapping, Set
from pathlib import Path

from frozendict import frozendict

from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.experiment.registry import Function, FunctionRegistry


@dataclasses.dataclass(frozen=True, kw_only=True)
class RepoSource(TaggedDataclass, fxtr_type="org.transluce.fxtr.project.RepoSource"):
    """A function implemented inside the pinned repository.

    Attributes:
        file: The source file, relative to the repository root, as a POSIX path: the commit's
            tree has it at this path.
        symbol: The body's qualified name within its module.
        start_line: The first line, decorators included, from 1.
        end_line: The last line, inclusive.
    """

    file: str
    symbol: str
    start_line: int
    end_line: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class ModuleSource(TaggedDataclass, fxtr_type="org.transluce.fxtr.project.ModuleSource"):
    """A function implemented outside the pinned repository, identified by its module.

    Attributes:
        module: The dotted name of the module defining the body (``__main__`` for a script run
            directly, which names nothing findable but is what is known).
        symbol: The body's qualified name within the module.
        start_line: The first line within the module's source, decorators included, from 1;
            ``None`` when the body has no source file on disk (a compiled module, or code made by
            ``exec`` or in a notebook cell).
        end_line: The last line, inclusive; ``None`` likewise.
        distribution: The distribution that provides the module's top-level package, when the
            process's installed metadata says; else ``None``.
        version: That distribution's version; ``None`` without one.
    """

    module: str
    symbol: str
    start_line: int | None
    end_line: int | None
    distribution: str | None
    version: str | None


type FunctionSource = RepoSource | ModuleSource
"""Where a registered function is implemented."""


@dataclasses.dataclass(frozen=True, kw_only=True)
class CodeVersion(DataclassEntity, fxtr_type="org.transluce.fxtr.project.CodeVersion"):
    """The code a process ran: a pinned commit of the project's repository, the project within
    it, whether the environment was checked, and where each function it could run is.

    Attributes:
        commit: The commit's hash, as hex. Always a git commit hash: a jj repository's commits are
            git commits in its store.
        tree: The hash of the commit's tree, as hex. A backup for finding the code again: if the
            pin is ever lost and the same content is committed afresh, the tree still matches.
        ephemeral: Whether the commit was made by fxtr from uncommitted (staged) changes and so
            exists only under fxtr's pin ref, not on any branch of the user's; ``False`` for a
            commit the user made.
        project_path: The project root relative to the repository root at that commit, as a POSIX
            path (``"."`` for the repository root itself): which project within the repository
            ran.
        environment_verified: Whether the process checked that its environment matched the
            project (the lockfile current, the venv synced, the interpreter the venv's) and found
            no problem. ``False`` when it skipped the check, so the code it ran may not be what the
            lockfile describes: part of how far to trust the rest.
        functions: Where each function the process could run is implemented, by registered name
            (:func:`function_sources`).
    """

    commit: str
    tree: str
    ephemeral: bool
    project_path: str
    environment_verified: bool
    functions: frozendict[str, FunctionSource]


def function_sources(
    registry: FunctionRegistry, repo_root: Path
) -> frozendict[str, FunctionSource]:
    """Where each function ``registry`` can run is implemented, by name. A name registered by
    several functions is left out: nothing can run it (see
    :mod:`fxtr.experiment.registry`).

    Args:
        registry: The process's registry.
        repo_root: The pinned repository's root.
    """
    distributions = importlib.metadata.packages_distributions()
    root = repo_root.resolve()
    # The interpreter's environment is never the repository's code, even when the venv lives in
    # the checkout (a project's .venv, gitignored): what is installed there is a package.
    environments = {Path(p).resolve() for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    return frozendict(
        (name, _source(fn, root, environments, distributions))
        for name, fn in sorted(registry.unambiguous().items())
    )


def _source(
    fn: Function,
    repo_root: Path,
    environments: Set[Path],
    distributions: Mapping[str, list[str]],
) -> FunctionSource:
    body = inspect.unwrap(fn.body)
    # Only a file on disk is source: code made by exec (or a notebook cell) has a pseudo-filename
    # such as "<string>", whose lines Python's line cache may hold for some other code entirely.
    try:
        file = inspect.getsourcefile(body)
        path = None if file is None or not Path(file).is_file() else Path(file).resolve()
        lines, start = inspect.getsourcelines(body) if path is not None else ([], 0)
    except (OSError, TypeError):
        path, lines, start = None, [], 0
    span = (start, start + len(lines) - 1) if lines else None
    if path is not None and span is not None:
        if path.is_relative_to(repo_root) and not any(path.is_relative_to(e) for e in environments):
            return RepoSource(
                file=path.relative_to(repo_root).as_posix(),
                symbol=body.__qualname__,
                start_line=span[0],
                end_line=span[1],
            )
    module = body.__module__
    provided_by = distributions.get(module.partition(".")[0], [])
    distribution = provided_by[0] if len(provided_by) == 1 else None
    return ModuleSource(
        module=module,
        symbol=body.__qualname__,
        start_line=None if span is None else span[0],
        end_line=None if span is None else span[1],
        distribution=distribution,
        version=None if distribution is None else importlib.metadata.version(distribution),
    )


__all__ = ["CodeVersion", "FunctionSource", "ModuleSource", "RepoSource", "function_sources"]
