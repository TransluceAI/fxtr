"""Pure unit tests for the stage-3a state-mirror + mutation-builder
pure helpers.

No workstation, no sandbox — just Python data shapes in, request /
patch / tracker-diff out.
"""

from __future__ import annotations

import pytest
from fxtr_workstation.client_protocol import (
    TrackEntityOp,
    TrackServiceOp,
    UntrackEntityOp,
    UntrackServiceOp,
)

from fxtr_workstation_mcp.mutation_builder import build_mutate_request
from fxtr_workstation_mcp.session_state import (
    KIND_CREATE_CALLBACK,
    KIND_EXPORT_ENTITY,
    KIND_EXPORT_SERVICE,
    KIND_FORGET,
    KIND_SCHEDULE_INVOKE,
    ForgetMissingKey,
    SessionMirror,
    SlotKeyAlreadyExists,
    desired_tracker_set,
)
from fxtr_ws_mcp_sandbox_bridge.protocols import slot_handle

pytestmark = pytest.mark.anyio


# --- desired_tracker_set ---------------------------------------------


async def test_desired_tracker_set_empty() -> None:
    assert desired_tracker_set({}) == (set(), set())


async def test_desired_tracker_set_exported_entity_and_service() -> None:
    data = {
        "foo": ["exported_entity", "abc1234"],
        "bar": ["exported_service", "s5"],
    }
    entities, services = desired_tracker_set(data)
    assert entities == {"abc1234"}
    assert services == {"s5"}


async def test_desired_tracker_set_two_keys_share_short_id() -> None:
    """A short_id referenced by multiple keys appears once in the
    desired set — converging on a workstation-tracker pin."""
    data = {
        "k1": ["exported_entity", "shared"],
        "k2": ["exported_entity", "shared"],
    }
    entities, services = desired_tracker_set(data)
    assert entities == {"shared"}
    assert services == set()


async def test_desired_tracker_set_invoke_result_walks_payload() -> None:
    """An ``["invoke_result", payload]`` value contributes any
    entity/service refs nested in its payload to the tracker set."""
    data = {
        "result_key": [
            "invoke_result",
            {
                "result_field": ["entity", "abc"],
                "list_field": [["service", "s1"], "literal"],
            },
        ],
    }
    entities, services = desired_tracker_set(data)
    # ``literal`` would be inside a literal-list-escape (``[[...]]``)
    # in real wire data; here it's a regular list so the walk
    # recurses element-by-element. The ``["service", "s1"]`` tag
    # still contributes.
    assert entities == {"abc"}
    assert services == {"s1"}


async def test_desired_tracker_set_invoke_pending_no_contribution() -> None:
    data = {"k": ["invoke_pending", 7]}
    entities, services = desired_tracker_set(data)
    assert entities == set()
    assert services == set()


# --- build_mutate_request --------------------------------------------


def _fresh_mirror(
    *, data_by_key: dict | None = None, ws_entities: set[str] | None = None
) -> SessionMirror:
    """Build a mirror as if we'd just received a snapshot with the
    given ``data_by_key`` and ``ws_tracked_entities``. The
    workstation state carries both top-level fields populated so
    the patch builder doesn't synthesize an init prologue."""
    m = SessionMirror()
    m.apply_workstation_view(
        state={
            "data_by_key": dict(data_by_key or {}),
            "callback_notifications": {},
        },
        last_state_seq=0,
        last_state_ack_seq=0,
        tracked_entities=sorted(ws_entities or set()),
        tracked_services=[],
    )
    return m


async def test_build_mutate_request_empty_returns_none() -> None:
    """A script that queues no ops and changes nothing about the
    tracker set produces no wire."""
    result = build_mutate_request(
        mirror=SessionMirror(),
        slot_counter=0,
        pending_changes=[],
        runner_queued_ops=[],
        entity_short_ids={},
        ack_seq=0,
        request_id=1,
        subscription_id=2,
        update_id=3,
    )
    assert result is None


