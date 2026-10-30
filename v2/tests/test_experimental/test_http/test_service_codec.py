"""Tests for the stateless service wire codec (``service_codec``)."""

from __future__ import annotations

import base64

import pytest

from fxtr2.data.entity_core import FxtrID, RawWeakFxtrID
from fxtr2.experimental.http.service_codec import (
    CallbackAddress,
    ServiceCodecError,
    ServiceDescriptor,
    TransientService,
    escape_value,
    unescape_args,
    unescape_value,
)


def _eid(byte: int) -> FxtrID:
    return FxtrID.from_tag_and_payload("d", bytes([byte]) * 32)


def _nid(byte: int) -> FxtrID:
    """A bare provenance-node (``n``) id, for layering in tests."""
    return FxtrID.from_tag_and_payload("n", bytes([byte]) * 32)


def _layered(data_byte: int, prov_byte: int) -> FxtrID:
    """A layered ``(data, prov)`` FxtrID: a ``d`` data id with an ``n`` prov parent."""
    return FxtrID.from_tag_and_payload(
        "d", bytes([data_byte]) * 32, provenance_parent=_nid(prov_byte)
    )


# --------------------------------------------------------------------------
# Primitives + containers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, True, False, 0, 42, -1, 3.5, "", "hello"])
def test_primitives_round_trip(value):
    assert unescape_value(escape_value(value)) == value


def test_list_is_wrapped_and_unwrapped():
    assert escape_value([1, 2, 3]) == [[1, 2, 3]]
    assert unescape_value([[1, 2, 3]]) == [1, 2, 3]
    # Nested.
    assert unescape_value(escape_value([[1], [2, 3]])) == [[1], [2, 3]]


def test_dict_round_trip():
    v = {"a": 1, "b": [1, 2], "c": {"d": "x"}}
    assert unescape_value(escape_value(v)) == v


def test_dict_non_str_key_rejected():
    with pytest.raises(ServiceCodecError):
        escape_value({1: "x"})


# --------------------------------------------------------------------------
# Entity tags
# --------------------------------------------------------------------------


def test_entity_round_trip():
    fid = _eid(1)
    escaped = escape_value(fid)
    assert escaped == ["entity", fid.id_string()]
    decoded = unescape_value(escaped)
    assert isinstance(decoded, FxtrID)
    assert decoded == fid


def test_entity_inside_container():
    fid = _eid(2)
    v = {"child": fid, "list": [fid]}
    decoded = unescape_value(escape_value(v))
    assert decoded["child"] == fid
    assert decoded["list"] == [fid]


def test_malformed_entity_id_string_rejected():
    with pytest.raises(ServiceCodecError):
        unescape_value(["entity", "not-a-valid-id"])


def test_entity_tag_wrong_arity_rejected():
    with pytest.raises(ServiceCodecError):
        unescape_value(["entity"])
    with pytest.raises(ServiceCodecError):
        unescape_value(["entity", 123])
    # 3-element with a non-str element, and 4+ elements — never silently
    # truncated to the first two.
    with pytest.raises(ServiceCodecError):
        unescape_value(["entity", _eid(1).id_string(), 123])
    with pytest.raises(ServiceCodecError):
        unescape_value(["entity", _eid(1).id_string(), _nid(2).id_string(), "extra"])


# --------------------------------------------------------------------------
# Layered (provenanced) entity refs
# --------------------------------------------------------------------------


def test_layered_entity_escape_provenanced():
    fid = _layered(1, 2)
    escaped = escape_value(fid, provenanced=True)
    assert escaped == ["entity", fid.id_string(), _nid(2).id_string()]
    # The data half stays the bare local id (no '@' anywhere).
    assert "@" not in escaped[1]
    assert "@" not in escaped[2]


def test_layered_entity_escape_bare_when_not_provenanced():
    # Default (provenanced=False) emits the bare 2-element form even for a
    # layered id.
    fid = _layered(1, 2)
    assert escape_value(fid) == ["entity", fid.id_string()]
    assert escape_value(fid, provenanced=False) == ["entity", fid.id_string()]


def test_layered_entity_unescape():
    decoded = unescape_value(["entity", _eid(1).id_string(), _nid(2).id_string()])
    assert isinstance(decoded, FxtrID)
    assert decoded == _eid(1)  # equality is data-level
    assert decoded._provenance_parent == _nid(2)


def test_layered_entity_round_trip():
    fid = _layered(3, 4)
    decoded = unescape_value(escape_value(fid, provenanced=True))
    assert decoded == fid
    assert decoded._provenance_parent == fid._provenance_parent


