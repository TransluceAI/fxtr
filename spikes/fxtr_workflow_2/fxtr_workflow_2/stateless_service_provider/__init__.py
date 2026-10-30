"""Stateless service providers: turn service-handle entities into running
services.

See ``plans/phase3/stage15_workflows/design/stateless_service_provider.md``.
"""

from __future__ import annotations

from .basic_provider import (
    BasicStatelessServiceProvider,
    BasicStatelessServiceProviderProto,
    UnknownHandleError,
    wrap_singleton,
)
from .handles import ExternalServiceHandle, ServiceHandle
from .protocols import (
    ExternalConnectorProto,
    StatelessServiceSpawnerProto,
    service_handle_target_type,
    spawn_resolved,
)

__all__ = [
    "BasicStatelessServiceProvider",
    "BasicStatelessServiceProviderProto",
    "ExternalConnectorProto",
    "ExternalServiceHandle",
    "ServiceHandle",
    "StatelessServiceSpawnerProto",
    "UnknownHandleError",
    "service_handle_target_type",
    "spawn_resolved",
    "wrap_singleton",
]
