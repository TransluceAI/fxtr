"""Tests for the output-channel system (``fxtr2.common.channels``).

Covers: the two smoke gates (``DataclassEntity`` + ``ChannelTag[S, V, R]``
multiple inheritance; ``ChannelUpdate`` union decode by ``kind``) and tag
``(S, V, R)`` inference; then the engine — lifecycle, mid-stream replay,
status-only emits + ``replace_history``, the pre-start window, residency across a
transient source scope, subchannels (unknown key / per-parent / cascade /
writer-release abort), the debug subchannel feed (``subscribe_debug_subchannels`` —
replay-then-live / name=None / start-independence / per-parent / teardown), the
lifecycle errors, the data-only service-ref rejection,
and the wire-envelope == dataclass-encoding pin. The dispatch (RPC-shape) path is
exercised throughout — every channel call goes through ``resolve`` + a proxy
(there is no local fast-path).

A `_split_scopes` harness (plus a three-task cross-tree variant) runs the
entity-flow paths with the owner / writer / reader in three separate *isolated*
(non-inheriting) scopes bridged from the owner, so the engine's cross-scope
copies are genuinely exercised (and loads/stores use explicit scopes — the
implicit active scope is not the isolated one; see ``scope_discipline.md``).
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest

from fxtr2.common.channels import (
    AbortedChannelSnapshot,
    ChannelAbortedBeforeStart,
    ChannelAbortedEvent,
    ChannelCompleteEvent,
    ChannelState,
    ChannelStateEvent,
    ChannelTag,
    ChannelUpdate,
    ChannelUpdateEvent,
    CompleteChannelSnapshot,
    FxtrChannelWriter,
    OpenChannelSnapshot,
    SubchannelOpened,
    create_root_channel,
    infer_channel_reader_type,
    infer_channel_types,
    iter_channel,
    start_channel_and_infer_types,
)
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, EntityRef, FxtrScope
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.service_dialect import decode_from_service_data, encode_to_service_data
from fxtr2.services.subscription_types import Subscriber
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

pytestmark = pytest.mark.anyio

_CHANNELS_TEST_NS = uuid.UUID("94478148-c508-4d29-af58-83e3b12375d6")
_RAW_RECORDER_TYPE_ID = system_entities.define_type(_CHANNELS_TEST_NS, "raw_recorder")


@dataclasses.dataclass(frozen=True)
class _Doc(DataclassEntity, fxtr_type=system_entities.define_type(_CHANNELS_TEST_NS, "doc")):
    """An entity referenced from channel messages in residency tests (1c)."""

    text: str


@dataclasses.dataclass(frozen=True)
class _IntsTag(
    DataclassEntity,
    ChannelTag[str, int, list[int]],
    fxtr_type=system_entities.define_type(_CHANNELS_TEST_NS, "ints_tag"),
):
    """Concrete tag: status is a str, messages are ints, the final value is the
    full list. (Distinct S/V/R types so inference is unambiguous.)"""

    label: str


@dataclasses.dataclass(frozen=True)
class _DocsTag(
    DataclassEntity,
    ChannelTag[str, EntityRef[_Doc], list[EntityRef[_Doc]]],
    fxtr_type=system_entities.define_type(_CHANNELS_TEST_NS, "docs_tag"),
):
    """Concrete tag whose messages carry entity refs."""

    label: str


@dataclasses.dataclass(frozen=True)
class _GenericTag[V](
    DataclassEntity,
    ChannelTag[str, V, list[V]],
    fxtr_type=system_entities.define_type(_CHANNELS_TEST_NS, "generic_tag"),
):
    """Generic tag class — V is unbound at the class level, so inference must
    reject it."""

    label: str


@dataclasses.dataclass(frozen=True)
class _PlainTag(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_CHANNELS_TEST_NS, "plain_tag"),
):
    """An entity that is not a ChannelTag at all."""

    label: str


@dataclasses.dataclass(frozen=True)
class _SubTag(
    _IntsTag,
    fxtr_type=system_entities.define_type(_CHANNELS_TEST_NS, "sub_tag"),
):
    """Subclass of a concrete tag — inference walks through it."""


@dataclasses.dataclass(frozen=True)
class _BoundTag(
    _GenericTag[str],
    fxtr_type=system_entities.define_type(_CHANNELS_TEST_NS, "bound_tag"),
):
    """Generic tag bound by a subclass — inference sees the binding."""


# ---------------------------------------------------------------------------
# Smoke gate 1: ChannelTag + DataclassEntity multiple inheritance
# ---------------------------------------------------------------------------


async def test_channel_tag_store_load_round_trip(local_fxtr_scope: FxtrScope) -> None:
    """The extra generic ChannelTag base must not break mashumaro store/load
    (it adds no fields)."""
    tag = _IntsTag(label="counts")
    ref = tag.store()
    loaded = await ref.load()
    assert loaded == tag
    assert isinstance(loaded, _IntsTag)
    assert isinstance(loaded, ChannelTag)
    # Roundtrip guarantee: re-store yields the same id.
    assert loaded.store().entity_id == ref.entity_id


async def test_channel_tag_ref_target_type_is_tag_class(local_fxtr_scope: FxtrScope) -> None:
    ref = _IntsTag(label="counts").store()
    assert ref.target_type is _IntsTag


# ---------------------------------------------------------------------------
# ChannelTag (S, V, R) inference
# ---------------------------------------------------------------------------


def test_infer_channel_types_concrete_tag() -> None:
    assert infer_channel_types(_IntsTag) == (str, int, list[int])
    assert infer_channel_types(_DocsTag) == (str, EntityRef[_Doc], list[EntityRef[_Doc]])


def test_infer_channel_types_subclass_of_concrete_tag() -> None:
    """Inference walks transitively through tag subclasses."""
    assert infer_channel_types(_SubTag) == (str, int, list[int])


def test_infer_channel_types_bound_generic_tag() -> None:
    """A generic tag bound by a subclass infers the bound types."""
    assert infer_channel_types(_BoundTag) == (str, str, list[str])


def test_infer_channel_types_rejects_generic_tag() -> None:
    with pytest.raises(TypeError, match="type variables"):
        infer_channel_types(_GenericTag)


def test_infer_channel_types_rejects_non_tag() -> None:
    with pytest.raises(TypeError, match="not a parameterized ChannelTag subclass"):
        infer_channel_types(_PlainTag)


def test_infer_channel_types_rejects_unparameterized_base() -> None:
    class _Unparameterized(ChannelTag):  # type: ignore[type-arg]
        pass

    with pytest.raises(TypeError, match="not a parameterized ChannelTag subclass"):
        infer_channel_types(_Unparameterized)


# ---------------------------------------------------------------------------
# Smoke gate 2: ChannelUpdate / ChannelState union decode by `kind` literal
# ---------------------------------------------------------------------------

_UPDATE_CASES: list[tuple[str, Any]] = [
    ("snapshot_open", lambda: OpenChannelSnapshot(status="running", values=(1, 2, 3))),
    ("snapshot_complete", lambda: CompleteChannelSnapshot(status="done", result=[1, 2, 3])),
    ("snapshot_abort", lambda: AbortedChannelSnapshot(status="failed", reason="boom")),
    (
        "update",
        lambda: ChannelUpdateEvent(events=(7, 8), status="running", replace_history=False),
    ),
    ("complete", lambda: ChannelCompleteEvent(result=[7], status="done")),
    ("abort", lambda: ChannelAbortedEvent(status="failed", reason="boom")),
    ("aborted_before_start", lambda: ChannelAbortedBeforeStart(reason="boom")),
]


@pytest.mark.parametrize("kind,make_event", _UPDATE_CASES, ids=[k for k, _ in _UPDATE_CASES])
def test_channel_update_union_round_trip(kind: str, make_event: Any) -> None:
    """Each event kind encodes to a kind-discriminated dict and decodes back to
    the right dataclass through the ChannelUpdate union. No event carries a tag."""
    event = make_event()
    annotation = ChannelUpdate[str, int, list[int]]
    encoded = encode_to_service_data(event, annotation)
    assert isinstance(encoded, dict)
    assert encoded["kind"] == kind
    assert "tag" not in encoded
    decoded = decode_from_service_data(encoded, annotation)
    assert decoded == event
    assert type(decoded) is type(event)


def test_channel_state_round_trip() -> None:
    state = ChannelState(status="running", kind="open")
    encoded = encode_to_service_data(state, ChannelState[str])
    decoded = decode_from_service_data(encoded, ChannelState[str])
    assert decoded == state
    assert isinstance(encoded, dict)
    assert encoded["kind"] == "open"


@pytest.mark.parametrize(
    "make_event,expected_kind",
    [
        (lambda: ChannelState(status="running", kind="open"), "open"),
        (lambda: ChannelAbortedBeforeStart(reason="boom"), "aborted_before_start"),
    ],
)
def test_channel_state_event_union_round_trip(make_event: Any, expected_kind: str) -> None:
    """The state-stream union decodes both members by their ``kind`` discriminator
    (ChannelState's renamed-from-stage ``kind`` + aborted_before_start)."""
    event = make_event()
    encoded = encode_to_service_data(event, ChannelStateEvent[str])
    assert isinstance(encoded, dict)
    assert encoded["kind"] == expected_kind
    decoded = decode_from_service_data(encoded, ChannelStateEvent[str])
    assert decoded == event
    assert type(decoded) is type(event)


def test_state_status_can_carry_fxtr_id() -> None:
    """A bare FxtrID status round-trips (sanity check that S can carry ids)."""
    fid = FxtrID.builtin("test-channels-status-stand-in")
    state = ChannelState[FxtrID](status=fid, kind="open")
    encoded = encode_to_service_data(state, ChannelState[FxtrID])
    decoded = decode_from_service_data(encoded, ChannelState[FxtrID])
    assert decoded == state


# ---------------------------------------------------------------------------
# Engine smoke tests (chunk 1b) — full suite lands in 1c
# ---------------------------------------------------------------------------


async def test_channel_lifecycle_smoke(local_fxtr_scope: FxtrScope) -> None:
    """create → start → emit → late subscriber sees snapshot + live update +
    complete (exercises create_root_channel, typed proxies, iter_channel)."""
    writer_ref, reader_ref = await create_root_channel(tag_type=_IntsTag, scope=local_fxtr_scope)
    wproxy = await writer_ref.resolve(local_fxtr_scope)
    tag_ref = _IntsTag(label="counts").store()
    await wproxy.start(tag_ref, "running")
    await wproxy.emit(events=[1, 2])

    async with iter_channel(reader_ref, local_fxtr_scope) as stream:
        await wproxy.emit(events=[3])
        await wproxy.finish([1, 2, 3], "done")
        got: list[Any] = []
        with anyio.fail_after(5):
            async for ev in stream:
                got.append(ev)
                if ev.kind == "complete":
                    break

    assert got[0].kind == "snapshot_open"
    assert got[0].values == (1, 2)
    assert got[0].status == "running"
    assert any(e.kind == "update" and e.events == (3,) for e in got)
    complete = got[-1]
    assert complete.kind == "complete"
    assert complete.result == [1, 2, 3]
    assert complete.status == "done"


async def test_subchannel_spawn_open_cascade_smoke(local_fxtr_scope: FxtrScope) -> None:
    """spawn_subchannel → emit its key conceptually → open_subchannel_reader →
    peek; parent finish reclaims the child (subsequent open raises)."""
    writer_ref, reader_ref = await create_root_channel(tag_type=_IntsTag, scope=local_fxtr_scope)
    wproxy = await writer_ref.resolve(local_fxtr_scope)
    await wproxy.start(_IntsTag(label="root").store(), "running")

    key, sub_writer = await wproxy.spawn_subchannel("cell")
    typed_sub = await start_channel_and_infer_types(
        sub_writer, _IntsTag(label="cell").store(), "subrunning", scope=local_fxtr_scope
    )
    sub_wproxy = await typed_sub.resolve(local_fxtr_scope)
    await sub_wproxy.emit(events=[10])

    rproxy = await reader_ref.resolve(local_fxtr_scope)
    sub_reader = infer_channel_reader_type(await rproxy.open_subchannel_reader(key), _IntsTag)
    sub_rproxy = await sub_reader.resolve(local_fxtr_scope)
    state = await sub_rproxy.peek_state()
    assert state.kind == "open"
    assert state.status == "subrunning"

    # Finish the child cleanly, then the parent; the child becomes unreachable.
    await sub_wproxy.finish([10], "subdone")
    await wproxy.finish([], "rootdone")
    with pytest.raises(Exception):
        await rproxy.open_subchannel_reader(key)


# ---------------------------------------------------------------------------
# Full suite (chunk 1c)
# ---------------------------------------------------------------------------


class _RawRecorder(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """Subscriber that records the raw (undecoded) wire envelopes, for pinning
    the exact dict shape published to the hubs."""

    def __init__(self, send: Any) -> None:
        super().__init__()
        self._send = send

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return _RAW_RECORDER_TYPE_ID

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        self._send.send_nowait(payload)


async def _first(receive: Any) -> Any:
    with anyio.fail_after(5):
        return await receive.receive()


async def _drain_kind(stream: Any, kind: str) -> list[Any]:
    """Collect events until (and including) the first of ``kind``."""
    got: list[Any] = []
    with anyio.fail_after(5):
        async for ev in stream:
            got.append(ev)
            if ev.kind == kind:
                break
    return got


async def _started_root(
    scope: FxtrScope, *, status: str = "running", label: str = "root"
) -> tuple[Any, Any, Any]:
    """create_root_channel(_IntsTag) + start; return (writer_proxy, reader_ref, wproxy)."""
    writer_ref, reader_ref = await create_root_channel(tag_type=_IntsTag, scope=scope)
    wproxy = await writer_ref.resolve(scope)
    await wproxy.start(_IntsTag(label=label).store(), status)
    return writer_ref, reader_ref, wproxy


@dataclasses.dataclass
class _SplitChannel:
    """A channel whose owner / writer / reader live in three separate isolated
    scopes (none inheriting from another), bridged from the owner. Every entity
    must be explicitly copied across scope boundaries — no active-scope shortcuts
    (see ``scope_discipline.md``)."""

    writer_ref: Any
    reader_ref: Any
    root_scope: FxtrScope  # owner / mount scope
    wscope: FxtrScope  # writer-invoker scope
    rscope: FxtrScope  # reader-invoker scope


@asynccontextmanager
async def _split_scopes(tag_type: Any) -> AsyncIterator[_SplitChannel]:
    """Set up a channel under three nested-but-isolated scopes + bridges.

    Nesting keeps the lifetimes ordered for ``independent_lease`` (the owner
    closes last); isolation means none of the three inherits from another, so the
    engine's cross-scope copies are genuinely exercised."""
    async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="ch_root") as root:
        writer_ref, reader_ref = await create_root_channel(tag_type=tag_type, scope=root)
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="ch_writer") as wscope:
            await wscope.bridge_service_from_scope(
                writer_ref, root, keepalive_via="independent_lease"
            )
            async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="ch_reader") as rscope:
                await rscope.bridge_service_from_scope(
                    reader_ref, root, keepalive_via="independent_lease"
                )
                yield _SplitChannel(writer_ref, reader_ref, root, wscope, rscope)