def test_entity_at_form_rejected():
    # The debug-only '@'-form must never be accepted on the wire (it would
    # otherwise be silently parsed into a layered id).
    at_form = f"{_eid(1).id_string()}@{_nid(2).id_string()}"
    with pytest.raises(ServiceCodecError):
        unescape_value(["entity", at_form])
    # Also rejected in either half of a layered ref.
    with pytest.raises(ServiceCodecError):
        unescape_value(["entity", at_form, _nid(2).id_string()])
    with pytest.raises(ServiceCodecError):
        unescape_value(["entity", _eid(1).id_string(), at_form])


def test_raw_ref_with_provenance_asserts():
    # CBOR-loaded refs are always bare; a Raw* whose inner id carries
    # provenance is a contract violation, not a silent drop.
    raw = RawWeakFxtrID(inner_id=_layered(1, 2))
    with pytest.raises(AssertionError):
        escape_value(raw)


def test_raw_ref_bare_escapes_plainly():
    raw = RawWeakFxtrID(inner_id=_eid(5))
    assert escape_value(raw, provenanced=True) == ["entity", _eid(5).id_string()]


# --------------------------------------------------------------------------
# Service descriptors
# --------------------------------------------------------------------------


def test_http_descriptor_round_trip_with_type_id():
    d = ServiceDescriptor(
        kind="http", url="/svc/echo", method="GET", service_type_id=_eid(3).id_string()
    )
    escaped = escape_value(d)
    assert escaped[0] == "service"
    assert escaped[1] == {
        "kind": "http",
        "url": "/svc/echo",
        "method": "GET",
        "service_type_id": _eid(3).id_string(),
    }
    decoded = unescape_value(escaped)
    assert decoded == d


def test_ws_descriptor_round_trip():
    d = ServiceDescriptor(kind="ws", url="ws://h/ws", name="scheduler")
    decoded = unescape_value(escape_value(d))
    assert decoded == d
    # Unset fields omitted from the wire form.
    assert "method" not in escape_value(d)[1]


def test_descriptor_invalid_kind_rejected():
    with pytest.raises(ServiceCodecError):
        unescape_value(["service", {"kind": "bogus"}])
    with pytest.raises(ServiceCodecError):
        unescape_value(["service", {"url": "/x"}])  # missing kind


def test_service_tag_requires_object_payload():
    with pytest.raises(ServiceCodecError):
        unescape_value(["service", "not-an-object"])


# --------------------------------------------------------------------------
# Callback addresses
# --------------------------------------------------------------------------


def test_callback_decodes_to_address():
    decoded = unescape_value(["callback", "inbox-uuid-123"])
    assert decoded == CallbackAddress("inbox-uuid-123")


def test_callback_tag_requires_string_uuid():
    with pytest.raises(ServiceCodecError):
        unescape_value(["callback", 7])


# --------------------------------------------------------------------------
# Bytes
# --------------------------------------------------------------------------


def test_bytes_round_trip():
    data = b"\x00\x01\x02\xff"
    escaped = escape_value(data)
    assert escaped == ["bytes", base64.b64encode(data).decode("ascii")]
    assert unescape_value(escaped) == data


def test_invalid_base64_rejected():
    with pytest.raises(ServiceCodecError):
        unescape_value(["bytes", "!!!not base64!!!"])


# --------------------------------------------------------------------------
# Transient services (ws-only escape)
# --------------------------------------------------------------------------


def test_transient_requires_register_callback():
    t = TransientService(impl={"unsubscribe": lambda *_a, **_k: None})
    with pytest.raises(ServiceCodecError):
        escape_value(t)  # no register_transient → rejected


def test_transient_registers_and_escapes():
    registered: list[TransientService] = []

    def register(t: TransientService) -> str:
        registered.append(t)
        return "transient-7"

    t = TransientService(impl={"unsubscribe": lambda *_a, **_k: None})
    escaped = escape_value(t, register_transient=register)
    assert escaped == ["service", {"kind": "transient", "id": "transient-7"}]
    assert registered == [t]
    # Decodes back to a transient descriptor.
    assert unescape_value(escaped) == ServiceDescriptor(kind="transient", id="transient-7")


# --------------------------------------------------------------------------
# unescape_args
# --------------------------------------------------------------------------


def test_unescape_args_happy():
    fid = _eid(4)
    args, kwargs = unescape_args(
        {"args": [1, ["entity", fid.id_string()]], "kwargs": {"name": "x"}}
    )
    assert args == [1, fid]
    assert kwargs == {"name": "x"}


def test_unescape_args_defaults_empty():
    args, kwargs = unescape_args({})
    assert args == []
    assert kwargs == {}


def test_unescape_args_wrong_shape_rejected():
    with pytest.raises(ServiceCodecError):
        unescape_args([1, 2])  # not an object
    with pytest.raises(ServiceCodecError):
        unescape_args({"args": "nope"})
    with pytest.raises(ServiceCodecError):
        unescape_args({"kwargs": [1]})
