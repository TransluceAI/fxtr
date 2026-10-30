"""Sandbox-side bridge for fxtr_workstation_mcp.

Imported on both sides:

- **Host:** installed as a path dep of ``fxtr_workstation_mcp``; the
  host imports :class:`McpHostProto` (to type :class:`McpHostImpl`),
  :class:`McpRunnerProto` (for typed sandbox dispatch), and
  :class:`RunnerConfig` (the carrier passed to ``connect``).

- **Sandbox:** the parent ``sandbox/`` directory is volume-mounted
  at ``/sandbox-stub`` and added to ``sys.path``. The sandbox
  imports ``fxtr_ws_mcp_sandbox_bridge.entry:run`` as its connect
  entry point, then ``script_runner`` + ``builtins`` to run the
  user's preamble + script.

Same namespace UUID + same ``define_type`` calls on both sides
produces matching :class:`FxtrID` values for the wire-format types,
so the host and sandbox can exchange refs by id.
"""