async def test_envelope_matches_dataclass_encoding(local_fxtr_scope: FxtrScope) -> None:
    """The raw wire envelope a subscriber receives equals the mashumaro encoding
    of the corresponding dataclass (pins the manual envelope builders)."""
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    await wproxy.emit(events=[1, 2])
    send, receive = anyio.create_memory_object_stream[Any](16)
    rec_ref = await local_fxtr_scope.mount(_RawRecorder(send))
    rproxy = await reader_ref.resolve(local_fxtr_scope)
    typed = rec_ref.replace_service_type(Subscriber[ChannelUpdate[str, int, list[int]]])
    handle = await rproxy.subscribe_full(typed)
    open_env = await _first(receive)
    expected = encode_to_service_data(
        OpenChannelSnapshot(status="running", values=(1, 2)),
        ChannelUpdate[str, int, list[int]],
    )
    assert open_env == expected
    await wproxy.emit(events=[3], status="working")
    update_env = await _first(receive)
    assert update_env == encode_to_service_data(
        ChannelUpdateEvent(events=(3,), status="working", replace_history=False),
        ChannelUpdate[str, int, list[int]],
    )
    await (await handle.resolve(local_fxtr_scope)).unsubscribe()


async def test_lifecycle_and_post_finish_snapshot(local_fxtr_scope: FxtrScope) -> None:
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    await wproxy.emit(events=[1, 2])
    await wproxy.finish([1, 2, 3], "done")
    # Post-finish subscriber sees only the complete snapshot (no message values).
    async with iter_channel(reader_ref, local_fxtr_scope) as stream:
        ev = await _first(stream)
        assert ev.kind == "snapshot_complete"
        assert ev.result == [1, 2, 3]
        assert ev.status == "done"
    # peek_state reflects completion.
    rproxy = await reader_ref.resolve(local_fxtr_scope)
    state = await rproxy.peek_state()
    assert state.kind == "complete"