async def test_build_mutate_request_single_export() -> None:
    """Fresh session: an init prologue + export's patch op + a
    tracker diff."""
    from fxtr_workstation_mcp.session_state import PendingChange

    mirror = SessionMirror()  # blank session, no data_by_key
    result = build_mutate_request(
        mirror=mirror,
        slot_counter=0,
        # Production: the connection enqueues an init prologue
        # when ``apply_workstation_view`` sees an empty state.
        # We construct that explicitly here.
        pending_changes=[PendingChange(op="initialize")],
        runner_queued_ops=[{"kind": KIND_EXPORT_ENTITY, "slot_key": "foo"}],
        entity_short_ids={slot_handle("foo"): "ent1"},
        ack_seq=0,
        request_id=10,
        subscription_id=11,
        update_id=12,
    )
    assert result is not None
    # Patch: init creates both top-level fields, then add the key.
    assert result.request.patch[0] == {"op": "add", "path": "/data_by_key", "value": {}}
    assert result.request.patch[1] == {
        "op": "add",
        "path": "/callback_notifications",
        "value": {},
    }
    assert result.request.patch[2] == {
        "op": "add",
        "path": "/data_by_key/foo",
        "value": ["exported_entity", "ent1"],
    }
    # Tracker diff: add entity ``ent1``.
    assert result.request.ops == [TrackEntityOp(short_id="ent1")]
    assert result.request.request_id == 10
    assert result.request.subscription_id == 11
    assert result.request.update_id == 12
    # Post-state reflects the new key + tracker set.
    assert result.post_state.get("data_by_key", {}) == {"foo": ["exported_entity", "ent1"]}
    assert result.desired_entities == {"ent1"}
    assert result.desired_services == set()


async def test_build_mutate_request_forget_emits_untrack() -> None:
    """Forgetting the only key referencing an entity → untrack."""
    mirror = _fresh_mirror(
        data_by_key={"foo": ["exported_entity", "ent1"]},
        ws_entities={"ent1"},
    )
    result = build_mutate_request(
        mirror=mirror,
        slot_counter=0,
        pending_changes=[],
        runner_queued_ops=[{"kind": KIND_FORGET, "slot_key": "foo"}],
        entity_short_ids={},
        ack_seq=0,
        request_id=1,
        subscription_id=2,
        update_id=3,
    )
    assert result is not None
    assert result.request.patch == [{"op": "remove", "path": "/data_by_key/foo"}]
    assert result.request.ops == [UntrackEntityOp(short_id="ent1")]
    assert result.post_state.get("data_by_key", {}) == {}
    assert result.desired_entities == set()


async def test_build_mutate_request_forget_one_of_two_keeps_track() -> None:
    """Two keys reference the same short_id; ``forget`` one → no
    untrack op (the other key still references it)."""
    mirror = _fresh_mirror(
        data_by_key={
            "k1": ["exported_entity", "shared"],
            "k2": ["exported_entity", "shared"],
        },
        ws_entities={"shared"},
    )
    result = build_mutate_request(
        mirror=mirror,
        slot_counter=0,
        pending_changes=[],
        runner_queued_ops=[{"kind": KIND_FORGET, "slot_key": "k1"}],
        entity_short_ids={},
        ack_seq=0,
        request_id=1,
        subscription_id=2,
        update_id=3,
    )
    assert result is not None
    assert result.request.patch == [{"op": "remove", "path": "/data_by_key/k1"}]
    # No tracker diff — ``shared`` is still referenced by ``k2``.
    assert result.request.ops == []
    assert result.desired_entities == {"shared"}


async def test_build_mutate_request_forget_missing_key_raises() -> None:
    with pytest.raises(ForgetMissingKey):
        build_mutate_request(
            mirror=SessionMirror(),
            slot_counter=0,
            pending_changes=[],
            runner_queued_ops=[{"kind": KIND_FORGET, "slot_key": "ghost"}],
            entity_short_ids={},
            ack_seq=0,
            request_id=1,
            subscription_id=2,
            update_id=3,
        )


async def test_build_mutate_request_export_service() -> None:
    """ExportServiceOp emits an add for the key + a TrackServiceOp."""
    result = build_mutate_request(
        mirror=SessionMirror(),
        slot_counter=0,
        pending_changes=[],
        runner_queued_ops=[{"kind": KIND_EXPORT_SERVICE, "slot_key": "svc", "short_id": "s7"}],
        entity_short_ids={},
        ack_seq=0,
        request_id=1,
        subscription_id=2,
        update_id=3,
    )
    assert result is not None
    # First op creates ``data_by_key``; second adds the key.
    assert any(
        p == {"op": "add", "path": "/data_by_key/svc", "value": ["exported_service", "s7"]}
        for p in result.request.patch
    )
    assert result.request.ops == [TrackServiceOp(short_id="s7")]
    assert result.desired_services == {"s7"}


