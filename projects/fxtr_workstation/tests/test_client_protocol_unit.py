"""Unit tests for the ``/client`` wire protocol parse/serialize layer.

End-to-end tests live in ``test_client_protocol.py`` once the
ASGI route is wired up.
"""

from __future__ import annotations

import json

import pytest
from fxtr_workstation.client_protocol import (
    RESOURCE_KINDS,
    AckResponse,
    CreateSessionRequest,
    ErrorResponse,
    ProtocolError,
    ResourceChangesEvent,
    ResourceSnapshotEvent,
    SessionCreatedResponse,
    SessionSnapshotEvent,
    SubscribeRequest,
    SubscribeSessionRequest,
    UnsubscribeRequest,
    parse_text,
    serialize_to_text,
)

# --- parse_text -------------------------------------------------------


@pytest.mark.parametrize("resource", sorted(RESOURCE_KINDS))
def test_parse_subscribe(resource: str) -> None:
    req = parse_text(json.dumps({"request_id": 7, "op": "subscribe", "resource": resource}))
    assert req == SubscribeRequest(request_id=7, resource=resource)  # type: ignore[arg-type]


def test_parse_unsubscribe() -> None:
    req = parse_text(json.dumps({"request_id": 12, "op": "unsubscribe", "subscription_id": 7}))
    assert req == UnsubscribeRequest(request_id=12, subscription_id=7)


def test_parse_invalid_json() -> None:
    with pytest.raises(ProtocolError, match="invalid JSON"):
        parse_text("{not json")


@pytest.mark.parametrize(
    "obj",
    [
        # not an object
        "not an object",
        # missing request_id
        {"op": "subscribe", "resource": "services"},
        # non-int request_id
        {"request_id": "x", "op": "subscribe", "resource": "services"},
        # bool request_id (msgspec rejects bool for int fields)
        {"request_id": True, "op": "subscribe", "resource": "services"},
        # unknown op (tagged-union discriminator fails)
        {"request_id": 1, "op": "wat", "resource": "services"},
        # missing op
        {"request_id": 1, "resource": "services"},
        # subscribe missing resource
        {"request_id": 1, "op": "subscribe"},
        # subscribe unknown resource (literal mismatch)
        {"request_id": 1, "op": "subscribe", "resource": "nope"},
        # unsubscribe missing subscription_id
        {"request_id": 1, "op": "unsubscribe"},
        # unsubscribe non-int subscription_id
        {"request_id": 1, "op": "unsubscribe", "subscription_id": "x"},
    ],
)
def test_parse_errors(obj: object) -> None:
    with pytest.raises(ProtocolError):
        parse_text(json.dumps(obj))


# --- serialize_to_text ------------------------------------------------


def test_serialize_ack() -> None:
    text = serialize_to_text(AckResponse(request_id=4))
    assert json.loads(text) == {"request_id": 4, "type": "ack"}


def test_serialize_error() -> None:
    text = serialize_to_text(ErrorResponse(request_id=5, message="boom"))
    assert json.loads(text) == {"request_id": 5, "type": "error", "message": "boom"}


def test_serialize_resource_snapshot() -> None:
    data: list[dict[str, object]] = [{"connection_id": "abc"}]
    text = serialize_to_text(ResourceSnapshotEvent(subscription_id=9, data=data))
    assert json.loads(text) == {
        "subscription_id": 9,
        "type": "resource_snapshot",
        "data": data,
    }


def test_serialize_resource_changes() -> None:
    text = serialize_to_text(
        ResourceChangesEvent(
            subscription_id=12,
            added=[{"connection_id": "abc"}],
            updated=[{"connection_id": "def"}],
            removed=["ghi"],
        )
    )
    assert json.loads(text) == {
        "subscription_id": 12,
        "type": "resource_changes",
        "added": [{"connection_id": "abc"}],
        "updated": [{"connection_id": "def"}],
        "removed": ["ghi"],
    }