async def test_mid_stream_replay_folds_equal(local_fxtr_scope: FxtrScope) -> None:
    """A subscriber joining mid-stream (snapshot of prior emits + live deltas)
    folds to the same accumulated values as a from-start subscriber."""
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    await wproxy.emit(events=[1, 2])
    async with iter_channel(reader_ref, local_fxtr_scope) as stream:
        await wproxy.emit(events=[3, 4])
        await wproxy.finish([1, 2, 3, 4], "done")
        events = await _drain_kind(stream, "complete")
    values: list[int] = []
    for ev in events:
        if ev.kind == "snapshot_open":
            values.extend(ev.values)
        elif ev.kind == "update":
            values.extend(ev.events)
    assert values == [1, 2, 3, 4]
    assert events[-1].result == [1, 2, 3, 4]


async def test_abort_event_and_post_abort_snapshot(local_fxtr_scope: FxtrScope) -> None:
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    async with iter_channel(reader_ref, local_fxtr_scope) as stream:
        await _first(stream)  # open snapshot
        await wproxy.abort("boom")
        ev = await _first(stream)
        assert ev.kind == "abort"
        assert ev.reason == "boom"
    async with iter_channel(reader_ref, local_fxtr_scope) as stream2:
        ev = await _first(stream2)
        assert ev.kind == "snapshot_abort"
        assert ev.reason == "boom"
    # abort is idempotent.
    await wproxy.abort("again")


