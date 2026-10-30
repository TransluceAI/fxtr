"""End-to-end tests for the direct (no-spawner) launcher.

Mirrors the shape of :mod:`tests.test_spawner` but bypasses
:class:`SandboxSpawnerImpl` and the bundle-driven RPC indirection.
Builds a long-lived ``eryx.SandboxFactory`` from the ``rpc_task``
example's site-packages and opens sandboxes off it via
:func:`create_direct_sandbox_environment`.

The factory is built per-test (rather than module-scoped) for now —
factory construction is ~3 s but the test suite is small. If we add
more direct-launcher tests we can hoist it into a fixture.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from typing import Protocol

import eryx
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types.protocols import SandboxConnectProto
from fxtr_sandboxing.deprecated.prepare import prepare
from fxtr_sandboxing.direct_launcher import create_direct_sandbox_environment
from fxtr_sandboxing.runner import (
    _check_and_maybe_repair_eryx_embedded_dir,
    build_site_packages_for_bundle,
)

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"


# Wire-compatible mirrors of `rpc_task.GreetingConfig` / `rpc_task.GreeterProto`
# (same UUID + define_type names). See tests/test_spawner.py for the full
# explanation.
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


async def _build_rpc_task_factory() -> tuple[eryx.SandboxFactory, str]:
    """Materialize the rpc_task bundle into a site-packages dir and build a
    factory over it. Returns ``(factory, entry_point)``.

    Uses the bundle path purely as a convenient way to assemble the right
    set of wheels + source files; the factory itself is what the direct
    launcher consumes — no TaskBundle entity is involved at run time.
    """
    import tempfile as _tempfile

    bundle_ref = await prepare(EXAMPLES_DIR / "rpc_task")
    bundle = await bundle_ref.load()
    site_packages = Path(_tempfile.mkdtemp(prefix="fxtr_site_packages_"))
    await build_site_packages_for_bundle(bundle, CACHE_DIR, site_packages)
    _check_and_maybe_repair_eryx_embedded_dir()
    factory = eryx.SandboxFactory(site_packages=str(site_packages))
    return factory, bundle.entry_point


async def test_direct_launcher_open_connect_greet():
    """End-to-end: build factory, open env, open sandbox, connect, call greet."""
    async with FxtrScope():
        factory, entry_point = await _build_rpc_task_factory()
        cfg_ref = GreetingConfig(greeting="hello", punctuation="!").store()

        async with create_direct_sandbox_environment(factory=factory) as env:
            async with env.open_sandbox(entry_point=entry_point) as sandbox_ref:
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                )
                sandbox = await sandbox_ref.resolve()
                user_ref_any = await sandbox.connect(cfg_ref)
                user_ref = user_ref_any.replace_service_type(GreeterProto)
                user = await user_ref.resolve()

                result = await user.greet("world")
                assert result == "hello, world!", f"got {result!r}"


async def test_direct_launcher_factory_reuse_across_sandboxes():
    """One factory, two back-to-back sandboxes — proves the factory is
    actually reusable and that teardown of the first sandbox doesn't
    poison the second.

    Each ``open_sandbox`` CM exit must fully tear down its sandbox before
    returning (per-call FxtrScope drains → bridge releases → rpc scope
    drains → run_in_factory exits → eryx process dies). If teardown were
    broken we'd hang in either CM exit or in the second open_sandbox.
    """
    async with FxtrScope():
        factory, entry_point = await _build_rpc_task_factory()
        cfg_a = GreetingConfig(greeting="hi", punctuation=".").store()
        cfg_b = GreetingConfig(greeting="hey", punctuation="?").store()

        async with create_direct_sandbox_environment(factory=factory) as env:
            async with env.open_sandbox(entry_point=entry_point) as sandbox_ref:
                typed = sandbox_ref.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                )
                sandbox = await typed.resolve()
                user_ref = (await sandbox.connect(cfg_a)).replace_service_type(GreeterProto)
                user = await user_ref.resolve()
                assert await user.greet("alice") == "hi, alice."

            async with env.open_sandbox(entry_point=entry_point) as sandbox_ref:
                typed = sandbox_ref.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                )
                sandbox = await typed.resolve()
                user_ref = (await sandbox.connect(cfg_b)).replace_service_type(GreeterProto)
                user = await user_ref.resolve()
                assert await user.greet("bob") == "hey, bob?"


# --------- volume / sys_path_additions test ---------

# Wire-compatible mirrors for the volume-mounted entry-point package below.
# Same pattern as RPC_TASK_NS / GreeterProto: distinct UUID namespace so this
# never collides with the rpc_task example types in the snapshot.
EXTRA_PKG_NS = uuid.UUID("c1b8a4f6-2a4c-4d8e-9b1c-7f3e5a9d8b2a")


@service_protocol
class EchoProto(Protocol):
    async def echo(self, value: str) -> str: ...


_VOLUME_PKG_SRC = """\
\"\"\"Tiny entry-point package mounted via volumes= for the direct launcher
test. Lives outside the factory's site-packages — only reachable because the
launcher inserts its mount point into sys.path.\"\"\"

