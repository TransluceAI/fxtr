"""Bootstrap stub for the MCP sandbox factory.

Never invoked at runtime — the MCP server uses
``_fxtr_mcp_runner.entry:run`` (volume-mounted) via
:class:`DirectSandboxEnvironment.open_sandbox`. This package exists
only so ``fxtr_sandboxing.prepare()`` can resolve our dep closure
(``fxtr-sandbox-rpc-adapter[fxtr2-portable]``) into a TaskBundle whose
wheels we materialize into the long-lived
:class:`eryx.SandboxFactory`'s site-packages.

The placeholder entry-point exists because ``prepare()`` requires
exactly one ``[project.entry-points."fxtr.service"]`` entry — its
target string is never resolved by anything we run.
"""

from __future__ import annotations


async def placeholder(_config: object) -> None:
    raise NotImplementedError(
        "mcp_sandbox_runtime.placeholder should never be invoked. "
        "The MCP server uses _fxtr_mcp_runner.entry:run via "
        "direct_launcher.open_sandbox(entry_point=...)."
    )
