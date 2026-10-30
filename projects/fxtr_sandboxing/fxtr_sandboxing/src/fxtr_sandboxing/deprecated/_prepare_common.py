"""Shared helpers for :mod:`fxtr_sandboxing.deprecated.prepare`.

Reading + validating pyproject, running ``uv lock`` + ``uv export``,
walking local-path source packages, and the trust-root whitelist
enforcement. (Historically shared with the deleted
``py_container_prepare`` — hence the split from ``prepare.py``.)

See `tests/test_prepare_file_walk.py` and `tests/test_prepare_whitelist.py`
for direct unit-test coverage of the file-walker + whitelist helpers.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Iterator

import anyio
from fxtr_sandbox_types import PySourcePackage


def read_pyproject(task_dir: Path) -> tuple[dict, Path]:
    """Read ``task_dir/pyproject.toml``, returning ``(parsed, path)``.

    Raises :class:`FileNotFoundError` if the file isn't there. The
    parsed dict has whatever structure the file declares — callers
    extract from it with ``.get(...)`` chains and validate as needed.
    """
    pyproject_path = task_dir / "pyproject.toml"
    if not pyproject_path.exists():
        raise FileNotFoundError(f"No pyproject.toml found in {task_dir}")
    with open(pyproject_path, "rb") as f:
        return tomllib.load(f), pyproject_path


def read_entry_point(project: dict, pyproject_path: Path) -> str:
    """Validate ``[project.entry-points."fxtr.service"]`` and return the
    single ``module:function`` value.

    Requires exactly one entry in that section. Raises :class:`ValueError`
    with a clear message if the section is missing, has the wrong number
    of entries, or the value isn't in ``module:function`` format.
    """
    entry_points = project.get("entry-points", {}).get("fxtr.service", {})
    if not entry_points:
        raise ValueError(f'No [project.entry-points."fxtr.service"] section in {pyproject_path}')
    if len(entry_points) != 1:
        raise ValueError(
            f'Expected exactly one entry point in [project.entry-points."fxtr.service"], '
            f"got {len(entry_points)}: {list(entry_points.keys())}"
        )
    entry_point = next(iter(entry_points.values()))
    if ":" not in entry_point:
        raise ValueError(f"Entry point must be in 'module:function' format, got: {entry_point!r}")
    return entry_point


async def resolve_deps(task_dir: Path) -> str:
    """Run ``uv lock`` + ``uv export --format pylock.toml`` against
    ``task_dir`` and return the pylock contents as a string.

    Exports without ``--no-emit-project`` so the task package itself
    is included in the result. Both invocations use ``check=True`` and
    capture stdout/stderr.
    """
    uv = find_uv()

    await anyio.run_process(
        [uv, "lock"],
        cwd=task_dir,
        check=True,
        stdout=-1,
        stderr=-1,
    )

    result = await anyio.run_process(
        [uv, "export", "--format", "pylock.toml", "--no-dev"],
        cwd=task_dir,
        check=True,
        stdout=-1,
        stderr=-1,
    )
    return result.stdout.decode("utf-8")


def find_uv() -> str:
    """Find the uv binary on PATH. Raises :class:`RuntimeError` if missing."""
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv not found on PATH. Install it: https://docs.astral.sh/uv/")
    return uv


def check_whitelist(
    pkg_name: str,
    pkg_path: Path,
    allowed_local_pkg_dirs: list[Path] | None,
) -> None:
    """Raise :class:`ValueError` if ``pkg_path`` (already ``.resolve()``\\ d)
    isn't under any entry in ``allowed_local_pkg_dirs``. No-op when the
    whitelist is ``None``."""
    if allowed_local_pkg_dirs is None:
        return
    for root in allowed_local_pkg_dirs:
        try:
            pkg_path.relative_to(root)
            return
        except ValueError:
            continue
    raise ValueError(
        f"Local package {pkg_name!r} at {pkg_path} is outside the allowed "
        f"local-package directories: {[str(d) for d in allowed_local_pkg_dirs]}"
    )


def build_source_package(
    pkg_dir: Path,
    allowed_local_pkg_dirs: list[Path] | None,
) -> PySourcePackage:
    """File-walk a local package's source dirs into a :class:`PySourcePackage`.

    Reads the package's pyproject.toml; chooses source directories
    per the priority documented in :mod:`fxtr_sandboxing.prepare`'s
    module docstring; copies text files directly into ``source_files``
    (no build backend invoked).

    Honors ``[tool.hatch.build.targets.wheel.force-include]`` and
    applies the whitelist to its source paths as well.
    """
    pyproject_path = pkg_dir / "pyproject.toml"
    if not pyproject_path.exists():
        raise FileNotFoundError(f"Local package at {pkg_dir} has no pyproject.toml")

    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)

    pkg_dirs = resolve_source_dirs(pkg_dir, pyproject)
    source_files: dict[str, str] = {}
    for src_dir in pkg_dirs:
        target_name = src_dir.name
        for path in walk_package_dir(src_dir):
            rel = path.relative_to(src_dir)
            key = f"{target_name}/{rel.as_posix()}"
            _read_into(source_files, key, path, pkg_dir)

    # force-include — hatch-style {source_rel: target_rel} table.
    force_include = (
        pyproject.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("force-include", {})
    )
    if not isinstance(force_include, dict):
        raise ValueError(
            "tool.hatch.build.targets.wheel.force-include must be a table "
            f"of source-path → target-path strings (in {pyproject_path})"
        )
    for src_rel, target_rel in force_include.items():
        if not isinstance(src_rel, str) or not isinstance(target_rel, str):
            raise ValueError(
                f"force-include entry {src_rel!r}={target_rel!r} must be "
                f"string → string (in {pyproject_path})"
            )
        src_path = (pkg_dir / src_rel).resolve()
        check_whitelist(
            f"{pkg_dir.name}/force-include[{src_rel}]", src_path, allowed_local_pkg_dirs
        )
        if src_path.is_dir():
            for path in walk_package_dir(src_path):
                rel = path.relative_to(src_path)
                key = f"{target_rel.rstrip('/')}/{rel.as_posix()}"
                _read_into(source_files, key, path, pkg_dir)
        elif src_path.is_file():
            _read_into(source_files, target_rel, src_path, pkg_dir)
        else:
            raise FileNotFoundError(
                f"force-include source {src_rel} (resolved to {src_path}) "
                f"in {pyproject_path} does not exist"
            )

    if not source_files:
        raise ValueError(
            f"Local package at {pkg_dir} produced no source files. "
            f"Configure [tool.fxtr.sandboxing] packages, "
            "[tool.hatch.build.targets.wheel] packages, "
            "or [tool.hatch.build.targets.wheel.force-include], "
            "or ensure auto-discovery finds a source directory."
        )
    return PySourcePackage(source_files=source_files)


def _read_into(target: dict[str, str], key: str, path: Path, pkg_dir: Path) -> None:
    """Read ``path`` as UTF-8 and store under ``key``. Raises a clear
    error for binary files."""
    try:
        target[key] = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError(
            f"Binary file {path} (in package {pkg_dir}). Only pure-Python "
            "packages (text files) are supported."
        )


def resolve_source_dirs(pkg_dir: Path, pyproject: dict) -> list[Path]:
    """Pick the directories that make up a local package's source.

    Resolution order (first match wins):

    1. ``[tool.fxtr.sandboxing] packages = [...]``
    2. ``[tool.hatch.build.targets.wheel] packages = [...]``
    3. Auto-discover ``<pkg_dir>/<normalized_name>/`` then
       ``<pkg_dir>/src/<normalized_name>/``.

    An explicit ``packages = []`` is allowed and returns ``[]`` — the
    caller is expected to supplement via ``force-include`` (this is
    the ``v2/portable`` shape).
    """
    tool = pyproject.get("tool", {})
    fxtr_cfg = tool.get("fxtr", {}).get("sandboxing", {})
    if "packages" in fxtr_cfg:
        return _validate_explicit_pkgs(pkg_dir, fxtr_cfg["packages"], "tool.fxtr.sandboxing")
    hatch_wheel = tool.get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    if "packages" in hatch_wheel:
        return _validate_explicit_pkgs(
            pkg_dir, hatch_wheel["packages"], "tool.hatch.build.targets.wheel"
        )

    project = pyproject.get("project", {})
    name = project.get("name")
    if not name:
        raise ValueError(
            f"Local package at {pkg_dir}: pyproject has no project.name and no "
            "explicit packages config; cannot auto-discover source dirs."
        )
    normalized = name.replace("-", "_").lower()
    flat = pkg_dir / normalized
    if flat.is_dir():
        return [flat]
    src = pkg_dir / "src" / normalized
    if src.is_dir():
        return [src]
    raise ValueError(
        f"Local package at {pkg_dir} (name={name!r}): no source dir found. "
        f"Tried {flat} and {src}. Configure [tool.fxtr.sandboxing] packages = [...] "
        "or [tool.hatch.build.targets.wheel] packages = [...] to override."
    )


def _validate_explicit_pkgs(pkg_dir: Path, raw: object, source_label: str) -> list[Path]:
    """Coerce an explicit ``packages = [...]`` config to a list of
    directories under ``pkg_dir``. Empty list is allowed (force-include
    is expected to fill in)."""
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise ValueError(f"{source_label}.packages must be a list of relative-path strings")
    out: list[Path] = []
    for rel in raw:
        candidate = (pkg_dir / rel).resolve()
        try:
            candidate.relative_to(pkg_dir.resolve())
        except ValueError:
            raise ValueError(
                f"{source_label}.packages entry {rel!r} resolves outside the "
                f"package dir {pkg_dir}"
            )
        if not candidate.is_dir():
            raise ValueError(
                f"{source_label}.packages entry {rel!r} is not a directory "
                f"(resolved to {candidate})"
            )
        out.append(candidate)
    return out


def walk_package_dir(src_dir: Path) -> Iterator[Path]:
    """Yield every file under ``src_dir`` that should be shipped.

    Skips ``__pycache__/`` and dot-prefixed dirs (e.g. ``.pytest_cache``,
    ``.mypy_cache``); skips ``*.pyc`` files and dot-prefixed files
    (e.g. ``.gitignore``). The skip rules apply at every depth.
    """
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(src_dir).parts
        if any(p == "__pycache__" or p.startswith(".") for p in rel_parts[:-1]):
            continue
        name = rel_parts[-1]
        if name.startswith(".") or name.endswith(".pyc"):
            continue
        yield path


def resolve_local_pkg_whitelist(
    allowed_local_pkg_dirs: list[Path] | None,
) -> list[Path] | None:
    """Pre-resolve the whitelist of trust-root paths. ``None`` stays
    ``None``."""
    if allowed_local_pkg_dirs is None:
        return None
    return [d.resolve() for d in allowed_local_pkg_dirs]
