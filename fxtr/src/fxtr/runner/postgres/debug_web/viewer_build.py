"""The checkout's built viewer (``frontend/dist``): whether it is current, and rebuilding it.

From a checkout, ``fxtr view`` serves the viewer's Vite dev server, which always builds from the
current sources. It serves ``frontend/dist`` instead when the dev server cannot start,
and that directory holds whatever was last built there, perhaps on another branch,
against another API. So :func:`build_viewer` records :func:`viewer_sources_fingerprint` in
:data:`VIEWER_SOURCES_FILENAME` inside the build, and :func:`viewer_stale_reason` compares it
with the current one; the serving side (``app.py``) rebuilds a stale viewer before serving it.

The fingerprint is :func:`~fxtr.project.views.package_sources_fingerprint` of ``frontend/``: its
files and those of the workspace packages it links (``ts/packages/``), found through its
``node_modules/``. A build made any other way (``pnpm --filter fxtr-ide build``, or
``pnpm build:dist``) records none, so it counts as stale and is rebuilt once by the first
``fxtr view`` that serves it. Registry dependencies are not hashed.

A build runs the frontend's own ``vite`` (``node_modules/.bin/vite build``, without the
typecheck its ``build`` script adds) into a scratch directory under ``node_modules/.cache/``, and
moves the result into place only once it has an ``index.html``, so a failed build leaves the
previous viewer as it was.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import anyio
import anyio.to_thread

from fxtr.project.views import package_sources_fingerprint, replace_directory

VIEWER_SOURCES_FILENAME = "fxtr-viewer-sources.json"
"""The file in a built viewer recording the :func:`viewer_sources_fingerprint` it was built from."""


class ViewerBuildError(Exception):
    """The viewer could not be built. The message says why and what to do."""


def viewer_sources_fingerprint(frontend: Path) -> str:
    """A hash of the files the viewer in ``frontend`` is built from (see the module docstring)."""
    return package_sources_fingerprint(frontend)


def viewer_stale_reason(frontend: Path) -> str | None:
    """Why ``frontend/dist`` needs a build (missing, or built from other sources than
    ``frontend`` has now), or ``None`` when it was built from the current sources."""
    dist = frontend / "dist"
    if not (dist / "index.html").is_file():
        return "it is not built"
    try:
        recorded = json.loads((dist / VIEWER_SOURCES_FILENAME).read_text())["sources"]
    except (OSError, ValueError, TypeError, KeyError):
        return "it was built outside `fxtr view`, so what it was built from is unknown"
    if recorded != viewer_sources_fingerprint(frontend):
        return "its sources changed since it was built"
    return None


async def build_viewer(frontend: Path) -> None:
    """Build the viewer in ``frontend`` into ``frontend/dist``, its output passing through to
    this process's, recording the sources it was built from. A failed build leaves the previous
    ``dist`` in place.

    Raises:
        ViewerBuildError: If the frontend's dependencies are not installed, the build fails, or
            it wrote no viewer.
    """
    vite = frontend / "node_modules" / ".bin" / "vite"
    if not vite.is_file():
        raise ViewerBuildError(
            f"its build tools are not installed ({vite} is missing): run `pnpm install` in the"
            " fxtr checkout"
        )
    # The hash of the sources as the build starts: an edit made during the build leaves the
    # viewer stale, as it should.
    sources = await anyio.to_thread.run_sync(viewer_sources_fingerprint, frontend)
    scratch = frontend / "node_modules" / ".cache" / "fxtr-viewer"
    scratch.mkdir(parents=True, exist_ok=True)
    out = scratch / f"build-{uuid.uuid4().hex}"
    try:
        command = [str(vite), "build", "--outDir", str(out), "--emptyOutDir", "--logLevel", "warn"]
        result = await anyio.run_process(
            command, cwd=frontend, stdout=None, stderr=None, check=False
        )
        if result.returncode != 0:
            raise ViewerBuildError(f"`vite build` in {frontend} failed (exit {result.returncode})")
        if not (out / "index.html").is_file():
            raise ViewerBuildError(f"`vite build` in {frontend} wrote no index.html to {out}")
        (out / VIEWER_SOURCES_FILENAME).write_text(json.dumps({"sources": sources}) + "\n")
        try:
            replace_directory(out, frontend / "dist", scratch)
        except OSError as exc:
            raise ViewerBuildError(f"could not move the built viewer into place: {exc}") from None
    finally:
        shutil.rmtree(out, ignore_errors=True)


async def refresh_viewer(frontend: Path) -> str | None:
    """Make sure ``frontend/dist`` is current before it is served, saying what it does: rebuild
    it when stale.

    Returns:
        Why the viewer must not be served (stale, and not rebuilt because the build could not
        run or failed), or ``None`` when it is current or rebuilt. A stale viewer may not match
        this server's API, and then shows only errors, so it is refused rather than served.
    """
    reason = await anyio.to_thread.run_sync(viewer_stale_reason, frontend)
    if reason is None:
        return None
    dist = frontend / "dist"
    print(f"building the viewer in {dist} ({reason})", flush=True)
    try:
        await build_viewer(frontend)
    except ViewerBuildError as exc:
        refusal = (
            f"The viewer in {dist} is out of date ({reason}), and could not be rebuilt: {exc}."
            " Fix that and restart `fxtr view`."
        )
        print(refusal, flush=True)
        return refusal
    print(f"built the viewer in {dist}", flush=True)
    return None
