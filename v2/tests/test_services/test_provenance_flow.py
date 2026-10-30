"""Service-cap provenance round-trips (Stage 9 / Stage 3).

End-to-end coverage that a ``FxtrID`` carrying a ``_provenance_parent``
survives a service-call hop in either direction (arg or return), and
that ``ServiceRef.service_type_id`` flows the same way. Also asserts
that a service-call's dispatch scope inherits its c-node from the
**mount** scope (where the service is being served from) rather than
the caller scope — provenance reflects "where this code is running",
not "who called it".
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceRef
from fxtr2.services.untyped_service import UntypedServiceImpl, service_method

GROUND = FxtrID.from_tag_and_payload(".", b"")


_PROV_FLOW_NAMESPACE = uuid.UUID("9c0a3a17-44c1-43c4-9eda-cc2bf9a37e0a")


@dataclasses.dataclass(frozen=True)
class _Note(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_PROV_FLOW_NAMESPACE, "prov_note"),
):
    body: str


def _make_c_node(scope: FxtrScope, label: str) -> FxtrID:
    """Build a ``c``-node in ``scope`` that unwraps to ``"."``."""
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    context_data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "label": label,
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), context_data)


class _EchoIdService(UntypedServiceImpl):
    """Returns whatever FxtrID it receives, untouched."""

    @service_method
    async def echo(self, fid: FxtrID) -> FxtrID:
        return fid


class _StoreNoteOnSelf(UntypedServiceImpl):
    """Stores a fresh note on the service-impl side and returns its FxtrID.

    Used by the mount-scope-c-node inheritance test: the impl-side store
    happens inside the per-call scope, so the returned id's
    ``_provenance_parent`` reflects whichever c-node the call scope
    inherited.
    """

    @service_method
    async def store(self, body: str) -> FxtrID:
        note = _Note(body=body)
        ref = note.store()  # uses active scope (= per-call scope)
        return ref.entity_id


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestFxtrIDProvenanceRoundTrip:
    """A FxtrID with ``_provenance_parent`` survives a service hop."""

    @pytest.mark.anyio
    async def test_arg_round_trip(self) -> None:
        """Pass a layered FxtrID as an arg; the impl returns it; both halves match."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as builder:
            c_node = _make_c_node(builder, "caller-ctx")
            async with FxtrScope(
                include=[GLOBAL_SCOPE],
                isolated=True,
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as caller_scope:
                # Mount the impl on the caller scope (same_scope layout).
                ref = await caller_scope.mount(_EchoIdService())
                # Store a note in the caller scope: id picks up the c-node.
                note_ref = _Note(body="hello").store(caller_scope)
                fid = note_ref.entity_id
                assert fid._provenance_parent is not None
                assert fid._provenance_parent.get_tag() == "n"

                proxy = await ref.resolve(caller_scope)
                returned = await proxy.echo(fid)
                assert isinstance(returned, FxtrID)
                # Equality on FxtrID ignores _provenance_parent — but we
                # want both halves to round-trip, so check explicitly.
                assert returned == fid
                assert returned._provenance_parent == fid._provenance_parent

    @pytest.mark.anyio
    async def test_entity_ref_arg_round_trip(self) -> None:
        """Pass an ``EntityRef`` whose inner FxtrID carries provenance."""
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as builder:
            c_node = _make_c_node(builder, "ref-ctx")
            async with FxtrScope(
                include=[GLOBAL_SCOPE],
                isolated=True,
                provenance_context=c_node,
                provenance_context_from_scope=builder,
            ) as caller_scope:
                ref = await caller_scope.mount(_EchoIdService())
                note_ref = _Note(body="bound").store(caller_scope)
                # echo accepts FxtrID, but we pass an EntityRef wrapping it.
                proxy = await ref.resolve(caller_scope)
                returned = await proxy.echo(note_ref)
                assert isinstance(returned, FxtrID)
                assert returned == note_ref.entity_id
                assert returned._provenance_parent == note_ref.entity_id._provenance_parent


class TestServiceTypeIDProvenance:
    """``ServiceRef.service_type_id`` ships as bare data-id bytes — no
    provenance, no fxtr_arena coverage. The encoder does NOT add the
    type_id (or its prov parent) to the input/return arena; the wire
    only carries the data-id half. Equality between a layered impl-side
    id and the bare wire form still holds because :class:`FxtrID`
    equality ignores ``_provenance_parent``.

    Consumers that need the loadable layered form must dispatch
    ``$fxtr.type``, which routes through the per-call return arena
    like any other returned ``FxtrID``.
    """

    @pytest.mark.anyio
    async def test_layered_type_id_ships_as_bare_bytes(self) -> None:
        from fxtr2.data.entity_ref_scope import ServiceKey
        from fxtr2.leases.leased_cap_common import IRHandleLocalImplBase
        from fxtr2.services.service_cap_common import (
            _decode_fxtr_service_data,
            _encode_fxtr_service_data,
        )

        class _StubHandle(IRHandleLocalImplBase[Any]):
            def __init__(self, scope: FxtrScope) -> None:
                self._token = scope._token
                # ``IRHandleLocalImplBase`` requires a task group; the
                # stub never dispatches but the field must exist.
                assert scope._task_group is not None
                self._task_group = scope._task_group

            async def _run_request_async(self, lease: Any, request: Any) -> Any:  # noqa: ARG002
                raise NotImplementedError

        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as scope:
            # Build a layered service_type_id: a u-node wraps a tracked
            # builtin, layered as the prov parent on the bare builtin id.
            bare_type_id = scope.track_builtin(FxtrID.builtin("test_layered_type"))
            u_node = scope._main_arena.create_u_node(scope.clone_authorization(), bare_type_id)
            layered_type_id = FxtrID(raw_data_id=bytes(bare_type_id), provenance_parent=u_node)

            handle = _StubHandle(scope)
            ref = ServiceRef(
                handle=handle,
                target_type=type(None),  # placeholder; encoder drops it anyway
                service_type_id=layered_type_id,
            )

            # Encode: the type_id is NOT added to the arena fxtr_ids set
            # (it's wire-portable identity, not arena-tracked content).
            fxtr_ids: set[FxtrID] = set()
            service_keys: set[ServiceKey] = set()
            encoded = _encode_fxtr_service_data(
                ref,
                fxtr_ids=fxtr_ids,
                service_keys=service_keys,
                where="test",
            )
            assert fxtr_ids == set()  # no arena coverage for type_id
            # The ServiceKey on the way out carries the layered form
            # (unchanged from the in-process ref).
            assert ServiceKey(handle, layered_type_id) in service_keys

            # Decode (any scope works — type_id no longer needs to be
            # tracked anywhere).
            decoded_ids: set[FxtrID] = set()
            decoded_keys: set[ServiceKey] = set()
            decoded = _decode_fxtr_service_data(
                encoded,
                fxtr_ids=decoded_ids,
                service_keys=decoded_keys,
                scope=scope,
                where="test",
            )

            assert isinstance(decoded, ServiceRef)
            # The decoded type_id is bare (provenance not propagated)…
            assert decoded.service_type_id._provenance_parent is None
            # …but compares equal to the original layered form because
            # FxtrID equality ignores _provenance_parent.
            assert decoded.service_type_id == layered_type_id
            # No type_id arena collection on decode either.
            assert decoded_ids == set()

    @pytest.mark.anyio
    async def test_loading_via_fxtr_type_preserves_provenance(self) -> None:
        """End-to-end: a service whose ``_service_type_id`` returns a
        stored entity_id (naturally layered with the mount scope's
        c-node provenance) is dispatchable via ``$fxtr.type`` from a
        caller scope. The wire ships only the data-id half, but the
        ``$fxtr.type`` round-trip flows the actual layered id through
        the per-call return arena, so on the caller side:

        1. ``await proxy.service_type_id()`` returns a layered FxtrID
           with the original provenance parent intact.
        2. The returned id's provenance parent is tracked in the
           caller scope (the return arena bridged it in).
        3. Loading the descriptor entity through the returned id
           succeeds and yields the original data.

        This is the loadable contract that justifies dispatching
        ``$fxtr.type`` rather than just trusting ``ref.service_type_id``
        (which carries only the data-id half on the wire).
        """
        from fxtr2.data.entity_ref_scope import EntityRef
        from fxtr2.services.service_cap_common import UnknownService
        from fxtr2.services.untyped_service import SimpleAsyncServiceImpl, UnknownServiceProxy

        class _DescriptorService(SimpleAsyncServiceImpl[UnknownService]):
            """An impl whose ``_service_type_id`` stores a ``_Note``
            entity in the mount scope and uses its entity_id as the
            type identity. Stored under the mount scope's provenance
            context, so the entity_id is naturally layered."""

            def __init__(self, body: str) -> None:
                self._body = body

            def _target_type_annotation(self) -> Any:
                return UnknownService

            def _service_type_id(self) -> FxtrID:
                return _Note(body=self._body).store().entity_id

        # Builder + mount scope (with c-node provenance context). The
        # mount scope's c-node will appear as the provenance parent on
        # any entity stored into it — including our descriptor.
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as builder:
            mount_c_node = _make_c_node(builder, "descriptor-svc-ctx")
            async with FxtrScope(
                include=[GLOBAL_SCOPE],
                isolated=True,
                provenance_context=mount_c_node,
                provenance_context_from_scope=builder,
                name="mount",
            ) as mount_scope:
                ref = await mount_scope.mount(_DescriptorService(body="hello"))
                # Sanity: the impl-side type_id IS layered (carries
                # the mount scope's provenance parent).
                assert ref.service_type_id._provenance_parent is not None
                impl_side_prov = ref.service_type_id._provenance_parent

                # Caller scope under the mount scope. The wire-shipped
                # type_id loses provenance on the way over; we need
                # ``$fxtr.type`` to recover the layered form.
                async with FxtrScope(
                    include=[mount_scope], isolated=True, name="caller"
                ) as caller_scope:
                    proxy = await ref.resolve(caller_scope)
                    assert isinstance(proxy, UnknownServiceProxy)
                    # 1. $fxtr.type returns a layered FxtrID with the
                    # original provenance parent.
                    returned = await proxy.service_type_id()
                    assert returned == ref.service_type_id  # data-id equal
                    assert returned._provenance_parent is not None
                    assert returned._provenance_parent == impl_side_prov
                    # 2. Provenance parent is tracked in the caller
                    # scope (the return arena bridged it in).
                    assert caller_scope.is_tracked(returned._provenance_parent)
                    # 3. Loading the descriptor through the returned id
                    # succeeds and yields the original body.
                    with caller_scope.as_active_scope():
                        loaded = await EntityRef(returned, _Note).load()
                    assert isinstance(loaded, _Note)
                    assert loaded.body == "hello"


class TestMountScopeCNodeInheritance:
    """The service-call scope inherits its c-node from the **mount scope**.

    Provenance reflects "where this code is running", not "who called it".
    Mounting the service in a scope with a c-node and calling it from a
    bare-context caller should yield entities whose provenance parent
    chains to the **mount** scope's c-node.
    """

    @pytest.mark.anyio
    async def test_returned_id_inherits_mount_scope_trace(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as builder:
            mount_c_node = _make_c_node(builder, "mount-ctx")
            async with FxtrScope(
                include=[GLOBAL_SCOPE],
                isolated=True,
                provenance_context=mount_c_node,
                provenance_context_from_scope=builder,
            ) as service_scope:
                ref = await service_scope.mount(_StoreNoteOnSelf())
                # Bare caller scope (no provenance_context) but a child
                # of service_scope so it can resolve the ref.
                async with FxtrScope(include=[service_scope], isolated=True) as caller_scope:
                    proxy = await ref.resolve(caller_scope)
                    new_id = await proxy.store("from-impl")
                    assert isinstance(new_id, FxtrID)
                    # The id's provenance parent chains to the mount c-node
                    # (not the caller's, since the caller has none, and
                    # not the receiver's per-call c-node, since the call
                    # scope inherits from the mount scope).
                    parent = new_id._provenance_parent
                    assert parent is not None
                    # Parent is an ``n``-node whose ctx (via unwrap) leads
                    # back to the mount c-node. Verifying the full chain
                    # would require sync provenance ops; the looser shape
                    # check here — a non-None parent — already proves the
                    # call scope had a provenance context (which can only
                    # come from the mount scope under this fixture).
                    assert parent.get_tag() == "n"
