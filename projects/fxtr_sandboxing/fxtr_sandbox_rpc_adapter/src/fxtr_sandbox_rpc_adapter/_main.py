"""Top-level sandbox runtime entry point.

`main(ctx, entry_point=...)` is what every sandbox runner — eryx and container
alike — invokes once it has a runtime-specific :class:`SandboxServiceContext`
connected to the host. It:

1. Imports the user callable from the bundle-pinned ``module:function`` string.
2. Layers the fxtr2 RPC JSON wire codec on top of ``ctx.main_protocol_stream``
   and opens a fresh isolated host scope.
3. Mounts a `_GuestMainImpl` bound to the user entrypoint on that scope.
4. Opens an `open_rpc_scope` connection. Body is `pass` — the conn drain is
   "served until the host disconnects."

Entry-point resolution does **not** use ``importlib.metadata.entry_points``;
callers pass the same string validated at ``prepare()`` time (or, in the
container case, discovered by ``fxtr_service_runner`` at startup).
"""

from __future__ import annotations

import importlib

from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.rpc.json_serdes import wrap_json_stream
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr_sandbox_types.service_context import SandboxServiceContext

from ._guest_main import EntrypointFn, _GuestMainImpl
from ._transport import _set_active_ctx


def _resolve_entrypoint(spec: str) -> EntrypointFn:
    """Load ``module:function`` from the pinned entry-point string."""
    if ":" not in spec:
        raise RuntimeError(
            f"Invalid entry point spec {spec!r}; expected 'module:function' "
            "(same format as pyproject entry-points values)."
        )
    module_name, attr = spec.split(":", 1)
    module = importlib.import_module(module_name)
    fn = getattr(module, attr)
    return fn  # type: ignore[no-any-return]


async def main(ctx: SandboxServiceContext, *, entry_point: str) -> None:
    """Run the sandbox-side RPC bridge against ``ctx``'s main-protocol stream.

    ``ctx`` is any object satisfying :class:`SandboxServiceContext` — the
    eryx and container runners each provide their own concrete impl. The
    adapter only consumes ``ctx.main_protocol_stream``; transport-level
    half-close vs. escape-frame EOF semantics are baked into whichever
    JSON-ObjectStream impl the runtime chose.

    Returns when the host disconnects the RPC connection (the
    ``open_rpc_scope`` body exits, the conn drains, and ``main`` follows).
    """
    entrypoint_fn = _resolve_entrypoint(entry_point)
    rpc_message_stream = wrap_json_stream(ctx.main_protocol_stream)

    # Install ``ctx`` as the active SandboxServiceContext for the
    # duration of this session, so user code can call
    # ``connect_transport(token)`` / ``get_client(authorizer_ref)`` as
    # free functions without threading the ctx through every call.
    with _set_active_ctx(ctx):
        async with FxtrScope(
            isolated=True, include=[GLOBAL_SCOPE], name="guest_host"
        ) as guest_host:
            with guest_host.as_active_scope():
                impl = _GuestMainImpl(entrypoint_fn)
                main_ref = await guest_host.mount(impl)
                async with open_rpc_scope(rpc_message_stream, main_ref, name="guest_rpc"):
                    pass  # served until host disconnects
