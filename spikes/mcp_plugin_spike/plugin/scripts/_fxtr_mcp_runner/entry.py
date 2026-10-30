"""``fxtr.service`` entry point for the sandbox-side MCP runner.

Mounted by :func:`fxtr_sandbox_rpc_adapter.main` per the
``module:function`` string the host's direct launcher passes:
``_fxtr_mcp_runner.entry:run``. The active scope at call time is the
``connect`` dispatch scope — :class:`McpRunnerConfig` is already
decoded and its ``host_ref`` is bridged into that scope.

This module owns the MCP-specific wire-format conventions:

- The set of names exposed to the user script via ``_fxtr_mcp_builtin``
  (``declare_value``, ``schedule_message``, ``host_info``).
- The validation rules on those names (``declare_value`` must take
  JSON-serializable values; ``schedule_message`` must take a number
  + a string; etc.).
- The shape of the response dict returned by ``runner.run()`` to the
  host (``{"declared": ..., "scheduled": [...]}``).

:mod:`script_runner` is the underlying primitive — it just exec's
preamble + script with whatever builtins this module hands it.

Per-spawn flow:

1. Host opens a sandbox via ``DirectSandboxEnvironment.open_sandbox(
   entry_point="_fxtr_mcp_runner.entry:run", volumes=[...])``.
2. Host calls ``sandbox.connect(McpRunnerConfig(preamble_src,
   script_src, host_ref))``.
3. The framework calls :func:`run` here. We mount a
   :class:`_GuestImpl` and return its ref. The host gets a
   ``ServiceRef[McpRunnerProto]`` chain-bridged back.
4. Host calls ``runner.run()``. We resolve the host ref, build the
   per-call builtins (closures over fresh ``declared``/``scheduled``
   containers + the resolved host's ``host_info``), invoke
   :func:`run_preamble_and_script`, and return the packaged result.
"""

from __future__ import annotations

import json
from typing import Any

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from .protocols import (
    MCP_RUNNER_TYPE_ID,
    McpRunnerConfig,
    McpRunnerProto,
)
from .script_runner import run_preamble_and_script


class _GuestImpl(TypedServiceImpl[McpRunnerProto]):
    """One-shot per-spawn service. ``run()`` executes the configured
    script exactly once.

    Reusing the impl across multiple ``run()`` calls would work, but
    the host driver opens a fresh sandbox per ``run_script`` tool
    call so each impl only sees one ``run()``.
    """

    def __init__(self, config: McpRunnerConfig) -> None:
        self._config = config

    def _target_type_annotation(self) -> Any:
        return McpRunnerProto

    def _service_type_id(self) -> FxtrID:
        return MCP_RUNNER_TYPE_ID

    @service_method
    async def run(self) -> dict[str, Any]:
        host = await self._config.host_ref.resolve()

        declared: dict[str, Any] = {}
        scheduled: list[dict[str, Any]] = []  # already wire-shaped

        def declare_value(key: str, value: Any) -> None:
            if not isinstance(key, str):
                raise TypeError(f"declare_value: key must be a string, got {type(key).__name__}")
            if key in declared:
                raise ValueError(f"declare_value: key {key!r} already declared")
            # JSON-shape gate: phase 1 only. Eventually this will accept
            # FxtrServiceData (refs/IDs/dataclasses) but for now we keep
            # the result wire-format trivially printable.
            try:
                json.dumps(value)
            except (TypeError, ValueError) as e:
                raise TypeError(
                    f"declare_value: value for key {key!r} is not " f"JSON-serializable: {e}"
                ) from e
            declared[key] = value

        def schedule_message(delay_seconds: float, message: str) -> None:
            if not isinstance(delay_seconds, (int, float)) or isinstance(delay_seconds, bool):
                raise TypeError(
                    "schedule_message: delay_seconds must be a number, got "
                    f"{type(delay_seconds).__name__}"
                )
            if not isinstance(message, str):
                raise TypeError(
                    "schedule_message: message must be a string, got " f"{type(message).__name__}"
                )
            scheduled.append({"delay_seconds": float(delay_seconds), "message": message})

        await run_preamble_and_script(
            self._config.preamble_src,
            self._config.script_src,
            builtins={
                "declare_value": declare_value,
                "schedule_message": schedule_message,
                # Bound async method on the resolved host proxy. From
                # the user script's perspective: a plain async callable;
                # the RPC bridge is invisible.
                "host_info": host.host_info,
            },
        )

        return {"declared": declared, "scheduled": scheduled}


async def run(config: McpRunnerConfig) -> ServiceRef[McpRunnerProto]:
    """Entry point: mount the per-spawn impl on the active dispatch scope."""
    return get_active_scope().mount(_GuestImpl(config))
