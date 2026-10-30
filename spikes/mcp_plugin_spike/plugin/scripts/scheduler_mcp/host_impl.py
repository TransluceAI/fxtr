"""Host-side implementation of :class:`McpHostProto`.

Mounted on the MCP server's lifespan-owned :class:`FxtrScope`; the
ref is bridged into each sandbox via :class:`McpRunnerConfig.host_ref`.

Phase 1 has one method, ``host_info``, which returns a descriptive
string. The user script can't reach it directly yet (no host-callable
builtin); the wiring exists so the host can grow methods without
breaking the wire format.
"""

from __future__ import annotations

from typing import Any

from _fxtr_mcp_runner.protocols import MCP_HOST_TYPE_ID, McpHostProto
from fxtr2.data.entity_core import FxtrID
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method


class McpHostImpl(TypedServiceImpl[McpHostProto]):
    def __init__(self, info_string: str) -> None:
        self._info = info_string

    def _target_type_annotation(self) -> Any:
        return McpHostProto

    def _service_type_id(self) -> FxtrID:
        return MCP_HOST_TYPE_ID

    @service_method
    async def host_info(self) -> str:
        return self._info
