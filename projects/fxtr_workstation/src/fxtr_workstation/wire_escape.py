"""List-based escape format for the workstation ``/client`` wire protocol.

The wire shape was inspired by Cap'n'Web's escape rules; we adopt only
the list-escape part of that format and don't take the rest of
Cap'n'Web. In code we call this "list-based escape".

Wire shape:

- Every literal JSON list ``[a, b, c]`` is encoded as ``[[a, b, c]]``.
- A 2-element list whose first element is a reserved tag string is a
  tagged special:

  - ``["entity", "<short-id>"]`` — entity reference.
  - ``["service", "<short-id>"]`` — service reference.
  - ``["bytes", "<base64>"]`` — raw bytes (standard base64).
  - ``["callback", <slot:int>]`` — session-scoped callback handle.
    Decoded only when the caller supplies a ``lookup_callback`` —
    otherwise the tag is rejected. The slot resolves against the
    *mutating* session's open-callback table (callers that don't
    have a session context shouldn't supply a ``lookup_callback``).

- Strings, numbers, booleans, and null pass through unchanged.
- Dicts are walked: keys must be strings, values recurse.

The reserved tag set is closed; any other shape that starts with a
string and isn't a literal-list escape is a validation error. All
current tags are 2-element; future n-ary tag additions extend the
per-tag arity rule below.

Pure functions: no I/O, no registry mutations. Callers thread their
state through the encode/lookup callbacks. (``bytes`` doesn't need a
callback — base64 encoding is mechanical.)
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from typing import Any, Final

from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import EntityRef, ServiceRef

#: Tag for entity refs on the wire.
TAG_ENTITY: Final = "entity"
#: Tag for service refs on the wire.
TAG_SERVICE: Final = "service"
#: Tag for raw bytes on the wire. Payload is standard base64.
TAG_BYTES: Final = "bytes"
#: Tag for callback refs on the wire. Decoded by callers that
#: supply a ``lookup_callback``; rejected otherwise.
TAG_CALLBACK: Final = "callback"

#: All reserved tag strings. Any list whose element 0 is in this set
#: is treated as a tagged form; element 0 outside this set means a
#: literal-list escape.
RESERVED_TAGS: Final[frozenset[str]] = frozenset({TAG_ENTITY, TAG_SERVICE, TAG_BYTES, TAG_CALLBACK})


class WireEscapeError(ValueError):
    """Raised when a value cannot be escaped or unescaped."""


def escape(
    value: Any,
    *,
    encode_entity: Callable[[FxtrID], str],
    encode_service: Callable[[ServiceRef[Any]], str],
) -> Any:
    """Encode ``value`` to its list-escape wire form.

    The callbacks resolve a workstation-tracked ref to its short ID
    string. ``encode_entity`` always receives a bare :class:`FxtrID`
    — :class:`EntityRef`, :class:`RawWeakFxtrID`, :class:`RawStrongFxtrID`,
    and :class:`RawFxtrEntityMarkerID` are all reduced to their
    underlying FxtrID first. (Raw\\* wrappers appear in CBOR loaded
    via ``load_cbor``; the wire format treats all entity-ref
    variants identically.) ``encode_service`` receives a
    :class:`ServiceRef`.
    """
    return _escape(value, encode_entity, encode_service)


def _escape(
    value: Any,
    encode_entity: Callable[[FxtrID], str],
    encode_service: Callable[[ServiceRef[Any]], str],
) -> Any:
    if isinstance(value, FxtrID):
        return [TAG_ENTITY, encode_entity(value)]
    if isinstance(value, EntityRef):
        return [TAG_ENTITY, encode_entity(value.entity_id)]
    if isinstance(value, (RawWeakFxtrID, RawStrongFxtrID, RawFxtrEntityMarkerID)):
        # CBOR-loaded entity refs come wrapped in one of these three
        # types (weak / strong / entity-marker, distinguishing CBOR
        # tag 33001 / 33002 / 33000). The wire vocabulary doesn't
        # preserve the strong/weak/marker distinction — receivers
        # treat them all as bare entity references via the entity tag.
        return [TAG_ENTITY, encode_entity(value.inner_id)]
    if isinstance(value, ServiceRef):
        return [TAG_SERVICE, encode_service(value)]
    if isinstance(value, bytes):
        return [TAG_BYTES, base64.b64encode(value).decode("ascii")]
    # bool must come before int because ``isinstance(True, int) is True``;
    # we want both to pass through, so listing them together is fine,
    # but be aware of this when adding new leaf types.
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise WireEscapeError(
                    f"escape: dict key must be str, got {type(k).__name__}: {k!r}"
                )
            out[k] = _escape(v, encode_entity, encode_service)
        return out
    if isinstance(value, (list, tuple)):
        # Wrap every literal list as a single-element list. The decoder
        # peels this layer off; tagged forms have a different shape.
        return [[_escape(v, encode_entity, encode_service) for v in value]]
    raise WireEscapeError(f"escape: unsupported value type: {type(value).__name__}: {value!r}")


def unescape(
    value: Any,
    *,
    lookup_entity: Callable[[str], FxtrID],
    lookup_service: Callable[[str], ServiceRef[Any]],
    lookup_callback: Callable[[int], Any] | None = None,
) -> Any:
    """Decode ``value`` from its list-escape wire form.

    The lookup callbacks resolve a short ID (or, for callbacks, a
    slot integer) to the corresponding workstation ref. They should
    raise :class:`WireEscapeError` (or any ``ValueError`` subclass)
    on unknown / inactive identifiers; the exception propagates to
    the caller of :func:`unescape`.

    ``lookup_callback`` is optional. When supplied (typically by a
    session-context-aware caller such as
    :class:`~fxtr_workstation.state.WorkstationState`'s mutation
    op-validator), the ``["callback", slot]`` tag resolves through
    it. When omitted, the tag is rejected — callers without a
    session context should not be accepting callback refs on the
    wire.

    ``EntityRef`` is *not* produced on this path — the wire vocabulary
    drops the target-type annotation and the decoder returns a bare
    :class:`FxtrID`. (This matches the ``FxtrServiceData`` convention
    on the v2 wire — see ``service_cap_common.py``.)
    """
    return _unescape(value, lookup_entity, lookup_service, lookup_callback)


def _unescape(
    value: Any,
    lookup_entity: Callable[[str], FxtrID],
    lookup_service: Callable[[str], ServiceRef[Any]],
    lookup_callback: Callable[[int], ServiceRef[Any]] | None,
) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise WireEscapeError(
                    f"unescape: dict key must be str, got {type(k).__name__}: {k!r}"
                )
            out[k] = _unescape(v, lookup_entity, lookup_service, lookup_callback)
        return out
    if isinstance(value, list):
        # Two valid shapes:
        #  1. Tagged: ``[tag, payload]`` with ``tag`` in RESERVED_TAGS.
        #  2. Literal-list escape: ``[inner_list]`` (length-1, element
        #     is a list); the inner list's items are decoded recursively.
        if len(value) >= 1 and isinstance(value[0], str) and value[0] in RESERVED_TAGS:
            return _decode_tagged(value, lookup_entity, lookup_service, lookup_callback)
        if len(value) != 1 or not isinstance(value[0], list):
            raise WireEscapeError(
                f"unescape: malformed list (expected tagged form or "
                f"single-element list-of-list escape): {value!r}"
            )
        return [_unescape(v, lookup_entity, lookup_service, lookup_callback) for v in value[0]]
    raise WireEscapeError(f"unescape: unsupported value type: {type(value).__name__}: {value!r}")


def _decode_tagged(
    value: list[Any],
    lookup_entity: Callable[[str], FxtrID],
    lookup_service: Callable[[str], ServiceRef[Any]],
    lookup_callback: Callable[[int], ServiceRef[Any]] | None,
) -> Any:
    tag = value[0]
    if tag == TAG_ENTITY:
        if len(value) != 2 or not isinstance(value[1], str):
            raise WireEscapeError(
                f"unescape: malformed entity tag (expected ['entity', " f"<short-id>]): {value!r}"
            )
        return lookup_entity(value[1])
    if tag == TAG_SERVICE:
        if len(value) != 2 or not isinstance(value[1], str):
            raise WireEscapeError(
                f"unescape: malformed service tag (expected ['service', " f"<short-id>]): {value!r}"
            )
        return lookup_service(value[1])
    if tag == TAG_BYTES:
        if len(value) != 2 or not isinstance(value[1], str):
            raise WireEscapeError(
                f"unescape: malformed bytes tag (expected ['bytes', " f"<base64>]): {value!r}"
            )
        try:
            return base64.b64decode(value[1], validate=True)
        except (binascii.Error, ValueError) as e:
            raise WireEscapeError(f"unescape: invalid base64 payload: {e}") from e
    if tag == TAG_CALLBACK:
        # ``isinstance(value[1], int) and not isinstance(value[1], bool)``
        # guards against the JSON ``true``/``false`` ⇒ Python ``bool``
        # corner case (``isinstance(True, int) is True``).
        if len(value) != 2 or not isinstance(value[1], int) or isinstance(value[1], bool):
            raise WireEscapeError(
                f"unescape: malformed callback tag (expected "
                f"['callback', <slot:int>]): {value!r}"
            )
        if lookup_callback is None:
            raise WireEscapeError(
                f"unescape: callback tag requires a session context "
                f"(lookup_callback was not supplied): {value!r}"
            )
        return lookup_callback(value[1])
    raise AssertionError(  # pragma: no cover — RESERVED_TAGS coverage
        f"unreachable: unknown reserved tag {tag!r}"
    )
