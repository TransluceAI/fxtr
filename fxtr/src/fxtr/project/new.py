"""A new project, from a template: what ``fxtr new`` writes.

:func:`write_project` writes an empty project: a ``pyproject.toml`` with its ``[tool.fxtr]``
table, a Python package under ``src/``, a ``.gitignore``, a ``.env`` for API keys, and a
``README.md`` with setup
instructions. It also writes a renderer package (``views/``, named in
``[tool.fxtr.views]``) with an empty bundle. Pass ``example=True`` to include a complete
greeting experiment, its ``launch.py``, and its renderers: the greeting's views and a results
overview. The documentation's project configuration page
(``docs/fxtr/reference/project-configuration.mdx``)
explains what it writes.
Local database configuration is written by ``fxtr init``.

**Where fxtr comes from.** :func:`find_checkout` finds the checkout the running fxtr was
imported from, or none.

- With no checkout, the project is *published*: it depends on fxtr from the package index, at
  the running fxtr's version or later.
- With a checkout, it depends on the checkout's fxtr by path: by relative paths from a project
  *inside* the checkout (under ``personal/``, say), which hold in every clone, and by absolute
  paths from one outside it.

Either way the renderer package owns its pnpm workspace, lockfile, and install: the boundary in
``views/pnpm-workspace.yaml`` keeps installation local even inside another workspace, so creating
a project writes nothing outside its directory. Inside the checkout it links the checkout's fxtr
packages (``link:`` by relative path); outside it, published or not, it links a copy of them kept
inside it (``vendor/fxtr/``, written by :func:`fxtr.project.views.vendor_packages` from the
checkout's ``ts/``, or from the installed fxtr's copy when published).

**The template** is the files under ``template/`` beside this module, which mirror the project's
layout (``src/package/`` is the package's directory, ``gitignore`` is ``.gitignore``, ``env`` is
``.env``, the gitignored file for API keys that :func:`fxtr.project.config.load_env_file` reads);
the one file not there is the renderer package's ``package.json``, whose dependencies depend on
where the project is, so :func:`_package_json` builds it. A template's ``{{name}}`` is replaced
by a value, and ``{{#flag}}...{{/flag}}`` (``{{^flag}}...{{/flag}}``) keeps its text only when
the flag is set (unset). An unknown name or flag is an error, so a typo in a template fails its
tests.

:func:`init_git` creates a git repository when the project is in none. ``fxtr new`` prints the
setup commands, including ``uv sync``, which writes ``uv.lock`` and the venv.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import keyword
import os
import re
import tomllib
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import anyio

import fxtr
from fxtr.project.config import ProjectError
from fxtr.project.views import VENDOR_DIR, vendor_packages

WORKFLOW_NAME = "greet_everyone"
"""The registered name of the template's workflow, for the next steps ``fxtr new`` prints."""

VIEWS_DIR = "views"
"""The renderer package's directory, relative to the project root."""

PACKAGE_MANAGER = "pnpm@10.12.2"
"""The renderer package's ``packageManager``: the example project's."""

VIEWS_DEV_DEPENDENCIES = {
    "@types/react": "^18.3.11",
    "jotai": "^2.10.0",
    "react": "^18.3.1",
    "typescript": "^5.6.3",
    "vite": "^6.3.5",
}
"""The renderer package's third-party development dependencies, at the example project's
versions (a test checks they agree)."""

VENDORED_DEV_DEPENDENCIES = {"@types/node": "^22.10.0"}
"""The development dependencies a renderer package linking vendored fxtr packages also needs:
``fxtr-view-bundle`` reads ``process.env``, whose types the checkout's workspace provides
otherwise."""

FXTR_EXTRAS = "fxtr[runner,debug-web]"
"""The project's fxtr requirement, without a version: the Postgres runner and the viewer."""

_TEMPLATE_FILES = (
    # (template relative to template/, target relative to project root, example only)
    ("pyproject.toml.tmpl", "pyproject.toml", False),
    ("README.md.tmpl", "README.md", False),
    ("gitignore.tmpl", ".gitignore", False),
    ("env.tmpl", ".env", False),
    ("launch.py.tmpl", "launch.py", True),
    ("src/package/__init__.py.tmpl", "src/{package}/__init__.py", False),
    ("src/package/experiment.py.tmpl", "src/{package}/experiment.py", True),
    ("views/tsconfig.json.tmpl", "views/tsconfig.json", False),
    ("views/vite.config.ts.tmpl", "views/vite.config.ts", False),
    ("views/pnpm-workspace.yaml.tmpl", "views/pnpm-workspace.yaml", False),
    ("views/src/bundle.ts.tmpl", "views/src/bundle.ts", False),
    ("views/src/greeting.tsx.tmpl", "views/src/greeting.tsx", True),
    ("views/src/overview.tsx.tmpl", "views/src/overview.tsx", True),
    ("views/src/overview.module.css.tmpl", "views/src/overview.module.css", True),
)

