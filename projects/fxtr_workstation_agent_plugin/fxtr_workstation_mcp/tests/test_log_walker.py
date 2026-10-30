"""Unit tests for :mod:`fxtr_workstation_mcp.log_walker`.

Drives the walker against a mock :class:`WorkstationServiceProto`
(only ``get_entity`` and ``get_service`` are exercised). No real
workstation, no /rpc plumbing — just verifies the wire-form →
typed-FrozenFxtrCbor substitution.

Covered:

- Primitive passthroughs (scalars, None, bytes).
- ``["entity", short_id]`` → :class:`RawWeakFxtrID` after
  ``get_entity`` resolves.
- ``["service", short_id]`` → :class:`RawStrongFxtrID` to a stored
  :class:`WorkstationServicePlaceholder` whose fields are populated
  by the ``$fxtr.type`` + ``$fxtr.ctx`` mock returns.
- ``["callback", N]`` → :class:`RawStrongFxtrID` to a stored
  :class:`OwnedCallbackPlaceholder` with the reverse-looked-up
  ``slot_key`` (and ``"<unknown>"`` fallback when the lookup misses).
- ``["bytes", b64]`` → decoded :class:`bytes`.
- ``[[inner]]`` literal-list-escape → tuple.
- Nested dicts / mixed structures.
- Per-walk caching: same short_id resolved once.
- Malformed shapes raise :class:`ValueError`.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from frozendict import frozendict
from fxtr2.data.entity_core import FxtrID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import NoOpService, service_method
from fxtr_workstation.protocols import WORKSTATION_SERVICE_TYPE_ID, WorkstationServiceProto

from fxtr_workstation_mcp.agent_log_types import (
    OwnedCallbackPlaceholder,
    WorkstationServicePlaceholder,
)
from fxtr_workstation_mcp.log_walker import walk_payload

pytestmark = pytest.mark.anyio


# --- Fixture + mocks --------------------------------------------------


@pytest.fixture
async def scope() -> AsyncIterator[FxtrScope]:
    """Fresh isolated scope, active for the test body. The walker
    stores intermediate placeholder entities into this scope; tests
    can ``await ref.load()`` them through the same scope."""
    async with FxtrScope(name="log_walker_test") as s:
        with s.as_active_scope():
            yield s


class _MockUntypedProxy:
    """Stub for the proxy returned by ``ServiceRef[Unknown].resolve()``.

    Exposes the two framework methods the walker calls:
    ``service_type_id`` (typed) and ``$fxtr.ctx`` (via getattr —
    untyped framework method).
    """

    def __init__(self, type_id: FxtrID, ctx_id: FxtrID) -> None:
        self._type_id = type_id
        self._ctx_id = ctx_id

    async def service_type_id(self) -> FxtrID:
        return self._type_id

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        if name == "$fxtr.ctx":
            return self._fxtr_ctx
        raise AttributeError(name)

    async def _fxtr_ctx(self) -> FxtrID:
        return self._ctx_id


class _MockServiceRef:
    """Stub for :class:`ServiceRef` returned by ``workstation.get_service``.

    The walker only ever calls ``replace_service_type`` + ``resolve``
    on this; nothing else.
    """

    def __init__(self, proxy: _MockUntypedProxy) -> None:
        self._proxy = proxy

    def replace_service_type(self, _type: Any) -> "_MockServiceRef":
        return self

    async def resolve(self, scope: FxtrScope | None = None) -> _MockUntypedProxy:  # noqa: ARG002
        return self._proxy


class _MockWorkstation:
    """Minimal :class:`WorkstationServiceProto` stand-in.

    ``get_entity`` resolves a short_id to a pre-registered FxtrID
    (the test sets up the mapping). ``get_service`` returns a
    :class:`_MockServiceRef` whose proxy answers
    ``service_type_id`` / ``$fxtr.ctx`` with pre-registered FxtrIDs.

    Tracks call counts on each method so tests can assert dedup
    behavior.
    """

    def __init__(self) -> None:
        self._entities: dict[str, FxtrID] = {}
        self._services: dict[str, tuple[FxtrID, FxtrID]] = {}
        self.get_entity_calls = 0
        self.get_service_calls = 0

    def register_entity(self, short_id: str, fid: FxtrID) -> None:
        self._entities[short_id] = fid

    def register_service(self, short_id: str, type_id: FxtrID, ctx_id: FxtrID) -> None:
        self._services[short_id] = (type_id, ctx_id)

    async def get_entity(self, short_id: str) -> FxtrID:
        self.get_entity_calls += 1
        return self._entities[short_id]

    async def get_service(self, short_id: str) -> ServiceRef[Any]:
        self.get_service_calls += 1
        type_id, ctx_id = self._services[short_id]
        proxy = _MockUntypedProxy(type_id, ctx_id)
        return _MockServiceRef(proxy)  # type: ignore[return-value]


class _MockWorkstationRef:
    """Duck-typed stand-in for
    ``ServiceRef[WorkstationServiceProto]`` that satisfies the
    walker's ``await self._workstation_ref.resolve(scope=temp)``
    call without forcing tests to spin up a real mounted workstation
    service. Returns the same :class:`_MockWorkstation` proxy on
    every resolve so the test's call-count assertions stay
    deterministic.

    These existing wire-form parsing tests do not exercise the
    scope-discipline aspects of the walker — that's the dedicated
    regression test :func:`test_walks_service_when_active_scope_is_global`,
    which uses a real :class:`ServiceRef` whose handle is registered
    in a mountable scope.
    """

    def __init__(self, proxy: _MockWorkstation) -> None:
        self._proxy = proxy

    async def resolve(self, scope: FxtrScope) -> _MockWorkstation:  # noqa: ARG002
        return self._proxy


def _ws(
    scope: FxtrScope, *, entity_ids: int = 0, service_ids: int = 0
) -> tuple["_MockWorkstation", list[FxtrID], list[tuple[FxtrID, FxtrID]]]:
    """Build a mock workstation with ``entity_ids`` registered entity
    short_ids (``s_e0``, ``s_e1``, …) and ``service_ids`` registered
    service short_ids (``s_s0``, …). Returns the workstation plus
    the parallel lists of allocated FxtrIDs the tests can compare
    against.

    All builtin IDs are tracked in the active ``scope`` so the
    placeholder ``.store()`` calls (which run during the walk) can
    succeed.
    """
    ws = _MockWorkstation()
    entity_fids: list[FxtrID] = []
    for i in range(entity_ids):
        fid = FxtrID.builtin(f"test_log_walker_entity_{uuid.uuid4().hex}_{i}")
        scope.track_builtin(fid)
        ws.register_entity(f"s_e{i}", fid)
        entity_fids.append(fid)
    service_id_pairs: list[tuple[FxtrID, FxtrID]] = []
    for i in range(service_ids):
        type_id = FxtrID.builtin(f"test_log_walker_svc_type_{uuid.uuid4().hex}_{i}")
        ctx_id = FxtrID.builtin(f"test_log_walker_svc_ctx_{uuid.uuid4().hex}_{i}")
        scope.track_builtin(type_id)
        scope.track_builtin(ctx_id)
        ws.register_service(f"s_s{i}", type_id, ctx_id)
        service_id_pairs.append((type_id, ctx_id))
    return ws, entity_fids, service_id_pairs


def _no_slot_keys(slot_n: int) -> str | None:
    """Default reverse-lookup that returns None — used when no
    callbacks are expected in the input."""
    _ = slot_n
    return None


# --- Primitive passthroughs -------------------------------------------


async def test_passthrough_scalars(scope: FxtrScope) -> None:
    """Scalars / None / strs / floats / ints / bools / bytes flow through."""
    ws, _, _ = _ws(scope)
    for value in (None, True, False, 0, 1, -3, 1.5, "hi", b"bytes"):
        out = await walk_payload(
            value,
            scope=scope,
            workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
            slot_key_for_slot_n=_no_slot_keys,
        )
        assert out == value


# --- Entity / service / callback substitution -------------------------


async def test_entity_tag_substitutes_to_raw_weak_id(scope: FxtrScope) -> None:
    """``["entity", short_id]`` → ``RawWeakFxtrID``."""
    ws, entity_fids, _ = _ws(scope, entity_ids=1)
    out = await walk_payload(
        ["entity", "s_e0"],
        scope=scope,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    assert out == RawWeakFxtrID(inner_id=entity_fids[0])
    assert ws.get_entity_calls == 1


async def test_service_tag_stores_placeholder_with_introspected_fields(
    scope: FxtrScope,
) -> None:
    """``["service", short_id]`` → ``RawStrongFxtrID`` to a stored
    :class:`WorkstationServicePlaceholder` whose three fields match
    the introspected ``$fxtr.type`` + ``$fxtr.ctx`` + short_id."""
    ws, _, svc_ids = _ws(scope, service_ids=1)
    type_id, ctx_id = svc_ids[0]
    out = await walk_payload(
        ["service", "s_s0"],
        scope=scope,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    assert isinstance(out, RawStrongFxtrID)

    # Reach into the stored placeholder via a manual load: build an
    # EntityRef[WorkstationServicePlaceholder] pointing at the raw
    # id and call .load().
    from fxtr2.conversion.converters.mashumaro_entities import EntityRef

    ref: EntityRef[WorkstationServicePlaceholder] = EntityRef(
        entity_id=out.inner_id, target_type=WorkstationServicePlaceholder
    )
    loaded = await ref.load()
    assert loaded.short_id == "s_s0"
    assert loaded.service_type_id == type_id
    assert loaded.service_ctx == ctx_id
    assert ws.get_service_calls == 1


async def test_callback_tag_stores_placeholder_with_reverse_looked_up_slot_key(
    scope: FxtrScope,
) -> None:
    """``["callback", N]`` → ``RawStrongFxtrID`` to a stored
    :class:`OwnedCallbackPlaceholder` with the reverse-looked-up
    slot_key."""
    ws, _, _ = _ws(scope)
    slot_keys = {4: "workstation_inbox", 7: "cb"}
    out = await walk_payload(
        ["callback", 4],
        scope=scope,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=slot_keys.get,
    )
    assert isinstance(out, RawStrongFxtrID)

    from fxtr2.conversion.converters.mashumaro_entities import EntityRef

    ref: EntityRef[OwnedCallbackPlaceholder] = EntityRef(
        entity_id=out.inner_id, target_type=OwnedCallbackPlaceholder
    )
    loaded = await ref.load()
    assert loaded.slot_n == 4
    assert loaded.slot_key == "workstation_inbox"


async def test_callback_tag_unknown_slot_key_falls_back_to_sentinel(
    scope: FxtrScope,
) -> None:
    """If the reverse-lookup returns None, the placeholder stores
    ``"<unknown>"`` as ``slot_key`` (with a warning logged)."""
    ws, _, _ = _ws(scope)
    out = await walk_payload(
        ["callback", 999],
        scope=scope,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    assert isinstance(out, RawStrongFxtrID)

    from fxtr2.conversion.converters.mashumaro_entities import EntityRef

    ref: EntityRef[OwnedCallbackPlaceholder] = EntityRef(
        entity_id=out.inner_id, target_type=OwnedCallbackPlaceholder
    )
    loaded = await ref.load()
    assert loaded.slot_n == 999
    assert loaded.slot_key == "<unknown>"


async def test_bytes_tag_is_base64_decoded(scope: FxtrScope) -> None:
    """``["bytes", b64]`` → decoded :class:`bytes`."""
    ws, _, _ = _ws(scope)
    blob = b"\x00\x01\x02hello"
    encoded = base64.b64encode(blob).decode("ascii")
    out = await walk_payload(
        ["bytes", encoded],
        scope=scope,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    assert out == blob


# --- Literal-list-escape + nested structures --------------------------


async def test_literal_list_escape_becomes_tuple(scope: FxtrScope) -> None:
    """``[[inner_items]]`` → tuple of walked items."""
    ws, entity_fids, _ = _ws(scope, entity_ids=1)
    out = await walk_payload(
        [[1, "hi", ["entity", "s_e0"]]],
        scope=scope,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    assert isinstance(out, tuple)
    assert out == (1, "hi", RawWeakFxtrID(inner_id=entity_fids[0]))


async def test_nested_dict(scope: FxtrScope) -> None:
    """Mappings recurse over values; keys must be strings."""
    ws, entity_fids, svc_ids = _ws(scope, entity_ids=1, service_ids=1)
    type_id, ctx_id = svc_ids[0]
    out = await walk_payload(
        {
            "ent": ["entity", "s_e0"],
            "svc": ["service", "s_s0"],
            "nested": {"inner": [[1, 2]]},
        },
        scope=scope,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    assert isinstance(out, frozendict)
    assert out["ent"] == RawWeakFxtrID(inner_id=entity_fids[0])
    assert isinstance(out["svc"], RawStrongFxtrID)
    assert out["nested"] == frozendict({"inner": (1, 2)})

    # Verify the stored service placeholder has the expected fields.
    from fxtr2.conversion.converters.mashumaro_entities import EntityRef

    svc_ref: EntityRef[WorkstationServicePlaceholder] = EntityRef(
        entity_id=out["svc"].inner_id, target_type=WorkstationServicePlaceholder
    )
    loaded_svc = await svc_ref.load()
    assert loaded_svc.service_type_id == type_id
    assert loaded_svc.service_ctx == ctx_id


# --- Per-walk caching -------------------------------------------------


async def test_repeated_entity_short_ids_resolve_once(scope: FxtrScope) -> None:
    """Two references to the same entity short_id in one walk only
    invoke ``get_entity`` once."""
    ws, _, _ = _ws(scope, entity_ids=1)
    await walk_payload(
        {"a": ["entity", "s_e0"], "b": ["entity", "s_e0"]},
        scope=scope,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    assert ws.get_entity_calls == 1


async def test_repeated_service_short_ids_dedupe_to_one_placeholder(
    scope: FxtrScope,
) -> None:
    """Two references to the same service short_id share one stored
    placeholder entity."""
    ws, _, _ = _ws(scope, service_ids=1)
    out = await walk_payload(
        {"a": ["service", "s_s0"], "b": ["service", "s_s0"]},
        scope=scope,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    assert isinstance(out, frozendict)
    assert isinstance(out["a"], RawStrongFxtrID)
    assert isinstance(out["b"], RawStrongFxtrID)
    assert out["a"].inner_id == out["b"].inner_id
    assert ws.get_service_calls == 1


async def test_repeated_callback_slots_dedupe(scope: FxtrScope) -> None:
    """Two references to the same callback slot share one stored
    placeholder entity."""
    ws, _, _ = _ws(scope)
    slot_keys = {3: "cb"}
    out = await walk_payload(
        {"x": ["callback", 3], "y": ["callback", 3]},
        scope=scope,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=slot_keys.get,
    )
    assert isinstance(out, frozendict)
    assert out["x"] == out["y"]


# --- Malformed inputs -------------------------------------------------


async def test_malformed_three_element_list_raises(scope: FxtrScope) -> None:
    """A list that is neither a 2-tagged form nor a 1-literal-escape is rejected."""
    ws, _, _ = _ws(scope)
    with pytest.raises(ValueError, match="malformed wire-escape list"):
        await walk_payload(
            [1, 2, 3],
            scope=scope,
            workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
            slot_key_for_slot_n=_no_slot_keys,
        )


async def test_unknown_wire_tag_raises(scope: FxtrScope) -> None:
    """An unrecognized tag raises rather than silently passing through."""
    ws, _, _ = _ws(scope)
    with pytest.raises(ValueError, match="unrecognized wire tag"):
        await walk_payload(
            ["mystery_tag", "data"],
            scope=scope,
            workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
            slot_key_for_slot_n=_no_slot_keys,
        )


async def test_dict_with_non_string_key_raises(scope: FxtrScope) -> None:
    """Non-string dict keys raise."""
    ws, _, _ = _ws(scope)
    with pytest.raises(ValueError, match="dict keys must be str"):
        await walk_payload(
            {1: "value"},  # type: ignore[dict-item]
            scope=scope,
            workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
            slot_key_for_slot_n=_no_slot_keys,
        )


async def test_entity_tag_with_non_str_payload_raises(scope: FxtrScope) -> None:
    """``["entity", non_str]`` raises before any RPC."""
    ws, _, _ = _ws(scope)
    with pytest.raises(ValueError, match="'entity' tag payload must be str"):
        await walk_payload(
            ["entity", 42],
            scope=scope,
            workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
            slot_key_for_slot_n=_no_slot_keys,
        )


async def test_callback_tag_with_non_int_payload_raises(scope: FxtrScope) -> None:
    """``["callback", "not-int"]`` raises before any storage."""
    ws, _, _ = _ws(scope)
    with pytest.raises(ValueError, match="'callback' tag payload must be int"):
        await walk_payload(
            ["callback", "not-int"],
            scope=scope,
            workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
            slot_key_for_slot_n=_no_slot_keys,
        )


# --- Regression: walker is independent of the active scope ------------
#
# The log writer task is started via ``impl_tg.start_soon`` and runs
# its loop without activating any user scope, so its active scope is
# the default :data:`GLOBAL_SCOPE`. The walker must not depend on the
# active scope being a mountable parent of its walk scope — every
# service resolution it performs has to use a scope the walker owns.
#
# The mocks above bypass real ``ServiceRef.resolve`` (their stubbed
# ``resolve()`` ignores its scope argument), which masks any reliance
# the walker has on the active scope. The test below mounts a *real*
# service on a sibling mountable scope and threads its real
# ``ServiceRef`` through the walker, so the resolve path actually hits
# ``FxtrScope.resolve_service``.


class _RealMockWorkstationImpl(TypedServiceImpl[WorkstationServiceProto]):
    """Real :class:`TypedServiceImpl` honoring
    :class:`WorkstationServiceProto`'s shape for the one method the
    regression test below exercises (``get_service``).

    The walker resolves the workstation ref through
    :meth:`FxtrScope.resolve_service`, so the ref must be backed by
    an actual mounted service rather than a duck-typed stub
    (otherwise the resolve path doesn't go through the real
    framework code and the bug being tested doesn't manifest).
    Other ``WorkstationServiceProto`` methods are intentionally not
    implemented — they aren't dispatched by this test.
    """

    def __init__(self, service_ref: ServiceRef[Any]) -> None:
        super().__init__()
        self._service_ref = service_ref

    def _target_type_annotation(self) -> Any:
        return WorkstationServiceProto

    def _service_type_id(self) -> FxtrID:
        return WORKSTATION_SERVICE_TYPE_ID

    @service_method
    async def get_service(self, service_short_id: str) -> ServiceRef[Any]:  # noqa: ARG002
        return self._service_ref


async def test_walks_service_when_active_scope_is_global() -> None:
    """Regression: walking ``["service", short_id]`` must succeed
    even when the active scope is :data:`GLOBAL_SCOPE` (the writer
    task's typical environment).

    Before the fix, ``stored_service_placeholder`` called
    ``svc_ref.resolve()`` without an explicit ``scope=``, which
    defaults to the active scope. With active=``GLOBAL_SCOPE`` (which
    is intentionally not mountable), that resolve raises
    ``RuntimeError: Scope <FxtrScope 'GLOBAL_SCOPE' ...> must be
    mountable...`` — the same error operators see when the agent
    presents data or invokes services in the demo.

    The :func:`walk_payload` call here threads a *real*
    ``ServiceRef[WorkstationServiceProto]`` (backed by
    :class:`_RealMockWorkstationImpl` mounted in a sibling scope)
    rather than a duck-typed mock, so the resolve actually hits
    :meth:`FxtrScope.resolve_service` and the bug surfaces.
    """
    # Two mountable scopes: ``ws_host`` owns both the mock workstation
    # impl AND the placeholder target service it returns from
    # ``get_service`` (same scope so the returned ref is reachable
    # through the call dispatcher's return arena). ``walker_scope``
    # includes ``ws_host`` so the workstation ref is resolvable from
    # the walker's destination scope.
    #
    # Both scopes also include :data:`GLOBAL_SCOPE` because the
    # :class:`WorkstationServicePlaceholder` (the entity the walker
    # stores when materializing the service ref) carries a
    # ``service_type_id`` field that's a type-descriptor entity
    # tracked in ``GLOBAL_SCOPE``. The production setup at
    # ``ws_connection._agent_mcp_session_lifecycle`` likewise
    # ``include=[..., GLOBAL_SCOPE]`` on the impl_scope for the
    # same reason.
    async with (
        FxtrScope(
            name="walker_regression_ws_host",
            isolated=True,
            include=[GLOBAL_SCOPE],
        ) as ws_host,
        FxtrScope(
            name="walker_regression_dest",
            isolated=True,
            include=[ws_host, GLOBAL_SCOPE],
        ) as walker_scope,
    ):
        # Reproduces the writer-task environment: no ``as_active_scope``
        # wrapping the body, so the active scope here is GLOBAL_SCOPE.
        assert get_active_scope() is GLOBAL_SCOPE, (
            "test precondition: this test body must run with the default "
            "GLOBAL_SCOPE active to reproduce the writer-task environment"
        )

        svc_ref: ServiceRef[Any] = (await ws_host.mount(NoOpService())).replace_service_type(
            UnknownService
        )
        workstation_ref = (
            await ws_host.mount(_RealMockWorkstationImpl(svc_ref))
        ).replace_service_type(WorkstationServiceProto)

        out = await walk_payload(
            ["service", "svc_a"],
            scope=walker_scope,
            workstation_ref=workstation_ref,
            slot_key_for_slot_n=_no_slot_keys,
        )
        assert isinstance(out, RawStrongFxtrID)
