"""List-based escape codec for the stateless HTTP/ws **service** wire.

This is the server-side ("resolved mode") twin of the workstation's
``wire_escape.py`` and the frontend's ``escape.ts``. Same list-escape
family (every literal list ``[a, b, c]`` becomes ``[[a, b, c]]``; a
2-element list whose head is a reserved tag is a tagged special), with
the identifier payloads swapped for the stateless world:

- ``["entity", "<id_string>"]`` — the full content-addressed
  ``FxtrID.id_string()`` (the workstation used a server short id; here
  there are no server-side short ids). A *layered* id (one carrying a
  provenance parent) adds a third element when the codec is run in
  ``provenanced`` mode: ``["entity", "<data_id>", "<prov_id>"]``.
  Both ids are bare local id_strings; the ``@``-form never appears on the
  wire (decode parses with ``FxtrID.from_id_string(..., allow_provenance=False)``,
  which rejects it).
- ``["service", <descriptor>]`` — a self-describing
  :class:`ServiceDescriptor` JSON object (``{kind, url, method, name,
  id, service_type_id}``), *not* a short id.
- ``["callback", "<uuid>"]`` — a client-minted inbox UUID, decoded to
  an opaque :class:`CallbackAddress` the service impl can hold or
  ignore (only the ws endpoint can actually deliver to it).
- ``["bytes", "<base64>"]`` — raw bytes (mechanical, no callback).

Design + resolved decisions live under
``projects/fxtr_workstation/plans/phase0/part7a_stateless_ws_server/``
(overview ``overview.md``, wire protocol ``design/01_wire_protocol.md``,
decision log ``open_questions.md`` — cited as "Q-x" below).

The codec is **pure + synchronous** and only walks values: it does not
touch the scope. Entity tags decode to a bare :class:`FxtrID` (parse
only — fetchability is the service impl's concern when it actually
loads the id; an unknown id simply fails the impl's load). Service
results may carry :class:`TransientService` objects (ws connection
context only); they escape via a caller-supplied ``register_transient``
callback that mints the connection-scoped id.

No ``error_type`` anywhere — failures surface as a ``message`` string,
matching the workstation (decision ``open_questions.md`` Q-K).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import EntityRef

#: Tag for entity refs on the wire (payload = ``id_string``).
TAG_ENTITY: Final = "entity"
#: Tag for service refs on the wire (payload = descriptor object).
TAG_SERVICE: Final = "service"
#: Tag for raw bytes on the wire (payload = standard base64).
TAG_BYTES: Final = "bytes"
#: Tag for callback / inbox refs on the wire (payload = inbox UUID).
TAG_CALLBACK: Final = "callback"

#: All reserved tag strings. A list whose element 0 is in this set is a
#: tagged form; element 0 outside it means a literal-list escape.
RESERVED_TAGS: Final[frozenset[str]] = frozenset({TAG_ENTITY, TAG_SERVICE, TAG_BYTES, TAG_CALLBACK})

#: Allowed descriptor ``kind`` values.
_SERVICE_KINDS: Final[frozenset[str]] = frozenset({"http", "ws", "transient"})


class ServiceCodecError(ValueError):
    """Raised when a value cannot be escaped or unescaped.

    Endpoints map this to a ``4xx`` JSON ``{message}`` response (a
    malformed request), distinct from a service impl raising (``5xx``).
    """


def encode_pin_value(fid: FxtrID, *, provenanced: bool) -> str | list[str]:
    """Encode a pin / discovery-entity id value (bare, or layered).

    Bare ``"<data_id>"``; when ``provenanced`` and ``fid`` is layered
    (carries a ``_provenance_parent``), the array ``["<data_id>",
    "<prov_id>"]`` (both bare local id_strings). Shared by ``/pins``,
    ``/pin``, and ``/discovery`` so the shape can't drift between them.
    This is the tag-less sibling of the layered entity escape
    (``["entity", data, prov]``) used for service results.
    """
    prov = fid._provenance_parent
    if provenanced and prov is not None:
        return [fid.id_string(), prov.id_string()]
    return fid.id_string()


@dataclass(frozen=True)
class ServiceDescriptor:
    """Self-describing service reference carried on the wire.

    ``kind`` is one of ``"http"`` / ``"ws"`` / ``"transient"``.
    ``service_type_id`` is a non-identity annotation (the type entity's
    ``id_string``) — decisions ``open_questions.md`` Q-H/Q-J; it is
    *not* part of descriptor identity and the client excludes it from
    shortId hashing.
    """

    kind: str
    url: str | None = None
    method: str | None = None  # http: "GET" | "POST"
    name: str | None = None  # ws: service name scoped to the ws url
    id: str | None = None  # transient: connection-scoped id
    service_type_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Encode to the JSON descriptor object, omitting unset fields.

        Returns:
            The descriptor as a plain dict with ``kind`` plus any of
            ``url`` / ``method`` / ``name`` / ``id`` / ``service_type_id``
            that are set.
        """
        out: dict[str, Any] = {"kind": self.kind}
        for field, value in (
            ("url", self.url),
            ("method", self.method),
            ("name", self.name),
            ("id", self.id),
            ("service_type_id", self.service_type_id),
        ):
            if value is not None:
                out[field] = value
        return out

    @classmethod
    def from_wire(cls, obj: Mapping[str, Any]) -> ServiceDescriptor:
        """Decode a descriptor from its JSON object form.

        Args:
            obj: The descriptor object (must carry a valid ``kind``).

        Returns:
            The parsed ``ServiceDescriptor``.

        Raises:
            ServiceCodecError: If ``kind`` is missing/invalid or any
                present field has the wrong type.
        """
        kind = obj.get("kind")
        if not isinstance(kind, str) or kind not in _SERVICE_KINDS:
            raise ServiceCodecError(
                f"service descriptor: invalid/missing 'kind' (expected one of "
                f"{sorted(_SERVICE_KINDS)}): {obj!r}"
            )

        def _opt_str(field: str) -> str | None:
            v = obj.get(field)
            if v is not None and not isinstance(v, str):
                raise ServiceCodecError(
                    f"service descriptor: '{field}' must be a string, got {type(v).__name__}"
                )
            return v

        return cls(
            kind=kind,
            url=_opt_str("url"),
            method=_opt_str("method"),
            name=_opt_str("name"),
            id=_opt_str("id"),
            service_type_id=_opt_str("service_type_id"),
        )


