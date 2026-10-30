"""Materialize a bundle's packages into a flat site-packages directory.

Used by the WASM runner, which feeds the result to
``eryx.SandboxFactory(site_packages=...)``. Bundles carry single-wheel
:class:`PyWheelPackage` entities (always ``py3-none-any``).

**The caller owns the output directory.** ``build_site_packages_for_bundle``
takes a ``target_dir: Path`` and writes into it; it doesn't create or clean
up the directory itself. Callers can use ``tempfile.TemporaryDirectory``
for a one-shot tempdir, or pass a stable cache directory for future
content-addressed reuse. The directory is created if it doesn't exist;
existing contents are preserved (the materializer doesn't pre-clean).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from fxtr_sandbox_types import PySourcePackage, PyWheelPackage, TaskBundle

from .wheels import fetch_wheels


async def build_site_packages_for_bundle(
    bundle: TaskBundle, cache_dir: Path, target_dir: Path
) -> Path:
    """Materialize a WASM :class:`TaskBundle` into ``target_dir``.

    Source packages are written file-by-file from their ``source_files``
    dict; wheel packages are downloaded (via :func:`fetch_wheels` against
    ``cache_dir``) and unzipped into ``target_dir``. Returns
    ``target_dir`` for chained-call convenience.
    """
    source_packages: list[PySourcePackage] = []
    wheel_packages: list[PyWheelPackage] = []

    for _name, pkg_ref in bundle.packages.items():
        pkg = await pkg_ref.load()
        if isinstance(pkg, PySourcePackage):
            source_packages.append(pkg)
        elif isinstance(pkg, PyWheelPackage):
            wheel_packages.append(pkg)
        else:
            raise TypeError(f"Unexpected package type: {type(pkg)}")

    wheel_refs = [(pkg.wheel_url, pkg.wheel_sha256) for pkg in wheel_packages]
    wheel_paths = fetch_wheels(wheel_refs, cache_dir)

    target_dir.mkdir(parents=True, exist_ok=True)
    _write_source_packages(target_dir, source_packages)
    _extract_wheels(target_dir, wheel_paths)
    return target_dir


def _write_source_packages(target_dir: Path, source_packages: list[PySourcePackage]) -> None:
    for src_pkg in source_packages:
        for rel_path, content in src_pkg.source_files.items():
            file_path = target_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)


def _extract_wheels(target_dir: Path, wheel_paths: list[Path]) -> None:
    for whl_path in wheel_paths:
        with zipfile.ZipFile(whl_path) as zf:
            zf.extractall(target_dir)
