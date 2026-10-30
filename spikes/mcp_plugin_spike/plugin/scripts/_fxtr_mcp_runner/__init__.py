"""Sandbox-side runtime for the mcp_plugin_spike runner.

Package layout:

- :mod:`script_runner` — pure-stdlib script-wrapping logic. Exec's a
  preamble + a user script in a fresh globals dict, with a per-call
  ``_fxtr_mcp_builtin`` module installed in ``sys.modules`` exposing
  ``declare_value`` / ``schedule_message`` closures. Top-level
  ``await`` in the script is supported via
  ``ast.PyCF_ALLOW_TOP_LEVEL_AWAIT``.
- :mod:`protocols` — wire-format types shared between host (the MCP
  server) and guest (the sandbox): ``McpRunnerProto``, ``McpHostProto``,
  ``McpRunnerConfig``. Depends on ``fxtr2`` / ``fxtr2-portable``.
- :mod:`entry` — the ``fxtr.service`` entry point. Mounts a
  ``_GuestImpl`` that, when ``run()`` is invoked, drives
  :mod:`script_runner` against the config-supplied preamble + script
  and returns the collected ``(declared, scheduled)`` to the host.
"""
