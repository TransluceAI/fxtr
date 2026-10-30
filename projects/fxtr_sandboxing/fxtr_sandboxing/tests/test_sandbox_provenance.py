"""End-to-end tests for sandbox provenance.

Spawn real sandboxes, call ``connect``, and verify the resulting
user-service refs carry a layered ``$fxtr.ctx`` marker whose c-node
unwraps to a :class:`SandboxProvenanceContext` with the expected
``bundle`` + ``config_snapshot``. Covers the user-facing properties:

- Per-call c-node + provenance flow into the user impl.
- ServiceRef leaves in config are replaced by stored
  :class:`ServiceConfigReplacementEntity` entries that capture each
  bridged service's ``$fxtr.type`` + ``$fxtr.ctx``.
- Escape-safety: an EntityRef pointing directly at a
  :class:`ServiceConfigReplacementEntity` in config is rejected before
  the user entrypoint runs. Wrapping it inside another DataclassEntity
  bypasses the check (by design — top-level walk only).
- Two ``connect`` calls on the same spawn produce *different*
  c-nodes when their configs differ.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from typing import Protocol

import anyio
import anyio.abc
import pytest
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import service_protocol
from fxtr2.services.untyped_service import UnknownServiceProxy
from fxtr_sandbox_types import (
    NetworkHandleAuthorizerProto,
    SandboxConnectProto,
    SandboxProvenanceContext,
    ServiceConfigReplacementEntity,
)
from fxtr_sandbox_types.protocols import NETWORK_HANDLE_AUTHORIZER_TYPE_ID
from fxtr_sandboxing.deprecated.prepare import prepare
from fxtr_sandboxing.spawner import create_sandbox_environment

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"

# Mirror of rpc_task / transport_task — same UUIDs so wire types match.
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


@service_protocol
class TransporterProto(Protocol):
    async def raw_echo(self, msg: str) -> str: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _read_marker_via_wire(user_ref: ServiceRef) -> FxtrID:  # type: ignore[type-arg]
    """Fetch the user-service ref's mount-scope marker via $fxtr.ctx."""
    untyped = user_ref.replace_service_type(UnknownService)
    proxy = await untyped.resolve()
    assert isinstance(proxy, UnknownServiceProxy)
    marker = await getattr(proxy, "$fxtr.ctx")()
    assert isinstance(marker, FxtrID), f"$fxtr.ctx returned non-FxtrID: {marker!r}"
    return marker


# ---------------------------------------------------------------------------
# 1. Smoke: provenance marker is set; c-node payload matches bundle
# ---------------------------------------------------------------------------


async def test_provenance_marker_set_on_user_ref():
    """The ServiceRef returned by ``connect`` has a layered ``$fxtr.ctx``
    whose ``_provenance_parent`` is a c-node, and the c-node unwraps to
    a :class:`SandboxProvenanceContext` whose ``bundle`` is the bundle we
    spawned with.
    """
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")
        cfg_ref = GreetingConfig(greeting="hi", punctuation=".").store()

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]) as bridge_scope,
        ):
            spawner = await env.spawner_ref.resolve()
            sandbox_ref = await spawner.spawn(bundle)
            sandbox_ref = sandbox_ref.replace_service_type(
                SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
            )
            sandbox = await sandbox_ref.resolve()
            user_ref = await sandbox.connect(cfg_ref)
            user_ref_t = user_ref.replace_service_type(GreeterProto)

            # Sanity: dispatch still works.
            user = await user_ref_t.resolve()
            assert await user.greet("world") == "hi, world."

            # Marker shape: bare "." with a c-node as _provenance_parent.
            marker = await _read_marker_via_wire(user_ref)
            assert marker.get_tag() == "."
            assert marker._provenance_parent is not None
            c_node = marker._provenance_parent
            assert c_node.get_tag() == "c"

            # Load the c-node's d-node payload as SandboxProvenanceContext.
            # The c-node was bridged into bridge_scope (the active scope at
            # the resolve call), so query against that scope's arena.
            ctx_data_id = bridge_scope.c_node_to_context_data(c_node)
            prov_ref: EntityRef[SandboxProvenanceContext] = EntityRef(
                entity_id=ctx_data_id, target_type=SandboxProvenanceContext
            )
            prov = await prov_ref.load()
            assert isinstance(prov, SandboxProvenanceContext)
            assert prov.bundle.entity_id == bundle.entity_id