async def test_build_mutate_request_rejects_export_over_existing_key() -> None:
    """Re-exporting an existing key without prior forget raises
    SlotKeyAlreadyExists (stage7post1 fix). Tracker diff isn't
    computed because the build aborts before it."""
    mirror = _fresh_mirror(
        data_by_key={"foo": ["exported_entity", "old"]},
        ws_entities={"old"},
    )
    with pytest.raises(SlotKeyAlreadyExists) as excinfo:
        build_mutate_request(
            mirror=mirror,
            slot_counter=0,
            pending_changes=[],
            runner_queued_ops=[{"kind": KIND_EXPORT_ENTITY, "slot_key": "foo"}],
            entity_short_ids={slot_handle("foo"): "new"},
            ack_seq=0,
            request_id=1,
            subscription_id=2,
            update_id=3,
        )
    assert excinfo.value.slot_key == "foo"
    assert excinfo.value.kind == KIND_EXPORT_ENTITY


async def test_build_mutate_request_create_callback_rejects_existing_key() -> None:
    """Original bug repro at the build_mutate_request level: a
    second create_callback against an existing ``callback_open``
    slot would have silently replaced the value, orphaning the
    first callback. We now error early."""
    mirror = _fresh_mirror(
        data_by_key={"demo_callback": ["callback_open", 3]},
    )
    with pytest.raises(SlotKeyAlreadyExists) as excinfo:
        build_mutate_request(
            mirror=mirror,
            slot_counter=5,
            pending_changes=[],
            runner_queued_ops=[{"kind": KIND_CREATE_CALLBACK, "slot_key": "demo_callback"}],
            entity_short_ids={},
            ack_seq=0,
            request_id=1,
            subscription_id=2,
            update_id=3,
        )
    assert excinfo.value.slot_key == "demo_callback"
    assert excinfo.value.running_value == ["callback_open", 3]


async def test_build_mutate_request_schedule_invoke_rejects_existing_key() -> None:
    """``schedule_invoke`` is one of the four data_by_key-writing
    kinds; the existence check applies uniformly. The slot
    allocation walker still runs (it sees the schedule_invoke op
    and tries to allocate a slot), but the per-op existence check
    fires before any wire ops are emitted."""
    mirror = _fresh_mirror(
        data_by_key={"slot": ["invoke_result", 42]},
    )
    with pytest.raises(SlotKeyAlreadyExists) as excinfo:
        build_mutate_request(
            mirror=mirror,
            slot_counter=0,
            pending_changes=[],
            runner_queued_ops=[
                {
                    "kind": KIND_SCHEDULE_INVOKE,
                    "slot_key": "slot",
                    "service_short_id": "s1",
                    "method_name": "m",
                    "args": [],
                    "entity_arg_handles": [],
                }
            ],
            entity_short_ids={},
            ack_seq=0,
            request_id=1,
            subscription_id=2,
            update_id=3,
        )
    assert excinfo.value.slot_key == "slot"
    assert excinfo.value.kind == KIND_SCHEDULE_INVOKE


async def test_build_mutate_request_export_new_while_forgetting_different_key() -> None:
    """Exporting a new slot_key in the same script that forgets a
    different (existing) slot_key works fine — the existence check
    only fires for the slot being written to."""
    mirror = _fresh_mirror(
        data_by_key={"existing": ["exported_entity", "x"]},
        ws_entities={"x"},
    )
    result = build_mutate_request(
        mirror=mirror,
        slot_counter=0,
        pending_changes=[],
        runner_queued_ops=[
            {"kind": KIND_EXPORT_ENTITY, "slot_key": "new"},
            {"kind": KIND_FORGET, "slot_key": "existing"},
        ],
        entity_short_ids={slot_handle("new"): "y"},
        ack_seq=0,
        request_id=1,
        subscription_id=2,
        update_id=3,
    )
    assert result is not None
    # Post-state has the new key, lost the old key.
    assert result.post_state["data_by_key"] == {"new": ["exported_entity", "y"]}
    # Tracker diff: untrack old, track new.
    assert UntrackEntityOp(short_id="x") in result.request.ops
    assert TrackEntityOp(short_id="y") in result.request.ops


