"""End-to-end spawn tests for the `NixWasmServiceDefn` arm (git-ref path),
plus the kind-agnostic `NixServiceDefn` auto-dispatch over the same fixture.

`prepare_workdir_repo` builds a throwaway workdir repo from the greeter
fixture template (a copy of `examples/rpc_task`), then we construct a
`NixWasmServiceDefn` host-side, `spawn` it through a `create_sandbox_
environment` configured with `git_repo_path` = that workdir, and exercise
the greeter. The spawner resolves the defn via `fxtr_sandboxing.nix`
(`nix eval` entryPoint + `nix build` sitePackages against the workdir's git
ref) and launches it in eryx.

This is the agent-facing spawn mechanism minus the MCP/run_script layer
(that E2E lives in the agent-plugin tests). Covers both the root-flake case
and the `flake_path` subpath case.

The nix builds offload to a remote builder and can exceed the default
per-test timeout on a cold cache, hence `@pytest.mark.deadline(seconds=...)`.
Set `FXTR_TEST_LIB_REV` to a committed rev to run against uncommitted library
work (see `fxtr_workdir_harness`); the default pins the library to git HEAD.
"""

from __future__ import annotations

import dataclasses
import shutil
import uuid
from pathlib import Path
from typing import Protocol

import pytest
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types import NixServiceDefn, NixWasmServiceDefn, SandboxConnectProto
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_workdir_harness import prepare_workdir_repo

pytestmark = [pytest.mark.anyio, pytest.mark.nix_runtime]

_GREETER_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "greeter"
_CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"

# Mirror of the fixture's `rpc_task.GreetingConfig` / `GreeterProto`. Same
# UUID + define_type strings → same wire-form types, so host and guest
# encode/decode interchangeably (the established cross-process pattern).
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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.deadline(seconds=600)
async def test_spawn_nix_wasm_service_greet(tmp_path: Path) -> None:
    """Root-flake case: build + spawn `fxtr.services.greeter`, connect, call."""
    async with FxtrScope():
        prepared = await prepare_workdir_repo(_GREETER_TEMPLATE, tmp_path / "wd")
        cfg_ref = GreetingConfig(greeting="hello", punctuation="!").store()

        async with (
            create_sandbox_environment(cache_dir=_CACHE_DIR, git_repo_path=prepared.path) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            defn_ref = NixWasmServiceDefn(
                git_commit=prepared.head_commit,
                flake_attr="fxtr.services.greeter",
            ).store()
            sandbox_ref = await spawner.spawn(defn_ref)
            sandbox_ref = sandbox_ref.replace_service_type(
                SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
            )
            sandbox = await sandbox_ref.resolve()
            user_ref = (await sandbox.connect(cfg_ref)).replace_service_type(GreeterProto)
            user = await user_ref.resolve()

            assert await user.greet("world") == "hello, world!"
            assert await user.add(2, 3) == 5


@pytest.mark.deadline(seconds=600)
async def test_spawn_nix_wasm_service_flake_subpath(tmp_path: Path) -> None:
    """`flake_path` case: the flake lives at a repo *subpath* (`&dir=svc`)."""
    # Assemble a template whose flake sits under `svc/` rather than at root.
    subpath_template = tmp_path / "subpath_template"
    subpath_template.mkdir()
    shutil.copytree(_GREETER_TEMPLATE, subpath_template / "svc")

    async with FxtrScope():
        prepared = await prepare_workdir_repo(subpath_template, tmp_path / "wd")
        cfg_ref = GreetingConfig(greeting="hi", punctuation=".").store()

        async with (
            create_sandbox_environment(cache_dir=_CACHE_DIR, git_repo_path=prepared.path) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            defn_ref = NixWasmServiceDefn(
                git_commit=prepared.head_commit,
                flake_attr="fxtr.services.greeter",
                flake_path="svc",
            ).store()
            sandbox_ref = await spawner.spawn(defn_ref)
            sandbox_ref = sandbox_ref.replace_service_type(
                SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
            )
            sandbox = await sandbox_ref.resolve()
            user_ref = (await sandbox.connect(cfg_ref)).replace_service_type(GreeterProto)
            user = await user_ref.resolve()

            assert await user.greet("there") == "hi, there."


@pytest.mark.deadline(seconds=600)
async def test_spawn_auto_kind_service(tmp_path: Path) -> None:
    """`NixServiceDefn` auto-dispatch: the spawner evals the attr's `kind`
    ("wasm" for the greeter fixture) and runs the matching arm; a second
    auto spawn of the same triple hits the per-env kind cache (asserted
    indirectly: it must not error and connects fine)."""
    async with FxtrScope():
        prepared = await prepare_workdir_repo(_GREETER_TEMPLATE, tmp_path / "wd")
        cfg_ref = GreetingConfig(greeting="hey", punctuation="?").store()

        async with (
            create_sandbox_environment(cache_dir=_CACHE_DIR, git_repo_path=prepared.path) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            defn_ref = NixServiceDefn(
                git_commit=prepared.head_commit,
                flake_attr="fxtr.services.greeter",
            ).store()
            for name, expected in [("world", "hey, world?"), ("again", "hey, again?")]:
                sandbox_ref = await spawner.spawn(defn_ref)
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                )
                sandbox = await sandbox_ref.resolve()
                user_ref = (await sandbox.connect(cfg_ref)).replace_service_type(GreeterProto)
                user = await user_ref.resolve()
                assert await user.greet(name) == expected


async def test_spawn_auto_kind_without_git_repo_raises(tmp_path: Path) -> None:
    """The auto arm needs the configured repo before it can eval the kind."""
    async with FxtrScope():
        defn_ref = NixServiceDefn(git_commit="0" * 40, flake_attr="fxtr.services.greeter").store()
        async with (
            create_sandbox_environment(cache_dir=_CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            with pytest.raises(Exception, match="git_repo_path"):
                await spawner.spawn(defn_ref)


async def test_spawn_nix_wasm_without_git_repo_raises(tmp_path: Path) -> None:
    """A spawner constructed without `git_repo_path` rejects the nix arm."""
    async with FxtrScope():
        defn_ref = NixWasmServiceDefn(
            git_commit="0" * 40, flake_attr="fxtr.services.greeter"
        ).store()
        async with (
            create_sandbox_environment(cache_dir=_CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            with pytest.raises(Exception, match="git_repo_path"):
                await spawner.spawn(defn_ref)
