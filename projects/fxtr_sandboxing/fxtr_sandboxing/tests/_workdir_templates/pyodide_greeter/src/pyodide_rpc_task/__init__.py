"""Greeter service fixture for the `NixPyodideServiceDefn` spawn tests.

Same shape as the wasi greeter fixture (`_workdir_templates/greeter/`), with
one deliberate addition: a `validated_add` method that routes through
**pydantic-core** — a PEP 783 `pyemscripten_*_wasm32` binary wheel — so the
E2E proves the lock-driven builder selected the emscripten wheel and the
extension module actually dlopens inside pyodide.

Built as `fxtr.services.pyodide_greeter` (see the sibling `flake.nix`) and
spawned via the git-ref path in `tests/test_nix_pyodide_spawner.py`, which
declares a wire-compatible host-side `PyodideGreeterProto` /
`GreetingConfig` (same `PYODIDE_TASK_NS` + `define_type` strings).
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

PYODIDE_TASK_NS = uuid.UUID("7c2e91d4-5b0a-4f6e-8d13-a94b7c05e2f1")

PYODIDE_GREETER_TYPE_ID = system_entities.define_type(PYODIDE_TASK_NS, "pyodide_greeter")


@dataclasses.dataclass(frozen=True)
class GreetingConfig(
    DataclassEntity,
    fxtr_type=system_entities.define_type(PYODIDE_TASK_NS, "greeting_config"),
):
    """Per-connect config for the greeter service."""

    greeting: str
    punctuation: str


@service_protocol
class PyodideGreeterProto(Protocol):
    """Protocol the pyodide greeter service serves."""

    async def greet(self, name: str) -> str: ...
    async def validated_add(self, a: str, b: str) -> int: ...
    async def platform_info(self) -> str: ...


class _Impl(TypedServiceImpl[PyodideGreeterProto]):
    def __init__(self, cfg: GreetingConfig) -> None:
        self._cfg = cfg

    def _target_type_annotation(self) -> Any:
        return PyodideGreeterProto

    def _service_type_id(self) -> FxtrID:
        return PYODIDE_GREETER_TYPE_ID

    @service_method
    async def greet(self, name: str) -> str:
        return f"{self._cfg.greeting}, {name}{self._cfg.punctuation}"

    @service_method
    async def validated_add(self, a: str, b: str) -> int:
        """Coerce string inputs through pydantic-core and add them.

        The point is the import: `pydantic_core._pydantic_core` is a
        compiled emscripten extension module from a PEP 783 wheel.
        """
        import pydantic_core

        validator = pydantic_core.SchemaValidator({"type": "int"})
        return validator.validate_python(a) + validator.validate_python(b)

    @service_method
    async def platform_info(self) -> str:
        import platform
        import sys

        return f"{platform.system()}/{platform.machine()}/py{sys.version_info.major}.{sys.version_info.minor}"


async def run(config: EntityRef[GreetingConfig]) -> ServiceRef[PyodideGreeterProto]:
    """fxtr.service entry point."""
    cfg = await config.load()
    return await get_active_scope().mount(_Impl(cfg))