async def test_status_only_emit_updates_state_and_full(local_fxtr_scope: FxtrScope) -> None:
    """A status-only emit (no events) updates the state stream and appears on the
    full stream as an empty-events update carrying the new status."""
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    async with iter_channel(reader_ref, local_fxtr_scope) as stream:
        await _first(stream)  # open snapshot (status="running")
        await wproxy.emit(status="working")
        ev = await _first(stream)
        assert ev.kind == "update"
        assert ev.events == ()
        assert ev.status == "working"
    rproxy = await reader_ref.resolve(local_fxtr_scope)
    assert (await rproxy.peek_state()).status == "working"


async def test_replace_history(local_fxtr_scope: FxtrScope) -> None:
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    await wproxy.emit(events=[1, 2, 3])
    async with iter_channel(reader_ref, local_fxtr_scope) as stream:
        # A live subscriber sees the replace_history update with the flag set.
        await _first(stream)  # open snapshot (values 1,2,3)
        await wproxy.emit(events=[99], replace_history=True)
        ev = await _first(stream)
        assert ev.kind == "update"
        assert ev.events == (99,)
        assert ev.replace_history is True
    # A new subscriber's snapshot reflects only the replaced history.
    async with iter_channel(reader_ref, local_fxtr_scope) as stream2:
        snap = await _first(stream2)
        assert snap.kind == "snapshot_open"
        assert snap.values == (99,)


async def test_pre_start_peek_blocks_until_start(local_fxtr_scope: FxtrScope) -> None:
    writer_ref, reader_ref = await create_root_channel(tag_type=_IntsTag, scope=local_fxtr_scope)
    rproxy = await reader_ref.resolve(local_fxtr_scope)
    wproxy = await writer_ref.resolve(local_fxtr_scope)
    result: dict[str, Any] = {}

    async with anyio.create_task_group() as tg:

        async def _peek() -> None:
            result["state"] = await rproxy.peek_state()

        tg.start_soon(_peek)
        await anyio.sleep(0.05)
        assert "state" not in result  # blocked until start
        await wproxy.start(_IntsTag(label="x").store(), "running")

    assert result["state"].kind == "open"
    assert result["state"].status == "running"


async def test_pre_start_subscribe_delivers_after_start(local_fxtr_scope: FxtrScope) -> None:
    writer_ref, reader_ref = await create_root_channel(tag_type=_IntsTag, scope=local_fxtr_scope)
    wproxy = await writer_ref.resolve(local_fxtr_scope)
    async with iter_channel(reader_ref, local_fxtr_scope) as stream:
        delivered_early = True
        with anyio.move_on_after(0.05):
            await stream.receive()
            delivered_early = False  # only reached if something was delivered
        assert delivered_early  # nothing before start
        await wproxy.start(_IntsTag(label="x").store(), "running")
        ev = await _first(stream)
        assert ev.kind == "snapshot_open"
        assert ev.status == "running"


