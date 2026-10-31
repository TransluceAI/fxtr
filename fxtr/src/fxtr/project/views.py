"""A project's view bundle, from the Python side: finding it, building it, and reading its manifest.

A project with renderers of its own has a renderer package, an npm package in its repository that
builds into a *view bundle*: a directory holding an ES module, an optional stylesheet, and a
manifest naming them with the contract version they were built at (the TypeScript side's contract
is ``fxtr-view``'s ``view_bundle.ts``; the build is ``fxtr-view-bundle``'s Vite preset).
``[tool.fxtr.views] package`` says where the package is
(:class:`~fxtr.project.config.ViewsConfig`), and the bundle is always the package's
:data:`VIEW_BUNDLE_OUT_DIR`, where the preset builds it.

:func:`build_views` is what ``fxtr views build`` runs, and what ``fxtr view`` runs at startup when
the bundle is stale. It runs the package's own ``build`` script, through ``npm run build``
(running a script is the same under every package manager; which one installed the package does
not matter here, and nothing is installed), into a scratch directory under the package's
``node_modules/`` (the preset writes there when :data:`VIEW_BUNDLE_OUT_DIR_ENV` names it), checks
the result with :func:`read_manifest`, and only then moves it into place. A failed build leaves the
previous bundle as it was, and raises :class:`ViewsBuildError` carrying the end of the build's
output (which also passes through to this process's). The build runs on the ``node`` on ``PATH``,
which must be at least :data:`MIN_NODE_MAJOR`; :func:`build_views` checks that first.

**Staleness.** A build records :func:`sources_fingerprint`, a hash of the files the bundle is built
from, in the bundle's :data:`VIEW_BUNDLE_SOURCES_FILENAME`. Those files are the package's and
those of every package it links locally (:func:`source_packages`: a dependency whose
``node_modules`` entry resolves outside any ``node_modules``, as ``link:`` and ``workspace:``
dependencies do), skipping ``node_modules/``, built output, dot-entries, ``*.tsbuildinfo``
(rewritten by a typecheck), and tests (:func:`is_source_path`). Registry dependencies are not
hashed. :func:`stale_reason` compares the recorded hash with the current one.

**Snapshots and versions.** :func:`read_bundle` captures a :class:`ViewBundle`: the manifest,
module, and optional stylesheet as immutable bytes, their content hash, and the recorded source
fingerprint. :func:`build_views` captures this snapshot before moving the output directory.
Readers serve the captured bytes, so a version always identifies the same files even after a
rebuild. :func:`bundle_version` reads a snapshot's hash; identical output has the same version.

**Vendoring.** A renderer package outside the fxtr checkout depends on the fxtr TypeScript
packages through a copy of them inside it, :data:`VENDOR_DIR` (``vendor/fxtr/``), which
:func:`vendor_packages` (``fxtr views vendor``) writes from the running fxtr: the staged copy an
installed fxtr carries (``fxtr/_ts``), else the checkout's ``ts/``, staged the same way
(:mod:`fxtr.project.ts_packages`). Its ``package.json`` links them
(``link:./vendor/fxtr/packages/fxtr-view``), and its ``tsconfig.json`` extends
``vendor/fxtr/tsconfig.base.json``. Inside the
package, the copies' imports of React, jotai, and one another resolve to the package's own
``node_modules``. The copy is the project's to commit; vendoring again replaces it, which is how a
project takes up a newer fxtr's, and marks the bundle stale, since the copy is among the package's
files. A package inside the checkout links the checkout's ``ts/packages/`` by relative path
instead.

:data:`VIEW_API_VERSION`, :data:`VIEW_BUNDLE_MANIFEST_FILENAME`, and :data:`VIEW_BUNDLE_OUT_DIR`
mirror the TypeScript constants of the same names in ``fxtr-view``, and
:data:`VIEW_BUNDLE_OUT_DIR_ENV` the one in ``fxtr-view-bundle``; each must change with its mirror.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

import anyio
import anyio.abc
import anyio.streams.text
import anyio.to_thread
from frozendict import frozendict

import fxtr
from fxtr.project import ts_packages
from fxtr.project.config import ProjectError, ViewsConfig

VIEW_API_VERSION = 3
"""The bundle contract's version this fxtr supports: ``fxtr-view``'s ``VIEW_API_VERSION``."""

VIEW_BUNDLE_MANIFEST_FILENAME = "fxtr-view-bundle-manifest.json"
"""The manifest's file name at the root of a bundle: ``fxtr-view``'s constant of the same name."""

VIEW_BUNDLE_OUT_DIR = "dist"
"""Where a renderer package's bundle is built, relative to the package: ``fxtr-view``'s
``VIEW_BUNDLE_OUT_DIR``."""

VIEW_BUNDLE_OUT_DIR_ENV = "FXTR_VIEW_BUNDLE_OUT_DIR"
"""The environment variable that redirects the preset's output from :data:`VIEW_BUNDLE_OUT_DIR`:
``fxtr-view-bundle``'s constant of the same name."""

