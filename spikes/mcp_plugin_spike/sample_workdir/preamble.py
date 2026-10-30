"""Sample preamble for the mcp_plugin_spike workdir.

Re-exec'd before each `run_script` call. Anything bound here is in
scope for the script. Plain top-level Python (no top-level await).
"""

from demo_pkg import GREETING, doubler  # noqa: F401  (used by sample scripts)

ANSWER = 42
