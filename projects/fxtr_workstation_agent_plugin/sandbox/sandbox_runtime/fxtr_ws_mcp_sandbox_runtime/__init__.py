"""Bootstrap stub task. Never imported by anything; the entry-point
declared in ``pyproject.toml`` is a placeholder.

``fxtr_sandboxing.deprecated.prepare()`` reads our ``pyproject.toml`` to get the
dep closure (``fxtr-sandbox-rpc-adapter[fxtr2-portable]``); that
closure is materialized into a site-packages dir used to build the
long-lived ``eryx.SandboxFactory``.

The actual entry-point invoked by sandboxes is
``fxtr_ws_mcp_sandbox_bridge.entry:run`` via volume-mount.
"""


def placeholder() -> None:  # pragma: no cover — never invoked
    raise RuntimeError(
        "sandbox_runtime.placeholder should never be invoked; "
        "the real sandbox entry point is "
        "fxtr_ws_mcp_sandbox_bridge.entry:run."
    )
