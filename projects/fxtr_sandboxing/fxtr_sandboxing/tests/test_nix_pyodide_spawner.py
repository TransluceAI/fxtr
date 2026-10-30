"""End-to-end spawn tests for the `NixPyodideServiceDefn` arm (git-ref path).

The pyodide sibling of `tests/test_nix_spawner.py`: `prepare_workdir_repo`
builds a throwaway workdir from the `pyodide_greeter` fixture, we construct
a `NixPyodideServiceDefn` host-side, `spawn` it through
`create_sandbox_environment`, and exercise the greeter. The spawner
resolves the defn via `fxtr_sandboxing.nix` (`nix eval` entryPoint +
`nix build` sitePackages/runtimeBundle against the workdir's git ref) and
launches it inside a permission-locked deno subprocess.

Beyond the greet round trip this covers the two pyodide-specific claims:

- `validated_add` routes through **pydantic-core**, a PEP 783
  `pyemscripten_*_wasm32` binary wheel — proving the lock-driven builder
  selected the emscripten wheel and the extension module dlopens in-guest.
- `platform_info` reports `Emscripten/wasm32` — the guest really is
  pyodide, not a host interpreter.

Skips when `deno` is not on PATH (an optional host dependency — only the
pyodide kind needs it). See `test_nix_spawner.py` for the slow-build
caveats (same harness + nix setup).
"""

from __future__ import annotations

import dataclasses
import shutil
import uuid
from pathlib import Path
from typing import Protocol

import anyio
import pytest
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types import NixPyodideServiceDefn, NixWasmServiceDefn, SandboxConnectProto
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_workdir_harness import prepare_workdir_repo

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.nix_runtime,
    pytest.mark.skipif(
        shutil.which("deno") is None,
        reason="pyodide backend needs deno on PATH",
    ),
]

_PYODIDE_GREETER_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "pyodide_greeter"
_CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"

# Mirror of the fixture's `pyodide_rpc_task` types. Same UUID + define_type
# strings → same wire-form types (the established cross-process pattern).
PYODIDE_TASK_NS = uuid.UUID("7c2e91d4-5b0a-4f6e-8d13-a94b7c05e2f1")


@dataclasses.dataclass(frozen=True)
class GreetingConfig(
    DataclassEntity,
    fxtr_type=system_entities.define_type(PYODIDE_TASK_NS, "greeting_config"),
):
    greeting: str
    punctuation: str


@service_protocol
class PyodideGreeterProto(Protocol):
    async def greet(self, name: str) -> str: ...
    async def validated_add(self, a: str, b: str) -> int: ...
    async def platform_info(self) -> str: ...


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.deadline(seconds=600)
async def test_spawn_nix_pyodide_service_greet_and_binary_wheel(tmp_path: Path) -> None:
    """Build + spawn `fxtr.services.pyodide_greeter`, connect, exercise it."""
    async with FxtrScope():
        prepared = await prepare_workdir_repo(_PYODIDE_GREETER_TEMPLATE, tmp_path / "wd")
        cfg_ref = GreetingConfig(greeting="hello", punctuation="!").store()

        async with (
            create_sandbox_environment(cache_dir=_CACHE_DIR, git_repo_path=prepared.path) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            defn_ref = NixPyodideServiceDefn(
                git_commit=prepared.head_commit,
                flake_attr="fxtr.services.pyodide_greeter",
            ).store()
            sandbox_ref = await spawner.spawn(defn_ref)
            # spawn() above includes the (slow, cold-cache-sensitive) nix
            # eval/build — that stays under the SIGALRM deadline. From here
            # on it's live interaction; fail_after cancels cleanly through
            # the async CMs and gives a readable traceback (unlike SIGALRM).
            with anyio.fail_after(120):
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], PyodideGreeterProto]
                )
                sandbox = await sandbox_ref.resolve()
                user_ref = (await sandbox.connect(cfg_ref)).replace_service_type(
                    PyodideGreeterProto
                )
                user = await user_ref.resolve()

                assert await user.greet("world") == "hello, world!"
                # The PEP 783 claim: pydantic-core's compiled extension module
                # (a pyemscripten wheel from PyPI) imports and runs in-guest.
                assert await user.validated_add("40", "2") == 42
                # The guest really is pyodide.
                info = await user.platform_info()
                assert info.startswith("Emscripten/wasm32"), info


@pytest.mark.deadline(seconds=600)
async def test_spawn_nix_pyodide_kind_mismatch(tmp_path: Path) -> None:
    """A wasm-kind spawn reference against a pyodide attr fails the kind
    check with an error naming the right defn type."""
    async with FxtrScope():
        prepared = await prepare_workdir_repo(_PYODIDE_GREETER_TEMPLATE, tmp_path / "wd")

        async with (
            create_sandbox_environment(cache_dir=_CACHE_DIR, git_repo_path=prepared.path) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            wrong_defn_ref = NixWasmServiceDefn(
                git_commit=prepared.head_commit,
                flake_attr="fxtr.services.pyodide_greeter",
            ).store()
            with pytest.raises(Exception, match="NixPyodideServiceDefn"):
                await spawner.spawn(wrong_defn_ref)
