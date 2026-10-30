"""Demo sandboxed service for ``basic_test_demo``.

A trivial hello-world greeter parameterized by a name. Runs
inside an eryx WASM sandbox spawned by a ``SandboxSpawnerProto``
service. The flake builds this directory as
``fxtr.services.hello_service`` (via ``mkWasmService``); the agent
spawns it by reference —
``NixWasmServiceDefn(git_commit=current_commit(), flake_attr="fxtr.services.hello_service")``
passed to the spawner via ``schedule_invoke``.

Config shape: a ``HelloConfig`` dataclass entity carrying the
``name`` to greet. The host stores it as a fxtr2 entity, passes
an :class:`EntityRef[HelloConfig]` as the ``connect`` arg; the
adapter decodes it against ``run``'s annotation before invoking
us.
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

#: Namespace UUID for this demo service. Stable across host/sandbox
#: so the wire-form ``fxtr_type`` FxtrID values match.
HELLO_SERVICE_NS = uuid.UUID("8f3c4a72-1b9d-4e2f-a0c5-6d8b2e9f3a47")

HELLO_TYPE_ID = system_entities.define_type(HELLO_SERVICE_NS, "hello_service")


@dataclasses.dataclass(frozen=True)
class HelloConfig(
    DataclassEntity,
    fxtr_type=system_entities.define_type(HELLO_SERVICE_NS, "hello_config"),
):
    """Per-spawn config: which name the service should greet."""

    name: str


@service_protocol
class HelloProto(Protocol):
    """Greeter protocol — one method that says hello to the configured name."""

    async def greet(self) -> str: ...


class _Impl(TypedServiceImpl[HelloProto]):
    def __init__(self, cfg: HelloConfig) -> None:
        self._cfg = cfg

    def _target_type_annotation(self) -> Any:
        return HelloProto

    def _service_type_id(self) -> FxtrID:
        return HELLO_TYPE_ID

    @service_method
    async def greet(self) -> str:
        return f"Hello, {self._cfg.name}!"


async def run(config: EntityRef[HelloConfig]) -> ServiceRef[HelloProto]:
    """``fxtr.service`` entry point.

    The active scope at call time is the ``connect`` dispatch
    scope — the config ref is already bridged in. We load the
    config, mount our impl on the scope, and return the ref;
    the framework chain-bridges it back to the caller (the
    workstation's invoke worker, for our use case).
    """
    cfg = await config.load()
    return await get_active_scope().mount(_Impl(cfg))