async def test_lifecycle_errors(local_fxtr_scope: FxtrScope) -> None:
    writer_ref, _ = await create_root_channel(tag_type=_IntsTag, scope=local_fxtr_scope)
    wproxy = await writer_ref.resolve(local_fxtr_scope)
    tag = _IntsTag(label="x").store()
    # emit / finish before start.
    with pytest.raises(Exception):
        await wproxy.emit(events=[1])
    with pytest.raises(Exception):
        await wproxy.finish([1], "done")
    await wproxy.start(tag, "running")
    # double start.
    with pytest.raises(Exception):
        await wproxy.start(tag, "running")
    await wproxy.finish([1], "done")
    # emit / double finish after finish.
    with pytest.raises(Exception):
        await wproxy.emit(events=[2])
    with pytest.raises(Exception):
        await wproxy.finish([2], "done")


async def test_message_residency_survives_transient_source(local_fxtr_scope: FxtrScope) -> None:
    """A message carrying an entity ref stored in a transient scope stays loadable
    by a late subscriber after that scope closed (the channel retains it)."""
    writer_ref, reader_ref = await create_root_channel(tag_type=_DocsTag, scope=local_fxtr_scope)
    wproxy = await writer_ref.resolve(local_fxtr_scope)
    await wproxy.start(_DocsTag(label="docs").store(), "running")
    async with FxtrScope() as transient:
        doc_ref = _Doc(text="hello").store(transient)
        wproxy_t = await writer_ref.resolve(transient)
        await wproxy_t.emit(events=[doc_ref])
    # transient closed; a late subscriber can still load the doc.
    async with iter_channel(reader_ref, local_fxtr_scope) as stream:
        snap = await _first(stream)
        assert snap.kind == "snapshot_open"
        (got_ref,) = snap.values
        doc = await got_ref.load()
        assert doc.text == "hello"


async def test_subchannel_unknown_key_and_per_parent(local_fxtr_scope: FxtrScope) -> None:
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    rproxy = await reader_ref.resolve(local_fxtr_scope)
    with pytest.raises(Exception):
        await rproxy.open_subchannel_reader("nonexistent")
    # A key is scoped to its own parent: a sibling channel can't open it.
    key, _ = await wproxy.spawn_subchannel("cell")
    _, reader2, _ = await _started_root(local_fxtr_scope, label="root2")
    rproxy2 = await reader2.resolve(local_fxtr_scope)
    with pytest.raises(Exception):
        await rproxy2.open_subchannel_reader(key)
    # ...but its own parent can.
    sub_reader = await rproxy.open_subchannel_reader(key)
    assert sub_reader is not None


async def test_subchannel_cascade_clean_abort(local_fxtr_scope: FxtrScope) -> None:
    """When a parent finishes with a still-open child, a live subscriber on the
    child receives a clean abort event."""
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    key, sub_writer = await wproxy.spawn_subchannel("cell")
    await start_channel_and_infer_types(
        sub_writer, _IntsTag(label="cell").store(), "subrunning", scope=local_fxtr_scope
    )
    rproxy = await reader_ref.resolve(local_fxtr_scope)
    sub_reader = infer_channel_reader_type(await rproxy.open_subchannel_reader(key), _IntsTag)
    async with iter_channel(sub_reader, local_fxtr_scope) as sub_stream:
        await _first(sub_stream)  # open snapshot
        # Parent finishes without the child finishing → child cascade-aborts.
        await wproxy.finish([], "rootdone")
        ev = await _first(sub_stream)
        assert ev.kind == "abort"


async def test_writer_release_aborts_open_channel(local_fxtr_scope: FxtrScope) -> None:
    """Dropping a started-but-unfinished subchannel writer (its mount scope drains)
    aborts the channel — the release-without-finish net."""
    writer_ref, reader_ref, _ = await _started_root(local_fxtr_scope)
    # Spawn from a holder scope so the returned writer ref is gated by it; closing
    # the holder without finishing must auto-abort the subchannel.
    async with FxtrScope() as holder:
        wproxy_h = await writer_ref.resolve(holder)
        key, sub_writer = await wproxy_h.spawn_subchannel("cell")
        typed_sub = await start_channel_and_infer_types(
            sub_writer, _IntsTag(label="cell").store(), "subrunning", scope=holder
        )
        await (await typed_sub.resolve(holder)).emit(events=[1])
    # holder closed → the writer ref released → the channel auto-aborts (poll, since
    # the release teardown drains concurrently with the holder's close).
    rproxy = await reader_ref.resolve(local_fxtr_scope)
    sub_reader = infer_channel_reader_type(await rproxy.open_subchannel_reader(key), _IntsTag)
    sub_rproxy = await sub_reader.resolve(local_fxtr_scope)
    with anyio.fail_after(5):
        while (await sub_rproxy.peek_state()).kind != "abort":
            await anyio.sleep(0.02)