@dataclass(frozen=True)
class CallbackAddress:
    """Opaque inbox address decoded from a ``["callback", uuid]`` arg.

    A service impl may hold it (to ``ctx.notify`` later, ws only) or
    ignore it (http services have no channel back). The uuid is
    client-minted and meaningful only to the ws endpoint's per-connection
    routing table.
    """

    uuid: str


@dataclass(frozen=True)
class TransientService:
    """A connection-scoped service a ws impl returns (e.g. the
    unsubscribe handle from ``subscribe``).

    ``impl`` is the same shape as a registered service impl — a
    ``{method: fn}`` map or an ``async (method, args, kwargs, ctx)``
    callable. The ws endpoint registers it in the per-connection table
    (minting an id) and dispatches invokes addressed to that id back to
    it. Escaping one outside a ws connection context raises.
    """

    impl: Any


#: Callback the ws endpoint supplies to ``escape_value`` so a
#: ``TransientService`` in a result gets registered + assigned a
#: connection-scoped id.
RegisterTransient = Callable[[TransientService], str]


# --------------------------------------------------------------------------
# Escape (server → wire)
# --------------------------------------------------------------------------


def _escape_entity_id(fid: FxtrID, *, provenanced: bool) -> list[Any]:
    """Encode an ``FxtrID`` to its entity-tag wire form.

    Bare ``["entity", data]``; when ``provenanced`` and the id is layered
    (carries a ``_provenance_parent``), the 3-element form ``["entity", data,
    prov]``. Both ids are bare local id_strings (``id_string()`` default).
    """
    prov = fid._provenance_parent
    if provenanced and prov is not None:
        return [TAG_ENTITY, fid.id_string(), prov.id_string()]
    return [TAG_ENTITY, fid.id_string()]


