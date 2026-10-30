"""Thin esbuild wrapper used by :class:`BasicDynamicDispatchImpl`.

Shells out to ``npx esbuild`` to compile a renderer's ``index.tsx``
into ESM bytes. ``npx`` is preferred over a direct ``esbuild`` binary
on ``$PATH`` because:

- It works out of the box anywhere Node.js is installed (which the
  workstation already requires for the frontend build).
- A project-local ``node_modules/esbuild`` (e.g. from the workspace's
  pnpm install) is picked up automatically.
- Without a local copy, ``npx`` will fetch + cache a transient one.

Callers wanting reproducibility should ensure a project-local
``esbuild`` is installed (typically as a workspace dependency); callers
who don't care can rely on the cached download.

If ``npx`` isn't on ``$PATH``, :func:`compile_renderer` raises
:class:`RendererCompileError`. Tests that exercise the real compile
path are gated on :func:`npx_available` so CI / dev environments
without Node can still run the rest of the suite.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

import anyio

#: The default set of bare-import specifiers the compiled renderer
#: module should NOT bundle — they'll be resolved at load time through
#: the frontend's import map (see :doc:`stage10b_dynamic_dispatch.md`).
#:
#: Anything not in this list will be bundled. Anything in this list
#: must be mapped to a real module instance by the frontend's
#: ``installFxtrExternals`` call (see
#: ``@fxtr-ui/react-dispatch-dynamic-shim``) before the renderer can
#: load.
DEFAULT_EXTERNALS: tuple[str, ...] = (
    "react",
    "react/jsx-runtime",
    "jotai",
    "jotai-effect",
    "@fxtr-ui/core",
    "@fxtr-ui/state-util",
    "@fxtr-ui/react",
    "@fxtr-ui/react-dispatch",
    # Holds the canonical workstation dispatch keys + Props +
    # fallbacks + RefLink + JsonRefTree + metadata views.
    # Externalised so runtime-loaded renderer modules and the host
    # workstation shell share one identity per UUID (single
    # canonical fallback set, single `RefLink` component).
    "@fxtr-ui/workstation-dispatch",
)

#: Entry-point filename a renderer directory must contain.
ENTRY_FILENAME = "index.tsx"


class RendererCompileError(RuntimeError):
    """Raised when esbuild fails or its environment is unavailable."""


def npx_available() -> bool:
    """Return ``True`` if ``npx`` is on ``$PATH``. Tests that exercise
    the real esbuild compile gate on this."""
    return shutil.which("npx") is not None


async def compile_renderer(
    directory: Path,
    *,
    externals: Sequence[str] = DEFAULT_EXTERNALS,
    entry_filename: str = ENTRY_FILENAME,
) -> bytes:
    """Compile ``directory/index.tsx`` into ESM bytes via ``npx esbuild``.

    The compiled bundle:

    - is ES-module format (``--format=esm``);
    - targets the browser (``--platform=browser``);
    - bundles dependencies (``--bundle``);
    - leaves every name in ``externals`` as a bare-specifier import
      for the frontend's import map to resolve.

    Subprocess execution rides on :func:`anyio.run_process` so the
    surrounding event loop stays responsive while esbuild runs
    (which can take a few hundred milliseconds even for tiny
    renderers).

    Raises :class:`RendererCompileError` if ``npx`` is unavailable, the
    entry file is missing, or esbuild exits non-zero (with the
    captured stderr for diagnostics).
    """
    if not npx_available():
        raise RendererCompileError(
            "npx is not on $PATH — install Node.js (which ships npx) so "
            "renderer compilation can shell out to esbuild."
        )
    entry = directory / entry_filename
    if not entry.is_file():
        raise RendererCompileError(f"Renderer directory {directory} has no {entry_filename}")
    cmd = [
        "npx",
        "--yes",
        "esbuild",
        str(entry),
        "--bundle",
        "--format=esm",
        "--platform=browser",
        # Automatic JSX transform — renderer TSX files can use
        # `<div/>` syntax without an `import React` boilerplate.
        # The compiled output adds bare-specifier imports from
        # `react/jsx-runtime`, which is in the default externals
        # list so it routes through the frontend's import-map shim.
        "--jsx=automatic",
    ]
    for ext in externals:
        cmd.append(f"--external:{ext}")
    result = await anyio.run_process(cmd, check=False)
    if result.returncode != 0:
        raise RendererCompileError(
            f"esbuild failed for {entry}: {result.stderr.decode(errors='replace')}"
        )
    return result.stdout
