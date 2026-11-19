"""How reasoning crosses a harness that does not speak the reasoning's own dialect.

A harness echoes reasoning back only through its dialect's opaque slot: an Anthropic `thinking`
block's `signature`, a Responses `reasoning` item's `encrypted_content`, a Gemini part's
`thoughtSignature`. A harness-side codec fills that slot in one of two ways:

- **Raw**, when the block's state is the dialect's own (an `AnthropicSignature` in a thinking
  block): the slot holds exactly what the provider issued, so the harness can resume its session
  directly against the provider.
- **Enveloped**, otherwise: the slot holds the block's form and state, recognizably wrapped. That
  covers another provider's state, a payload type of another codebase, and reasoning with no
  state at all (literal text, or a summary that came unsigned), which the harness would otherwise
  have no way to carry.

Decoding an echo inverts that: an enveloped value is unwrapped, and anything else is the dialect's
own state. That rule is what tells an Anthropic signature from an OpenAI payload with no item id,
which look alike: a foreign state never crosses a harness unwrapped.

In a string slot (`envelope_text`) the envelope holds one block: `behaviors-reasoning-v1:`
followed by unpadded URL-safe base64 of the UTF-8 JSON
`{"form": ..., "reasoning_state": <tagged JSON> | null}`, keys sorted, no whitespace.

A Gemini `thoughtSignature` is different on three counts. It is a bytes field carried as base64,
which clients decode and re-encode in either alphabet (google-genai for Python echoes it
URL-safe). Gemini carries one signature per model response, and the harnesses rebuild their
history to match: Antigravity CLI merges a response's thought text into one part and
concatenates its signatures' bytes onto one part, and Gemini CLI leaves thought parts out. So a
Gemini response's reasoning travels in bundles (`envelope_bytes`) holding each block's text and
position, and the bytes are self-delimiting so that concatenated bundles still read: per bundle,
the prefix, the body's length in ASCII decimal and `:`, then the body,
`{"blocks": [{"at", "form", "reasoning_state", "text"}, ...]}`; all of it standard base64. A
bundle is recognized by its decoded bytes, never its base64 text (`read_bundles`).

The envelope reads a state's JSON and its `$type` tag, never its fields: only the codec that owns
a payload type decodes it (`behaviors.implementations.dialects.codec_utils.own_reasoning_state`).
"""

import base64
import binascii
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from behaviors.types.adaptation import ResponseLoss
from behaviors.types.content import ContentReasoning, ReasoningForm
from behaviors.types.json import JSONObject
from behaviors.types.message import AssistantMessage, ChatMessage
from fxtr.core.entity_data import thaw_fxtr_data
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData

logger = logging.getLogger(__name__)

ENVELOPE_PREFIX = "behaviors-reasoning-v1:"
_ENVELOPE_PREFIX_BYTES = ENVELOPE_PREFIX.encode("ascii")
_FORMS: frozenset[str] = frozenset({"full", "summary", "redacted"})

_warned_non_json: set[str] = set()


@dataclass(frozen=True)
class EchoedReasoning:
    """What a harness's opaque slot held.

    Attributes:
        form: The block's form, when the slot recorded it (an envelope); None
            when the dialect's own block type decides it.
        state: The block's state, or None if it had none.
    """

    form: ReasoningForm | None
    state: DynamicData | None


@dataclass(frozen=True)
class BundledReasoning:
    """One reasoning block of a Gemini response, as a bundle carries it.

    Attributes:
        at: Where the block goes among the response's other content: before the group numbered
            `at`, counting each tool call as a group and each run of text that only reasoning
            interrupts as one group. A block after all of them has the number of groups.
        form: The block's form.
        text: The block's text.
        state: The block's state, or None if it had none.
    """

    at: int
    form: ReasoningForm
    text: str
    state: DynamicData | None


