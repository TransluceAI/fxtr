"""Hatch build hook: ship the built experiment viewer and the TypeScript packages renderers use
inside the ``fxtr`` sdist and wheel.

Each is a directory of the repository included as package data:

- the viewer (``frontend/`` at the repository root, built by ``pnpm build:dist``) as
  ``fxtr/runner/postgres/debug_web/_frontend``, which ``fxtr view`` serves;
- the TypeScript packages a renderer package can depend on (``ts/``), as ``fxtr/_ts``, which
  ``fxtr views vendor`` copies into a project. Unlike the viewer this is not the directory as it
  is but a staged copy of it, written by ``fxtr.project.ts_packages.stage`` into a temporary
  directory for the build.

The viewer is in :data:`SHIPPED`. Each one's source is either:

- the checkout's directory, next to this package, when building from a checkout; or
- the copy an sdist already carries under ``src/``, when building a wheel from that sdist (where
  the rest of the repository is absent), so installers never need Node.

A non-editable build lacking one fails, rather than shipping a package without it. Editable
installs skip the hook: a checkout serves and vendors its own directories directly.
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

SHIPPED = (
    # (where it lives inside the package; its source, relative to the repository root; a file it
    # always has; how to make it when it is missing)
    (
        "fxtr/runner/postgres/debug_web/_frontend",
        "frontend/dist",
        "index.html",
        "Build the package with `pnpm build:dist` at the repository root, which builds the"
        " viewer first.",
    ),
)


TS_PACKAGED = "fxtr/_ts"
"""Where the staged TypeScript packages live inside the package."""

TS_MARKER = "tsconfig.base.json"
"""A file the staged TypeScript packages always have."""


def load_ts_packages(root: Path) -> ModuleType:
    """``fxtr.project.ts_packages``, loaded from this package's source: the build environment does
    not have fxtr installed."""
    path = root / "src" / "fxtr" / "project" / "ts_packages.py"
    spec = importlib.util.spec_from_file_location("_fxtr_ts_packages", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShippedFilesBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    _staging: Path | None = None

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        root = Path(self.root)
        self._ship_ts_packages(root, build_data)
        for packaged, source, marker, remedy in SHIPPED:
            if (root / "src" / packaged / marker).is_file():
                # An unpacked sdist: the carried copy is package data like any other file.
                continue
            checkout = root.parent / source
            if not (checkout / marker).is_file():
                raise RuntimeError(
                    f"{checkout} has no {marker}, so this build would ship without it. {remedy}"
                )
            target = f"src/{packaged}" if self.target_name == "sdist" else packaged
            build_data["force_include"][str(checkout)] = target

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)
            self._staging = None

    def _ship_ts_packages(self, root: Path, build_data: dict[str, Any]) -> None:
        if (root / "src" / TS_PACKAGED / TS_MARKER).is_file():
            return  # an unpacked sdist, as for SHIPPED
        ts_root = root.parent / "ts"
        if not (ts_root / TS_MARKER).is_file():
            raise RuntimeError(
                f"{ts_root} has no {TS_MARKER}, so this build would ship without the TypeScript"
                " packages. Build it from a complete checkout of the repository."
            )
        self._staging = Path(tempfile.mkdtemp(prefix="fxtr-ts-"))
        staged = self._staging / "_ts"
        load_ts_packages(root).stage(ts_root, staged)
        target = f"src/{TS_PACKAGED}" if self.target_name == "sdist" else TS_PACKAGED
        build_data["force_include"][str(staged)] = target
