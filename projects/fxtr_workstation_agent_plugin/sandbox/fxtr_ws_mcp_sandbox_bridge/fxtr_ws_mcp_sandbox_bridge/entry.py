"""``fxtr.service`` entry point for the sandbox-side MCP runner.

Mounted by :func:`fxtr_sandbox_rpc_adapter.main` per the
``module:function`` string the host's direct launcher passes:
``fxtr_ws_mcp_sandbox_bridge.entry:run``. The active scope at call
time is the ``connect`` dispatch scope — :class:`RunnerConfig` is
already decoded and its ``host_ref`` is bridged into that scope.

Per-spawn flow:

1. Host opens a sandbox via
   ``DirectSandboxEnvironment.open_sandbox(entry_point="fxtr_ws_mcp_sandbox_bridge.entry:run", ...)``.
2. Host calls ``sandbox.connect(RunnerConfig(preamble_src, script_src,
   host_ref))``.
3. The framework calls :func:`run` here. We mount a
   :class:`_GuestImpl` and return its ref. The host gets a
   ``ServiceRef[McpRunnerProto]`` chain-bridged back.
4. Host calls ``runner.run()``. We resolve the host ref, build the
   per-call ``_fxtr_mcp_builtin`` closures (closures over the host
   proxy), invoke :func:`run_preamble_and_script`, package the
   result as a :class:`RunResult`, and return its dict shape.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from .builtins import build_builtins
from .protocols import (
    MCP_RUNNER_TYPE_ID,
    McpRunnerProto,
    RunnerConfig,
    RunResult,
)
from .script_runner import run_preamble_and_script


class _GuestImpl(TypedServiceImpl[McpRunnerProto]):
    """One-shot per-spawn service. ``run()`` executes the configured
    script exactly once.

    Reusing the impl across multiple ``run()`` calls would work, but
    the host driver opens a fresh sandbox per ``run_script`` tool call
    so each impl only sees one ``run()``.
    """

    def __init__(self, config: RunnerConfig) -> None:
        self._config = config

    def _target_type_annotation(self) -> Any:
        return McpRunnerProto

    def _service_type_id(self) -> FxtrID:
        return MCP_RUNNER_TYPE_ID

    @service_method
    async def run(self) -> dict[str, Any]:
        host = await self._config.host_ref.resolve()
        queued_ops: list[dict[str, Any]] = []
        builtins = build_builtins(host, queued_ops)
        outcome = await run_preamble_and_script(
            self._config.preamble_src,
            self._config.script_src,
            builtins=builtins,
        )
        # On error: zero out queued_ops on the wire. Scripts are
        # atomic — a script that raised midway should never apply
        # its partial side-effects. Gating at the sandbox boundary
        # (rather than on the host) keeps the host's contract simple
        # ("if error is set, don't mutate") and removes a temping
        # bug where someone forgets to gate ``_apply_script_mutation``.
        wire_queued_ops = [] if outcome.error else queued_ops
        return asdict(
            RunResult(
                queued_ops=wire_queued_ops,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                error=outcome.error,
            )
        )


async def run(config: RunnerConfig) -> ServiceRef[McpRunnerProto]:
    """Entry point: mount the per-spawn impl on the active dispatch
    scope. Returns the chain-bridged ref that the host invokes
    ``run()`` on."""
    return await get_active_scope().mount(_GuestImpl(config))
