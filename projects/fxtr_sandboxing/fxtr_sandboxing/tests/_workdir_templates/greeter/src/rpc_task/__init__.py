"""Greeter service fixture for the `NixWasmServiceDefn` spawn tests.

A sandbox service parameterized by an `EntityRef[GreetingConfig]` config:
the host stores a `GreetingConfig` and passes its ref as the `connect`
config; the adapter decodes it against `run`'s annotated signature, and the
impl loads the entity and uses its fields to format responses (`greet` /
`add`).

Built as `fxtr.services.greeter` (see the sibling `flake.nix`) and spawned
via the git-ref path in `tests/test_nix_spawner.py`, which declares a
wire-compatible host-side `GreeterProto` / `GreetingConfig` (same
`RPC_TASK_NS` + `define_type` strings). Copied from `examples/rpc_task`.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Protocol

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

# UUID + define_type calls are evaluated once at module load. Both the host's
# test module and this sandbox-side module call `define_type` with the same
# (namespace_uuid, type_name) pair, so they produce the same wire-form
# `fxtr_type` FxtrID even though the Python classes are distinct.
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


async def run(config: EntityRef[GreetingConfig]) -> ServiceRef[GreeterProto]:
    """fxtr.service entry point.

    The active scope at call time is the ``connect`` dispatch scope —
    args are already bridged in. Mount and return; the framework
    chain-bridges the ref back to the host caller.
    """
    cfg = await config.load()
    return await get_active_scope().mount(_Impl(cfg))
