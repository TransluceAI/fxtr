"""Trivial user package for the mcp_plugin_spike sample workdir.

Mounted into the sandbox via the `packages/` volume; reachable from
both the preamble and the user script as `from demo_pkg import ...`.
"""

GREETING = "hello from demo_pkg"


def doubler(x: int) -> int:
    return x * 2