_PROJECT_NAME = re.compile(r"[a-z0-9]([a-z0-9._-]*[a-z0-9])?", re.IGNORECASE)
_SECTION = re.compile(r"\{\{([#^])(\w+)\}\}\n?(.*?)\{\{/\2\}\}\n?", re.DOTALL)
_VARIABLE = re.compile(r"\{\{(\w+)\}\}")


class NewProjectError(ProjectError):
    """A project cannot be created as asked, or a step after writing it failed. The message says
    what to do."""


@dataclasses.dataclass(frozen=True)
class NewProject:
    """A project :func:`write_project` wrote.

    Attributes:
        root: The project root.
        name: The project's name (``[project] name``), normalized.
        package: The Python package its code is in, under ``src/``.
        checkout: The fxtr checkout it depends on, or ``None`` for a published project, which
            depends on fxtr from the package index.
        in_checkout: Whether the project is inside that checkout (so it refers to it by relative
            paths).
        example: Whether it includes the greeting experiment and launcher.
        files: What was written from the template, in order.
    """

    root: Path
    name: str
    package: str
    checkout: Path | None
    in_checkout: bool
    example: bool
    files: tuple[Path, ...]


def project_names(name: str) -> tuple[str, str]:
    """The normalized project name and the package name for ``name``: ``My_Project`` is the
    project ``my-project`` in the package ``my_project``.

    Raises:
        NewProjectError: If ``name`` is not a valid project name, or makes no usable package name.
    """
    if not _PROJECT_NAME.fullmatch(name):
        raise NewProjectError(
            f"{name!r} is not a project name: use letters, digits, and '-', '_', or '.' between"
            " them (pass --name to choose one other than the directory's)"
        )
    project = re.sub(r"[-_.]+", "-", name).lower()
    package = project.replace("-", "_")
    if not package.isidentifier() or keyword.iskeyword(package) or package == "fxtr":
        raise NewProjectError(
            f"the project {project!r} would be the Python package {package!r}, which cannot be"
            " imported as the project's own: pass --name to choose another"
        )
    return project, package


def _is_checkout(path: Path) -> bool:
    """Whether ``path`` holds fxtr's ``fxtr/pyproject.toml``."""
    try:
        with (path / "fxtr" / "pyproject.toml").open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return isinstance(data.get("project"), dict) and data["project"].get("name") == "fxtr"


def find_checkout() -> Path | None:
    """The checkout the running fxtr was imported from, or ``None`` when it was installed from
    a package index (the project is then published).
    """
    # <checkout>/fxtr/src/fxtr/__init__.py, when fxtr is imported from a checkout.
    checkout = Path(fxtr.__file__).resolve().parents[3]
    return checkout if _is_checkout(checkout) else None


def write_project(
    root: Path,
    *,
    checkout: Path | None,
    name: str | None = None,
    example: bool = False,
) -> NewProject:
    """Write a new project at ``root`` (see the module docstring), depending on ``checkout``.

    Args:
        root: The project root: a directory that does not exist yet, or is empty.
        checkout: The fxtr checkout (:func:`find_checkout`), or ``None`` for a published project.
        name: The project's name; the directory's name by default.
        example: Whether to include the greeting experiment and launcher; omitted by default.

    Raises:
        NewProjectError: If ``root`` holds anything, the name is not usable, the checkout lacks
            what the project needs.
    """
    root = root.expanduser().resolve()
    checkout = None if checkout is None else checkout.resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise NewProjectError(f"{root} exists and is not an empty directory")
    project, package = project_names(root.name if name is None else name)
    published = checkout is None
    in_checkout = checkout is not None and root.is_relative_to(checkout)
    vendored = not in_checkout
    if checkout is not None:
        missing = [p for p in _TS_PACKAGES if not (checkout / "ts" / "packages" / p).is_dir()]
        if missing:
            raise NewProjectError(f"{checkout} has no ts/packages/{missing[0]}")

    def where(target: Path, start: Path) -> str:
        """How the project refers to ``target`` in the checkout, from the directory ``start``."""
        return Path(os.path.relpath(target, start)).as_posix() if in_checkout else str(target)

    version = importlib.metadata.version("fxtr")
    values = {
        "project_name": project,
        "package": package,
        "views_package": f"{project}-views",
        "workflow": WORKFLOW_NAME,
        "fxtr_version": version,
        "fxtr_requirement": f"{FXTR_EXTRAS}>={version}" if published else FXTR_EXTRAS,
        "fxtr_checkout": "" if checkout is None else str(checkout),
        "fxtr_path": "" if checkout is None else where(checkout / "fxtr", root),
        "pyright_base": "" if checkout is None else where(checkout / "pyright-base.json", root),
        "pyright_include": json.dumps(["src", "launch.py"] if example else ["src"]),
        "ts_base": (
            where(checkout / "ts" / "tsconfig.base.json", root / VIEWS_DIR)
            if checkout is not None and in_checkout
            else f"./{(VENDOR_DIR / 'tsconfig.base.json').as_posix()}"
        ),
    }
    flags = {
        "in_checkout": in_checkout,
        "published": published,
        "vendored": vendored,
        "example": example,
    }

    written: list[Path] = []
    for template, target, example_only in _TEMPLATE_FILES:
        if example_only and not example:
            continue
        text = render(_template_text(template), values, flags)
        written.append(_write(root / target.format(package=package), text))
    packages_path = (
        where(checkout / "ts" / "packages", root / VIEWS_DIR)
        if checkout is not None and in_checkout
        else f"./{(VENDOR_DIR / 'packages').as_posix()}"
    )
    package_json = _package_json(project, packages_path=packages_path, vendored=vendored)
    written.append(_write(root / VIEWS_DIR / "package.json", package_json))
    if vendored:
        vendor_packages(root / VIEWS_DIR, ts=None if checkout is None else checkout / "ts")
    return NewProject(
        root=root,
        name=project,
        package=package,
        checkout=checkout,
        in_checkout=in_checkout,
        example=example,
        files=tuple(written),
    )


