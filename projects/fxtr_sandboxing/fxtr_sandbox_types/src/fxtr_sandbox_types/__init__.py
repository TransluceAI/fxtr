"""Shared types and protocols for the fxtr sandbox spike.

Both the host (`fxtr_sandboxing`) and the sandbox-side runtime
(`fxtr_sandbox_rpc_adapter`) depend on this package so they can
agree on entity types (bundles, source mounts, ...) and service
protocols (`SandboxConnectProto`, `SandboxSpawnerProto`, ...) without
either side reaching into the other.

Pair this package with either `fxtr2` (host) or `fxtr2-portable`
(sandbox) — see the `[fxtr2]` and `[fxtr2-portable]` extras.
"""

from .binding_request import BindingConnectRequest, JSONValue
from .deprecated_entities import PySourcePackage, PyWheelPackage, TaskBundle
from .entities import (
    SERVICE_CONFIG_REPLACEMENT_TYPE_ID,
    DirectLauncherSentinel,
    NixContainerServiceDefn,
    NixPyodideServiceDefn,
    NixServiceDefn,
    NixWasmServiceDefn,
    SandboxProvenanceContext,
    ServiceConfigReplacementEntity,
)
from .json_object_stream import JSONObjectStream, JSONTypes
from .protocols import (
    SANDBOX_GUEST_IMPL_TYPE_ID,
    NetworkHandleAuthorizerProto,
    SandboxConnectProto,
    SandboxGuestImplProto,
    SandboxSpawnerProto,
    TokenWitnessProto,
)
from .service_context import SandboxServiceContext
from .socket_json_stream import SocketJSONStream

__all__ = [
    "SANDBOX_GUEST_IMPL_TYPE_ID",
    "SERVICE_CONFIG_REPLACEMENT_TYPE_ID",
    "BindingConnectRequest",
    "DirectLauncherSentinel",
    "JSONObjectStream",
    "JSONTypes",
    "JSONValue",
    "NetworkHandleAuthorizerProto",
    "NixContainerServiceDefn",
    "NixPyodideServiceDefn",
    "NixServiceDefn",
    "NixWasmServiceDefn",
    "PySourcePackage",
    "PyWheelPackage",
    "SandboxConnectProto",
    "SandboxGuestImplProto",
    "SandboxProvenanceContext",
    "SandboxServiceContext",
    "SandboxSpawnerProto",
    "ServiceConfigReplacementEntity",
    "SocketJSONStream",
    "TaskBundle",
    "TokenWitnessProto",
]