def escape_value(
    value: Any,
    *,
    register_transient: RegisterTransient | None = None,
    provenanced: bool = False,
) -> Any:
    """Encode a service result to its list-escape wire form.

    ``FxtrID`` / ``EntityRef`` / ``Raw*FxtrID`` → ``["entity", data]``
    (or the layered ``["entity", data, prov]`` when ``provenanced`` and
    the id is layered); ``ServiceDescriptor`` → ``["service", descriptor]``;
    ``bytes`` → base64; lists wrapped ``[[...]]``; dicts walked.

    Args:
        value: The result value to encode.
        register_transient: Callback that registers a
            ``TransientService`` and returns its connection-scoped id.
            Required iff ``value`` (transitively) contains a transient
            — transients are ws-only.
        provenanced: When ``True``, layered ids emit the 3-element form
            so the prov id reaches the client. When ``False`` (default),
            ids emit bare even if layered — provenance is a server-side
            opt-in, so a non-provenanced server emits only bare ids.

    Returns:
        The JSON-compatible list-escape form.

    Raises:
        ServiceCodecError: On an unsupported value type, a non-str dict
            key, or a ``TransientService`` with no ``register_transient``.
    """
    if isinstance(value, FxtrID):
        return _escape_entity_id(value, provenanced=provenanced)
    if isinstance(value, EntityRef):
        return _escape_entity_id(value.entity_id, provenanced=provenanced)
    if isinstance(value, (RawWeakFxtrID, RawStrongFxtrID, RawFxtrEntityMarkerID)):
        # CBOR-loaded refs (tags 33001 / 33002 / 33000); the wire
        # vocabulary treats all entity-ref variants identically. Provenance
        # is never serialized in CBOR, so these are always bare — assert
        # that premise rather than silently dropping a provenance parent.
        assert (
            value.inner_id._provenance_parent is None
        ), f"Raw entity ref unexpectedly carries provenance: {value.inner_id!r}"
        return [TAG_ENTITY, value.inner_id.id_string()]
    if isinstance(value, TransientService):
        if register_transient is None:
            raise ServiceCodecError(
                "escape: a TransientService can only be returned from a ws "
                "connection context (no register_transient was supplied)"
            )
        return [
            TAG_SERVICE,
            ServiceDescriptor(kind="transient", id=register_transient(value)).to_wire(),
        ]
    if isinstance(value, ServiceDescriptor):
        return [TAG_SERVICE, value.to_wire()]
    if isinstance(value, bytes):
        return [TAG_BYTES, base64.b64encode(value).decode("ascii")]
    # bool is a subclass of int; both pass through.
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ServiceCodecError(
                    f"escape: dict key must be str, got {type(k).__name__}: {k!r}"
                )
            out[k] = escape_value(v, register_transient=register_transient, provenanced=provenanced)
        return out
    if isinstance(value, (list, tuple)):
        return [
            [
                escape_value(v, register_transient=register_transient, provenanced=provenanced)
                for v in value
            ]
        ]
    raise ServiceCodecError(f"escape: unsupported value type: {type(value).__name__}: {value!r}")


# --------------------------------------------------------------------------
# Unescape (wire → server)
# --------------------------------------------------------------------------


