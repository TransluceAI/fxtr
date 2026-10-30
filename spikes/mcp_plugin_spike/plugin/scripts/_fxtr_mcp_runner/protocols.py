"""Wire-format types shared between host (MCP server) and guest (sandbox).

Defines the service protocols and the ``McpRunnerConfig`` carrier that the
host passes to the sandbox via :class:`SandboxConnectProto.connect`. Both
sides import this module — on the host via the installed
:mod:`_fxtr_mcp_runner` package, in the sandbox via the volume-mounted copy.

Imports require ``fxtr2`` (host) or ``fxtr2-portable`` (sandbox). Same
namespace, same module-level ``define_type`` calls → same wire-form
``fxtr_type`` FxtrIDs on both sides.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr2.services.typed_service import service_protocol

MCP_RUNNER_NS = uuid.UUID("4f2e1a93-7c5d-4b8e-9af1-3d7c2e8a5b6f")

MCP_RUNNER_TYPE_ID: FxtrID = system_entities.define_type(MCP_RUNNER_NS, "mcp_runner")
MCP_HOST_TYPE_ID: FxtrID = system_entities.define_type(MCP_RUNNER_NS, "mcp_host")


@service_protocol
class McpHostProto(Protocol):
    """Host-side capability bridged into the sandbox via ``McpRunnerConfig``.

    Phase 1 has just one method — ``host_info`` — which returns a
    descriptive string. It exists primarily to validate the bidirectional
    bridge end-to-end and to give the entry point a place to wire the
    host ref through. Future methods will include the workstation hooks
    (``copy_entity``, ``get_entity``, etc.) sketched in
    ``projects/fxtr_workstation/plans/phase0/sketch.md``.
    """

    async def host_info(self) -> str: ...


@service_protocol
class McpRunnerProto(Protocol):
    """Guest-side service the MCP server invokes per ``run_script`` tool call.

    One method, ``run()``, executes the configured preamble + script
    and returns the collected ``(declared, scheduled)`` as a JSON-shaped
    dict so the host can apply scheduled side effects and surface
    declared values to the agent.

    The returned shape is::

        {
            "declared": {"<key>": <json-value>, ...},
            "scheduled": [
                {"delay_seconds": <float>, "message": "<string>"},
                ...
            ],
        }

    All values are JSON-serializable primitives (phase 1 limitation;
    enforced by ``declare_value`` itself).
    """

    async def run(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class McpRunnerConfig:
    """Per-spawn config the host hands the sandbox via ``connect``.

    Plain ``@dataclass(frozen=True)`` (not a fxtr ``DataclassEntity``) —
    same shape as ``examples/nested_relay_task``'s ``NestedRelayConfig``,
    because it carries a ``ServiceRef`` and entity-shaped types can't.

    Fields:
        preamble_src: Source of the user's ``preamble.py`` (raw string).
        script_src: Source of the per-call user script (raw string).
        host_ref: Bridged ``McpHostProto`` ref. Phase 1's entry point
            doesn't expose this to the user script; it's reserved for
            future workstation hooks. Kept on the config now so adding
            host-side calls doesn't break the wire format later.
    """

    preamble_src: str
    script_src: str
    host_ref: ServiceRef[McpHostProto]
