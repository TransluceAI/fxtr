"""Wire-compatible mirror of the ``container_greeter`` fixture's types.

Same ``RPC_TASK_NS`` UUID + ``define_type`` strings as the fixture's
``container_rpc_task`` package (and ``tests/test_nix_container_spawner.py``),
so host and guest encode/decode the same wire-form types — the established
cross-process pattern. Kept in sync by hand (copy of the test's definitions).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Protocol

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.services.typed_service import service_protocol

RPC_TASK_NS = uuid.UUID("3a6b1d05-9e7f-4a82-b1c3-eef9a52d0c8b")


@dataclasses.dataclass(frozen=True)
class GreetingConfig(
    DataclassEntity,
    fxtr_type=system_entities.define_type(RPC_TASK_NS, "greeting_config"),
):
    greeting: str
    punctuation: str


@service_protocol
class GreeterProto(Protocol):
    async def greet(self, name: str) -> str: ...
    async def add(self, a: int, b: int) -> int: ...
    async def platform_info(self) -> dict[str, str]: ...
