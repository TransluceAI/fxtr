"""Dynamic dispatch system — backend support for runtime-loaded
frontend renderers.

See ``plans/phase0/part3_frontend_ui/stage10b_dynamic_dispatch.md`` for
the full design. This submodule provides:

- :mod:`dynamic_dispatch_types` — the typed service protocol
  (``WorkstationDispatcherProto``), the renderer entity type
  (``RendererEntity``), and the resolve-call return shape
  (``RendererResolution``).
- :mod:`basic_dynamic_dispatch` — one concrete implementation that
  walks a set of renderer directories, compiles each via esbuild,
  creates renderer entities, and resolves type lookups against a
  startup-built table.
"""

from __future__ import annotations

from .basic_dynamic_dispatch import (
    BasicDynamicDispatchImpl,
    MountedBasicDynamicDispatch,
    mount_basic_dynamic_dispatch,
)
from .dynamic_dispatch_types import (
    DYNAMIC_DISPATCH_NS,
    RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID,
    RENDERER_ENTITY_TYPE_ID,
    WORKSTATION_DISPATCHER_TYPE_ID,
    NixDispatcherProto,
    ReloadEverythingNotification,
    RendererEntity,
    RendererResolution,
    WorkstationDispatcherProto,
)
from .nix_configurable_dispatch import (
    MountedNixConfigurableDispatch,
    NixConfigurableDispatchImpl,
    mount_nix_configurable_dispatch,
)

__all__ = [
    "DYNAMIC_DISPATCH_NS",
    "RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID",
    "RENDERER_ENTITY_TYPE_ID",
    "WORKSTATION_DISPATCHER_TYPE_ID",
    "BasicDynamicDispatchImpl",
    "MountedBasicDynamicDispatch",
    "MountedNixConfigurableDispatch",
    "NixConfigurableDispatchImpl",
    "NixDispatcherProto",
    "ReloadEverythingNotification",
    "RendererEntity",
    "RendererResolution",
    "WorkstationDispatcherProto",
    "mount_basic_dynamic_dispatch",
    "mount_nix_configurable_dispatch",
]