def unescape_value(value: Any) -> Any:
    """Decode a single value from its list-escape wire form.

    Entity tags decode to a bare ``FxtrID`` (parse only); service tags
    to a ``ServiceDescriptor``; callback tags to a ``CallbackAddress``;
    bytes tags to ``bytes``.

    Args:
        value: The list-escape wire value.

    Returns:
        The decoded Python value.

    Raises:
        ServiceCodecError: On a malformed tag, an unsupported value
            type, or a non-str dict key.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ServiceCodecError(
                    f"unescape: dict key must be str, got {type(k).__name__}: {k!r}"
                )
            out[k] = unescape_value(v)
        return out
    if isinstance(value, list):
        if len(value) >= 1 and isinstance(value[0], str) and value[0] in RESERVED_TAGS:
            return _decode_tagged(value)
        if len(value) != 1 or not isinstance(value[0], list):
            raise ServiceCodecError(
                f"unescape: malformed list (expected tagged form or single-element "
                f"list-of-list escape): {value!r}"
            )
        return [unescape_value(v) for v in value[0]]
    raise ServiceCodecError(f"unescape: unsupported value type: {type(value).__name__}: {value!r}")


def unescape_args(payload: Any) -> tuple[list[Any], dict[str, Any]]:
    """Decode an invocation payload ``{"args": [...], "kwargs": {...}}``.

    Args:
        payload: The invocation payload object; both ``args`` and
            ``kwargs`` are optional (default empty).

    Returns:
        A ``(args, kwargs)`` tuple of decoded values.

    Raises:
        ServiceCodecError: If the payload (or its ``args`` / ``kwargs``)
            has the wrong shape, or any contained value is malformed.
    """
    if not isinstance(payload, Mapping):
        raise ServiceCodecError(
            f"unescape_args: payload must be an object, got {type(payload).__name__}"
        )
    raw_args = payload.get("args", [])
    raw_kwargs = payload.get("kwargs", {})
    if not isinstance(raw_args, list):
        raise ServiceCodecError("unescape_args: 'args' must be a list")
    if not isinstance(raw_kwargs, Mapping):
        raise ServiceCodecError("unescape_args: 'kwargs' must be an object")
    args = [unescape_value(a) for a in raw_args]
    kwargs: dict[str, Any] = {}
    for k, v in raw_kwargs.items():
        if not isinstance(k, str):
            raise ServiceCodecError(f"unescape_args: kwarg key must be str, got {type(k).__name__}")
        kwargs[k] = unescape_value(v)
    return args, kwargs


def _parse_wire_id(s: str) -> FxtrID:
    """Parse a bare wire id_string, mapping failures to ``ServiceCodecError``.

    Uses ``FxtrID.from_id_string(allow_provenance=False)`` (which rejects the
    ``@``-form) so a malformed/forbidden id in an escaped value surfaces as a
    codec error (a ``4xx``), not an uncaught ``ValueError``.
    """
    try:
        return FxtrID.from_id_string(s, allow_provenance=False)
    except (ValueError, KeyError) as e:
        raise ServiceCodecError(f"unescape: invalid entity id_string {s!r}: {e}") from e


def _decode_tagged(value: list[Any]) -> Any:
    tag = value[0]
    if tag == TAG_ENTITY:
        # Bare ``['entity', data]`` or layered ``['entity', data, prov]``;
        # both ids are bare local id_strings. Strict arity — exactly 2 or 3,
        # never longer (no silently-ignored trailing elements).
        if len(value) == 2 and isinstance(value[1], str):
            return _parse_wire_id(value[1])
        if len(value) == 3 and isinstance(value[1], str) and isinstance(value[2], str):
            data = _parse_wire_id(value[1])
            prov = _parse_wire_id(value[2])
            return FxtrID(raw_data_id=bytes(data), provenance_parent=prov)
        raise ServiceCodecError(
            f"unescape: malformed entity tag (expected ['entity', <data_id>] or "
            f"['entity', <data_id>, <prov_id>]): {value!r}"
        )
    if tag == TAG_SERVICE:
        if len(value) != 2 or not isinstance(value[1], Mapping):
            raise ServiceCodecError(
                f"unescape: malformed service tag (expected ['service', <descriptor>]): {value!r}"
            )
        return ServiceDescriptor.from_wire(value[1])
    if tag == TAG_BYTES:
        if len(value) != 2 or not isinstance(value[1], str):
            raise ServiceCodecError(
                f"unescape: malformed bytes tag (expected ['bytes', <base64>]): {value!r}"
            )
        try:
            return base64.b64decode(value[1], validate=True)
        except (binascii.Error, ValueError) as e:
            raise ServiceCodecError(f"unescape: invalid base64 payload: {e}") from e
    if tag == TAG_CALLBACK:
        if len(value) != 2 or not isinstance(value[1], str):
            raise ServiceCodecError(
                f"unescape: malformed callback tag (expected ['callback', <uuid>]): {value!r}"
            )
        return CallbackAddress(value[1])
    raise AssertionError(f"unreachable: unknown reserved tag {tag!r}")  # pragma: no cover