async def test_emit_rejects_service_refs(local_fxtr_scope: FxtrScope) -> None:
    writer_ref, reader_ref = await create_root_channel(tag_type=_IntsTag, scope=local_fxtr_scope)
    wproxy = await writer_ref.resolve(local_fxtr_scope)
    await wproxy.start(_IntsTag(label="x").store(), "running")
    # Retype V to FxtrServiceData so the proxy will *encode* a service ref (bypassing
    # the data-only typing); the impl must still reject it at the copy step.
    leaky = writer_ref.replace_service_type(
        FxtrChannelWriter[_IntsTag, str, FxtrServiceData, list[int]]
    )
    leaky_proxy = await leaky.resolve(local_fxtr_scope)
    with pytest.raises(Exception):
        await leaky_proxy.emit(events=[reader_ref])  # reader_ref is a ServiceRef


async def test_finish_rejects_service_refs(local_fxtr_scope: FxtrScope) -> None:
    writer_ref, reader_ref = await create_root_channel(tag_type=_IntsTag, scope=local_fxtr_scope)
    wproxy = await writer_ref.resolve(local_fxtr_scope)
    await wproxy.start(_IntsTag(label="x").store(), "running")
    leaky = writer_ref.replace_service_type(FxtrChannelWriter[_IntsTag, str, int, FxtrServiceData])
    leaky_proxy = await leaky.resolve(local_fxtr_scope)
    with pytest.raises(Exception):
        await leaky_proxy.finish(reader_ref, "done")


async def test_cross_tree_three_isolated_scopes() -> None:
    """Root owner, writer-invoker, and reader-invoker each live in their own
    isolated scope (none inheriting from another), bridged from the root. Forces
    every entity to be copied across genuine scope boundaries: a doc stored in the
    writer scope must reach the reader scope (writer -> root data scope -> reader)
    and load there."""
    box: dict[str, Any] = {}
    writer_scope_ready = anyio.Event()
    reader_scope_ready = anyio.Event()
    bridged = anyio.Event()
    reader_attached = anyio.Event()
    writer_done = anyio.Event()
    received: dict[str, Any] = {}

    async def root_task() -> None:
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="root") as root:
            box["w"], box["r"] = await create_root_channel(tag_type=_DocsTag, scope=root)
            await writer_scope_ready.wait()
            await reader_scope_ready.wait()
            # independent_lease: root closes *after* the invoker scopes (its drain
            # waits on their bridged leases) — fine across concurrent sibling tasks.
            await box["wscope"].bridge_service_from_scope(
                box["w"], root, keepalive_via="independent_lease"
            )
            await box["rscope"].bridge_service_from_scope(
                box["r"], root, keepalive_via="independent_lease"
            )
            bridged.set()
            # root's `async with` exit drains here, blocking until the invoker
            # scopes release their leases (i.e. their tasks finish).

    async def writer_task() -> None:
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="wscope") as wscope:
            box["wscope"] = wscope
            writer_scope_ready.set()
            await bridged.wait()
            wproxy = await box["w"].resolve(wscope)
            tag = _DocsTag(label="d").store(wscope)
            doc = _Doc(text="cross-tree").store(wscope)
            await wproxy.start(tag, "running")
            await reader_attached.wait()
            await wproxy.emit(events=[doc])
            await wproxy.finish([doc], "done")
            writer_done.set()

    async def reader_task() -> None:
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="rscope") as rscope:
            box["rscope"] = rscope
            reader_scope_ready.set()
            await bridged.wait()
            async with iter_channel(box["r"], rscope) as stream:
                reader_attached.set()
                events = await _drain_kind(stream, "complete")
                received["kinds"] = [e.kind for e in events]
                (update,) = [e for e in events if e.kind == "update"]
                (live_ref,) = update.events
                # Explicit scope (rscope is isolated, so it is *not* the active
                # scope — scope_discipline.md): the doc must have been copied here.
                received["live_text"] = (await live_ref.load(rscope)).text
            # Also via the proxy return path (peek_full → complete snapshot).
            await writer_done.wait()
            rproxy = await box["r"].resolve(rscope)
            snap = await rproxy.peek_full()
            received["peek_text"] = (await snap.result[0].load(rscope)).text

    async with anyio.create_task_group() as tg:
        tg.start_soon(root_task)
        tg.start_soon(writer_task)
        tg.start_soon(reader_task)

    assert received["live_text"] == "cross-tree"
    assert received["peek_text"] == "cross-tree"
    assert received["kinds"][0] == "snapshot_open"
    assert received["kinds"][-1] == "complete"


# ---------------------------------------------------------------------------
# Split-scope coverage: entity-flow paths under fully separated scopes
# (owner / writer / reader isolated + bridged — catches active-scope / wrong-
# scope-tracking bugs the local_fxtr_scope tests can't, per scope_discipline.md).
# ---------------------------------------------------------------------------


async def test_split_subchannel_and_get_tag_entity_flow() -> None:
    async with _split_scopes(_IntsTag) as ch:
        wproxy = await ch.writer_ref.resolve(ch.wscope)
        await wproxy.start(_IntsTag(label="root").store(ch.wscope), "running")
        key, sub_writer = await wproxy.spawn_subchannel("cell")
        # The subchannel carries entity messages (its own DocsTag).
        sub_typed = await start_channel_and_infer_types(
            sub_writer, _DocsTag(label="cell").store(ch.wscope), "subrunning", scope=ch.wscope
        )
        sub_wproxy = await sub_typed.resolve(ch.wscope)
        doc = _Doc(text="sub-doc").store(ch.wscope)
        await sub_wproxy.emit(events=[doc])

        # Reader side, separate isolated scope.
        rproxy = await ch.reader_ref.resolve(ch.rscope)
        # get_tag across scopes: the parent tag loads from the reader scope.
        parent_tag = await rproxy.get_tag()
        assert (await parent_tag.load(ch.rscope)).label == "root"
        sub_reader = infer_channel_reader_type(await rproxy.open_subchannel_reader(key), _DocsTag)
        async with iter_channel(sub_reader, ch.rscope) as stream:
            snap = await _first(stream)
            assert snap.kind == "snapshot_open"
            (doc_ref,) = snap.values
            assert (await doc_ref.load(ch.rscope)).text == "sub-doc"


