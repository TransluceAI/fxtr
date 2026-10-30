"""Sandbox-side runtime for the fxtr isolation spike.

`main(ctx, entry_point=...)` is the universal sandbox entry point. Both the eryx
and container runners hand ``ctx`` (a :class:`SandboxServiceContext` impl) plus
the bundle-pinned ``module:function`` string. It imports that callable,
mounts a `SandboxConnectProto` impl, opens an `open_rpc_scope` connection,
and serves until the host disconnects.

The adapter is parametric over which fxtr2 flavor is installed as a peer:
``fxtr2`` (full, for hosts and Linux containers) or ``fxtr2-portable``
(pure-Python subset, for the eryx WASM sandbox). It declares neither as a
base dep — install one via the matching extra:

  fxtr-sandbox-rpc-adapter[fxtr2]            # full Linux runtimes
  fxtr-sandbox-rpc-adapter[fxtr2-portable]   # eryx WASM sandbox

If neither is importable when the adapter loads, we raise
:class:`ImportError` with a message naming both extras, so the
diagnostic at runtime is "you forgot to install a peer" rather than a
plain "no module named fxtr2".
"""

from __future__ import annotations

# Peer-dependency check. Run at module import time so any caller that
# `import fxtr_sandbox_rpc_adapter`s gets a clear error before they hit
# something that fails deeper in the stack.
try:
    import fxtr2 as _fxtr2  # noqa: F401
    import fxtr_sandbox_types as _fxtr_sandbox_types  # noqa: F401
except ImportError as _e:
    raise ImportError(
        "fxtr_sandbox_rpc_adapter requires either `fxtr2` (full) or "
        "`fxtr2-portable` (pure-Python subset) to be installed as a peer, "
        "plus `fxtr_sandbox_types` with the matching extra. Install one of:\n"
        "  fxtr-sandbox-rpc-adapter[fxtr2]            (host / full Linux container)\n"
        "  fxtr-sandbox-rpc-adapter[fxtr2-portable]   (eryx WASM sandbox)\n"
        f"\nUnderlying error: {_e}"
    ) from _e


from ._main import main
from ._transport import connect_transport


def __getattr__(name: str):
    """Module-level lazy attribute access for the `[http]` extra.

    `get_client` lives in `_http`, which imports `httpx` / `httpcore`
    inside the function — but importing the module itself is cheap.
    Loading it on first attribute access keeps `import
    fxtr_sandbox_rpc_adapter` itself free of any [http]-extra
    requirements while making `from fxtr_sandbox_rpc_adapter import
    get_client` and `fxtr_sandbox_rpc_adapter.get_client(...)` both
    work transparently. PEP 562 module __getattr__.
    """
    if name == "get_client":
        from ._http import get_client as _real_get_client

        return _real_get_client
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["connect_transport", "get_client", "main"]
