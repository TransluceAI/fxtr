"""``fxtr_nix_runner`` — run fxtr's nix invocations on host nix or in podman.

The shared *backend* beneath fxtr's three nix wrappers (spawner, MCP,
dispatcher). It owns the host-vs-podman switch, builder-container
lifecycle, ref/path translation, output materialization, and in-container
image delivery.

Two tiers:

- **transport tier** (this module + ``_backend`` / ``_host`` / ``_podman``):
  lightweight on purpose — **anyio only, no fxtr2** — so all three wrappers can
  depend on it. Entry point: :func:`resolve_backend`; per-repo work goes through
  ``async with backend.builder_for(repo) as handle:`` and then the handle's
  ``run_nix`` / ``flake_ref_root`` / ``materialize`` / ``expose_host_endpoint``
  methods.
- **manager tier** (``rehost`` and, later, the flake manager): fxtr2-aware —
  repo-store discovery, the build-time rehost, the read adapters. Import those
  modules explicitly (``from fxtr_nix_runner.rehost import rehost_context``);
  they are **not** re-exported here, so ``import fxtr_nix_runner`` stays fxtr2-
  free.
"""

from fxtr_nix_runner._backend import (
    Backend,
    DeliveredImage,
    ExposedEndpoint,
    Handle,
    Materialized,
    NixResult,
)
from fxtr_nix_runner._clear import clear
from fxtr_nix_runner._config import (
    ConfigError,
    ContainerRuntime,
    NixRuntimeConfig,
    PodmanConfig,
    default_config_path,
    default_materialize_root,
    load_config,
    resolve_container_runtime,
)
from fxtr_nix_runner._host import HostBackend, HostHandle
from fxtr_nix_runner._lifecycle import RefcountedResourceRegistry
from fxtr_nix_runner._podman import BuilderTransportError
from fxtr_nix_runner._resolve import resolve_backend

__all__ = [
    # Interface
    "Backend",
    "Handle",
    "DeliveredImage",
    "ExposedEndpoint",
    "Materialized",
    "NixResult",
    "resolve_backend",
    # Host backend
    "HostBackend",
    "HostHandle",
    # Recycle (podman warm-builder transport failure)
    "BuilderTransportError",
    # Lifecycle
    "RefcountedResourceRegistry",
    # Config
    "ConfigError",
    "ContainerRuntime",
    "NixRuntimeConfig",
    "PodmanConfig",
    "load_config",
    "resolve_container_runtime",
    "default_config_path",
    "default_materialize_root",
    # CLI
    "clear",
]
