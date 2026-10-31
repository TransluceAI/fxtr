"""The fxtr TypeScript packages a project's renderer package can depend on, as fxtr ships them.

A renderer package inside the fxtr checkout links ``ts/packages/`` by relative path, and their
own dependencies are installed in the checkout. Outside it there is no checkout to link, and the
packages are not on npm, so fxtr carries them:
:func:`stage` writes a *staged* copy of ``ts/`` (the shape below), which the fxtr sdist and wheel
carry as ``fxtr/_ts`` (``fxtr/hatch_build.py``) and which ``fxtr views vendor`` copies into a
project (:mod:`fxtr.project.views`).

```
tsconfig.base.json      the compiler options renderer packages extend
css-modules.d.ts        its declaration of CSS Modules imports
packages/<name>/        each of PROJECT_PACKAGES: its package.json, and the directories its
                        "exports" name (src/, testing/), without tests, configs, or node_modules
```

Each staged ``package.json`` drops ``devDependencies`` and the ``workspace:`` entries of
``dependencies``, which resolve only in the checkout's workspace; the registry dependencies it
keeps (:func:`registry_dependencies`) are ones a project that depends on the package must
install itself, since nothing installs a linked package's dependencies.

This module uses only the standard library: the build hook loads it by path, outside any
environment with fxtr installed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

PROJECT_PACKAGES = (
    "fxtr-view",
    "fxtr-view-kit",
    "fxtr-view-bundle",
    "fxtr-view-shell-util",
    "fxtr-view-behaviors",
)
"""The packages a renderer package can depend on. The viewer's own packages
(``fxtr-view-default-renderers`` and those it uses) are not among them."""

BASE_FILES = ("tsconfig.base.json", "css-modules.d.ts")
"""The files at the root of ``ts/`` a renderer package needs."""

MANIFEST_FIELDS = ("name", "version", "private", "type", "description", "exports")
"""The ``package.json`` fields a staged package keeps as they are."""


def staged_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """A package's ``package.json`` as staged (see the module docstring)."""
    staged = {field: manifest[field] for field in MANIFEST_FIELDS if field in manifest}
    dependencies = {
        name: spec
        for name, spec in manifest.get("dependencies", {}).items()
        if not spec.startswith("workspace:")
    }
    if dependencies:
        staged["dependencies"] = dependencies
    if "peerDependencies" in manifest:
        staged["peerDependencies"] = manifest["peerDependencies"]
    return staged


def stage(ts_root: Path, target: Path) -> None:
    """Write the staged copy of the checkout's ``ts/`` directory ``ts_root`` to ``target``, which
    must not exist.

    Raises:
        FileNotFoundError: If ``ts_root`` lacks a base file or one of :data:`PROJECT_PACKAGES`.
    """
    target.mkdir(parents=True)
    for name in BASE_FILES:
        shutil.copy2(ts_root / name, target / name)
    for name in PROJECT_PACKAGES:
        source = ts_root / "packages" / name
        manifest = json.loads((source / "package.json").read_text())
        package = target / "packages" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps(staged_manifest(manifest), indent=2) + "\n"
        )
        for directory in sorted(_exported_directories(manifest)):
            shutil.copytree(
                source / directory,
                package / directory,
                ignore=shutil.ignore_patterns("__pycache__"),
            )


def registry_dependencies(staged: Path) -> dict[str, dict[str, str]]:
    """The registry dependencies of each package in the staged tree ``staged`` that has any, by
    package name."""
    found: dict[str, dict[str, str]] = {}
    for name in PROJECT_PACKAGES:
        manifest = json.loads((staged / "packages" / name / "package.json").read_text())
        if manifest.get("dependencies"):
            found[name] = manifest["dependencies"]
    return found


def _exported_directories(manifest: dict[str, Any]) -> set[str]:
    """The top-level directories a package's ``exports`` point into (``./src/index.ts`` is
    ``src``)."""
    exports = manifest.get("exports", {})
    targets = exports.values() if isinstance(exports, dict) else [exports]
    return {Path(target).parts[0] for target in targets if isinstance(target, str)}


__all__ = [
    "BASE_FILES",
    "MANIFEST_FIELDS",
    "PROJECT_PACKAGES",
    "registry_dependencies",
    "stage",
    "staged_manifest",
]
