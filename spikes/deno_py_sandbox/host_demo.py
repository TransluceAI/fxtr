"""End-to-end greeter demo for the pyodide-in-deno sandbox spike.

Drives the standard spawn→connect→typed-service flow (the ``rpc_task``
greeter) against a pyodide guest via :func:`host_common.open_deno_sandbox`.

Run from the fxtr_sandboxing package env:

    cd projects/fxtr_sandboxing/fxtr_sandboxing
    uv run python ../../../spikes/deno_py_sandbox/host_demo.py

Prereqs: ``./build_guest_site_packages.sh`` has been run, and ``deno`` is on
PATH with a warm pyodide npm cache (the deno subprocess is granted no
network; warm the cache once with any ``deno run -A`` of main.ts if needed).
"""

from __future__ import annotations

import dataclasses
import sys
import time
import uuid
from typing import Protocol

import anyio
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types import SandboxConnectProto

from host_common import open_deno_sandbox

# Wire-compatible mirrors of rpc_task's types (same namespace UUID + names →
# same wire-form type ids; see tests/test_spawner.py for the full story).
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


async def main() -> None:
    t0 = time.monotonic()
    async with FxtrScope():
        cfg_ref = GreetingConfig(greeting="hello", punctuation="!").store()

        async with open_deno_sandbox("rpc_task:run") as sandbox_ref:
            typed = sandbox_ref.replace_service_type(
                SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
            )
            sandbox = await typed.resolve()
            user_ref = (await sandbox.connect(cfg_ref)).replace_service_type(GreeterProto)
            user = await user_ref.resolve()
            t_connect = time.monotonic()
            print(f"[host] connected service in {t_connect - t0:.2f}s", file=sys.stderr)

            greeting = await user.greet("world")
            total = await user.add(3, 7)
            print(f"[host] greet -> {greeting!r}", file=sys.stderr)
            print(f"[host] add   -> {total}", file=sys.stderr)
            assert greeting == "hello, world!", greeting
            assert total == 10, total
            print(
                f"[host] round-trip OK ({time.monotonic() - t_connect:.3f}s for 2 calls)",
                file=sys.stderr,
            )


if __name__ == "__main__":
    anyio.run(main)
