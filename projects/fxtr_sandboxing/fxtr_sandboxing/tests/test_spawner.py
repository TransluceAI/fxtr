"""Stage-1 end-to-end spawner tests against a real eryx sandbox.

Bundles `examples/rpc_task` with the new fxtr.service entry-point shape, opens
a sandbox environment, spawns the bundle, calls `connect` with various
`EntityRef[GreetingConfig]` configs, and exercises the resulting greeter
service. Validates:

- The full spawn → connect → user-method-call path through `open_rpc_scope`.
- Typed-config decoding (`EntityRef[GreetingConfig]` round-trip via
  `decode_from_service_data`).
- Multi-connect: one `spawn` ref serving two `connect` calls with different
  configs.
- Omitting `replace_service_type` after `spawn` fails on the first `connect`
  with `OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE` (placeholder dialect guard).
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from pathlib import Path
from typing import Protocol

import pytest
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.services.service_dialect import OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types.protocols import SandboxConnectProto
from fxtr_sandboxing.deprecated.prepare import prepare
from fxtr_sandboxing.spawner import create_sandbox_environment

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"


# Mirror of `rpc_task.GreetingConfig` and `rpc_task.GreeterProto`. Same UUID
# + type-name pair via `define_type` produces the same wire-form `fxtr_type`
# FxtrID, so the host's `GreetingConfig` and the sandbox's `GreetingConfig`
# encode/decode interchangeably even though they're separate Python classes.
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


async def test_spawn_then_connect_greet():
    """End-to-end: open env, spawn, connect with a config, call greet."""
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")
        cfg_ref = GreetingConfig(greeting="hello", punctuation="!").store()

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            sandbox_ref = await spawner.spawn(bundle)
            sandbox_ref = sandbox_ref.replace_service_type(
                SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
            )
            sandbox = await sandbox_ref.resolve()
            user_ref_any = await sandbox.connect(cfg_ref)
            user_ref = user_ref_any.replace_service_type(GreeterProto)
            user = await user_ref.resolve()

            result = await user.greet("world")
            assert result == "hello, world!", f"got {result!r}"


async def test_spawn_without_replace_fails_on_connect_with_placeholder_error():
    """``spawn`` yields ``SandboxConnectProto[OverrideThisTypeBeforeUsing, ...]``.
    Skipping ``replace_service_type`` leaves the connect codec's ``config``
    slot annotated with the placeholder; the first ``connect`` encode hits
    :class:`~fxtr2.services.service_dialect.OverridePlaceholderServiceStrategy`.
    """
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")
        cfg_ref = GreetingConfig(greeting="hello", punctuation="!").store()

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            sandbox_ref = await spawner.spawn(bundle)
            sandbox = await sandbox_ref.resolve()
            with pytest.raises(
                TypeError,
                match=re.escape(OVERRIDE_THIS_TYPE_BEFORE_USING_MESSAGE),
            ):
                await sandbox.connect(cfg_ref)  # type: ignore


async def test_spawn_once_connect_twice():
    """One spawn ref, two connects with different configs — each gets its own
    user-scope and impl instance using its own config."""
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")
        cfg_a = GreetingConfig(greeting="hi", punctuation=".").store()
        cfg_b = GreetingConfig(greeting="hey", punctuation="?").store()

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            sandbox_ref = await spawner.spawn(bundle)
            sandbox_ref = sandbox_ref.replace_service_type(
                SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
            )
            sandbox = await sandbox_ref.resolve()

            ref_a = (await sandbox.connect(cfg_a)).replace_service_type(GreeterProto)
            user_a = await ref_a.resolve()
            assert await user_a.greet("alice") == "hi, alice."

            ref_b = (await sandbox.connect(cfg_b)).replace_service_type(GreeterProto)
            user_b = await ref_b.resolve()
            assert await user_b.greet("bob") == "hey, bob?"


async def test_teardown_basic():
    """Two back-to-back sessions in one env: confirm the second spawn works
    cleanly after the first has fully torn down.

    Each session opens a sub-scope, spawns + connects, exercises the service,
    and exits. Exiting the sub-scope releases the chain bridge to call_scope,
    which propagates all the way down: sandbox_ref + connect_ref drop →
    open_rpc_scope drains → eryx sandbox process exits. If teardown were
    broken (hung tasks, stuck drains), either the sub-scope exit would block
    forever, the second spawn would fail, or the env CM exit would hang. We
    expect a fast, clean run.
    """
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")
        cfg_a = GreetingConfig(greeting="hi", punctuation=".").store()
        cfg_b = GreetingConfig(greeting="hey", punctuation="?").store()

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()

            async with FxtrScope(name="session-a"):
                sandbox_ref = await spawner.spawn(bundle)
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                )
                sandbox = await sandbox_ref.resolve()
                user_ref = (await sandbox.connect(cfg_a)).replace_service_type(GreeterProto)
                user = await user_ref.resolve()
                assert await user.greet("alice") == "hi, alice."
            # session-a drains here. Sandbox stack should fully tear down
            # before we proceed to session-b.

            async with FxtrScope(name="session-b"):
                sandbox_ref = await spawner.spawn(bundle)
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                )
                sandbox = await sandbox_ref.resolve()
                user_ref = (await sandbox.connect(cfg_b)).replace_service_type(GreeterProto)
                user = await user_ref.resolve()
                assert await user.greet("bob") == "hey, bob?"
        # env CM exits. No hung tasks expected.


async def test_add():
    """A non-config-touching method round-trips through the bridge cleanly."""
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")
        cfg_ref = GreetingConfig(greeting="hi", punctuation=".").store()

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            sandbox_ref = await spawner.spawn(bundle)
            sandbox_ref = sandbox_ref.replace_service_type(
                SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
            )
            sandbox = await sandbox_ref.resolve()
            user_ref = (await sandbox.connect(cfg_ref)).replace_service_type(GreeterProto)
            user = await user_ref.resolve()
            assert await user.add(3, 7) == 10
