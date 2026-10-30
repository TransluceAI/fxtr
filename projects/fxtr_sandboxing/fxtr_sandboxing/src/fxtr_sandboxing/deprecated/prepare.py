"""Prepare a task directory into a WASM-targeted :class:`TaskBundle` entity.

We **never** invoke a build backend. Local packages are file-walked
statically; transitive wheel deps are fetched/resolved by ``uv lock``
(which can still download wheels but won't run sdist build hooks if
the pyproject only references wheel-resolvable deps). An optional
whitelist parameter forbids local-package directories that resolve
outside a trust-root list.

The pyproject reading, source-package walking, whitelist enforcement,
and ``uv lock`` / ``uv export`` invocations live in
:mod:`fxtr_sandboxing.deprecated._prepare_common` (historically shared
with the deleted py_container prepare path); this module is the thin
WASM-specific orchestration on top of those helpers.

Supported pyproject subset:

* ``[project]`` is required (we read ``name``, ``version``,
  ``requires-python``, ``entry-points."fxtr.service"``).
* Source-package directories are chosen via, in order:

  1. ``[tool.fxtr.sandboxing] packages = [...]`` — our own explicit
     override; entries are paths relative to the pyproject's dir.
  2. ``[tool.hatch.build.targets.wheel] packages = [...]`` — same
     interpretation, reused from hatch's convention so a hatch-
     configured package doesn't need to write both.
  3. Auto-discover: try ``<project>/<normalized_name>/`` then
     ``<project>/src/<normalized_name>/``
     (``normalized_name = name.replace("-", "_").lower()``).

  The basename of each chosen dir is its name in site-packages.
  Hatch's ``sources``/``include``/``exclude`` are **not** honored
  — agents put everything they want shipped inside the named
  directory.

* ``[tool.hatch.build.targets.wheel.force-include]`` (dict mapping
  source path → target path) **is** honored. Needed for ``v2/portable``,
  which assembles itself from sibling-dir ``../fxtr2/`` files. Whitelist
  enforcement applies to force-include source paths too, so
  agents can't use it to escape their workdir.

* Build-hook-generated files (e.g. ``_version.py`` produced by
  hatch-vcs) are not picked up — the walker reads what's on disk.
* Single-file modules at project root are not supported.
* Native-extension wheels remain unsupported (only ``py3-none-any``).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from packaging.pylock import Package, PackageWheel

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr_sandbox_types import PySourcePackage, PyWheelPackage, TaskBundle

from ._prepare_common import (
    build_source_package,
    check_whitelist,
    read_entry_point,
    read_pyproject,
    resolve_deps,
    resolve_local_pkg_whitelist,
)


async def prepare(
    task_dir: Path,
    *,
    allowed_local_pkg_dirs: list[Path] | None = None,
) -> EntityRef[TaskBundle]:
    """Prepare a task directory into a TaskBundle.

    This:
    1. Reads and validates pyproject.toml (`fxtr.service` entry point + requires-python)
    2. Resolves dependencies via uv lock + uv export --format pylock.toml
    3. Classifies each package as local (-> PySourcePackage via file-walk)
       or remote (-> PyWheelPackage)
    4. Stores all entities into the active fxtr2 scope and returns a ref to the TaskBundle

    The validated ``module:function`` string is stored on ``TaskBundle.entry_point``
    and passed into ``fxtr_sandbox_rpc_adapter.main`` inside the sandbox (see
    ``runner.py`` / ``_runner.py``).

    Args:
        task_dir: Directory containing the project's ``pyproject.toml``.
        allowed_local_pkg_dirs: Optional whitelist of trust roots. When
            set, every resolved local-package directory's ``.resolve()``
            must be equal to or under one of the (also-resolved) trust
            roots. ``None`` (default) imposes no restriction. The MCP
            plugin always passes a non-None list.

    Requires an active FxtrScope.
    """
    task_dir = task_dir.resolve()

    pyproject, pyproject_path = read_pyproject(task_dir)
    project = pyproject.get("project", {})
    requires_python = project.get("requires-python", ">=3.12")
    entry_point = read_entry_point(project, pyproject_path)

    pylock_toml = await resolve_deps(task_dir)

    resolved_whitelist = resolve_local_pkg_whitelist(allowed_local_pkg_dirs)
    packages = _build_packages(pylock_toml, task_dir, resolved_whitelist)

    bundle = TaskBundle(
        packages=packages,
        requires_python=requires_python,
        entry_point=entry_point,
    )
    return bundle.store()


def _build_packages(
    pylock_toml: str,
    task_dir: Path,
    allowed_local_pkg_dirs: list[Path] | None,
) -> dict[str, EntityRef[PySourcePackage | PyWheelPackage]]:
    """Parse pylock.toml and build WASM-flavored package entities.

    Source (``directory``) packages become :class:`PySourcePackage`
    via the shared file-walker. Remote (``wheels``) packages become
    :class:`PyWheelPackage` pinned to the single ``py3-none-any``
    wheel — WASM doesn't support native extensions.
    """
    from packaging.pylock import Pylock

    data = tomllib.loads(pylock_toml)
    if "packages" not in data:
        return {}

    lock = Pylock.from_dict(data)
    packages: dict[str, EntityRef[PySourcePackage | PyWheelPackage]] = {}

    for pkg in lock.packages:
        name = str(pkg.name)

        if pkg.directory is not None:
            pkg_path = (task_dir / pkg.directory.path).resolve()
            check_whitelist(name, pkg_path, allowed_local_pkg_dirs)
            source_pkg = build_source_package(pkg_path, allowed_local_pkg_dirs)
            packages[name] = source_pkg.store()

        elif pkg.wheels is not None and len(pkg.wheels) > 0:
            wheel = _find_pure_python_wheel(pkg)
            assert wheel.url is not None
            wheel_pkg = PyWheelPackage(
                name=name,
                version=str(pkg.version),
                index=pkg.index or "",
                wheel_url=wheel.url,
                wheel_sha256=wheel.hashes["sha256"],
            )
            packages[name] = wheel_pkg.store()

        elif pkg.vcs is not None:
            raise ValueError(f"VCS dependency {name!r} is not yet supported.")
        elif pkg.archive is not None:
            raise ValueError(f"Archive dependency {name!r} is not yet supported.")
        else:
            raise ValueError(
                f"Package {name!r} has no recognized source "
                f"(no directory, wheels, vcs, or archive)."
            )

    return packages


def _find_pure_python_wheel(pkg: Package) -> PackageWheel:
    """Find a py3-none-any wheel for a package.

    WASM bundles only support pure-Python wheels — raises if no
    ``py3-none-any`` wheel is present in the package's wheels list.
    """
    from urllib.parse import urlparse

    wheels = pkg.wheels
    assert wheels is not None
    for wheel in wheels:
        url = wheel.url
        if url is None:
            continue
        filename = urlparse(url).path.rsplit("/", 1)[-1]
        if filename.endswith("-none-any.whl"):
            return wheel

    raise ValueError(
        f"No pure-Python (py3-none-any) wheel found for {pkg.name}=={pkg.version}. "
        f"Packages with native extensions are not currently supported."
    )
