"""``resolve_backend`` — pick the backend (host or podman) from config.

The single entry point fxtr's nix wrappers call to obtain a backend.
"""

from __future__ import annotations

from fxtr_nix_runner._backend import Backend
from fxtr_nix_runner._config import NixRuntimeConfig, load_config
from fxtr_nix_runner._host import HostBackend


def resolve_backend(config: NixRuntimeConfig | None = None) -> Backend:
    """Construct the configured backend.

    Args:
        config: The resolved config selecting the backend. If ``None``, it's
            loaded via :func:`~fxtr_nix_runner.load_config` (``FXTR_NIX_BACKEND``
            env + ``~/.config/fxtr/nix_runtime.toml``).

    Returns:
        A :class:`~fxtr_nix_runner.Backend` — :class:`HostBackend` for
        ``nix_backend == "host"``, :class:`~fxtr_nix_runner._podman.PodmanBackend`
        for ``"podman"``. Cheap to construct; hold one instance for a process /
        server so concurrent same-repo ``builder_for`` calls share a builder.

    Raises:
        ConfigError: ``nix_backend`` is not configured (no file/env) — the
            message points at ``fxtr_nix runtime setup``.
        ValueError: the resolved ``nix_backend`` is neither host nor podman.
    """
    if config is None:
        config = load_config()
    backend = config.require_nix_backend()
    if backend == "host":
        return HostBackend()
    if backend == "podman":
        from fxtr_nix_runner._podman import PodmanBackend

        return PodmanBackend(config.podman)
    # require_nix_backend / _coerce_backend already reject anything else.
    raise ValueError(f"unknown backend {backend!r}")