async def test_split_replace_history_entity_flow() -> None:
    async with _split_scopes(_DocsTag) as ch:
        wproxy = await ch.writer_ref.resolve(ch.wscope)
        await wproxy.start(_DocsTag(label="d").store(ch.wscope), "running")
        doc1 = _Doc(text="first").store(ch.wscope)
        doc2 = _Doc(text="second").store(ch.wscope)
        await wproxy.emit(events=[doc1])
        await wproxy.emit(events=[doc2], replace_history=True)
        # A fresh reader (separate scope) sees only the replaced history and loads it.
        async with iter_channel(ch.reader_ref, ch.rscope) as stream:
            snap = await _first(stream)
            assert snap.kind == "snapshot_open"
            (only,) = snap.values
            assert (await only.load(ch.rscope)).text == "second"


async def test_split_subscribe_state_entity_flow() -> None:
    """subscribe_state delivers status across scopes; finish then peek_full's
    result loads from the reader scope (the durable-scope return path)."""
    async with _split_scopes(_DocsTag) as ch:
        wproxy = await ch.writer_ref.resolve(ch.wscope)
        await wproxy.start(_DocsTag(label="d").store(ch.wscope), "running")
        doc = _Doc(text="final-doc").store(ch.wscope)
        await wproxy.finish([doc], "done")
        rproxy = await ch.reader_ref.resolve(ch.rscope)
        state = await rproxy.peek_state()
        assert state.kind == "complete"
        assert state.status == "done"
        snap = await rproxy.peek_full()
        assert snap.kind == "snapshot_complete"
        assert (await snap.result[0].load(ch.rscope)).text == "final-doc"


async def test_abort_before_start_delivers_variant(local_fxtr_scope: FxtrScope) -> None:
    """A subchannel aborted before it ever started: peek_full / peek_state /
    subscribe deliver the statusless ChannelAbortedBeforeStart variant (no block,
    no error); get_tag raises (there is no tag)."""
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    key, sub_writer = await wproxy.spawn_subchannel("cell")
    # Abort the (un-inferred) subchannel writer without ever starting it. `abort`
    # takes only a str, so it works on the still-_U-typed writer.
    sub_wproxy = await sub_writer.resolve(local_fxtr_scope)
    await sub_wproxy.abort("never started")

    rproxy = await reader_ref.resolve(local_fxtr_scope)
    # Retype to *some* concrete tag we expected (the variant ignores S/V/R).
    sub_reader = infer_channel_reader_type(await rproxy.open_subchannel_reader(key), _DocsTag)
    sub_rproxy = await sub_reader.resolve(local_fxtr_scope)

    state = await sub_rproxy.peek_state()
    assert isinstance(state, ChannelAbortedBeforeStart)
    assert state.reason == "never started"

    full = await sub_rproxy.peek_full()
    assert isinstance(full, ChannelAbortedBeforeStart)

    with pytest.raises(Exception):
        await sub_rproxy.get_tag()


# ---------------------------------------------------------------------------
# Debug subchannel feed (chunk 1e)
# ---------------------------------------------------------------------------


async def _subscribe_debug(reader_ref: Any, scope: FxtrScope) -> tuple[Any, Any]:
    """Subscribe a raw recorder to ``reader``'s debug subchannel feed; return
    ``(receive_stream, unsubscribe_handle)``. Raw envelopes are recorded so the
    exact wire shape is asserted (a decode round-trip is checked separately)."""
    send, receive = anyio.create_memory_object_stream[Any](64)
    rec_ref = await scope.mount(_RawRecorder(send))
    rproxy = await reader_ref.resolve(scope)
    typed = rec_ref.replace_service_type(Subscriber[SubchannelOpened])
    handle = await rproxy.subscribe_debug_subchannels(typed)
    return receive, handle


async def test_debug_subchannels_replay_then_live(local_fxtr_scope: FxtrScope) -> None:
    """A debug-feed subscriber is seeded with one SubchannelOpened per already-spawned
    child (in spawn order), then receives one per subsequent spawn."""
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    k1, _ = await wproxy.spawn_subchannel("a")
    k2, _ = await wproxy.spawn_subchannel("b")

    receive, handle = await _subscribe_debug(reader_ref, local_fxtr_scope)
    seed1 = await _first(receive)
    seed2 = await _first(receive)
    assert seed1 == {"kind": "subchannel_opened", "key": k1, "name": "a"}
    assert seed2 == {"kind": "subchannel_opened", "key": k2, "name": "b"}

    # Live: a spawn after subscription is delivered as a fresh event.
    k3, _ = await wproxy.spawn_subchannel("c")
    live = await _first(receive)
    assert live == {"kind": "subchannel_opened", "key": k3, "name": "c"}
    # And it decodes to the dataclass.
    assert decode_from_service_data(live, SubchannelOpened) == SubchannelOpened(key=k3, name="c")

    await (await handle.resolve(local_fxtr_scope)).unsubscribe()


