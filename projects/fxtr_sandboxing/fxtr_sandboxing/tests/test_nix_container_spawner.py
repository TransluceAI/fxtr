"""End-to-end spawn tests for the `NixContainerServiceDefn` arm (git-ref path).

The container sibling of `tests/test_nix_spawner.py`: `prepare_workdir_repo`
builds a throwaway workdir repo from the `container_greeter` fixture template
(a port of `examples/container_rpc_task/task_src` onto the nix-built image
path), then we construct a `NixContainerServiceDefn` host-side, `spawn` it
through a `create_sandbox_environment` configured with `git_repo_path` =
that workdir, and exercise the greeter. The spawner resolves the defn via
`fxtr_sandboxing.nix` (kind check + `build_image` against the workdir's git
ref), the nix-runner backend's `deliver_image` loads the image into the
container runtime (returning the ref + `--platform`), and
`fxtr_sandboxing.nix_container_runner` runs it with that ref/platform.

Also covers the `kind` discriminator mismatch (a `NixWasmServiceDefn`
pointed at the container attr fails with an error naming the right defn
type).

The spawn test sweeps over all supported container runtimes via the
`container_runtime` fixture (see `conftest.py`) — podman and Apple
Container exercise different delivery + socket-plumbing surfaces;
runtimes not on PATH skip. The nix builds offload to a
remote builder and the image is a full-fxtr2 Linux venv, so cold-cache runs
are slow — hence `@pytest.mark.deadline(seconds=...)`. Set
`FXTR_TEST_LIB_REV` to a committed rev to run against uncommitted library
work (see `fxtr_workdir_harness`); the default pins the library to git HEAD.
Host-env note: emulated (non-native-arch) images additionally need a guest
kernel whose auxv the host's binfmt translator understands — see the
"Rosetta auxv bug" finding in
`projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md`.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from typing import Protocol

import pytest
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.services.typed_service import service_protocol
from fxtr_nix_runner.perf import collect_perf
from fxtr_sandbox_types import NixContainerServiceDefn, NixWasmServiceDefn, SandboxConnectProto
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_workdir_harness import prepare_workdir_repo

pytestmark = [pytest.mark.anyio, pytest.mark.nix_runtime]

_CONTAINER_GREETER_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "container_greeter"
_CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"

# Mirror of the fixture's `container_rpc_task` types. Same UUID +
# define_type strings → same wire-form types, so host and guest
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
    async def platform_info(self) -> dict[str, str]: ...


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.deadline(seconds=900)
async def test_spawn_nix_container_service_greet(tmp_path: Path, container_runtime: str) -> None:
    """Build + spawn `fxtr.services.greeter` (container kind), connect, call.

    Spawns twice off one env: the second spawn exercises the image-cache
    path (the runtime's image store already has the content-addressed tag,
    so the skopeo copy + load are skipped).

    Also asserts the **warm-builder** guarantee: the builder is started at most
    once across both spawns (0 on the host nix backend, which has no builder; 1
    on the podman backend, held warm by the spawner's `keep_builder_warm` pin —
    never per-spawn). See `plans/stage2/2p4_persistent_spawner/plan.md`.
    """
    async with FxtrScope():
        prepared = await prepare_workdir_repo(_CONTAINER_GREETER_TEMPLATE, tmp_path / "wd")
        cfg_ref = GreetingConfig(greeting="hello", punctuation="!").store()

        async with (
            create_sandbox_environment(cache_dir=_CACHE_DIR, git_repo_path=prepared.path) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            defn_ref = NixContainerServiceDefn(
                git_commit=prepared.head_commit,
                flake_attr="fxtr.services.greeter",
            ).store()

            with collect_perf() as events:
                sandbox_ref = await spawner.spawn(defn_ref)
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                )
                sandbox = await sandbox_ref.resolve()
                user_ref = (await sandbox.connect(cfg_ref)).replace_service_type(GreeterProto)
                user = await user_ref.resolve()

                assert await user.greet("world") == "hello, world!"
                assert await user.add(2, 3) == 5
                info = await user.platform_info()
                assert info["system"] == "Linux"

                # Second spawn: image already in the runtime's store (the
                # content-addressed tag matches), so delivery is a cache hit.
                sandbox_ref2 = await spawner.spawn(defn_ref)
                sandbox_ref2 = sandbox_ref2.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                )
                sandbox2 = await sandbox_ref2.resolve()
                cfg_ref2 = GreetingConfig(greeting="hi", punctuation=".").store()
                user2_ref = (await sandbox2.connect(cfg_ref2)).replace_service_type(GreeterProto)
                user2 = await user2_ref.resolve()
                assert await user2.greet("again") == "hi, again."

            builder_starts = sum(1 for e in events if e.name == "builder.start")
            assert builder_starts <= 1, (
                f"builder started {builder_starts}x across two spawns; the warm-builder "
                "pin isn't holding it across spawns"
            )
            # Build-pipeline cache: the second spawn of the same defn reuses the
            # first's delivered image — eval/build/deliver run exactly once, and
            # the second spawn records a cache hit.
            assert sum(1 for e in events if e.name == "nix.eval.kind") == 1
            assert sum(1 for e in events if e.name == "nix.build.image") == 1
            assert sum(1 for e in events if e.name == "spawn.cache_hit") == 1


@pytest.mark.deadline(seconds=900)
async def test_spawn_kind_mismatch_raises(tmp_path: Path) -> None:
    """A `NixWasmServiceDefn` pointed at a container-kind attr is rejected
    with an error naming the right defn type (and vice-versa shape).

    Not swept over `container_runtime`: the kind check happens at nix-eval
    time, before any container runtime is touched (so no runtime needed
    at all — only nix)."""
    async with FxtrScope():
        prepared = await prepare_workdir_repo(_CONTAINER_GREETER_TEMPLATE, tmp_path / "wd")

        async with (
            create_sandbox_environment(cache_dir=_CACHE_DIR, git_repo_path=prepared.path) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            wrong_defn = NixWasmServiceDefn(
                git_commit=prepared.head_commit,
                flake_attr="fxtr.services.greeter",
            ).store()
            with pytest.raises(Exception, match="NixContainerServiceDefn"):
                await spawner.spawn(wrong_defn)


async def test_spawn_nix_container_without_git_repo_raises() -> None:
    """A spawner constructed without `git_repo_path` rejects the nix arm."""
    async with FxtrScope():
        defn_ref = NixContainerServiceDefn(
            git_commit="0" * 40, flake_attr="fxtr.services.greeter"
        ).store()
        async with (
            create_sandbox_environment(cache_dir=_CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            with pytest.raises(Exception, match="git_repo_path"):
                await spawner.spawn(defn_ref)