def test_parse_subscribe_with_debug_flag() -> None:
    """The new ``debug`` flag opts into debug-only fields. Defaults
    to false when omitted."""
    req_default = parse_text(
        json.dumps({"request_id": 1, "op": "subscribe", "resource": "services"})
    )
    assert req_default == SubscribeRequest(request_id=1, resource="services")
    assert req_default.debug is False  # type: ignore[union-attr]
    req_debug = parse_text(
        json.dumps({"request_id": 2, "op": "subscribe", "resource": "services", "debug": True})
    )
    assert req_debug == SubscribeRequest(request_id=2, resource="services", debug=True)


def test_serialize_state_update_with_closed_flag() -> None:
    """A close-mutation produces a StateUpdateEvent with closed=True."""
    import msgspec
    from fxtr_workstation.client_protocol import StateUpdateEvent

    text = (
        msgspec.json.Encoder()
        .encode(StateUpdateEvent(seq=1, ack_seq=0, state={}, closed=True))
        .decode()
    )
    assert json.loads(text) == {
        "kind": "state_update",
        "seq": 1,
        "ack_seq": 0,
        "state": {},
        "closed": True,
        "tracked_entities": [],
        "tracked_services": [],
        "slot_counter": 0,
    }


# --- Round-trip ----------------------------------------------------


# --- Session ops (stage 3a) ------------------------------------------


def test_parse_create_session() -> None:
    req = parse_text(json.dumps({"request_id": 3, "op": "create_session", "metadata": {"k": 1}}))
    assert req == CreateSessionRequest(request_id=3, metadata={"k": 1})


def test_parse_subscribe_session() -> None:
    req = parse_text(json.dumps({"request_id": 5, "op": "subscribe_session", "session_id": "abc"}))
    assert req == SubscribeSessionRequest(request_id=5, session_id="abc")


def test_parse_subscribe_session_debug() -> None:
    from fxtr_workstation.client_protocol import SubscribeSessionDebugRequest

    req = parse_text(
        json.dumps({"request_id": 8, "op": "subscribe_session_debug", "session_id": "abc"})
    )
    assert req == SubscribeSessionDebugRequest(request_id=8, session_id="abc")


def test_serialize_session_debug_snapshot() -> None:
    from fxtr_workstation.client_protocol import SessionDebugSnapshotEvent

    text = serialize_to_text(
        SessionDebugSnapshotEvent(
            subscription_id=12,
            session_id="abc",
            entity_refcounts={"d1": 1, "d2": 2},
            service_refcounts={"s0": 1},
            slot_counter=5,
        )
    )
    assert json.loads(text) == {
        "subscription_id": 12,
        "type": "session_debug_snapshot",
        "session_id": "abc",
        "entity_refcounts": {"d1": 1, "d2": 2},
        "service_refcounts": {"s0": 1},
        "slot_counter": 5,
        "open_callbacks": {},
        "open_invokes": {},
        "open_loads": {},
        "preserved_events": [],
    }


def test_serialize_session_created() -> None:
    text = serialize_to_text(SessionCreatedResponse(request_id=3, session_id="abc"))
    assert json.loads(text) == {
        "request_id": 3,
        "type": "session_created",
        "session_id": "abc",
    }


def test_serialize_session_snapshot() -> None:
    from fxtr_workstation.client_protocol import StateUpdateEvent

    text = serialize_to_text(
        SessionSnapshotEvent(
            subscription_id=5,
            session_id="abc",
            last_state=StateUpdateEvent(seq=0, ack_seq=0, state={"k": 1}, slot_counter=0),
            preserved_events=[],
            subsequent_events=[],
        )
    )
    assert json.loads(text) == {
        "subscription_id": 5,
        "type": "session_snapshot",
        "session_id": "abc",
        "last_state": {
            "kind": "state_update",
            "seq": 0,
            "ack_seq": 0,
            "state": {"k": 1},
            "closed": False,
            "tracked_entities": [],
            "tracked_services": [],
            "slot_counter": 0,
        },
        "preserved_events": [],
        "subsequent_events": [],
    }


# --- Round-trip ----------------------------------------------------


def test_round_trip_subscribe() -> None:
    """Mostly a sanity check that the on-wire shape is the documented
    flat form (no nested ``args``)."""
    obj = {"request_id": 1, "op": "subscribe", "resource": "services"}
    req = parse_text(json.dumps(obj))
    assert isinstance(req, SubscribeRequest)
    # And the field names match the wire keys exactly.
    assert req.request_id == 1 and req.resource == "services"