async def test_build_mutate_request_forget_then_create_callback_in_same_mutation() -> None:
    """Cross-script forget+create against the same slot_key works
    because the running state is mutated in-place: the forget op
    removes the key from the running data_by_key before the
    create_callback's existence check runs.

    (In-script forget+create is blocked by the sandbox's
    ``used_slot_keys`` set, so this scenario actually only arises
    if `pending_changes` previously populated the slot — which is
    fine and tested elsewhere. Here we exercise the within-mutation
    semantics of `_apply_script_op` directly.)
    """
    mirror = _fresh_mirror(
        data_by_key={"k": ["callback_open", 1]},
    )
    result = build_mutate_request(
        mirror=mirror,
        slot_counter=2,
        pending_changes=[],
        # Note: a real script can't issue both ops against the same
        # slot_key (sandbox blocks). This test exists to verify the
        # builder's running-state semantics; it bypasses the sandbox.
        runner_queued_ops=[
            {"kind": KIND_FORGET, "slot_key": "k"},
            {"kind": KIND_CREATE_CALLBACK, "slot_key": "k"},
        ],
        entity_short_ids={},
        ack_seq=0,
        request_id=1,
        subscription_id=2,
        update_id=3,
    )
    assert result is not None
    # The patch should remove then add (since the forget cleared
    # the key from the running state before create_callback's
    # existence check).
    assert result.request.patch == [
        {"op": "remove", "path": "/data_by_key/k"},
        {"op": "add", "path": "/data_by_key/k", "value": ["callback_open", 3]},
    ]


# --- Pre-flush of unflushed entries (3-post-2) -----------------------


async def test_build_mutate_request_preflushes_pending_changes() -> None:
    """A mutation built while ``_pending_changes`` has entries
    emits a ``replace`` per entry (in log order) at the head of
    the patch, before the script's queued ops layer on top. The
    post-state reflects the running state including the pre-flush
    + the script ops."""
    from fxtr_workstation_mcp.session_state import PendingChange

    # Workstation has key "x" set to ["invoke_pending", 1]; locally
    # we promoted it to ["invoke_result", "RX"] and recorded the
    # pending change. Mirror.data_by_key is the local view (with
    # the promotion); ws_tracked_entities sees no entity yet because
    # the workstation's tracker set is unchanged from before.
    mirror = _fresh_mirror(
        data_by_key={"x": ["invoke_result", "RX"]},
        ws_entities=set(),
    )
    pending = [
        PendingChange(op="set", path=("data_by_key", "x"), value=["invoke_result", "RX"], seq=11)
    ]
    result = build_mutate_request(
        mirror=mirror,
        slot_counter=0,
        pending_changes=pending,
        runner_queued_ops=[{"kind": KIND_EXPORT_ENTITY, "slot_key": "y"}],
        entity_short_ids={slot_handle("y"): "ent_y"},
        ack_seq=11,
        request_id=1,
        subscription_id=2,
        update_id=3,
    )
    assert result is not None
    # Patch:
    # 1. ``replace`` for the pending "x" (workstation has it
    #    pending, we promoted to result locally).
    # 2. ``add`` for the new "y" key from the script's
    #    export_entity op.
    assert result.request.patch == [
        {"op": "replace", "path": "/data_by_key/x", "value": ["invoke_result", "RX"]},
        {"op": "add", "path": "/data_by_key/y", "value": ["exported_entity", "ent_y"]},
    ]


async def test_build_mutate_request_pending_only_still_sends() -> None:
    """An empty script with only pending changes to flush still
    produces a non-None result (we need to absorb the local effects
    into the workstation view)."""
    from fxtr_workstation_mcp.session_state import PendingChange

    mirror = _fresh_mirror(
        data_by_key={"k": ["invoke_result", "v"]},
        ws_entities=set(),
    )
    result = build_mutate_request(
        mirror=mirror,
        slot_counter=0,
        pending_changes=[
            PendingChange(op="set", path=("data_by_key", "k"), value=["invoke_result", "v"], seq=5)
        ],
        runner_queued_ops=[],
        entity_short_ids={},
        ack_seq=5,
        request_id=1,
        subscription_id=2,
        update_id=3,
    )
    assert result is not None
    assert result.request.patch == [
        {"op": "replace", "path": "/data_by_key/k", "value": ["invoke_result", "v"]},
    ]


async def test_build_mutate_request_no_unflushed_no_script_returns_none() -> None:
    """Empty unflushed + empty script = no-op = None."""
    result = build_mutate_request(
        mirror=SessionMirror(),
        slot_counter=0,
        pending_changes=[],
        runner_queued_ops=[],
        entity_short_ids={},
        ack_seq=0,
        request_id=1,
        subscription_id=2,
        update_id=3,
    )
    assert result is None
