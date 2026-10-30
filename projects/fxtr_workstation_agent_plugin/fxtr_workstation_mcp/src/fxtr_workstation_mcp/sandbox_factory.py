"""Bootstrap the MCP server's long-lived ``eryx.SandboxFactory``.

Drives :func:`fxtr_sandboxing.deprecated.prepare` against our in-tree
``sandbox_runtime`` task to assemble the dep closure
(``fxtr-sandbox-rpc-adapter[fxtr2-portable]`` + transitives), then
materializes that into a site-packages dir and builds the factory
over it. The factory is used by :class:`DirectSandboxEnvironment`
for fast warm spawns: factory build is paid once at MCP startup
(~3 s); per spawn is just ``factory.create_sandbox`` (~10–20 ms)
plus the cold-import cost of whatever the volume-mounted user
code touches.

The actual entry-point invoked at runtime is
``fxtr_ws_mcp_sandbox_bridge.entry:run`` (volume-mounted, see
:func:`build_runner_volumes`). The bundle from prepare() is used
purely for its dep closure — its declared placeholder entry-point
is never resolved.

Mostly a port of ``mcp_plugin_spike/plugin/scripts/scheduler_mcp/
sandbox_factory.py``, retargeted at ``sandbox_runtime`` (our
dep-closure stub) and ``fxtr_ws_mcp_sandbox_bridge`` (our
volume-mounted runner package).
"""

from __future__ import annotations

import logging
from pathlib import Path

import eryx
from fxtr_sandboxing.deprecated.prepare import prepare
from fxtr_sandboxing.runner import (
    _check_and_maybe_repair_eryx_embedded_dir,
    build_site_packages_for_bundle,
)

_log = logging.getLogger(__name__)

# This file: fxtr_workstation_mcp/fxtr_workstation_mcp/sandbox_factory.py
# Up two: fxtr_workstation_mcp/  (the package's pyproject lives here)
# Up three: fxtr_workstation_agent_plugin/
# sandbox_runtime stub:  ../../sandbox/sandbox_runtime/
# bridge package:        ../../sandbox/fxtr_ws_mcp_sandbox_bridge/
_PKG_ROOT = Path(__file__).resolve().parent.parent.parent
_PROJECT_ROOT = _PKG_ROOT.parent
SANDBOX_RUNTIME_DIR = _PROJECT_ROOT / "sandbox" / "sandbox_runtime"
SANDBOX_BRIDGE_ROOT = _PROJECT_ROOT / "sandbox" / "fxtr_ws_mcp_sandbox_bridge"
"""The bridge package's pyproject-root. Inside this dir the
importable package is ``fxtr_ws_mcp_sandbox_bridge/`` (one level
deeper). We volume-mount *this* directory at ``/sandbox-stub`` so
``import fxtr_ws_mcp_sandbox_bridge.entry`` resolves inside the
sandbox — exactly the pattern the spike uses for
``plugin/scripts/`` containing ``_fxtr_mcp_runner/``."""


async def bootstrap_factory(
    *,
    cache_dir: Path,
    sandbox_runtime_dir: Path = SANDBOX_RUNTIME_DIR,
) -> eryx.SandboxFactory:
    """Build the long-lived factory.

    Args:
        cache_dir: Persistent cache dir. Used for two things:
            wheel downloads (consumed by
            :func:`build_site_packages_for_bundle`) and the
            content-addressed materialized site-packages dirs (one
            subdir per bundle FxtrID under ``cache_dir/site_packages/``).
            Safe to share across MCP server runs.
        sandbox_runtime_dir: Path to the dep-closure stub task.
            Defaults to the in-tree ``sandbox_runtime``.

    Requires an active :class:`FxtrScope` — :func:`prepare` stores
    the bundle entity into the active scope. The scope can be
    transient (just for the duration of this call); the bundle's
    materialized site-packages dir lives under ``cache_dir`` and
    outlives the scope.

    Caching shape: the site-packages dir is keyed on the bundle's
    content-addressed FxtrID, with a ``.ready`` marker dropped after a
    successful materialization. On startup, if the marker is present
    we trust the existing dir and skip the materialize step entirely;
    otherwise (no marker / partial-write recovery / first run) we
    materialize fresh and write the marker on success. Same bundle
    across MCP restarts ⇒ near-zero startup cost; new bundle ⇒ pay the
    materialize-once cost.
    """
    bundle_ref = await prepare(sandbox_runtime_dir)
    bundle = await bundle_ref.load()

    site_packages_path = cache_dir / "site_packages" / bundle_ref.entity_id.id_string()
    ready_marker = site_packages_path / ".ready"
    if not ready_marker.is_file():
        site_packages_path.mkdir(parents=True, exist_ok=True)
        await build_site_packages_for_bundle(bundle, cache_dir, site_packages_path)
        ready_marker.touch()

    _check_and_maybe_repair_eryx_embedded_dir()
    return eryx.SandboxFactory(site_packages=str(site_packages_path))