def is_json_only(state: DynamicData) -> bool:
    """Whether every value in `state` is a JSON value (no bytes, no entity ids): only such a state
    can cross a harness in an envelope."""
    try:
        json.dumps(thaw_fxtr_data(state.data), allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def dropped_state_losses(output: Sequence[ChatMessage]) -> tuple[ResponseLoss, ...]:
    """What a harness is not sent of `output`'s reasoning: the state of each block whose state is
    not JSON values only (`is_json_only`), as one `reasoning_state_dropped` loss naming every such
    state type, or none. Every dialect's own state types are JSON values only, so such a state is
    always another's, and every harness dialect drops it (`_state_json`); the harness echoes the
    block without it, and the model reasons afresh on that turn."""
    counts: dict[str, int] = {}
    for message in output:
        if not isinstance(message, AssistantMessage):
            continue
        for block in message.content:
            if isinstance(block, ContentReasoning) and block.reasoning_state is not None:
                state = block.reasoning_state
                if not is_json_only(state):
                    counts[state.type] = counts.get(state.type, 0) + 1
    if not counts:
        return ()
    blocks = ", ".join(f"{n} with {state_type}" for state_type, n in sorted(counts.items()))
    return (
        ResponseLoss(
            "reasoning_state_dropped",
            f"reasoning block(s) sent without their state ({blocks}): a state that crosses a "
            "harness must be JSON values only",
        ),
    )


def _state_json(state: DynamicData | None) -> object:
    """`state` as the JSON an envelope carries: None for no state, and for a state that is not
    JSON values only (dropped, with a warning)."""
    if state is None:
        return None
    if not is_json_only(state):
        if state.type not in _warned_non_json:
            _warned_non_json.add(state.type)
            logger.warning(
                "dropping reasoning state of type %s on its way to a harness: a payload that "
                "crosses a harness must be JSON values only",
                state.type,
            )
        return None
    return thaw_fxtr_data(state.data)


def _state_from_json(value: object) -> DynamicData | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("reasoning_state is not an object")
    # `from_data` raises (a ValueError) for a mapping without a valid `$type` tag.
    return DynamicData.from_data(cast("JSONObject", value))


def _form_from_json(value: object) -> ReasoningForm:
    if not isinstance(value, str) or value not in _FORMS:
        raise ValueError(f"unknown form {value!r}")
    return cast("ReasoningForm", value)


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _body(form: ReasoningForm, state: DynamicData | None) -> bytes:
    return _json_bytes({"form": form, "reasoning_state": _state_json(state)})


def envelope_text(form: ReasoningForm, state: DynamicData | None) -> str:
    """The envelope for a string slot."""
    encoded = base64.urlsafe_b64encode(_body(form, state)).decode("ascii").rstrip("=")
    return ENVELOPE_PREFIX + encoded


def envelope_bytes(blocks: Sequence[BundledReasoning]) -> str:
    """The bundle of a Gemini response's reasoning, for its `thoughtSignature`."""
    body = _json_bytes(
        {
            "blocks": [
                {
                    "at": block.at,
                    "form": block.form,
                    "reasoning_state": _state_json(block.state),
                    "text": block.text,
                }
                for block in blocks
            ]
        }
    )
    framed = _ENVELOPE_PREFIX_BYTES + str(len(body)).encode("ascii") + b":" + body
    return base64.b64encode(framed).decode("ascii")


def _b64decode(value: str, *, urlsafe_only: bool = False) -> bytes | None:
    """`value` decoded as base64 in either alphabet (or only the URL-safe one), padding optional;
    None if it is not base64."""
    normalized = value if urlsafe_only else value.replace("+", "-").replace("/", "_")
    try:
        return base64.urlsafe_b64decode(normalized + "=" * (-len(normalized) % 4))
    except (binascii.Error, ValueError):
        return None


def _unwrap(body: bytes | None) -> EchoedReasoning:
    """The block an envelope body records. A body that does not decode loses its state (it was
    an envelope, so it is not the dialect's own state either)."""
    try:
        if body is None:
            raise ValueError("not base64")
        decoded = json.loads(body)
        if not isinstance(decoded, dict):
            raise ValueError("not an object")
        payload = cast("dict[str, object]", decoded)
        return EchoedReasoning(
            form=_form_from_json(payload.get("form")),
            state=_state_from_json(payload.get("reasoning_state")),
        )
    except (ValueError, UnicodeDecodeError) as error:
        logger.warning("dropping an unreadable reasoning envelope: %s", error)
        return EchoedReasoning(form=None, state=None)


def read_text_slot(value: str, native: Callable[[str], TaggedDataclass]) -> EchoedReasoning:
    """What a string slot echoed back: an envelope, or else the dialect's own state, which
    `native` builds from the raw value."""
    if value.startswith(ENVELOPE_PREFIX):
        return _unwrap(_b64decode(value[len(ENVELOPE_PREFIX) :], urlsafe_only=True))
    return EchoedReasoning(form=None, state=DynamicData.from_tagged(native(value)))


def read_bundles(value: str) -> tuple[BundledReasoning, ...] | None:
    """The reasoning blocks a Gemini `thoughtSignature` bundles, in order, across every bundle
    concatenated into it; None when it holds no bundle (a signature Gemini issued). Bytes before
    the first bundle are skipped: a Gemini signature streamed before a response's bundle was
    known, which the bundle carries too. Reading stops at the first bundle that does not decode,
    keeping the blocks before it."""
    raw = _b64decode(value)
    position = -1 if raw is None else raw.find(_ENVELOPE_PREFIX_BYTES)
    if raw is None or position < 0:
        return None
    blocks: list[BundledReasoning] = []
    try:
        while position < len(raw):
            if not raw.startswith(_ENVELOPE_PREFIX_BYTES, position):
                raise ValueError("bytes after a bundle that are not another bundle")
            position += len(_ENVELOPE_PREFIX_BYTES)
            colon = raw.index(b":", position)
            length = int(raw[position:colon])
            body = raw[colon + 1 : colon + 1 + length]
            if len(body) != length:
                raise ValueError("a truncated bundle")
            position = colon + 1 + length
            blocks.extend(_bundled(json.loads(body)))
    except (ValueError, UnicodeDecodeError) as error:
        logger.warning("dropping the rest of an unreadable reasoning bundle: %s", error)
    return tuple(blocks)


def _bundled(payload: object) -> list[BundledReasoning]:
    if not isinstance(payload, dict):
        raise ValueError("not an object")
    entries = cast("dict[str, object]", payload).get("blocks")
    if not isinstance(entries, list):
        raise ValueError("no blocks")
    out: list[BundledReasoning] = []
    for entry in cast("list[object]", entries):
        if not isinstance(entry, dict):
            raise ValueError("a block that is not an object")
        fields = cast("dict[str, object]", entry)
        at, text = fields.get("at"), fields.get("text")
        if type(at) is not int or at < 0 or not isinstance(text, str):
            raise ValueError("a block without a position or text")
        out.append(
            BundledReasoning(
                at=at,
                form=_form_from_json(fields.get("form")),
                text=text,
                state=_state_from_json(fields.get("reasoning_state")),
            )
        )
    return out
