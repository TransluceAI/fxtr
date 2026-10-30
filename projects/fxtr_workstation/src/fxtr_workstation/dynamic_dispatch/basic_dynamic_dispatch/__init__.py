"""One specific :class:`WorkstationDispatcherProto` impl, backed by a
set of TSX renderer directories that are esbuild-compiled at startup.

See :mod:`fxtr_workstation.dynamic_dispatch.basic_dynamic_dispatch.service_impl`
for the impl class + usage pattern, and :mod:`.compile` for the
esbuild wrapper.
"""

from __future__ import annotations

from .compile import (
    DEFAULT_EXTERNALS,
    ENTRY_FILENAME,
    RendererCompileError,
    compile_renderer,
    npx_available,
)
from .service_impl import (
    DEFAULT_SUBTYPE_KEY,
    BasicDynamicDispatchImpl,
    MountedBasicDynamicDispatch,
    mount_basic_dynamic_dispatch,
)

__all__ = [
    "DEFAULT_EXTERNALS",
    "DEFAULT_SUBTYPE_KEY",
    "ENTRY_FILENAME",
    "BasicDynamicDispatchImpl",
    "MountedBasicDynamicDispatch",
    "RendererCompileError",
    "compile_renderer",
    "mount_basic_dynamic_dispatch",
    "npx_available",
]
