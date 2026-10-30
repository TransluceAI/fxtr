"""Stage 3c.1 tests: list-based escape codec.

Pure-function tests for ``fxtr_workstation.wire_escape.escape`` /
``unescape``. ServiceRefs are built via ``scope.mount(EchoImpl())``
(real instances); FxtrIDs are synthesized with
``FxtrID.from_tag_and_payload`` because we don't need them to be
content-addressed to any real entity for the codec.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, ServiceRef
from fxtr_workstation.wire_escape import (
    RESERVED_TAGS,
    TAG_BYTES,
    TAG_CALLBACK,
    TAG_ENTITY,
    TAG_SERVICE,
    WireEscapeError,
    escape,
    unescape,
)

from ._fixtures import EchoImpl, EchoProto

# --- Helpers --------------------------------------------------------


def _fid(byte: int) -> FxtrID:
    """Build a synthetic ``d``-tagged FxtrID for codec tests."""
    return FxtrID.from_tag_and_payload("d", bytes([byte]) * 32)


def _entity_table(*ids: FxtrID) -> tuple[Callable[[FxtrID], str], Callable[[str], FxtrID]]:
    """Round-trip table for entities: encode by index, lookup by string key."""
    by_id = {fid: f"e{i}" for i, fid in enumerate(ids)}
    by_short = {short: fid for fid, short in by_id.items()}

    def encode(fid: FxtrID) -> str:
        return by_id[fid]

    def lookup(short: str) -> FxtrID:
        if short not in by_short:
            raise WireEscapeError(f"unknown entity short id {short!r}")
        return by_short[short]

    return encode, lookup


def _service_table(
    *refs: ServiceRef[Any],
) -> tuple[Callable[[ServiceRef[Any]], str], Callable[[str], ServiceRef[Any]]]:
    """Round-trip table for services."""
    by_ref = {ref: f"s{i}" for i, ref in enumerate(refs)}
    by_short = {short: ref for ref, short in by_ref.items()}

    def encode(ref: ServiceRef[Any]) -> str:
        return by_ref[ref]

    def lookup(short: str) -> ServiceRef[Any]:
        if short not in by_short:
            raise WireEscapeError(f"unknown service short id {short!r}")
        return by_short[short]

    return encode, lookup


def _no_entities(_: FxtrID) -> str:
    raise AssertionError("encode_entity should not be called")


def _no_services(_: ServiceRef[Any]) -> str:
    raise AssertionError("encode_service should not be called")


def _no_lookup_entity(_: str) -> FxtrID:
    raise AssertionError("lookup_entity should not be called")


def _no_lookup_service(_: str) -> ServiceRef[Any]:
    raise AssertionError("lookup_service should not be called")


# --- Reserved tag set is what we expect ----------------------------


def test_reserved_tags_are_exactly_four() -> None:
    assert RESERVED_TAGS == {TAG_ENTITY, TAG_SERVICE, TAG_BYTES, TAG_CALLBACK}


# --- Primitives pass through ---------------------------------------


@pytest.mark.parametrize("value", [None, True, False, 0, 1, -7, 3.14, "", "hello"])
def test_primitive_round_trip(value: Any) -> None:
    encoded = escape(value, encode_entity=_no_entities, encode_service=_no_services)
    assert encoded == value
    decoded = unescape(encoded, lookup_entity=_no_lookup_entity, lookup_service=_no_lookup_service)
    assert decoded == value


# --- Lists wrap ----------------------------------------------------


def test_empty_list_wraps_to_double_nested() -> None:
    encoded = escape([], encode_entity=_no_entities, encode_service=_no_services)
    assert encoded == [[]]
    decoded = unescape(encoded, lookup_entity=_no_lookup_entity, lookup_service=_no_lookup_service)
    assert decoded == []


def test_flat_list_round_trip() -> None:
    value = [1, 2, "three", None, True]
    encoded = escape(value, encode_entity=_no_entities, encode_service=_no_services)
    assert encoded == [[1, 2, "three", None, True]]
    decoded = unescape(encoded, lookup_entity=_no_lookup_entity, lookup_service=_no_lookup_service)
    assert decoded == value


def test_nested_list_round_trip() -> None:
    value: list[Any] = [[1, [2, 3]], []]
    encoded = escape(value, encode_entity=_no_entities, encode_service=_no_services)
    # Each list level adds a wrap.
    assert encoded == [[[[1, [[2, 3]]]], [[]]]]
    decoded = unescape(encoded, lookup_entity=_no_lookup_entity, lookup_service=_no_lookup_service)
    assert decoded == value


def test_tuple_encodes_as_list() -> None:
    encoded = escape((1, 2, 3), encode_entity=_no_entities, encode_service=_no_services)
    assert encoded == [[1, 2, 3]]
    decoded = unescape(encoded, lookup_entity=_no_lookup_entity, lookup_service=_no_lookup_service)
    # Decoding always produces a list (wire has no tuple).
    assert decoded == [1, 2, 3]


# --- Dicts walk ----------------------------------------------------


def test_empty_dict_round_trip() -> None:
    encoded = escape({}, encode_entity=_no_entities, encode_service=_no_services)
    assert encoded == {}
    decoded = unescape(encoded, lookup_entity=_no_lookup_entity, lookup_service=_no_lookup_service)
    assert decoded == {}


def test_dict_round_trip_with_nested_list() -> None:
    value = {"a": 1, "b": [2, 3], "c": {"d": "e"}}
    encoded = escape(value, encode_entity=_no_entities, encode_service=_no_services)
    assert encoded == {"a": 1, "b": [[2, 3]], "c": {"d": "e"}}
    decoded = unescape(encoded, lookup_entity=_no_lookup_entity, lookup_service=_no_lookup_service)
    assert decoded == value


def test_escape_rejects_non_string_dict_keys() -> None:
    with pytest.raises(WireEscapeError, match="dict key must be str"):
        escape({1: "x"}, encode_entity=_no_entities, encode_service=_no_services)


# --- Entity / service round trips ----------------------------------


def test_entity_fxtr_id_round_trip() -> None:
    fid = _fid(0xAA)
    encode, lookup = _entity_table(fid)
    encoded = escape(fid, encode_entity=encode, encode_service=_no_services)
    assert encoded == [TAG_ENTITY, "e0"]
    decoded = unescape(encoded, lookup_entity=lookup, lookup_service=_no_lookup_service)
    assert decoded == fid


def test_entity_ref_encodes_as_entity_tag_and_decodes_to_bare_fxtr_id() -> None:
    """``EntityRef`` on the encode side reduces to ``FxtrID`` via
    ``entity_id``; decoding always returns a bare ``FxtrID`` (target-type
    annotation is dropped on the untyped wire path)."""
    fid = _fid(0xBB)
    encode, lookup = _entity_table(fid)
    ref = EntityRef(fid, target_type=UnknownEntity)
    encoded = escape(ref, encode_entity=encode, encode_service=_no_services)
    assert encoded == [TAG_ENTITY, "e0"]
    decoded = unescape(encoded, lookup_entity=lookup, lookup_service=_no_lookup_service)
    assert decoded == fid
    assert not isinstance(decoded, EntityRef)


@pytest.mark.parametrize("wrapper_cls", [RawWeakFxtrID, RawStrongFxtrID, RawFxtrEntityMarkerID])
def test_raw_fxtr_id_wrappers_encode_as_entity_tag(wrapper_cls: type) -> None:
    """``Raw*FxtrID`` wrappers (the CBOR forms of weak/strong/entity-
    marker refs) reduce to bare ``FxtrID`` on the encode side; the
    wire format collapses the strong/weak/marker distinction. Decode
    always returns a bare ``FxtrID`` (consistent with EntityRef)."""
    fid = _fid(0xCD)
    encode, lookup = _entity_table(fid)
    wrapped = wrapper_cls(inner_id=fid)
    encoded = escape(wrapped, encode_entity=encode, encode_service=_no_services)
    assert encoded == [TAG_ENTITY, "e0"]
    decoded = unescape(encoded, lookup_entity=lookup, lookup_service=_no_lookup_service)
    assert decoded == fid


async def test_service_ref_round_trip() -> None:
    async with FxtrScope(name="codec-test") as scope:
        svc = (await scope.mount(EchoImpl())).replace_service_type(EchoProto)
        encode, lookup = _service_table(svc)
        encoded = escape(svc, encode_entity=_no_entities, encode_service=encode)
        assert encoded == [TAG_SERVICE, "s0"]
        decoded = unescape(encoded, lookup_entity=_no_lookup_entity, lookup_service=lookup)
        assert decoded == svc


# --- Mixed nesting -------------------------------------------------


def test_entities_inside_dicts_and_lists() -> None:
    fid_a = _fid(1)
    fid_b = _fid(2)
    encode, lookup = _entity_table(fid_a, fid_b)
    value = {
        "head": fid_a,
        "tail": [fid_b, "literal", [fid_a]],
        "deep": {"x": fid_b},
    }
    encoded = escape(value, encode_entity=encode, encode_service=_no_services)
    assert encoded == {
        "head": [TAG_ENTITY, "e0"],
        "tail": [
            [
                [TAG_ENTITY, "e1"],
                "literal",
                [[[TAG_ENTITY, "e0"]]],
            ]
        ],
        "deep": {"x": [TAG_ENTITY, "e1"]},
    }
    decoded = unescape(encoded, lookup_entity=lookup, lookup_service=_no_lookup_service)
    assert decoded == value


# --- Wire-level decoder rejection paths ----------------------------


def test_unescape_rejects_callback_tag_without_lookup() -> None:
    """Without a ``lookup_callback``, the callback tag is rejected.

    Callers without a session context (typically anything that isn't
    op-validation inside :class:`WorkstationState`) shouldn't be
    accepting callback refs on the wire.
    """
    with pytest.raises(WireEscapeError, match="requires a session context"):
        unescape(
            [TAG_CALLBACK, 0],
            lookup_entity=_no_lookup_entity,
            lookup_service=_no_lookup_service,
        )


def test_unescape_callback_tag_resolves_via_lookup() -> None:
    """With a ``lookup_callback``, the callback tag dispatches to it
    and the resolved value is substituted into the output."""
    sentinel = object()

    def _lookup_callback(slot: int) -> object:
        assert slot == 7
        return sentinel

    result = unescape(
        [TAG_CALLBACK, 7],
        lookup_entity=_no_lookup_entity,
        lookup_service=_no_lookup_service,
        lookup_callback=_lookup_callback,
    )
    assert result is sentinel


@pytest.mark.parametrize(
    "bad",
    [
        [TAG_CALLBACK],  # missing payload
        [TAG_CALLBACK, 7, "extra"],  # too many elements
        [TAG_CALLBACK, "not-an-int"],  # payload not an int
        [TAG_CALLBACK, True],  # bool — caught by the bool guard
    ],
)
def test_unescape_rejects_malformed_callback_tag(bad: list[Any]) -> None:
    with pytest.raises(WireEscapeError, match="malformed callback tag"):
        unescape(
            bad,
            lookup_entity=_no_lookup_entity,
            lookup_service=_no_lookup_service,
            lookup_callback=lambda _slot: None,
        )


@pytest.mark.parametrize(
    "bad",
    [
        # tagged-form arity errors
        [TAG_ENTITY],  # missing payload
        [TAG_ENTITY, "id", "extra"],  # too many elements
        [TAG_ENTITY, 42],  # payload not a string
        [TAG_SERVICE],
        [TAG_SERVICE, "id", "extra"],
        [TAG_SERVICE, 42],
        [TAG_BYTES],
        [TAG_BYTES, "AAA=", "extra"],
        [TAG_BYTES, 42],
    ],
)
def test_unescape_rejects_malformed_tagged_forms(bad: list[Any]) -> None:
    fid = _fid(0)
    _, lookup_e = _entity_table(fid)
    with pytest.raises(WireEscapeError, match="malformed"):
        unescape(bad, lookup_entity=lookup_e, lookup_service=_no_lookup_service)


@pytest.mark.parametrize(
    "bad",
    [
        [],  # length 0 — not tagged, not a literal-list escape
        ["foo"],  # non-reserved string at index 0, length != 1
        ["foo", "bar"],  # ditto
        [1, 2, 3],  # length > 1, not tagged
        [42],  # length 1 but element is not a list
    ],
)
def test_unescape_rejects_unwrapped_or_garbled_lists(bad: list[Any]) -> None:
    with pytest.raises(WireEscapeError, match="malformed list"):
        unescape(bad, lookup_entity=_no_lookup_entity, lookup_service=_no_lookup_service)


def test_unescape_lookup_failure_propagates() -> None:
    _, lookup_e = _entity_table(_fid(0))  # only "e0" is known
    with pytest.raises(WireEscapeError, match="unknown entity short id"):
        unescape(
            [TAG_ENTITY, "nope"],
            lookup_entity=lookup_e,
            lookup_service=_no_lookup_service,
        )


def test_escape_rejects_unsupported_leaf() -> None:
    with pytest.raises(WireEscapeError, match="unsupported value type"):
        escape({1, 2}, encode_entity=_no_entities, encode_service=_no_services)


def test_unescape_rejects_unsupported_leaf() -> None:
    with pytest.raises(WireEscapeError, match="unsupported value type"):
        unescape(
            {1, 2},
            lookup_entity=_no_lookup_entity,
            lookup_service=_no_lookup_service,
        )


# --- Bytes round trip ----------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"hello",
        bytes(range(256)),  # all byte values
        b"\x00\x01\x02",
    ],
)
def test_bytes_round_trip(raw: bytes) -> None:
    encoded = escape(raw, encode_entity=_no_entities, encode_service=_no_services)
    assert isinstance(encoded, list)
    assert len(encoded) == 2
    assert encoded[0] == TAG_BYTES
    assert isinstance(encoded[1], str)
    decoded = unescape(encoded, lookup_entity=_no_lookup_entity, lookup_service=_no_lookup_service)
    assert decoded == raw
    assert isinstance(decoded, bytes)


def test_unescape_rejects_invalid_base64() -> None:
    with pytest.raises(WireEscapeError, match="invalid base64"):
        unescape(
            [TAG_BYTES, "not!valid!base64!"],
            lookup_entity=_no_lookup_entity,
            lookup_service=_no_lookup_service,
        )


def test_bytes_inside_a_dict_round_trips() -> None:
    value = {"blob": b"\x01\x02\x03", "label": "x"}
    encoded = escape(value, encode_entity=_no_entities, encode_service=_no_services)
    decoded = unescape(encoded, lookup_entity=_no_lookup_entity, lookup_service=_no_lookup_service)
    assert decoded == value


# --- Literal lists that contain reserved-tag-shaped values ---------


def test_literal_list_containing_a_tagged_form_round_trips() -> None:
    """A literal Python list whose contents *happen* to look like a
    tagged form should still survive round-trip — the outer wrap
    distinguishes literal from tagged."""
    fid = _fid(7)
    encode, lookup = _entity_table(fid)
    # The Python value is a list that contains an FxtrID.
    value = [fid]
    encoded = escape(value, encode_entity=encode, encode_service=_no_services)
    # Outer wrap = list-escape; inner [TAG_ENTITY, "e0"] = tagged form.
    assert encoded == [[[TAG_ENTITY, "e0"]]]
    decoded = unescape(encoded, lookup_entity=lookup, lookup_service=_no_lookup_service)
    assert decoded == [fid]