VIEW_BUNDLE_SOURCES_FILENAME = "fxtr-view-bundle-sources.json"
"""The file in a bundle recording the :func:`sources_fingerprint` it was built from. Written by
:func:`build_views`; the viewer backend does not serve it."""


MIN_NODE_MAJOR = 22
"""The oldest Node major a renderer package builds with: the repository's ``.nvmrc`` and its
``package.json``'s ``engines``."""

BUILD_OUTPUT_LINES = 40
"""How many of a failed build's last output lines :class:`ViewsBuildError` keeps, leaving out
stack frames (:data:`_STACK_FRAME`), which follow the error and its code frame and say nothing a
renderer author needs."""

_STACK_FRAME = re.compile(r"^\s+at \S")
"""A line of a JavaScript stack trace (``    at fn (file.js:1:2)``)."""


VENDOR_DIR = Path("vendor") / "fxtr"
"""Where a renderer package outside the checkout keeps its copy of the fxtr TypeScript packages,
relative to the package."""

PACKAGED_TS = Path(fxtr.__file__).resolve().parent / "_ts"
"""The staged TypeScript packages an installed ``fxtr`` sdist or wheel carries
(``fxtr/hatch_build.py``)."""

CHECKOUT_TS = Path(fxtr.__file__).resolve().parents[3] / "ts"
"""The TypeScript tree of the checkout this module lives in, when it lives in one
(``<checkout>/fxtr/src/fxtr/__init__.py``)."""


class ViewsError(ProjectError):
    """The project's view bundle cannot be found, built, or read. The message says what to do."""


class ViewsBuildError(ViewsError):
    """The renderer package's build script failed.

    Attributes:
        output: The end of what it printed, less stack traces (at most
            :data:`BUILD_OUTPUT_LINES` lines), where the reason usually is.
    """

    def __init__(self, message: str, output: str) -> None:
        super().__init__(message)
        self.output = output


@dataclasses.dataclass(frozen=True)
class ViewBundleManifest:
    """A bundle's manifest (``ViewBundleManifest`` in ``fxtr-view``), with where it was read.

    Attributes:
        dist: The bundle's directory, which the file names are relative to.
        api_version: The contract version the bundle was built at.
        module: The ES module's file.
        stylesheet: The stylesheet's file, or ``None`` for a package without styles.
    """

    dist: Path
    api_version: int
    module: str
    stylesheet: str | None


@dataclasses.dataclass(frozen=True)
class ViewBundle:
    """An immutable snapshot of a build. Its version hashes exactly the bytes in ``files``.

    ``build_views`` captures these bytes before publishing the build directory. Readers keep
    this object rather than reopening ``dist/``, which another build may replace at any time.
    ``sources`` is the recorded input fingerprint, absent for bundles built outside fxtr.
    """

    manifest: ViewBundleManifest
    files: frozendict[str, bytes]
    version: str
    sources: str | None

    def stale_reason(self, views: ViewsConfig) -> str | None:
        """Why this snapshot's inputs differ from the renderer package's current sources."""
        if self.sources is None:
            return "it was built without `fxtr views build`, so what it was built from is unknown"
        if self.sources != sources_fingerprint(views):
            return "its sources changed since it was built"
        return None