# ---------------------------------------------------------------------------
# 2. ServiceRef in config → ServiceConfigReplacementEntity in snapshot
# ---------------------------------------------------------------------------


async def test_service_ref_in_config_replaced_in_snapshot():
    """``transport_task`` takes a ``ServiceRef[NetworkHandleAuthorizerProto]``
    as its config. After ``connect``, the c-node payload's
    ``config_snapshot`` should be a single RawStrongFxtrID pointing at a
    :class:`ServiceConfigReplacementEntity` whose ``service_type_id``
    matches the authorizer's type-id.
    """
    from fxtr2.data.entity_core import RawStrongFxtrID

    def _sync_handler(stream: anyio.abc.ByteStream) -> None:
        # Drop the stream — we're not exercising transport here, just
        # need a ServiceRef to bridge into the config.
        stream.aclose  # noqa: B018 — referenced just to keep ref alive

    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "transport_task")
        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]) as bridge_scope,
        ):
            authorizer_ref = await env.add_raw_transport(_sync_handler)

            spawner = await env.spawner_ref.resolve()
            sandbox_ref = await spawner.spawn(bundle)
            sandbox_ref = sandbox_ref.replace_service_type(
                SandboxConnectProto[ServiceRef[NetworkHandleAuthorizerProto], TransporterProto]
            )
            sandbox = await sandbox_ref.resolve()
            user_ref = await sandbox.connect(authorizer_ref)

            marker = await _read_marker_via_wire(user_ref)
            c_node = marker._provenance_parent
            assert c_node is not None and c_node.get_tag() == "c"

            ctx_data_id = bridge_scope.c_node_to_context_data(c_node)
            prov_ref: EntityRef[SandboxProvenanceContext] = EntityRef(
                entity_id=ctx_data_id, target_type=SandboxProvenanceContext
            )
            prov = await prov_ref.load()
            # Config was a single ServiceRef → snapshot is a single
            # RawStrongFxtrID leaf pointing at a SCRE.
            snapshot = prov.config_snapshot
            assert isinstance(
                snapshot, RawStrongFxtrID
            ), f"expected RawStrongFxtrID snapshot, got {type(snapshot).__name__}"
            scre_ref: EntityRef[ServiceConfigReplacementEntity] = EntityRef(
                entity_id=snapshot.inner_id,
                target_type=ServiceConfigReplacementEntity,
            )
            scre = await scre_ref.load()
            assert isinstance(scre, ServiceConfigReplacementEntity)
            # service_type_id should be the authorizer's type id.
            assert scre.service_type_id == NETWORK_HANDLE_AUTHORIZER_TYPE_ID


# ---------------------------------------------------------------------------
# 3. Per-connect provenance: two connects → different c-nodes for different configs
# ---------------------------------------------------------------------------


async def test_per_connect_different_c_nodes_for_different_configs():
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")
        cfg_a = GreetingConfig(greeting="hi", punctuation="!").store()
        cfg_b = GreetingConfig(greeting="hello", punctuation=".").store()

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

            user_ref_a = await sandbox.connect(cfg_a)
            marker_a = await _read_marker_via_wire(user_ref_a)
            c_node_a = marker_a._provenance_parent

            user_ref_b = await sandbox.connect(cfg_b)
            marker_b = await _read_marker_via_wire(user_ref_b)
            c_node_b = marker_b._provenance_parent

            assert c_node_a is not None
            assert c_node_b is not None
            assert c_node_a != c_node_b, (
                "Different configs should produce different c-nodes " f"({c_node_a} vs {c_node_b})"
            )


# ---------------------------------------------------------------------------
# 4. Per-connect provenance: same config → same c-node (content-addressed)
# ---------------------------------------------------------------------------


async def test_per_connect_same_c_node_for_same_config():
    """Two connects with the same config produce the same c-node id —
    content-addressing makes provenance reproducible.
    """
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")
        cfg = GreetingConfig(greeting="hi", punctuation="!").store()

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

            ref1 = await sandbox.connect(cfg)
            ref2 = await sandbox.connect(cfg)
            marker1 = await _read_marker_via_wire(ref1)
            marker2 = await _read_marker_via_wire(ref2)
            assert marker1._provenance_parent == marker2._provenance_parent


# ---------------------------------------------------------------------------
# 5. Escape-safety: passing a SCRE EntityRef as config is rejected
# ---------------------------------------------------------------------------