async def test_debug_subchannels_name_none(local_fxtr_scope: FxtrScope) -> None:
    """spawn_subchannel without a name → the feed carries name=None (and the key is
    a bare uuid, no prefix)."""
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    key, _ = await wproxy.spawn_subchannel()
    assert "-" not in key  # bare uuid4 hex, no "name-" prefix

    receive, handle = await _subscribe_debug(reader_ref, local_fxtr_scope)
    seed = await _first(receive)
    assert seed == {"kind": "subchannel_opened", "key": key, "name": None}
    assert decode_from_service_data(seed, SubchannelOpened) == SubchannelOpened(key=key, name=None)
    await (await handle.resolve(local_fxtr_scope)).unsubscribe()


async def test_debug_subchannels_independent_of_start(local_fxtr_scope: FxtrScope) -> None:
    """The debug feed concerns children, not the tag — it works before the channel
    is started (subscribe sees an empty seed; a pre-start spawn is delivered live)."""
    writer_ref, reader_ref = await create_root_channel(tag_type=_IntsTag, scope=local_fxtr_scope)
    wproxy = await writer_ref.resolve(local_fxtr_scope)
    # No start() — subscribing the debug feed must not block.
    receive, handle = await _subscribe_debug(reader_ref, local_fxtr_scope)
    # Spawning is allowed on an unstarted (open-able) parent; the event arrives live.
    key, _ = await wproxy.spawn_subchannel("early")
    live = await _first(receive)
    assert live == {"kind": "subchannel_opened", "key": key, "name": "early"}
    await (await handle.resolve(local_fxtr_scope)).unsubscribe()


async def test_debug_subchannels_per_parent_namespace(local_fxtr_scope: FxtrScope) -> None:
    """Each channel has its own feed: a grandchild spawned on a child appears in the
    child's feed, not the root's."""
    _, root_reader, wproxy = await _started_root(local_fxtr_scope)
    child_key, child_writer = await wproxy.spawn_subchannel("child")
    typed_child = await start_channel_and_infer_types(
        child_writer, _IntsTag(label="child").store(), "crun", scope=local_fxtr_scope
    )
    child_wproxy = await typed_child.resolve(local_fxtr_scope)
    grandchild_key, _ = await child_wproxy.spawn_subchannel("grandchild")

    # Root feed: only the child.
    root_receive, root_handle = await _subscribe_debug(root_reader, local_fxtr_scope)
    root_seed = await _first(root_receive)
    assert root_seed["key"] == child_key
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.25):
            await root_receive.receive()

    # Child feed: only the grandchild.
    rproxy = await root_reader.resolve(local_fxtr_scope)
    child_reader = infer_channel_reader_type(
        await rproxy.open_subchannel_reader(child_key), _IntsTag
    )
    child_receive, child_handle = await _subscribe_debug(child_reader, local_fxtr_scope)
    child_seed = await _first(child_receive)
    assert child_seed["key"] == grandchild_key
    assert child_seed["name"] == "grandchild"

    await (await root_handle.resolve(local_fxtr_scope)).unsubscribe()
    await (await child_handle.resolve(local_fxtr_scope)).unsubscribe()


async def test_debug_subchannels_feed_closes_on_parent_finish(local_fxtr_scope: FxtrScope) -> None:
    """When a node is reclaimed (its parent finishes), its debug feed is torn down
    cleanly — the subscriber's recorder stream closes without error/hang."""
    _, root_reader, wproxy = await _started_root(local_fxtr_scope)
    child_key, child_writer = await wproxy.spawn_subchannel("child")
    await start_channel_and_infer_types(
        child_writer, _IntsTag(label="child").store(), "crun", scope=local_fxtr_scope
    )
    rproxy = await root_reader.resolve(local_fxtr_scope)
    child_reader = infer_channel_reader_type(
        await rproxy.open_subchannel_reader(child_key), _IntsTag
    )
    receive, _ = await _subscribe_debug(child_reader, local_fxtr_scope)

    # Finishing the root reclaims the child (teardown walk closes its subchannels hub).
    await wproxy.finish([], "rootdone")
    # The child is now unreachable; the debug subscription was torn down. Draining
    # the recorder must not hang (it either closes or simply yields nothing more).
    with anyio.move_on_after(2):
        with contextlib.suppress(anyio.EndOfStream, anyio.ClosedResourceError):
            while True:
                await receive.receive()


async def test_debug_subchannels_feed_closed_after_terminal(local_fxtr_scope: FxtrScope) -> None:
    """A channel's own debug feed is closed once it finishes (children are gone),
    so a *late* subscribe to a terminal channel's feed errors rather than serving
    stale keys — even though the node itself stays queryable (peek_full still works)."""
    _, reader_ref, wproxy = await _started_root(local_fxtr_scope)
    await wproxy.spawn_subchannel("cell")
    await wproxy.finish([], "done")

    rproxy = await reader_ref.resolve(local_fxtr_scope)
    # The node is still queryable for its terminal snapshot...
    snap = await rproxy.peek_full()
    assert snap.kind == "snapshot_complete"
    # ...but its debug feed is closed: a late subscribe errors.
    with pytest.raises(Exception):
        await _subscribe_debug(reader_ref, local_fxtr_scope)
