"""Container greeter service fixture for the NixContainerServiceDefn spawn tests.

Ported from ``examples/container_rpc_task/task_src`` (the deprecated
registry-hosted container demo) onto the nix-built image path. Mirrors the
WASM ``greeter`` fixture's ``GreeterProto`` / ``EntityRef[GreetingConfig]``
config shape, plus a ``platform_info()`` method so tests can assert the
call really crossed the container boundary (macOS host → Linux container).

The same RPC_TASK_NS UUID + define_type strings as the other greeter
demos, so host tests can mount one shared ``GreeterProto`` definition and
use it against any of the runtimes.
"""

from __future__ import annotations

import dataclasses
import platform
import uuid
from typing import Any, Protocol

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

# Same UUID + define_type strings as examples/rpc_task — produces the same
# wire-form fxtr_type FxtrIDs even though the Python classes are distinct.
RPC_TASK_NS = uuid.UUID("3a6b1d05-9e7f-4a82-b1c3-eef9a52d0c8b")

GREETER_TYPE_ID = system_entities.define_type(RPC_TASK_NS, "greeter")


@dataclasses.dataclass(frozen=True)
class GreetingConfig(
    DataclassEntity,
    fxtr_type=system_entities.define_type(RPC_TASK_NS, "greeting_config"),
):
    """Per-connect config for the greeter service."""

    greeting: str
    punctuation: str


@service_protocol
class GreeterProto(Protocol):
    """Protocol the greeter service serves."""

    async def greet(self, name: str) -> str: ...
    async def add(self, a: int, b: int) -> int: ...
    async def platform_info(self) -> dict[str, str]: ...


class _Impl(TypedServiceImpl[GreeterProto]):
    def __init__(self, cfg: GreetingConfig) -> None:
        self._cfg = cfg

    def _target_type_annotation(self) -> Any:
        return GreeterProto

    def _service_type_id(self) -> FxtrID:
        return GREETER_TYPE_ID

    @service_method
    async def greet(self, name: str) -> str:
        return f"{self._cfg.greeting}, {name}{self._cfg.punctuation}"

    @service_method
    async def add(self, a: int, b: int) -> int:
        return a + b

    @service_method
    async def platform_info(self) -> dict[str, str]:
        return {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "system": platform.system(),
        }


async def run(config: EntityRef[GreetingConfig]) -> ServiceRef[GreeterProto]:
    """fxtr.service entry point.

    The active scope at call time is the ``connect`` dispatch scope —
    args are already bridged in. Mount and return; the framework
    chain-bridges the ref back to the host caller.
    """
    cfg = await config.load()
    return await get_active_scope().mount(_Impl(cfg))