def build_runner_volumes(
    *,
    user_packages_dir: Path | None = None,
    sandbox_bridge_root: Path = SANDBOX_BRIDGE_ROOT,
) -> tuple[list[tuple[str, str, bool]], list[str], list[str]]:
    """Build ``(volumes, sys_path_additions, warnings)`` for one
    :meth:`DirectSandboxEnvironment.open_sandbox` call.

    Two mounts:

    - ``sandbox_bridge_root`` (the bridge's pyproject-root, parent of
      the importable ``fxtr_ws_mcp_sandbox_bridge/`` package
      directory) at ``/sandbox-stub`` (read-only). The sandbox
      imports ``fxtr_ws_mcp_sandbox_bridge.entry:run`` from here.
    - ``user_packages_dir`` at ``/local-pkgs`` (read-only). Contains
      the user's local packages, one src-layout python project per
      subdirectory. Omitted if the working directory has no
      ``packages/`` subdir.

    Sys-path entries:

    - ``/sandbox-stub`` always.
    - For each immediate subdirectory of ``user_packages_dir`` that
      has both a ``pyproject.toml`` and an ``src/`` subdirectory:
      ``/local-pkgs/<name>/src``. This is the same src-layout the
      flake's ``fxtr.services.<name>`` (built by ``mkWasmService``)
      consumes, so a package that imports correctly from ``run_script``
      is also ready to be built + spawned via the git-ref path with no
      layout changes.
    - Subdirectories that have a ``pyproject.toml`` but no ``src/``
      produce a warning (returned in ``warnings``) and contribute
      nothing to ``sys.path`` — the agent should move the package
      modules under ``src/<pkg>/``.
    - Subdirectories without a ``pyproject.toml`` are ignored
      silently (so scratch dirs, caches, or non-Python assets under
      ``packages/`` don't trip the warning).

    Warnings are also logged via the module logger; the caller is
    expected to surface them to the agent (typically by prepending
    them to the ``run_script`` response).
    """
    volumes: list[tuple[str, str, bool]] = [
        (str(sandbox_bridge_root), "/sandbox-stub", True),
    ]
    sys_path_additions: list[str] = ["/sandbox-stub"]
    warnings: list[str] = []
    if user_packages_dir is not None:
        volumes.append((str(user_packages_dir), "/local-pkgs", True))
        missing_src: list[str] = []
        for entry in sorted(user_packages_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not (entry / "pyproject.toml").is_file():
                continue
            if (entry / "src").is_dir():
                sys_path_additions.append(f"/local-pkgs/{entry.name}/src")
            else:
                missing_src.append(entry.name)
        if missing_src:
            msg = (
                "package(s) under packages/ have a pyproject.toml but no "
                f"src/ subdir, so they won't be importable from run_script: "
                f"{', '.join(missing_src)}. Move the package modules to "
                "<pkg>/src/<pkg>/ to match the src layout that "
                "mkWasmService also expects."
            )
            _log.warning(msg)
            warnings.append(msg)
    return volumes, sys_path_additions, warnings
