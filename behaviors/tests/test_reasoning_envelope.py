"""Reasoning state in a harness's opaque slot: the envelope, the packings that preceded it, and
the rule that a value without an envelope is the slot dialect's own state."""

import base64
import itertools
import json
import random
from dataclasses import dataclass

import pytest

from behaviors.implementations.dialects._reasoning_envelope import (
    ENVELOPE_PREFIX,
    BundledReasoning,
    EchoedReasoning,
    envelope_bytes,
    envelope_text,
    read_bundles,
    read_text_slot,
)
from behaviors.implementations.dialects.anthropic_messages import AnthropicSignature
from behaviors.types.content import ContentReasoning, ReasoningForm
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData


@dataclass(frozen=True)
class _ElsewhereState(TaggedDataclass, fxtr_type="com.example.ElsewhereState.v1"):
    """A payload type another codebase's model codec might define."""

    token: str
    steps: tuple[int, ...]


@dataclass(frozen=True)
class _BinaryState(TaggedDataclass, fxtr_type="com.example.BinaryState.v1"):
    blob: bytes


def _anthropic(raw: str) -> TaggedDataclass:
    return AnthropicSignature(signature=raw)


_ELSEWHERE = DynamicData.from_tagged(_ElsewhereState(token="t" * 2000, steps=(1, 2, 3)))


@pytest.mark.parametrize(
    "state", [_ELSEWHERE, DynamicData.from_tagged(AnthropicSignature("sig")), None]
)
@pytest.mark.parametrize("form", ["full", "summary", "redacted"])
def test_an_envelope_reads_back_as_the_block_it_wrapped(
    form: ReasoningForm, state: DynamicData | None
) -> None:
    text = envelope_text(form, state)
    assert text.startswith(ENVELOPE_PREFIX)
    assert read_text_slot(text, _anthropic) == EchoedReasoning(form, state)


_BUNDLE = (
    BundledReasoning(0, "summary", "first", _ELSEWHERE),
    BundledReasoning(1, "full", "second", None),
    BundledReasoning(1, "redacted", "", DynamicData.from_tagged(AnthropicSignature("sig"))),
)


def test_a_bundle_reads_back_as_its_blocks() -> None:
    assert read_bundles(envelope_bytes(_BUNDLE)) == _BUNDLE


def test_a_bundle_is_recognized_whatever_base64_alphabet_a_client_echoes() -> None:
    # Text whose base64 has `+`, `/` and padding, which a URL-safe unpadded echo all changes.
    bundle = (BundledReasoning(0, "summary", "???>>>", None),)
    sent = envelope_bytes(bundle)
    urlsafe_unpadded = base64.urlsafe_b64encode(base64.b64decode(sent)).decode().rstrip("=")
    assert urlsafe_unpadded != sent
    assert read_bundles(urlsafe_unpadded) == bundle


def test_bundles_a_harness_concatenated_read_as_all_their_blocks() -> None:
    # Antigravity CLI joins the bytes of every signature of a response onto one part.
    first, second = envelope_bytes(_BUNDLE[:1]), envelope_bytes(_BUNDLE[1:])
    joined = base64.b64encode(base64.b64decode(first) + base64.b64decode(second)).decode()
    assert read_bundles(joined) == _BUNDLE


def test_a_truncated_bundle_keeps_the_blocks_before_it() -> None:
    first = base64.b64decode(envelope_bytes(_BUNDLE[:1]))
    second = base64.b64decode(envelope_bytes(_BUNDLE[1:]))
    cut = base64.b64encode(first + second[:-5]).decode()
    assert read_bundles(cut) == _BUNDLE[:1]


def test_a_value_without_an_envelope_is_the_slot_dialects_own_state() -> None:
    assert read_text_slot("EqQBCkYIBxgC", _anthropic) == EchoedReasoning(
        None, DynamicData.from_tagged(AnthropicSignature("EqQBCkYIBxgC"))
    )
    assert read_bundles(base64.b64encode(b"gemini signature bytes").decode()) is None


def _text_envelope(payload: object) -> str:
    """A string-slot envelope around any JSON payload."""
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return ENVELOPE_PREFIX + encoded


def test_a_state_that_is_not_json_crosses_without_its_state() -> None:
    binary = DynamicData.from_tagged(_BinaryState(blob=b"\x00\x01"))
    assert read_text_slot(envelope_text("summary", binary), _anthropic) == EchoedReasoning(
        "summary", None
    )


def test_an_unreadable_envelope_loses_its_state_rather_than_reading_as_native() -> None:
    assert read_text_slot(ENVELOPE_PREFIX + "not-json", _anthropic) == EchoedReasoning(None, None)


def test_reasoning_with_a_foreign_state_round_trips_through_storage() -> None:
    block = ContentReasoning(text="hm", form="summary", reasoning_state=_ELSEWHERE)
    data = DEFAULT_CONVERTER.unstructure_entity(block)
    assert DEFAULT_CONVERTER.structure(data, ContentReasoning) == block


# --- every input reads as a value or a documented drop --------------------------------------------

# A JSON value of each type, and objects shaped like states: untagged, mis-tagged, and tagged with
# a type whose fields are wrong (which the envelope passes on: only the owning codec decodes it).
_JSON_VALUES: tuple[object, ...] = (
    None,
    "full",
    "x",
    0,
    -1,
    1.5,
    True,
    [],
    [1],
    {},
    {"$type": 5},
    {"$type": "com.example.Unknown.v1"},
    {"$type": AnthropicSignature.fxtr_type_id(), "signature": 3},
)


def _framed(payload: object) -> str:
    body = json.dumps(payload).encode()
    return base64.b64encode(b"behaviors-reasoning-v1:%d:" % len(body) + body).decode()


@pytest.mark.parametrize("value", _JSON_VALUES, ids=repr)
def test_an_envelope_with_any_field_value_reads_without_raising(value: object) -> None:
    texts = [
        _text_envelope({"form": value, "reasoning_state": None}),
        _text_envelope({"form": "full", "reasoning_state": value}),
        _text_envelope(value),
    ]
    for text in texts:
        read_text_slot(text, _anthropic)
    entry = {"at": 0, "form": "full", "text": "t", "reasoning_state": None}
    bundles = [
        _framed(value),
        _framed({"blocks": value}),
        _framed({"blocks": [value]}),
        *(_framed({"blocks": [{**entry, field: value}]}) for field in entry),
    ]
    for bundle in bundles:
        read_bundles(bundle)


def test_random_bytes_in_a_slot_read_without_raising() -> None:
    generator = random.Random(0)
    for _, framed in itertools.product(range(2000), (False, True)):
        raw = generator.randbytes(generator.randrange(48))
        if framed:
            raw = b"behaviors-reasoning-v1:" + raw
        read_bundles(base64.b64encode(raw).decode())
        read_text_slot(ENVELOPE_PREFIX + base64.urlsafe_b64encode(raw).decode(), _anthropic)
        read_text_slot(raw.decode("latin-1"), _anthropic)