def bundle_dir(views: ViewsConfig) -> Path:
    """The directory the renderer package's bundle is built into."""
    return views.package / VIEW_BUNDLE_OUT_DIR


def read_manifest(dist: Path) -> ViewBundleManifest:
    """The manifest of the bundle in ``dist``, checked: well formed, at :data:`VIEW_API_VERSION`,
    and naming files that are there.

    Raises:
        ViewsError: If there is no manifest, or it is malformed, at another version, or names a
            missing file.
    """
    return _parse_manifest(dist, _manifest_bytes(dist))


def _manifest_bytes(dist: Path) -> bytes:
    path = dist / VIEW_BUNDLE_MANIFEST_FILENAME
    if not path.is_file():
        raise ViewsError(f"{dist} holds no view bundle: run `fxtr views build`")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ViewsError(f"{path} is not a readable manifest: {exc}") from None


def _parse_manifest(dist: Path, raw: bytes) -> ViewBundleManifest:
    path = dist / VIEW_BUNDLE_MANIFEST_FILENAME
    try:
        data: Any = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ViewsError(f"{path} is not a readable manifest: {exc}") from None
    fields = data if isinstance(data, dict) else {}
    api_version, module, stylesheet = (
        fields.get("apiVersion"),
        fields.get("module"),
        fields.get("stylesheet"),
    )
    if (
        not isinstance(api_version, int)
        or not isinstance(module, str)
        or not isinstance(stylesheet, str | None)
    ):
        raise ViewsError(f"{path} is not a view bundle manifest (apiVersion, module, stylesheet?)")
    if api_version != VIEW_API_VERSION:
        raise ViewsError(
            f"{path} was built at view API version {api_version}; this fxtr supports"
            f" {VIEW_API_VERSION}. Rebuild the renderer package against this fxtr's fxtr-view."
        )
    for name in (module, stylesheet):
        if name is not None and not (dist / name).is_file():
            raise ViewsError(f"{path} names {name}, which is not in {dist}")
    return ViewBundleManifest(
        dist=dist, api_version=api_version, module=module, stylesheet=stylesheet
    )


def read_bundle(dist: Path) -> ViewBundle:
    """Capture a bundle's manifest and assets once; hash and serving use these same bytes."""
    raw = _manifest_bytes(dist)
    manifest = _parse_manifest(dist, raw)
    files = {VIEW_BUNDLE_MANIFEST_FILENAME: raw}
    try:
        for name in (manifest.module, manifest.stylesheet):
            if name is not None:
                files[name] = (dist / name).read_bytes()
    except OSError as exc:
        raise ViewsError(f"could not read the view bundle in {dist}: {exc}") from None
    digest = hashlib.sha256()
    for name, content in files.items():
        digest.update(f"{name}\0".encode())
        digest.update(hashlib.sha256(content).digest())
    return ViewBundle(manifest, frozendict(files), digest.hexdigest()[:16], _bundle_sources(dist))


def bundle_version(dist: Path) -> str | None:
    """The content version of a valid bundle, or ``None`` when none can be read."""
    try:
        return read_bundle(dist).version
    except ViewsError:
        return None


def _bundle_sources(dist: Path) -> str | None:
    try:
        recorded = json.loads((dist / VIEW_BUNDLE_SOURCES_FILENAME).read_text())["sources"]
        return recorded if isinstance(recorded, str) else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def sources_fingerprint(views: ViewsConfig) -> str:
    """A hash of the files the renderer package's bundle is built from (see the module
    docstring for which)."""
    return package_sources_fingerprint(views.package)


