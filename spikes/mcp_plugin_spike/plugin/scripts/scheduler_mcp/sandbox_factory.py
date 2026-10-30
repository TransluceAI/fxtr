"""Bootstrap the MCP server's long-lived eryx.SandboxFactory.

Drives ``fxtr_sandboxing.prepare`` against the in-tree
``mcp-sandbox-runtime`` task to assemble the dep closure
(``fxtr-sandbox-rpc-adapter[fxtr2-portable]`` + transitives), then
materializes that into a site-packages dir and builds the factory
over it. The factory is used by :class:`DirectSandboxEnvironment`
for fast warm spawns: factory build is paid once at MCP startup
(~3 s); per spawn is just ``factory.create_sandbox`` (~10–20 ms)
plus the cold-import cost of whatever the volume-mounted user code
touches.

The actual entry-point invoked at runtime is
``_fxtr_mcp_runner.entry:run`` (volume-mounted, see
:func:`build_runner_volumes`). The bundle from prepare() is used
purely for its dependency closure — its declared placeholder
entry-point is never resolved.
"""

from __future__ import annotations

from pathlib import Path

import eryx
from fxtr_sandboxing.prepare import prepare
from fxtr_sandboxing.runner import (
    _check_and_maybe_repair_eryx_embedded_dir,
    build_site_packages_for_bundle,
)


async def bootstrap_factory(
    *,
    sandbox_runtime_dir: Path,
    cache_dir: Path,
) -> eryx.SandboxFactory:
    """Build the long-lived factory.

    Args:
        sandbox_runtime_dir: Path to the ``mcp-sandbox-runtime`` task
            directory (contains the ``pyproject.toml`` with the
            adapter dep). Typically
            ``<plugin_root>/sandbox_runtime``.
        cache_dir: Wheel download cache for ``build_site_packages_for_
            bundle``. Safe to share across MCP server runs.

    Requires an active FxtrScope (``prepare`` stores entities into the
    active scope).
    """
    bundle_ref = await prepare(sandbox_runtime_dir)
    bundle = await bundle_ref.load()
    site_packages = await build_site_packages_for_bundle(bundle, cache_dir)
    _check_and_maybe_repair_eryx_embedded_dir()
    return eryx.SandboxFactory(site_packages=str(site_packages))


def build_runner_volumes(
    *,
    plugin_scripts_dir: Path,
    user_packages_dir: Path | None = None,
) -> tuple[list[tuple[str, str, bool]], list[str]]:
    """Build ``(volumes, sys_path_additions)`` for one
    :meth:`DirectSandboxEnvironment.open_sandbox` call.

    Two mounts:

    - ``plugin_scripts_dir`` at ``/sandbox-stub`` (read-only). Contains
      the ``_fxtr_mcp_runner`` package; sandbox imports
      ``_fxtr_mcp_runner.entry:run`` from here. This dir also contains
      ``scheduler_mcp/`` and other host-only modules — they're
      reachable but never imported by anything inside the sandbox.
    - ``user_packages_dir`` at ``/local-pkgs`` (read-only). Contains
      the user's local packages (subdirs are importable). Omitted if
      the working directory has no ``packages/`` subdir.

    Both mount points are inserted into ``sys.path`` so packages under
    them are importable directly.
    """
    volumes: list[tuple[str, str, bool]] = [
        (str(plugin_scripts_dir), "/sandbox-stub", True),
    ]
    sys_path_additions: list[str] = ["/sandbox-stub"]
    if user_packages_dir is not None:
        volumes.append((str(user_packages_dir), "/local-pkgs", True))
        sys_path_additions.append("/local-pkgs")
    return volumes, sys_path_additions
