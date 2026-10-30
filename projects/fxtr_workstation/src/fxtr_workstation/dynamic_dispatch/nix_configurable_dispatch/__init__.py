"""Nix/git-driven :class:`WorkstationDispatcherProto` impl.

A UI preset is a plain nix attrset (``fxtr.uiPresets.<name>``)
referenced by git commit; :class:`NixConfigurableDispatchImpl` resolves it
via a git flake ref, builds the renderer ESM derivations, wraps the bytes in
``RendererEntity``s at serve time, and serves the unchanged
:class:`WorkstationDispatcherProto` surface (so the frontend needs no work).
The extended :class:`NixDispatcherProto` adds ``set_preset``.

The preset eval/build nix layer (``eval_preset_table`` / ``build_drv_outputs``)
lives in :mod:`fxtr_nix_runner.renderers`; see :mod:`.service_impl` for the impl +
mount helper. Design:
``plans/phase0/part5_nix_workdir/design_notes/06_ui_presets.md``.
"""

from __future__ import annotations

from fxtr_nix_runner.renderers import build_drv_outputs, eval_preset_table

from .service_impl import (
    MountedNixConfigurableDispatch,
    NixConfigurableDispatchImpl,
    mount_nix_configurable_dispatch,
)

__all__ = [
    "MountedNixConfigurableDispatch",
    "NixConfigurableDispatchImpl",
    "build_drv_outputs",
    "eval_preset_table",
    "mount_nix_configurable_dispatch",
]