def in_repository(path: Path) -> bool:
    """Whether ``path`` is inside a git or jj checkout (a ``.git`` or ``.jj`` at or above it)."""
    path = path.resolve()
    return any((d / ".git").exists() or (d / ".jj").exists() for d in (path, *path.parents))


async def init_git(root: Path) -> None:
    """Make ``root`` a git repository (``git init``).

    Raises:
        NewProjectError: If git is missing or fails.
    """
    try:
        result = await anyio.run_process(["git", "init", "-q"], cwd=root, check=False)
    except FileNotFoundError:
        raise NewProjectError(
            f"git is not installed: the project is written in {root}; run `git init` there"
            " once it is"
        ) from None
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise NewProjectError(
            f"`git init` in {root} failed (exit {result.returncode}): {detail}; the project"
            " is written, so fix that and run it again there"
        )


def render(text: str, values: Mapping[str, str], flags: Mapping[str, bool]) -> str:
    """Render a template (see the module docstring): sections first, outermost first, then values.
    The result ends with exactly one newline.

    Raises:
        KeyError: If the template names a value or flag it is not given.
    """

    def section(match: re.Match[str]) -> str:
        kind, flag, body = match.groups()
        return body if flags[flag] == (kind == "#") else ""

    # Repeatedly, so a section inside another is rendered once the outer one has been.
    while (rendered := _SECTION.sub(section, text)) != text:
        text = rendered
    return _VARIABLE.sub(lambda match: values[match.group(1)], text).rstrip("\n") + "\n"


# ---- helpers ----

_TS_PACKAGES = ("fxtr-view", "fxtr-view-kit", "fxtr-view-bundle")
"""The fxtr TypeScript packages a renderer package depends on."""


def _template_text(name: str) -> str:
    return resources.files("fxtr.project").joinpath("template", name).read_text()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _package_json(project: str, *, packages_path: str, vendored: bool) -> str:
    """The renderer package's ``package.json``, linking the fxtr TypeScript packages in
    ``packages_path``: the checkout's, or the package's vendored copy, which also needs the
    development dependencies the checkout's workspace provides otherwise."""

    def fxtr_package(package: str) -> str:
        return f"link:{packages_path}/{package}"

    data: dict[str, Any] = {
        "name": f"{project}-views",
        "private": True,
        "version": "0.0.0",
        "type": "module",
        "description": f"The {project} project's renderers, built into its view bundle.",
        "scripts": {"build": "vite build --configLoader runner", "typecheck": "tsc --noEmit"},
        "dependencies": {p: fxtr_package(p) for p in ("fxtr-view", "fxtr-view-kit")},
        "devDependencies": {"fxtr-view-bundle": fxtr_package("fxtr-view-bundle")}
        | VIEWS_DEV_DEPENDENCIES
        | (VENDORED_DEV_DEPENDENCIES if vendored else {}),
        "packageManager": PACKAGE_MANAGER,
    }
    return json.dumps(data, indent=2) + "\n"


__all__ = [
    "PACKAGE_MANAGER",
    "VIEWS_DEV_DEPENDENCIES",
    "VIEWS_DIR",
    "WORKFLOW_NAME",
    "NewProject",
    "NewProjectError",
    "find_checkout",
    "in_repository",
    "init_git",
    "project_names",
    "render",
    "write_project",
]