async def test_escape_safety_direct_scre_rejected():
    """If the user smuggles a :class:`ServiceConfigReplacementEntity`
    ref directly into config, the walker rejects it before the user
    entrypoint runs.
    """
    async with FxtrScope() as outer_scope:
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")

        # Build a SCRE on the host. The host scope is reachable from the
        # sandbox call-scope via the wire arena.
        builtin_ctx = outer_scope.track_builtin(FxtrID.from_tag_and_payload(".", b""))
        builtin_type = outer_scope.track_builtin(FxtrID.builtin("fake-svc-type"))
        scre_ref = ServiceConfigReplacementEntity(
            service_type_id=builtin_type, service_ctx=builtin_ctx
        ).store()

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            sandbox_ref = await spawner.spawn(bundle)
            # Use the untyped wire shape so we can pass a forged
            # EntityRef[Any] without the dialect rejecting it.
            from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService

            sandbox_ref = sandbox_ref.replace_service_type(
                SandboxConnectProto[FxtrServiceData, UnknownService]
            )
            sandbox = await sandbox_ref.resolve()

            # The walker should raise SandboxProvenanceEscapeError
            # (surfaces as an IRException on the wire). The dispatch
            # body's error-encoding raises a corresponding IRException
            # on the caller side.
            from fxtr2.leases.leased_cap_ir import IRException

            with pytest.raises(IRException) as excinfo:
                await sandbox.connect(scre_ref.entity_id)
            # The wire-encoded error category comes from the exception
            # type name. The walker raises SandboxProvenanceEscapeError.
            assert "SandboxProvenanceEscapeError" in str(excinfo.value) or (
                excinfo.value.category == "SandboxProvenanceEscapeError"
            )


# ---------------------------------------------------------------------------
# 6. Escape-safety: wrapping a SCRE inside another entity bypasses the check
# ---------------------------------------------------------------------------


WRAPPER_NS = uuid.UUID("8f7a4e2c-3d6b-4e1a-9c5f-1e3d7a8b2c4e")


@dataclasses.dataclass(frozen=True)
class SCREWrapper(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WRAPPER_NS, "scre_wrapper"),
):
    """User-owned wrapper that hides a SCRE inside. The escape-safety
    check is top-level only; deliberately wrapping bypasses it.
    """

    inner: EntityRef[ServiceConfigReplacementEntity]


async def test_escape_safety_wrapping_bypasses_check():
    """Wrapping a SCRE inside another DataclassEntity the user owns
    is not rejected — the walker only inspects top-level FxtrServiceData
    leaves, not entity contents. This counts as deliberate opt-in.
    """
    async with FxtrScope() as outer_scope:
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")
        builtin_ctx = outer_scope.track_builtin(FxtrID.from_tag_and_payload(".", b""))
        builtin_type = outer_scope.track_builtin(FxtrID.builtin("fake-svc-type"))
        scre_ref = ServiceConfigReplacementEntity(
            service_type_id=builtin_type, service_ctx=builtin_ctx
        ).store()
        wrapper_ref = SCREWrapper(inner=scre_ref).store()

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            sandbox_ref = await spawner.spawn(bundle)
            # Sandbox entry-point declares EntityRef[GreetingConfig] but
            # we hand it an EntityRef pointing to an SCREWrapper. The
            # decoder will fail (wrong type tag) — but only *after* the
            # provenance walker accepts the config, which is what we
            # want to verify here.
            sandbox_ref = sandbox_ref.replace_service_type(
                SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
            )
            sandbox = await sandbox_ref.resolve()

            # The walker doesn't raise — it lets the EntityRef through
            # (top-level walk, doesn't recurse into entity contents).
            # The subsequent decode-against-annotation will then fail
            # because the wrapper is the wrong type. We want a
            # type-error from decode, NOT a SandboxProvenanceEscapeError
            # from the walker.
            from fxtr2.leases.leased_cap_ir import IRException

            with pytest.raises(IRException) as excinfo:
                # Deliberately wrong type at the user-facing signature
                # (SCREWrapper vs GreetingConfig) — the point of this
                # test is exercising the walker's escape-safety path,
                # not the per-entrypoint decode. We want the walker to
                # let this through, then dispatch to fail downstream
                # for some other reason.
                await sandbox.connect(wrapper_ref)  # type: ignore[arg-type]
            assert "SandboxProvenanceEscapeError" not in str(excinfo.value)