import uuid
from typing import Any, Protocol

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

EXTRA_PKG_NS = uuid.UUID("c1b8a4f6-2a4c-4d8e-9b1c-7f3e5a9d8b2a")
ECHO_TYPE_ID = system_entities.define_type(EXTRA_PKG_NS, "echo")


@service_protocol
class EchoProto(Protocol):
    async def echo(self, value: str) -> str: ...


class _Impl(TypedServiceImpl[EchoProto]):
    def _target_type_annotation(self) -> Any:
        return EchoProto

    def _service_type_id(self) -> FxtrID:
        return ECHO_TYPE_ID

    @service_method
    async def echo(self, value: str) -> str:
        # Prefix proves the call really hit code from the mounted volume,
        # not something baked into the factory's site-packages.
        return f"volume-echo:{value}"


async def run(_config: None) -> ServiceRef[EchoProto]:
    return await get_active_scope().mount(_Impl())
"""


async def test_direct_launcher_with_volume_mounted_entry_point(tmp_path):
    """Entry-point package lives in a host dir mounted via ``volumes=``.

    Proves the warm-spawn path the MCP plugin will use: factory provides
    the stable platform (fxtr2-portable + adapter, here via the rpc_task
    site-packages), the actual user code is reached via a per-spawn
    volume mount + ``sys_path_additions``.
    """
    # Materialize a minimal `extra_pkg` python package on disk under tmp_path.
    pkg_dir = tmp_path / "extra_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text(_VOLUME_PKG_SRC)

    async with FxtrScope():
        # Reuse the rpc_task factory only to get fxtr2-portable + the adapter
        # in the snapshot; we don't import or call rpc_task itself.
        factory, _ = await _build_rpc_task_factory()

        async with create_direct_sandbox_environment(factory=factory) as env:
            async with env.open_sandbox(
                entry_point="extra_pkg:run",
                volumes=[(str(tmp_path), "/extra-pkgs", True)],
                sys_path_additions=["/extra-pkgs"],
            ) as sandbox_ref:
                typed = sandbox_ref.replace_service_type(SandboxConnectProto[None, EchoProto])
                sandbox = await typed.resolve()
                user_ref = (await sandbox.connect(None)).replace_service_type(EchoProto)
                user = await user_ref.resolve()
                assert await user.echo("hello") == "volume-echo:hello"


async def test_direct_launcher_add():
    """Method that doesn't touch the config — round-trips through the bridge."""
    async with FxtrScope():
        factory, entry_point = await _build_rpc_task_factory()
        cfg_ref = GreetingConfig(greeting="hi", punctuation=".").store()

        async with create_direct_sandbox_environment(factory=factory) as env:
            async with env.open_sandbox(entry_point=entry_point) as sandbox_ref:
                typed = sandbox_ref.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                )
                sandbox = await typed.resolve()
                user_ref = (await sandbox.connect(cfg_ref)).replace_service_type(GreeterProto)
                user = await user_ref.resolve()
                assert await user.add(3, 7) == 10