def package_sources_fingerprint(package: Path) -> str:
    """A hash of the files of the npm package ``package`` and of every package it links locally,
    skipping ``node_modules/``, built output (``dist/``), and dot-entries: what a build of the
    package reads, less its registry dependencies and TypeScript's ``*.tsbuildinfo``. Also the
    viewer's (``debug_web``'s ``viewer_build``)."""
    digest = hashlib.sha256()
    for package in source_packages(package):
        for path in _source_files(package):
            digest.update(f"{path}\0".encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def stale_reason(views: ViewsConfig) -> str | None:
    """Why the project's bundle needs a build (missing, invalid, or built from other sources than
    the package has now), or ``None`` when it was built from the current sources."""
    try:
        return read_bundle(bundle_dir(views)).stale_reason(views)
    except ViewsError as exc:
        return str(exc)


async def build_views(views: ViewsConfig) -> ViewBundle:
    """Build the project's view bundle: run the renderer package's ``build`` script into a
    scratch directory, its output passing through to this process's, check the bundle it wrote,
    record its sources, capture its immutable snapshot, and move it to :func:`bundle_dir`,
    replacing the previous bundle. Returns the captured snapshot.

    When the build fails the previous bundle stays. When another build moves its bundle into
    place first, that one is kept.

    Raises:
        ViewsError: If the package has no ``package.json``, declares dependencies but has no
            ``node_modules/``, Node is missing or older than :data:`MIN_NODE_MAJOR`, npm is
            missing, or the build wrote no valid bundle.
        ViewsBuildError: If the build script fails.
    """
    package = views.package
    if not (package / "package.json").is_file():
        raise ViewsError(f"{package} has no package.json: is [tool.fxtr.views] package right?")
    if _dependency_names(package) and not (package / "node_modules").is_dir():
        raise ViewsError(f"{package}'s dependencies are not installed: run `pnpm install` there")
    await _check_node()
    # The hash of the sources as the build starts: an edit made during the build leaves the
    # bundle stale, as it should.
    sources = await anyio.to_thread.run_sync(sources_fingerprint, views)
    scratch = package / "node_modules" / ".cache" / "fxtr-views"
    scratch.mkdir(parents=True, exist_ok=True)
    out = scratch / f"build-{uuid.uuid4().hex}"
    out.mkdir()  # not mkdtemp's: it becomes dist/, so it gets the umask's permissions
    try:
        await _run_build(package, out)
        if not (out / VIEW_BUNDLE_MANIFEST_FILENAME).is_file():
            raise ViewsError(
                f"the build wrote no view bundle to {out}: does the package's vite.config.ts use"
                " the fxtr-view-bundle preset?"
            )
        read_manifest(out)
        (out / VIEW_BUNDLE_SOURCES_FILENAME).write_text(json.dumps({"sources": sources}) + "\n")
        bundle = await anyio.to_thread.run_sync(read_bundle, out)
        try:
            replace_directory(out, bundle_dir(views), scratch)
        except OSError as exc:
            raise ViewsError(
                f"could not move the built bundle to {bundle_dir(views)}: {exc}"
            ) from None
    finally:
        shutil.rmtree(out, ignore_errors=True)
    return dataclasses.replace(
        bundle, manifest=dataclasses.replace(bundle.manifest, dist=bundle_dir(views))
    )


@dataclasses.dataclass(frozen=True)
class VendoredPackages:
    """What :func:`vendor_packages` wrote.

    Attributes:
        directory: The copy, :data:`VENDOR_DIR` in the renderer package.
        fxtr_version: The version of the fxtr it was copied from.
        missing_dependencies: Registry packages the copied packages the renderer package depends
            on need, which its ``package.json`` does not declare, with their version ranges.
    """

    directory: Path
    fxtr_version: str
    missing_dependencies: dict[str, str]


def vendor_packages(package: Path, *, ts: Path | None = None) -> VendoredPackages:
    """Copy the running fxtr's TypeScript packages into the renderer package ``package``, at
    :data:`VENDOR_DIR`, replacing any copy there (see the module docstring).

    Args:
        package: The renderer package.
        ts: A checkout's ``ts/`` to stage the packages from instead of the running fxtr's.

    Raises:
        ViewsError: If ``package`` is inside the fxtr checkout, or this fxtr has no TypeScript
            packages to copy.
    """
    package = package.resolve()
    checkout_ts = CHECKOUT_TS if ts is None else ts.resolve()
    if checkout_ts.is_dir() and package.is_relative_to(checkout_ts.parent):
        raise ViewsError(
            f"{package} is inside the fxtr checkout, where a renderer package links the checkout's"
            " fxtr packages (ts/packages/) by relative path; there is nothing to vendor"
        )
    version = importlib.metadata.version("fxtr")
    target = package / VENDOR_DIR
    target.parent.mkdir(parents=True, exist_ok=True)
    fresh = target.parent / f".fxtr-{uuid.uuid4().hex}"
    try:
        if ts is None and (PACKAGED_TS / ts_packages.BASE_FILES[0]).is_file():
            shutil.copytree(PACKAGED_TS, fresh)
        elif (checkout_ts / ts_packages.BASE_FILES[0]).is_file():
            ts_packages.stage(checkout_ts, fresh)
        else:
            raise ViewsError(f"this fxtr carries no TypeScript packages (no {PACKAGED_TS})")
        (fresh / "README.md").write_text(
            f"Copied from fxtr {version} by `fxtr views vendor`, which replaces this directory: do"
            " not edit it.\n"
        )
        replace_directory(fresh, target, target.parent)
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
    declared = _dependency_names(package)
    missing = {
        name: spec
        for dependency, needs in ts_packages.registry_dependencies(target).items()
        if dependency in declared
        for name, spec in needs.items()
        if name not in declared
    }
    return VendoredPackages(directory=target, fxtr_version=version, missing_dependencies=missing)


async def _check_node() -> None:
    """Refuse a missing ``node``, or one older than :data:`MIN_NODE_MAJOR`, saying how to fix it,
    before a build fails on it less clearly."""
    install = f"install Node {MIN_NODE_MAJOR} or later (with nvm: `nvm install {MIN_NODE_MAJOR}`)"
    try:
        result = await anyio.run_process(["node", "--version"], check=False)
    except FileNotFoundError:
        raise ViewsError(f"Node is not installed (it builds the renderers): {install}") from None
    version = result.stdout.decode().strip()
    try:
        major = int(version.removeprefix("v").split(".")[0])
    except ValueError:
        return  # an unrecognized version string: let the build itself say what is wrong
    if major < MIN_NODE_MAJOR:
        raise ViewsError(
            f"the renderers build with Node {MIN_NODE_MAJOR} or later, and the `node` on PATH"
            f" ({shutil.which('node')}) is {version}: {install}, then build again in a shell where"
            " `node --version` shows it"
        )


async def _run_build(package: Path, out: Path) -> None:
    """Run the package's build script into ``out``, its output passing through to this process's
    and its last lines kept for a :class:`ViewsBuildError`."""
    command = ["npm", "run", "build"]
    tail: collections.deque[str] = collections.deque(maxlen=BUILD_OUTPUT_LINES)

    async def relay(stream: anyio.abc.ByteReceiveStream, to: TextIO) -> None:
        async for line in anyio.streams.text.TextReceiveStream(stream, errors="replace"):
            to.write(line)
            to.flush()
            tail.extend(kept for kept in line.splitlines() if not _STACK_FRAME.match(kept))

    try:
        process = await anyio.open_process(
            command,
            cwd=package,
            env={**os.environ, VIEW_BUNDLE_OUT_DIR_ENV: str(out)},
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise ViewsError("npm is not installed (it runs the renderer package's build)") from None
    async with process:
        assert process.stdout is not None and process.stderr is not None
        async with anyio.create_task_group() as tg:
            tg.start_soon(relay, process.stdout, sys.stdout)
            tg.start_soon(relay, process.stderr, sys.stderr)
        returncode = await process.wait()
    if returncode != 0:
        raise ViewsBuildError(
            f"`{' '.join(command)}` in {package} failed (exit {returncode})", "\n".join(tail)
        )


def replace_directory(built: Path, dist: Path, scratch: Path) -> None:
    """Replace the build output ``dist`` with the fresh build ``built``. A directory cannot be
    renamed over another, so the old one is moved aside into ``scratch`` (on the same file
    system) first, and restored if ``built`` cannot take its place. When another build's output
    takes ``dist`` first, that one is kept.

    Raises:
        OSError: If ``dist`` could not be replaced; it is then as it was.
    """
    retired = scratch / f"retired-{uuid.uuid4().hex}"
    try:
        dist.rename(retired)
    except FileNotFoundError:
        pass
    try:
        built.rename(dist)
    except OSError:
        if not dist.exists():  # not another build's output arriving first: put the old one back
            if retired.exists():
                retired.rename(dist)
            raise
    shutil.rmtree(retired, ignore_errors=True)


def _dependency_names(package: Path) -> set[str]:
    try:
        data: Any = json.loads((package / "package.json").read_text())
    except (OSError, ValueError):
        return set()
    names: set[str] = set()
    for field in ("dependencies", "devDependencies", "peerDependencies"):
        declared = data.get(field) if isinstance(data, dict) else None
        if isinstance(declared, dict):
            names.update(name for name in declared if isinstance(name, str))
    return names


def source_packages(package: Path) -> list[Path]:
    """``package`` and every package it links locally, transitively: the directories whose files
    (:func:`is_source_path`) a build of ``package`` reads."""
    found: set[Path] = set()
    pending = [package.resolve()]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        for name in _dependency_names(current):
            try:
                target = (current / "node_modules" / name).resolve(strict=True)
            except OSError:
                continue
            if "node_modules" not in target.parts:
                pending.append(target)
    return sorted(found)


_SKIPPED_DIRECTORIES = frozenset(
    {"node_modules", VIEW_BUNDLE_OUT_DIR, "test", "tests", "__tests__"}
)
"""Directories no build reads: installed packages, built output, and tests."""


def _is_source_file_name(name: str) -> bool:
    """Not a dot-file, TypeScript's incremental-build state (which a typecheck rewrites), or a
    test or test configuration (``*.test.*``, ``*.spec.*``, ``vitest.config.*``)."""
    return not (
        name.startswith(".")
        or name.endswith(".tsbuildinfo")
        or ".test." in name
        or ".spec." in name
        or name.startswith("vitest.config.")
    )


def _is_source_directory_name(name: str) -> bool:
    return name not in _SKIPPED_DIRECTORIES and not name.startswith(".")


def is_source_path(package: Path, path: Path) -> bool:
    """Whether ``path``, a file inside the package directory ``package``, is one the
    fingerprint hashes: neither it nor a directory between it and ``package`` is skipped."""
    parts = path.relative_to(package).parts
    return (
        bool(parts)
        and _is_source_file_name(parts[-1])
        and all(_is_source_directory_name(part) for part in parts[:-1])
    )


def _source_files(package: Path) -> Iterator[Path]:
    for directory, dirnames, filenames in os.walk(package):
        dirnames[:] = sorted(d for d in dirnames if _is_source_directory_name(d))
        for name in sorted(filenames):
            if _is_source_file_name(name):
                yield Path(directory) / name


__all__ = [
    "BUILD_OUTPUT_LINES",
    "CHECKOUT_TS",
    "MIN_NODE_MAJOR",
    "PACKAGED_TS",
    "VENDOR_DIR",
    "VIEW_API_VERSION",
    "VIEW_BUNDLE_MANIFEST_FILENAME",
    "VIEW_BUNDLE_OUT_DIR",
    "VIEW_BUNDLE_OUT_DIR_ENV",
    "VIEW_BUNDLE_SOURCES_FILENAME",
    "VendoredPackages",
    "ViewBundle",
    "ViewBundleManifest",
    "ViewsBuildError",
    "ViewsError",
    "build_views",
    "bundle_dir",
    "bundle_version",
    "is_source_path",
    "package_sources_fingerprint",
    "read_bundle",
    "read_manifest",
    "replace_directory",
    "source_packages",
    "sources_fingerprint",
    "stale_reason",
    "vendor_packages",
]
