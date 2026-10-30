"""End-to-end tests for ``UntypedServiceImpl`` / ``UnknownServiceProxy``.

Most tests are parametrised by a ``service_layout`` fixture that places
the mount + caller scopes in one of three configurations:

- ``same_scope``: a single scope mounts and resolves the impl. The
  call scope ends up as a descendant of the data's home; this is the
  simplest setup and exercises the dominated-antichain branch in
  ``_adopt_inheritance_arena_claim``.
- ``child_scope``: caller_scope is a child of service_scope. Resolve
  walks find the handle on the parent.
- ``sibling_attach``: service_scope and caller_scope are independent
  siblings; caller_scope explicitly registers the handle via
  ``attach_bridged_lowlevel_cap`` with an :class:`IndependentLocalBridge`
  (simulating what an RPC bridge will do in substage 7).

Future RPC layout will add a fourth ``params`` value to the fixture.
The same parametrization is the canonical place to verify "operations
on mounted services work as expected, regardless of how the caller
came to know the handle".
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, EntityRef, FxtrScope, ServiceKey, ServiceRef
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.untyped_service import (
    SimpleAsyncServiceImpl,
    UnknownServiceProxy,
    UntypedServiceImpl,
    service_method,
)

# ---------------------------------------------------------------------------
# Test entity type (registered at module import for service-data round-trips).
# ---------------------------------------------------------------------------


_UNTYPED_SERVICE_TYDEF_UUID = uuid.UUID("3a1f5b0e-0a2e-4d4e-8c5b-9f7d1e0c4a6b")


@dataclasses.dataclass(frozen=True)
class _Note(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_UNTYPED_SERVICE_TYDEF_UUID, "note"),
):
    body: str


# ---------------------------------------------------------------------------
# Sample impls
# ---------------------------------------------------------------------------


class _EchoService(UntypedServiceImpl):
    """Plain primitives in / out."""

    @service_method
    async def echo(self, value: Any) -> Any:
        return value

    @service_method
    async def add(self, a: int, b: int) -> int:
        return a + b

    @service_method
    async def kw(self, *, x: int = 0, y: int = 0) -> int:
        return x + y

    @service_method
    async def boom(self, msg: str) -> Any:
        raise ValueError(msg)

    @service_method
    async def raises_irexception(self) -> Any:
        raise IRException(message="hand-rolled", category="HandRolled")

    # Not tagged → not exposed.
    async def secret(self) -> str:
        return "should not be reachable"


class _NestedService(UntypedServiceImpl):
    @service_method
    async def round_trip(self, value: Any) -> Any:
        return value

    @service_method
    async def returns_non_data(self) -> Any:
        return object()  # not FxtrServiceData


class _OverridingService(_EchoService):
    @service_method
    async def echo(self, value: Any) -> Any:
        return ("override", value)


class _NoteService(UntypedServiceImpl):
    """Exercises FxtrID / EntityRef round-trips on the wire."""

    @service_method
    async def read_body(self, note_id: FxtrID) -> str:
        # The receiver gets a bare FxtrID; wrap with EntityRef for typed loading.
        ref: EntityRef[_Note] = EntityRef(note_id, _Note)
        loaded = await ref.load()
        return loaded.body

    @service_method
    async def first_body_in_list(self, note_ids: list[FxtrID]) -> str:
        ref: EntityRef[_Note] = EntityRef(note_ids[0], _Note)
        loaded = await ref.load()
        return loaded.body

    @service_method
    async def echo_id(self, note_id: FxtrID) -> FxtrID:
        # Pass-through return: the ID came from the input arena and is
        # also tracked on the caller's home scope, so this round-trips.
        return note_id

    @service_method
    async def reads_via_entity_ref(self, ref: EntityRef[_Note]) -> str:
        # Receiver sees a bare FxtrID even when sender passed an EntityRef.
        # The receiver wraps in a typed ref for loading.
        assert isinstance(ref, FxtrID)  # decoded to bare FxtrID
        typed: EntityRef[_Note] = EntityRef(ref, _Note)
        loaded = await typed.load()
        return f"got {loaded.body}"

    @service_method
    async def returns_new_id(self) -> FxtrID:
        # Stores a new entity in the call scope and returns its ID.
        # Substage 6 step 5 wires the chain-converter ret_ref +
        # ``fxtr_arena`` machinery so the caller can load the new
        # entity from its home scope after the call returns.
        new_ref = _Note(body="local").store()
        return new_ref.entity_id


class _SpawnerService(UntypedServiceImpl):
    """Mounts a new service inside the call body and returns its ``ServiceRef``.

    Exercises step 5's chain-converter return path for the
    ``call_scope.main`` token: the spawned ref's bridge on the dispatch
    scope is the identity-converter ``DependentLocalBridge`` from
    ``mount_lowlevel_cap_as_service``, so the wire chain converter
    reduces to plain ``derived.exchange_forward``.
    """

    @service_method
    async def spawn_echo(self) -> Any:
        scope = FxtrScope.get_active()
        return await scope.mount(_EchoService())

    @service_method
    async def spawn_with_peer(self, peer: Any) -> Any:
        """Spawn a fresh ``_EchoService`` after receiving a peer ref.

        ``peer`` is unused on the wire — its only role is to inject the
        peer's token into the dispatch scope's derived inputs, so that
        the returned ref's chain converter has ``input_tokens =
        [home_main, peer_token]``. When the caller's bridge for
        ``peer`` is itself a ``DependentLocalBridge`` /
        ``DependentPipelinedBridge`` (e.g. a previously spawned ref),
        the caller-side install walks through
        :class:`_DependentLocalMintSpec` / :class:`_DependentPipelinedMintSpec`
        while building the new chain bridge.
        """
        assert isinstance(peer, ServiceRef)
        scope = FxtrScope.get_active()
        return await scope.mount(_EchoService())

    @service_method
    async def store_note_with_peer(self, peer: Any, body: str) -> FxtrID:
        """Receives a ServiceRef arg and returns a fresh ``FxtrID``.

        The dispatch scope's derived inputs include both ``home_main``
        (from the caller's input arena lease) and the peer's token
        (from the caller's ServiceRef arg lease). The returned arena's
        chain converter therefore has multi-input
        (``input_tokens = [home_main, peer_token]``); the caller-side
        arena attach has to mint leases for *both* via its local
        derived ``D``.

        ``peer`` itself is unused — it just forces the multi-input
        shape on the dispatch scope.
        """
        assert isinstance(peer, ServiceRef)
        new_ref = _Note(body=body).store()
        return new_ref.entity_id

    @service_method
    async def spawn_echo_and_store_note(self, body: str) -> dict[str, Any]:
        """Mixed return: a fresh ServiceRef + a fresh FxtrID in one call.

        Both reference ``call_scope.main`` (the spawned service's mount
        token, and the forked arena's token). Exercises the
        encode-side branch where the same target token both has a
        chain-bridge install (from a service handle) and an arena
        attach (from a forked sub-arena).
        """
        scope = FxtrScope.get_active()
        spawned = await scope.mount(_EchoService())
        new_ref = _Note(body=body).store()
        return {"spawned": spawned, "note_id": new_ref.entity_id}


class _CallbackService(UntypedServiceImpl):
    """Receives a ``ServiceRef`` arg and dispatches a method on it.

    Exercises step 4: the caller passes a ServiceRef pointing at
    another mounted impl; the receiver bridges it into the call scope
    and the user method calls back through the proxy.
    """

    @service_method
    async def call_echo(self, peer: Any, value: Any) -> Any:
        # ``peer`` arrives as a ``ServiceRef[UnknownService]``; resolve
        # against the active scope (the call scope) and dispatch.
        assert isinstance(peer, ServiceRef)
        proxy = await peer.resolve()
        return await proxy.echo(value)

    @service_method
    async def call_add(self, peer: Any, a: int, b: int) -> int:
        assert isinstance(peer, ServiceRef)
        proxy = await peer.resolve()
        return await proxy.add(a, b)

    @service_method
    async def returns_arg_ref(self, peer: Any) -> Any:
        # Pass-through: return the ServiceRef arg unchanged. The proxy
        # validates it's in the input handle set.
        return peer


class _PeerCallback(UntypedServiceImpl):
    """A service that holds a peer ``ServiceRef`` and dispatches it on call.

    Used by the mutually-referencing test (the §5 "S1 ↔ S2 → returns
    callback" scenario). When the caller invokes ``callback``, the
    method resolves the held peer ref against the active scope (the
    per-call sub-scope) and forwards the value through ``echo`` —
    closing the loop back to whoever the peer was on the originating
    side.

    The peer ref is captured at construction; resolve happens at call
    time against the active scope, which walks parent bridges to find
    the peer's cap-bridge (installed when the peer was passed in as
    an arg to the surrounding call).
    """

    def __init__(self, peer: ServiceRef[Any]) -> None:
        self._peer = peer

    @service_method
    async def callback(self, value: Any) -> Any:
        proxy = await self._peer.resolve()
        return await proxy.echo(value)


class _CallbackMaker(UntypedServiceImpl):
    """Receives a peer ref and returns a fresh ``_PeerCallback`` closing over it.

    Hosted on the "B" side of the mutually-referencing scenario.
    """

    @service_method
    async def make_callback(self, peer: Any) -> Any:
        assert isinstance(peer, ServiceRef)
        scope = FxtrScope.get_active()
        return await scope.mount(_PeerCallback(peer))


# ---------------------------------------------------------------------------
# Layout-parametrising fixture and helper
# ---------------------------------------------------------------------------


_LAYOUTS = ("same_scope", "child_scope", "sibling_attach")


@pytest.fixture(params=_LAYOUTS)
def service_layout(request: pytest.FixtureRequest) -> str:
    """Parametrises tests across the three mount/caller layouts.

    Tests using this fixture run three times — once per layout — and
    the same dispatch behaviour is asserted in each. The future RPC
    variant will plug in here as a fourth value.
    """
    return request.param


def _make_ref_visible_from_caller(
    ref: ServiceRef[Any], service_scope: FxtrScope, caller_scope: FxtrScope
) -> None:
    """Ensure ``ref.handle`` is reachable from ``caller_scope``'s resolve walk.

    For ``same_scope`` and ``child_scope`` layouts the ref is already
    reachable (handle is mounted on a scope in the parent chain); the
    redundant attach here is idempotent at the bridge-dict level. For
    ``sibling_attach`` the explicit attach is required.

    Used by tests that mount a *second* impl alongside the parametrized
    one and need it visible from the caller side too.
    """
    caller_scope.track_builtin(ref.service_type_id)
    caller_scope.attach_bridged_lowlevel_cap(
        ref.handle,
        ref.target_type,
        ref.service_type_id,
        IndependentLocalBridge(lease=service_scope.clone_authorization()),
    )


@asynccontextmanager
async def mounted_service(
    impl: UntypedServiceImpl, layout: str
) -> AsyncIterator[tuple[ServiceRef[Any], FxtrScope, FxtrScope]]:
    """Yield ``(ref, service_scope, caller_scope)`` for the given layout.

    All three layouts produce a working ``ref.resolve(caller_scope)``
    flow; tests typically only need ``caller_scope`` for storing data
    and resolving the proxy. ``service_scope`` is exposed so tests can
    assert on the impl-side scope when relevant.

    For ``same_scope``, ``service_scope is caller_scope``.
    """
    if layout == "same_scope":
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(impl)
            yield ref, scope, scope
    elif layout == "child_scope":
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as service_scope:
            ref = await service_scope.mount(impl)
            async with FxtrScope(include=[service_scope], isolated=True) as caller_scope:
                yield ref, service_scope, caller_scope
    elif layout == "sibling_attach":
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as service_scope:
            ref = await service_scope.mount(impl)
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as caller_scope:
                # Bridge the handle into caller_scope so its resolve walk
                # finds it. The Lease[service_scope.token] is held by
                # caller_scope until it drains; the structural lifetime
                # ordering (caller exits first, then service) is enforced
                # by the nesting of `async with`s.
                caller_scope.track_builtin(ref.service_type_id)
                caller_scope.attach_bridged_lowlevel_cap(
                    ref.handle,
                    UnknownService,
                    ref.service_type_id,
                    IndependentLocalBridge(lease=service_scope.clone_authorization()),
                )
                yield ref, service_scope, caller_scope
    else:
        raise ValueError(f"Unknown layout: {layout!r}")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# ServiceRef value semantics — pure value, no service infrastructure needed
# ---------------------------------------------------------------------------


class TestServiceRefValue:
    def test_equality(self) -> None:
        from fxtr2.leases.lease_types import Lease, LifetimeToken
        from fxtr2.leases.leased_cap_ir import IRHandle, IRValue, Request, UnwrapForPipelining
        from fxtr2.leases.local_lease import UnconditionalLocalLifetime

        class _StubHandle(IRHandle[Any]):
            def __init__(self, token: LifetimeToken[Any]) -> None:
                self._token = token

            @property
            def token(self) -> LifetimeToken[Any]:
                return self._token

            async def request(
                self,
                lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],  # noqa: ARG002
                request: Request,  # noqa: ARG002
            ) -> IRValue:
                raise NotImplementedError

            def dispatch(
                self,
                lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],  # noqa: ARG002
                request: Request,  # noqa: ARG002
            ) -> Any:
                raise NotImplementedError

        with UnconditionalLocalLifetime("v").revoke_after_block() as lt:
            h1 = _StubHandle(lt.token)
            h2 = _StubHandle(lt.token)
            tid_a = FxtrID.builtin("untyped_service")
            tid_b = FxtrID.builtin("other_service")
            # Same handle, same target_type, same type_id → equal.
            assert ServiceRef(
                handle=h1, target_type=UnknownService, service_type_id=tid_a
            ) == ServiceRef(handle=h1, target_type=UnknownService, service_type_id=tid_a)
            # Different handle → unequal.
            assert ServiceRef(
                handle=h1, target_type=UnknownService, service_type_id=tid_a
            ) != ServiceRef(handle=h2, target_type=UnknownService, service_type_id=tid_a)
            # Same handle / target_type, different service_type_id → unequal.
            assert ServiceRef(
                handle=h1, target_type=UnknownService, service_type_id=tid_a
            ) != ServiceRef(handle=h1, target_type=UnknownService, service_type_id=tid_b)


# ---------------------------------------------------------------------------
# Mount / resolve plumbing — testing the mechanics themselves, not parametrised
# ---------------------------------------------------------------------------


class TestMountAndResolve:
    @pytest.mark.anyio
    async def test_mount_returns_serviceref(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(_EchoService())
            assert isinstance(ref, ServiceRef)
            assert ref.target_type is UnknownService

    @pytest.mark.anyio
    async def test_resolve_returns_proxy(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            assert isinstance(proxy, UnknownServiceProxy)

    @pytest.mark.anyio
    async def test_mount_rejects_lifespanless_scope(self) -> None:
        # A lifespanless scope has no task group and cannot host services.
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        try:
            with pytest.raises(RuntimeError, match="no task group"):
                await scope.mount(_EchoService())
        finally:
            scope.force_close()


# ---------------------------------------------------------------------------
# Method dispatch — primitives in / out (parametrised)
# ---------------------------------------------------------------------------


class TestPrimitiveCalls:
    @pytest.mark.anyio
    async def test_echo_int(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            assert await proxy.echo(42) == 42

    @pytest.mark.anyio
    async def test_echo_str(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            assert await proxy.echo("hello") == "hello"

    @pytest.mark.anyio
    async def test_echo_bytes(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            assert await proxy.echo(b"\x00\x01") == b"\x00\x01"

    @pytest.mark.anyio
    async def test_echo_none(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            assert await proxy.echo(None) is None

    @pytest.mark.anyio
    async def test_echo_bool(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            assert await proxy.echo(True) is True
            assert await proxy.echo(False) is False

    @pytest.mark.anyio
    async def test_echo_float(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            assert await proxy.echo(3.14) == 3.14

    @pytest.mark.anyio
    async def test_positional_args(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            assert await proxy.add(2, 3) == 5

    @pytest.mark.anyio
    async def test_keyword_args(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            assert await proxy.kw(x=10, y=32) == 42

    @pytest.mark.anyio
    async def test_nested_list(self, service_layout: str) -> None:
        async with mounted_service(_NestedService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            value: list[Any] = [1, "two", [3, [4]], {"five": 5}]
            assert await proxy.round_trip(value) == value

    @pytest.mark.anyio
    async def test_nested_dict(self, service_layout: str) -> None:
        async with mounted_service(_NestedService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            value: dict[str, Any] = {"a": 1, "b": [2, 3], "c": {"d": None}}
            assert await proxy.round_trip(value) == value


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    @pytest.mark.anyio
    async def test_python_exception_becomes_irexception(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            with pytest.raises(IRException) as exc_info:
                await proxy.boom("kaboom")
            assert exc_info.value.category == "ValueError"
            assert "kaboom" in exc_info.value.message

    @pytest.mark.anyio
    async def test_irexception_passes_through_unchanged(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            with pytest.raises(IRException) as exc_info:
                await proxy.raises_irexception()
            assert exc_info.value.category == "HandRolled"
            assert exc_info.value.message == "hand-rolled"

    @pytest.mark.anyio
    async def test_unknown_method_raises(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            with pytest.raises(IRException) as exc_info:
                await proxy.does_not_exist()
            assert exc_info.value.category == "UnknownMethod"

    @pytest.mark.anyio
    async def test_untagged_method_not_exposed(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            # `secret` is defined but not @service_method.
            with pytest.raises(IRException) as exc_info:
                await proxy.secret()
            assert exc_info.value.category == "UnknownMethod"

    @pytest.mark.anyio
    async def test_dunder_attributes_raise_attributeerror(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            with pytest.raises(AttributeError):
                proxy.__nonexistent_dunder__  # type: ignore[attr-defined]

    @pytest.mark.anyio
    async def test_non_data_arg_raises_locally(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            # Passing a non-FxtrServiceData arg surfaces as TypeError on
            # the proxy side before any wire activity.
            with pytest.raises(TypeError, match="FxtrServiceData"):
                await proxy.echo(object())

    @pytest.mark.anyio
    async def test_non_data_return_becomes_irexception(self, service_layout: str) -> None:
        async with mounted_service(_NestedService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            with pytest.raises(IRException) as exc_info:
                await proxy.returns_non_data()
            assert exc_info.value.category == "MalformedReturn"

    @pytest.mark.anyio
    async def test_malformed_fxtr_arena_rejected(self) -> None:
        """A non-IRHandle value in ``fxtr_arena`` rejects as MalformedPacket.

        Hand-builds the packet and bypasses the proxy's encoding step.
        Mount on the outer scope and resolve on a child to side-step
        the self-mount restriction (see
        ``plans/phase3/stage8c_user_caps/resolve_into_mount_scope_drain_cycle.md``)
        — the validation path under test is independent of scope
        topology.
        """
        from fxtr2.leases.leased_cap_ir import Call, IRTagged
        from fxtr2.services.service_cap_common import _FXTR_CALL_TAG

        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as service_scope:
            ref = await service_scope.mount(_EchoService())
            async with FxtrScope(include=[service_scope], isolated=True) as caller_scope:
                proxy = await ref.resolve(caller_scope)
                assert isinstance(proxy, UnknownServiceProxy)
                bad_packet = IRTagged(
                    _FXTR_CALL_TAG,
                    {
                        "args": [42],
                        "kwargs": {},
                        "leases": [],
                        "fxtr_arena": "not-actually-an-arena",  # not an IRHandle
                    },
                )
                with pytest.raises(IRException) as exc_info:
                    await ref.handle.request(
                        proxy._ctx.mint_lease_for_key(proxy._ctx.proxy_key),
                        Call("echo", bad_packet),
                    )
                assert exc_info.value.category == "MalformedPacket"


# ---------------------------------------------------------------------------
# Subclassing / overrides
# ---------------------------------------------------------------------------


class TestSubclassingOverrides:
    @pytest.mark.anyio
    async def test_subclass_override_takes_precedence(self, service_layout: str) -> None:
        async with mounted_service(_OverridingService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            # Tuples normalize to lists across the wire (IR's Sequence form).
            assert await proxy.echo("v") == ["override", "v"]
            # Inherited methods still reachable.
            assert await proxy.add(1, 2) == 3


# ---------------------------------------------------------------------------
# FxtrID / EntityRef round-trips
# ---------------------------------------------------------------------------


class TestFxtrIdRoundTrip:
    @pytest.mark.anyio
    async def test_fxtrid_arg_loaded_through_bridge(self, service_layout: str) -> None:
        async with mounted_service(_NoteService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            note_ref = _Note(body="hello").store(caller_scope)
            proxy = await ref.resolve(caller_scope)
            result = await proxy.read_body(note_ref.entity_id)
            assert result == "hello"

    @pytest.mark.anyio
    async def test_entityref_arg_decoded_to_fxtrid(self, service_layout: str) -> None:
        async with mounted_service(_NoteService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            note_ref = _Note(body="payload").store(caller_scope)
            proxy = await ref.resolve(caller_scope)
            # User passes the EntityRef; receiver sees a bare FxtrID
            # and re-wraps with the typed EntityRef on its side.
            result = await proxy.reads_via_entity_ref(note_ref)
            assert result == "got payload"

    @pytest.mark.anyio
    async def test_multiple_fxtrids_in_nested_container(self, service_layout: str) -> None:
        async with mounted_service(_NoteService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            r1 = _Note(body="first").store(caller_scope)
            r2 = _Note(body="second").store(caller_scope)
            proxy = await ref.resolve(caller_scope)
            result = await proxy.first_body_in_list([r1.entity_id, r2.entity_id])
            assert result == "first"

    @pytest.mark.anyio
    async def test_fxtrid_passthrough_return(self, service_layout: str) -> None:
        """The receiver returns the same FxtrID it was given.

        That ID is already tracked in the home scope, so the proxy's
        pass-through validation succeeds.
        """
        async with mounted_service(_NoteService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            note_ref = _Note(body="x").store(caller_scope)
            proxy = await ref.resolve(caller_scope)
            returned = await proxy.echo_id(note_ref.entity_id)
            assert isinstance(returned, FxtrID)
            assert returned == note_ref.entity_id

    @pytest.mark.anyio
    async def test_returning_new_fxtrid_loads_through_chain_bridge(
        self, service_layout: str
    ) -> None:
        """Receiver-side: a new FxtrID returned from the call is bridged
        into the caller's home scope via the chain-converter ret_ref +
        ``fxtr_arena``, and is loadable as an entity from the caller.
        ``same_scope`` is xfail'd by the fixture (see
        ``service_layout``).
        """
        with anyio.fail_after(10):
            async with mounted_service(_NoteService(), service_layout) as (
                ref,
                _,
                caller_scope,
            ):
                proxy = await ref.resolve(caller_scope)
                new_id = await proxy.returns_new_id()
                assert isinstance(new_id, FxtrID)
                # Load the entity from the caller's scope to confirm the
                # arena bridge works.
                typed: EntityRef[_Note] = EntityRef(new_id, _Note)
                loaded = await typed.load(caller_scope)
                assert loaded.body == "local"

    @pytest.mark.anyio
    async def test_passing_untracked_id_errors_locally(self, service_layout: str) -> None:
        """Proxy-side: a FxtrID not tracked in the home scope is rejected."""
        # Build an ID in an unrelated scope so the caller scope doesn't have it.
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as elsewhere_scope:
            note_ref = _Note(body="elsewhere").store(elsewhere_scope)
            stranger_id = note_ref.entity_id

            async with mounted_service(_NoteService(), service_layout) as (
                ref,
                _,
                caller_scope,
            ):
                proxy = await ref.resolve(caller_scope)
                # Auto-lift via fork_arena_handle_for_ids fails to
                # find the ID in any parent scope and raises.
                with pytest.raises(ValueError, match="not found"):
                    await proxy.read_body(stranger_id)

    @pytest.mark.anyio
    async def test_resolve_requires_task_group(self) -> None:
        """``resolve_service`` rejects lifespanless home scopes.

        Specific to a lifespanless caller, so not parametrised. Resolved
        proxies might need to fork arenas / spawn dispatch tasks, so the home
        scope must have an open task group.
        """
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as service_scope:
            ref = await service_scope.mount(_NoteService())
            # A lifespanless home scope has no task group and can't resolve.
            caller_scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
            try:
                with pytest.raises(RuntimeError, match="no task group"):
                    await ref.resolve(caller_scope)
            finally:
                caller_scope.force_close()


# ---------------------------------------------------------------------------
# ServiceRef args (step 4)
# ---------------------------------------------------------------------------


class TestServiceRefArg:
    """Caller passes a ``ServiceRef`` arg; receiver bridges it and dispatches.

    Each test mounts both services on the **same** ``service_scope``
    (so the resolve flow doesn't compound across mounts), but the
    ``service_layout`` fixture still varies how the ``CallbackService``'s
    handle is reachable from the caller.
    """

    @pytest.mark.anyio
    async def test_pass_serviceref_and_dispatch(self, service_layout: str) -> None:
        async with mounted_service(_CallbackService(), service_layout) as (
            cb_ref,
            service_scope,
            caller_scope,
        ):
            # Mount the peer (an EchoService) on the same service_scope
            # and make it visible from the caller side.
            echo_ref = await service_scope.mount(_EchoService())
            _make_ref_visible_from_caller(echo_ref, service_scope, caller_scope)
            cb_proxy = await cb_ref.resolve(caller_scope)
            result = await cb_proxy.call_echo(echo_ref, "ping")
            assert result == "ping"

    @pytest.mark.anyio
    async def test_pass_serviceref_with_primitives(self, service_layout: str) -> None:
        async with mounted_service(_CallbackService(), service_layout) as (
            cb_ref,
            service_scope,
            caller_scope,
        ):
            echo_ref = await service_scope.mount(_EchoService())
            _make_ref_visible_from_caller(echo_ref, service_scope, caller_scope)
            cb_proxy = await cb_ref.resolve(caller_scope)
            result = await cb_proxy.call_add(echo_ref, 7, 35)
            assert result == 42

    @pytest.mark.anyio
    async def test_serviceref_passthrough_return(self, service_layout: str) -> None:
        """The receiver returns the same ServiceRef it was passed.

        That handle is in the input handle set, so the proxy's
        pass-through validation succeeds and we get back an equal ref.
        """
        async with mounted_service(_CallbackService(), service_layout) as (
            cb_ref,
            service_scope,
            caller_scope,
        ):
            echo_ref = await service_scope.mount(_EchoService())
            _make_ref_visible_from_caller(echo_ref, service_scope, caller_scope)
            cb_proxy = await cb_ref.resolve(caller_scope)
            returned = await cb_proxy.returns_arg_ref(echo_ref)
            assert isinstance(returned, ServiceRef)
            assert returned == echo_ref
            # And it's still resolvable on the caller side.
            echo_proxy = await returned.resolve(caller_scope)
            assert await echo_proxy.echo("verify") == "verify"

    @pytest.mark.anyio
    async def test_unregistered_serviceref_arg_rejected(self, service_layout: str) -> None:
        """Passing a ServiceRef whose handle isn't in the home scope errors."""
        async with mounted_service(_CallbackService(), service_layout) as (
            cb_ref,
            _,
            caller_scope,
        ):
            # Mount an EchoService on an UNRELATED scope (not visible to
            # caller_scope's resolve walk). Trying to pass it should
            # error before any wire activity.
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as stranger_scope:
                stranger_ref = await stranger_scope.mount(_EchoService())
                cb_proxy = await cb_ref.resolve(caller_scope)
                with pytest.raises(ValueError, match="not registered"):
                    await cb_proxy.call_echo(stranger_ref, "x")


# ---------------------------------------------------------------------------
# Spawner pattern — mount a new service inside the call, return its ref
# ---------------------------------------------------------------------------


class TestSpawnerPattern:
    """Step-5 chain-converter return path for fresh ServiceRefs.

    The user method mounts a new service on the active scope (the
    dispatch ``call_scope``) and returns its ``ServiceRef``. Caller
    receives the ref bridged via the chain converter, resolves it,
    and invokes a method on the spawned service.
    """

    @pytest.mark.anyio
    async def test_spawn_and_invoke_echo(self, service_layout: str) -> None:
        async with mounted_service(_SpawnerService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            with anyio.fail_after(10):
                proxy = await ref.resolve(caller_scope)
                spawned_ref = await proxy.spawn_echo()
                assert isinstance(spawned_ref, ServiceRef)
                spawned_proxy = await spawned_ref.resolve(caller_scope)
                assert await spawned_proxy.echo("hello") == "hello"

    @pytest.mark.anyio
    async def test_multi_input_arena_attach(self, service_layout: str) -> None:
        """Returning a new FxtrID from a call that received a ServiceRef arg.

        Forces ``input_tokens = {home_main, peer_token}`` on the
        chain converter for the returned arena's token, exercising
        the multi-input path on the caller-side install.
        """
        async with mounted_service(_SpawnerService(), service_layout) as (
            ref,
            service_scope,
            caller_scope,
        ):
            # Mount a peer service whose ServiceRef we pass as an arg.
            peer_impl = _EchoService()
            peer_ref = await service_scope.mount(peer_impl)
            _make_ref_visible_from_caller(peer_ref, service_scope, caller_scope)
            with anyio.fail_after(10):
                proxy = await ref.resolve(caller_scope)
                new_id = await proxy.store_note_with_peer(peer_ref, "multi")
                assert isinstance(new_id, FxtrID)
                typed: EntityRef[_Note] = EntityRef(new_id, _Note)
                loaded = await typed.load(caller_scope)
                assert loaded.body == "multi"

    @pytest.mark.anyio
    async def test_spawn_and_store_in_one_call(self, service_layout: str) -> None:
        """Single call returns both a fresh ServiceRef and a fresh FxtrID.

        Both share ``call_scope.main`` as the target token, so the
        return packet has one ``new_dep_leases`` entry plus an
        ``fxtr_arena``. Caller installs the chain bridge for the
        ServiceRef and the lazily-attach arena bridge in the same
        ``_install_returned_resources`` pass.
        """
        async with mounted_service(_SpawnerService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            with anyio.fail_after(10):
                proxy = await ref.resolve(caller_scope)
                result = await proxy.spawn_echo_and_store_note("mixed")
                assert isinstance(result, dict)
                spawned_ref = result["spawned"]
                note_id = result["note_id"]
                assert isinstance(spawned_ref, ServiceRef)
                assert isinstance(note_id, FxtrID)
                # Use the spawned service.
                spawned_proxy = await spawned_ref.resolve(caller_scope)
                assert await spawned_proxy.echo("ping") == "ping"
                # Load the new entity through the bridged arena.
                typed: EntityRef[_Note] = EntityRef(note_id, _Note)
                loaded = await typed.load(caller_scope)
                assert loaded.body == "mixed"

    @pytest.mark.anyio
    async def test_chain_of_chain_returns(self, service_layout: str) -> None:
        """Multi-level chain: spawn_with_peer takes a previously-spawned ref.

        Walks the ``_DependentPipelinedMintSpec`` branch in
        :func:`_build_chain_bridge`. Sequence:

        1. ``spawn_echo()`` → ``first_ref`` chain-bridged in caller
           (``DependentPipelinedBridge`` whose at-rest leases include
           K_call_1 and a ``_UseBaseSpec`` for ``home_main``).
        2. ``spawn_with_peer(first_ref)`` runs in a fresh
           ``call_scope_2`` whose derived inputs are ``{K_call_2,
           home_main, first_ref.token}``. The returned
           ``second_ref``'s chain converter therefore has
           ``input_tokens = [home_main, first_ref.token]``.
        3. Caller installs a chain bridge for ``second_ref``. For
           ``first_ref.token``, the input bridge is
           ``DependentPipelinedBridge``, so the install snapshots its
           ``leases`` into the merged dict and stores a
           ``_DependentPipelinedMintSpec``.
        4. Resolving + invoking ``second_ref`` mints a chain-of-chain
           lease: the outer chain converter dispatches the inner
           chain converter (via the spec's ``inner_converter``)
           to get a pipelined ``Lease[first_ref.token]``, then
           pipelines that as one of the ``Call("convert", [...])``
           args for the outer wire converter.
        """
        async with mounted_service(_SpawnerService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            with anyio.fail_after(10):
                proxy = await ref.resolve(caller_scope)
                first_ref = await proxy.spawn_echo()
                assert isinstance(first_ref, ServiceRef)
                # Pass first_ref as peer; second_ref's chain bridge
                # therefore depends on first_ref's chain bridge.
                second_ref = await proxy.spawn_with_peer(first_ref)
                assert isinstance(second_ref, ServiceRef)
                second_proxy = await second_ref.resolve(caller_scope)
                # Each invocation walks the chain-of-chain mint path.
                assert await second_proxy.echo("nested") == "nested"
                # Repeat to make sure subsequent dispatches don't
                # exhaust any one-shot pipelined state.
                assert await second_proxy.add(2, 3) == 5

    @pytest.mark.anyio
    async def test_multi_input_arena_attach_with_dependent_peer(self, service_layout: str) -> None:
        """Variant of ``test_multi_input_arena_attach`` where the peer is dependent-bridged.

        Exercises the chain-of-chain path on the *arena* attach (rather
        than on a service-handle install): the returned arena's
        chain converter has ``input_tokens = [home_main, peer_token]``,
        and ``peer_token`` lives behind a caller-side
        ``DependentPipelinedBridge``. The arena-attach pipeline therefore
        dispatches one inner chain (peer's wire converter) inside
        another (the arena's wire converter) on every wire call.
        """
        async with mounted_service(_SpawnerService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            with anyio.fail_after(10):
                proxy = await ref.resolve(caller_scope)
                # The peer is itself a chain-bridge return — its
                # caller-side bridge is `DependentPipelinedBridge`, not
                # Independent.
                peer_ref = await proxy.spawn_echo()
                assert isinstance(peer_ref, ServiceRef)
                new_id = await proxy.store_note_with_peer(peer_ref, "dep_peer")
                assert isinstance(new_id, FxtrID)
                typed: EntityRef[_Note] = EntityRef(new_id, _Note)
                loaded = await typed.load(caller_scope)
                assert loaded.body == "dep_peer"


# ---------------------------------------------------------------------------
# Mutually-referencing scopes — the §5 acceptance scenario
# ---------------------------------------------------------------------------


class TestMutuallyReferencingScopes:
    """Two scopes hosting services that reference each other through a returned ref.

    The scenario from §5 of part6_plan: S1 mounts A, S2 mounts B
    (a "callback maker"); S1 attaches B's ref. S1 calls B passing
    A's ref. B mounts a fresh callback service C closing over A's
    ref and returns it. Back on S1, dispatching C must walk back
    through B's call scope to A and call A.echo, returning the
    value to S1.

    The lease graph is a DAG (no cycle): C's lifetime is gated by
    B's call scope, which is gated by S1's main lease (the dispatch
    held it as input). A's lifetime is gated by S1 directly. When
    S1 drops its references on exit, everything drains in order.
    """

    @pytest.mark.anyio
    async def test_dag_drains_cleanly(self) -> None:
        # Two sibling mountable scopes — neither is the parent of the
        # other. Drain order matters: the *caller* (S1) holds a
        # ``Lease[S2.main]`` via its attach-bridge to B, so for the
        # nesting to drain cleanly S1 must close *before* S2. We
        # achieve that by making S2 the outer ``async with`` (closes
        # last) and S1 the inner one (closes first) — same convention
        # as ``mounted_service``'s ``sibling_attach`` layout.
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="S2") as scope_s2:
            b_ref = await scope_s2.mount(_CallbackMaker())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True, name="S1") as scope_s1:
                a_ref = await scope_s1.mount(_EchoService())
                # S1 attaches B's ref so it can dispatch B. The
                # Lease[S2.main] is held by S1 until S1 drains
                # (released in S1's _on_token_end teardown).
                scope_s1.track_builtin(b_ref.service_type_id)
                scope_s1.attach_bridged_lowlevel_cap(
                    b_ref.handle,
                    UnknownService,
                    b_ref.service_type_id,
                    IndependentLocalBridge(lease=scope_s2.clone_authorization()),
                )
                # The natural shape: S1 passes ``a_ref`` to
                # ``make_callback``. That installs A's bridge on B's
                # call scope as a Dependent (case-3 reverse) ref.
                # ``_PeerCallback`` is mounted on B's call scope and
                # captures ``a_ref``. When the caller later invokes
                # the returned callback, ``_PeerCallback.callback``'s
                # active scope is a sub-call scope of B's call scope;
                # its ``resolve`` walk finds A via the parent chain —
                # no S2-side attach to A needed.
                with anyio.fail_after(10):
                    b_proxy = await b_ref.resolve(scope_s1)
                    callback_ref = await b_proxy.make_callback(a_ref)
                    assert isinstance(callback_ref, ServiceRef)
                    callback_proxy = await callback_ref.resolve(scope_s1)
                    # Round-trip: S1 → C (S2) → A (S1) → return.
                    assert await callback_proxy.callback("ping") == "ping"
                    # Repeat once to confirm state is stable.
                    assert await callback_proxy.callback(42) == 42


# ---------------------------------------------------------------------------
# Drain / cleanup
# ---------------------------------------------------------------------------


class TestDrain:
    @pytest.mark.anyio
    async def test_drain_after_successful_calls(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            for i in range(5):
                assert await proxy.echo(i) == i
        # Scope exits drain; if any call leaked a lease the autouse
        # fixture would catch it.

    @pytest.mark.anyio
    async def test_drain_after_failing_calls(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            for _ in range(3):
                with pytest.raises(IRException):
                    await proxy.boom("oops")
            # And a successful call afterwards still works.
            assert await proxy.echo(42) == 42

    @pytest.mark.anyio
    async def test_concurrent_calls_drain_clean(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            results: list[int] = []

            async def call_one(n: int) -> None:
                results.append(await proxy.add(n, 1))

            async with anyio.create_task_group() as tg:
                for n in range(10):
                    tg.start_soon(call_one, n)

            assert sorted(results) == [n + 1 for n in range(10)]


# ---------------------------------------------------------------------------
# service_type_id behaviours (post-§2)
# ---------------------------------------------------------------------------


class TestServiceTypeIdNotTracked:
    """``service_type_id`` is wire-portable identity, not arena-tracked
    content — neither :meth:`mount_lowlevel_cap_as_service` nor
    :meth:`attach_bridged_lowlevel_cap` requires it to be tracked in
    the destination scope. Consumers wanting the loadable form
    dispatch ``$fxtr.type``.
    """

    @pytest.mark.anyio
    async def test_mount_accepts_untracked_type_id(self) -> None:
        """Mount succeeds even when the type_id has never been tracked
        in the mount scope (the framework no longer enforces this; impls
        choose for themselves whether to track for `$fxtr.type` purposes).
        """
        from fxtr2.services.service_cap_common import CommonServiceImplHandle

        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as scope:
            assert scope._task_group is not None
            handle = CommonServiceImplHandle(
                _EchoService(),
                scope,
                scope._task_group,
                None,
            )
            stranger_type_id = FxtrID.builtin("stranger_type")
            assert not scope.is_tracked(stranger_type_id)
            ref = scope.mount_lowlevel_cap_as_service(handle, UnknownService, stranger_type_id)
            assert ref.service_type_id == stranger_type_id

    @pytest.mark.anyio
    async def test_attach_accepts_untracked_type_id(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as src:
            ref = await src.mount(_EchoService())
            async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as dest:
                # ``ref.service_type_id`` is not tracked in dest, and
                # that's fine — attach succeeds anyway.
                assert not dest.is_tracked(ref.service_type_id)
                attached = dest.attach_bridged_lowlevel_cap(
                    ref.handle,
                    UnknownService,
                    ref.service_type_id,
                    IndependentLocalBridge(lease=src.clone_authorization()),
                )
                assert attached.service_type_id == ref.service_type_id
                # Still not tracked after attach.
                assert not dest.is_tracked(ref.service_type_id)


class TestUnknownServiceProxyTypeId:
    """The proxy's ``service_type_id()`` async method wire-dispatches
    ``$fxtr.type`` and returns the impl's type identity in loadable
    form (tracked in the home scope). The same id as an opaque hash is
    also accessible via ``proxy._ctx.service_type_id`` for advanced
    users that don't need to load it.
    """

    @pytest.mark.anyio
    async def test_proxy_exposes_type_id(self, service_layout: str) -> None:
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            assert isinstance(proxy, UnknownServiceProxy)
            # Wire-dispatched: matches the impl's id and the wire hash
            # on the original ServiceRef.
            loadable_type_id = await proxy.service_type_id()
            assert loadable_type_id == ref.service_type_id
            assert loadable_type_id == FxtrID.builtin("untyped_service")
            # The opaque hash on the context agrees too.
            assert proxy._ctx.service_type_id == loadable_type_id

    @pytest.mark.anyio
    async def test_proxy_type_id_tracked_in_home_scope(self, service_layout: str) -> None:
        """After ``$fxtr.type`` returns, the id is loadable from the
        home scope (the return-arena bridges it into the home arena)."""
        async with mounted_service(_EchoService(), service_layout) as (
            ref,
            _,
            caller_scope,
        ):
            proxy = await ref.resolve(caller_scope)
            type_id = await proxy.service_type_id()
            assert caller_scope.is_tracked(type_id)


class TestServiceTypeIdWireRoundTrip:
    """A ServiceRef sent over a service call carries its ``service_type_id``
    through the wire — the receiver decodes it correctly, and any rebuild
    on a return trip preserves it.
    """

    @pytest.mark.anyio
    async def test_serviceref_arg_preserves_type_id(self, service_layout: str) -> None:
        # Use _CallbackService.returns_arg_ref which echoes the ServiceRef
        # arg back unchanged — confirms the type_id round-trips both ways.
        async with mounted_service(_CallbackService(), service_layout) as (
            cb_ref,
            service_scope,
            caller_scope,
        ):
            # Mount a peer service alongside; make it visible to caller.
            peer_ref = await service_scope.mount(_EchoService())
            _make_ref_visible_from_caller(peer_ref, service_scope, caller_scope)

            cb_proxy = await cb_ref.resolve(caller_scope)
            returned: Any = await cb_proxy.returns_arg_ref(peer_ref)
            assert isinstance(returned, ServiceRef)
            # Same type_id on the way back as the way out.
            assert returned.service_type_id == peer_ref.service_type_id
            assert returned.service_type_id == FxtrID.builtin("untyped_service")
            # And the resolved proxy still has it.
            returned_proxy = await returned.resolve(caller_scope)
            assert isinstance(returned_proxy, UnknownServiceProxy)
            assert await returned_proxy.service_type_id() == peer_ref.service_type_id


class TestSimpleAsyncServiceImplLayer:
    """``SimpleAsyncServiceImpl[T]`` is the @service_method-discovery
    layer; subclasses can pin a non-UnknownService target type and
    drive a custom resolver. This is the seam typed services (§8)
    will plug into.
    """

    @pytest.mark.anyio
    async def test_custom_target_type_resolver(self) -> None:
        """A SimpleAsyncServiceImpl subclass with a non-UnknownService
        target type uses the registered resolver for that type at
        resolve time."""
        from fxtr2.data.entity_ref_scope import (
            ServiceProxyContext,
            ServiceTypeAnnotation,
            register_service_resolver,
        )

        class _MyTypedTarget:
            """Custom target — the resolver returns one of these."""

            def __init__(self, ctx: ServiceProxyContext) -> None:
                self.ctx = ctx

        async def _my_typed_resolver(
            context: ServiceProxyContext,
            target_type: ServiceTypeAnnotation[_MyTypedTarget],  # noqa: ARG001
        ) -> _MyTypedTarget:
            return _MyTypedTarget(context)

        register_service_resolver(_MyTypedTarget, _my_typed_resolver, allow_implicit_overrides=True)

        _MY_TYPE_ID = FxtrID.builtin("test_my_typed_service")

        class _MyTypedImpl(SimpleAsyncServiceImpl[_MyTypedTarget]):
            def _target_type_annotation(self) -> ServiceTypeAnnotation[_MyTypedTarget]:
                return _MyTypedTarget

            def _service_type_id(self) -> FxtrID:
                from fxtr2.data.entity_ref_scope import get_active_scope

                return get_active_scope().track_builtin(_MY_TYPE_ID)

            @service_method
            async def hello(self) -> str:
                return "hi"

        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as service_scope:
            ref = await service_scope.mount(_MyTypedImpl())
            # The ServiceRef carries our custom target_type and type_id.
            assert ref.target_type is _MyTypedTarget
            assert ref.service_type_id == _MY_TYPE_ID
            async with FxtrScope(include=[service_scope], isolated=True) as caller_scope:
                resolved = await ref.resolve(caller_scope)
                # Resolver returned our custom target type, not UnknownServiceProxy.
                assert isinstance(resolved, _MyTypedTarget)
                assert resolved.ctx.service_type_id == _MY_TYPE_ID


class TestServiceKeyCollisionOnSharedHandle:
    """Multiple ``ServiceKey``s sharing the same handle but with
    different ``service_type_id``s coexist independently — the unit
    of identity is ``ServiceKey``, not the bare handle.
    """

    @pytest.mark.anyio
    async def test_two_type_ids_per_handle(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as service_scope:
            ref = await service_scope.mount(_EchoService())
            tid_a = ref.service_type_id
            tid_b = service_scope.track_builtin(FxtrID.builtin("test_other_type"))

            # Re-register the same handle under a second type_id.
            second_ref = service_scope.attach_bridged_lowlevel_cap(
                ref.handle,
                UnknownService,
                tid_b,
                bridge=None,  # entry already exists from the mount
            )
            assert second_ref.service_type_id == tid_b
            assert ref.service_type_id == tid_a
            assert tid_a != tid_b

            # Both keys are in _known_handles.
            entry = service_scope._cap_bridges[ref.handle.token]
            assert ServiceKey(ref.handle, tid_a) in entry.handles
            assert ServiceKey(ref.handle, tid_b) in entry.handles

            # forget_service removes only the targeted key; the bridge
            # stays alive because the other key still uses it.
            service_scope.forget_service(ref)
            assert ServiceKey(ref.handle, tid_a) not in service_scope._known_handles
            assert ServiceKey(ref.handle, tid_b) in service_scope._known_handles
            # Entry still exists.
            assert ref.handle.token in service_scope._cap_bridges


class TestForgetServiceTypeIdMatching:
    """``forget_service`` matches on the full ``ServiceKey`` (handle +
    type_id). A ServiceRef with a wrong type_id is rejected even if
    the handle is registered under a different type_id.
    """

    @pytest.mark.anyio
    async def test_forget_with_wrong_type_id_raises(self) -> None:
        async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as scope:
            ref = await scope.mount(_EchoService())
            wrong_type_id = scope.track_builtin(FxtrID.builtin("test_wrong_type"))
            wrong_ref = ServiceRef(
                handle=ref.handle,
                target_type=UnknownService,
                service_type_id=wrong_type_id,
            )
            with pytest.raises(ValueError, match="not registered"):
                scope.forget_service(wrong_ref)
            # The original ref is still registered and forgettable.
            scope.forget_service(ref)
