"""JSON wire codec for the LeasedCapIR lowering protocol.

Turns :class:`RPCMessage` / :class:`Expression` / :class:`IRStatic` values into JSON-serializable
trees (and back). The on-the-wire shape is heavily inspired by `Cap'n'Web's JSON protocol
<https://github.com/cloudflare/capnweb/blob/main/protocol.md>`_: primitives are literal, plain
JSON objects are literal, and lists are used as an escape mechanism with the first element
being a string tag. This keeps primitive-heavy payloads cheap and matches what a JavaScript
reader would expect.

The codec is pinned to protocol version 1. The only forward-compat hook is the serialized form
of :class:`HelloMessage` (``["fxtr_rpc_hello", 1]``); a decoder that sees any other payload
under that tag raises :class:`ProtocolVersionError`.

See ``plans/phase3/stage8_lease_system/overview.md`` §"Stage E" for the full wire-form table.
"""

from __future__ import annotations

import base64
import traceback
from typing import Any, Callable, Literal, Mapping, Sequence, cast

import anyio.abc

from fxtr2.leases.leased_cap_common import _quote_traceback_info
from fxtr2.leases.leased_cap_ir import (
    _PRE_RPC_TRACEBACK_HEADER,
    Call,
    GetItem,
    IRException,
    IRStatic,
    IRTagged,
)
from fxtr2.rpc.lowering_types import (
    AbortMessage,
    AwaitFutureExpr,
    BreakMessage,
    ClosedMessage,
    DictExpr,
    DiscardMessage,
    ExportedIRFutureExpr,
    ExportedIRHandleExpr,
    ExportedLeaseExpr,
    ExportedLifetimeTokenExpr,
    Expression,
    HandleDispatchExpr,
    HandleRequestExpr,
    HelloMessage,
    ImportedIRFutureExpr,
    ImportedIRHandleExpr,
    ImportedLeaseExpr,
    ImportedLifetimeTokenExpr,
    IRTaggedExpr,
    ListExpr,
    PullMessage,
    PushMessage,
    RejectMessage,
    ResolveMessage,
    RPCMessage,
    StaticExpr,
    UnpullMessage,
    WalkFutureExpr,
    WatchMessage,
)

type JSONTypes = (
    None | bool | int | float | str | Sequence["JSONTypes"] | Mapping[str, "JSONTypes"]
)
"""JSON-compatible value tree. Input accepts any Sequence/Mapping; output is always list/dict."""


_PROTOCOL_VERSION = 1


####################################################################################################
# Exception hierarchy
####################################################################################################


class JsonCodecError(Exception):
    """Base class for all JSON codec errors."""


class MalformedMessageError(JsonCodecError):
    """A top-level message value did not match any known shape."""


class MalformedExpressionError(JsonCodecError):
    """An expression value did not match any known shape."""


class MalformedStaticError(JsonCodecError):
    """A static value did not match any known shape."""


class NonStaticInStaticContextError(JsonCodecError):
    """Encountered a value that is a valid expression but not a valid static (e.g. an import
    reference appeared inside a ``RejectMessage`` payload)."""


class ProtocolVersionError(JsonCodecError):
    """A ``HelloMessage`` payload referenced a protocol version we do not support."""


####################################################################################################
# Encoders
####################################################################################################


_FORMAT_MESSAGE_TRUNCATE_AT = 1024
"""Maximum :attr:`IRException.message` length, in characters, that
:func:`_build_wire_traceback_info` will inline into a fresh wire-side
``traceback_info`` segment. Longer messages are truncated to this length plus a
``"... (truncated)"`` suffix *for the formatted segment only* — the full
``message`` field still travels on the wire untouched. Bounds the per-hop wire
growth even when the user-thrown exception carries a huge message."""


def _build_wire_traceback_info(exc: IRException[Any]) -> str | None:
    """Compute the wire-side ``traceback_info`` value for ``exc``.

    If the exception has accumulated fresh local Python state since it was last
    encoded (non-``None`` ``__traceback__`` or any ``__notes__``), wrap the result
    of :func:`traceback.format_exception` in a fresh ``Earlier (pre-RPC)`` block:
    the new segment is ``>``-quoted under the header, and any existing
    ``traceback_info`` is also ``>``-quoted (which compounds with its own internal
    ``>`` levels to deepen the nesting indicator). The result preserves the
    ``traceback_info`` invariant — it always begins with the ``Earlier (pre-RPC)``
    header line and the chain content sits one or more ``>`` levels deeper.

    If no fresh local state is present, pass through ``exc.traceback_info``
    unchanged (no extra hop to record).

    The IRException is temporarily mutated to clear ``traceback_info`` and truncate
    ``message`` to :data:`_FORMAT_MESSAGE_TRUNCATE_AT` chars before calling
    :func:`traceback.format_exception`. Both are restored before return. Without
    this, :meth:`IRException.__str__` — which ``format_exception`` calls to render
    the exception's header line — would fold the full existing ``traceback_info``
    and full ``message`` into the new segment, causing each hop to embed the prior
    chain verbatim and re-embed it again on the next encode (quadratic wire growth).

    The on-wire ``message`` field is unchanged from ``exc.message``; only the new
    segment built into ``traceback_info`` sees the truncated form.
    """
    if exc.__traceback__ is None and not getattr(exc, "__notes__", None):
        return exc.traceback_info  # passthrough
    saved_traceback_info = exc.traceback_info
    saved_message = exc.message
    exc.traceback_info = None
    if len(saved_message) > _FORMAT_MESSAGE_TRUNCATE_AT:
        exc.message = saved_message[:_FORMAT_MESSAGE_TRUNCATE_AT] + "... (truncated)"
    try:
        new_segment = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    finally:
        exc.traceback_info = saved_traceback_info
        exc.message = saved_message
    quoted_new = _quote_traceback_info(new_segment)
    if saved_traceback_info is None:
        return f"{_PRE_RPC_TRACEBACK_HEADER}\n{quoted_new}"
    quoted_existing = _quote_traceback_info(saved_traceback_info)
    return f"{_PRE_RPC_TRACEBACK_HEADER}\n{quoted_new}\n{quoted_existing}"


def encode_static(value: IRStatic) -> JSONTypes:
    """Encode an :data:`IRStatic` tree to a JSON-serializable value.

    Primitive leaves (``None`` / ``bool`` / ``int`` / ``float`` / ``str``) are emitted literally.
    ``bytes`` use the ``["bytes", "<base64>"]`` escape. Plain mappings are literal JSON objects;
    plain sequences are double-wrapped (``[[...]]``) per the Cap'n'Web array-escape convention.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if isinstance(value, IRException):
        data_enc: JSONTypes = None if value.data is None else encode_static(value.data)
        # Encode-time enrichment: prepend a fresh ``format_exception`` segment for
        # any local Python ``__traceback__`` / ``__notes__`` accumulated since the
        # last serialization, with the existing ``traceback_info`` line-quoted one
        # nesting level deeper. See the IRException docstring for the propagation
        # model.
        wire_traceback_info: JSONTypes = _build_wire_traceback_info(value)
        return ["error", value.message, value.category, data_enc, wire_traceback_info]
    if isinstance(value, IRTagged):
        return ["tag", encode_static(value.tag), encode_static(value.value)]
    if isinstance(value, Mapping):
        out: dict[str, JSONTypes] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise MalformedStaticError(
                    f"Static mapping key must be str; got {type(k).__name__}"
                )
            out[k] = encode_static(v)
        return out
    if isinstance(value, Sequence):
        # Not str / bytes (handled above). Array-escape.
        return [[encode_static(x) for x in value]]
    raise MalformedStaticError(f"Cannot encode {type(value).__name__!s} as IRStatic")


def encode_expression(expr: Expression[Any]) -> JSONTypes:
    """Encode an :class:`Expression` to a JSON-serializable value.

    ``StaticExpr`` values are inlined (no outer wrapper) using the same escape rules as
    :func:`encode_static`. All other expression variants use a list-tagged form.
    """
    if isinstance(expr, StaticExpr):
        return encode_static(expr.value)
    if isinstance(expr, ListExpr):
        return [[encode_expression(e) for e in expr.elements]]
    if isinstance(expr, DictExpr):
        return {k: encode_expression(v) for k, v in expr.items.items()}
    if isinstance(expr, IRTaggedExpr):
        return ["tag", encode_static(expr.tag), encode_expression(expr.value)]
    if isinstance(expr, ImportedLifetimeTokenExpr):
        return ["import_token", expr.import_id]
    if isinstance(expr, ExportedLifetimeTokenExpr):
        return ["export_token", expr.token_export_id]
    if isinstance(expr, ImportedLeaseExpr):
        return ["import_lease", expr.grant_import_id]
    if isinstance(expr, ExportedLeaseExpr):
        return ["export_lease", expr.grant_export_id, expr.token_export_id]
    if isinstance(expr, ImportedIRHandleExpr):
        return ["import_handle", expr.import_id]
    if isinstance(expr, ExportedIRHandleExpr):
        return ["export_handle", expr.handle_export_id, expr.token_export_id]
    if isinstance(expr, ImportedIRFutureExpr):
        return ["import_future", expr.import_id]
    if isinstance(expr, ExportedIRFutureExpr):
        return ["export_future", expr.future_export_id, expr.token_export_id]
    if isinstance(expr, AwaitFutureExpr):
        return ["await", encode_expression(expr.future), encode_expression(expr.lease)]
    if isinstance(expr, WalkFutureExpr):
        return [
            "walk",
            encode_expression(expr.future),
            [_encode_path_step(s) for s in expr.attr_path],
        ]
    if isinstance(expr, HandleRequestExpr):
        return [
            "request",
            encode_expression(expr.handle),
            encode_expression(expr.lease),
            _encode_request_form(expr.request),
            [encode_expression(w) for w in expr.wait_for],
        ]
    if isinstance(expr, HandleDispatchExpr):
        return [
            "dispatch",
            encode_expression(expr.handle),
            encode_expression(expr.lease),
            _encode_request_form(expr.request),
            [encode_expression(w) for w in expr.wait_for],
        ]
    raise MalformedExpressionError(f"Cannot encode {type(expr).__name__} as Expression")


def encode_message(msg: RPCMessage) -> JSONTypes:
    """Encode an :class:`RPCMessage` to a JSON-serializable value."""
    if isinstance(msg, HelloMessage):
        return ["fxtr_rpc_hello", _PROTOCOL_VERSION]
    if isinstance(msg, PushMessage):
        return ["push", encode_expression(msg.expression)]
    if isinstance(msg, PullMessage):
        if msg.grant_import_id == msg.future_import_id + 1:
            return ["pull", msg.future_import_id]
        return ["pull", msg.future_import_id, msg.grant_import_id]
    if isinstance(msg, ResolveMessage):
        return ["resolve", msg.future_export_id, encode_expression(msg.expression)]
    if isinstance(msg, RejectMessage):
        return ["reject", msg.future_export_id, _encode_static_expression(msg.expression)]
    if isinstance(msg, WatchMessage):
        return ["watch", msg.token_import_id]
    if isinstance(msg, ClosedMessage):
        return ["closed", msg.token_export_id]
    if isinstance(msg, BreakMessage):
        return ["break", msg.grant_export_id, msg.reason]
    if isinstance(msg, DiscardMessage):
        return ["discard", msg.import_id, msg.refcount]
    if isinstance(msg, UnpullMessage):
        return ["unpull", msg.future_import_id, msg.refcount]
    if isinstance(msg, AbortMessage):
        return ["abort", _encode_static_expression(msg.expression)]
    raise MalformedMessageError(f"Cannot encode {type(msg).__name__} as RPCMessage")


####################################################################################################
# Decoders
####################################################################################################


def decode_static(value: JSONTypes) -> IRStatic:
    """Decode a JSON-serializable tree to an :data:`IRStatic` value.

    Raises :class:`NonStaticInStaticContextError` if the tree contains any expression-only tag
    (e.g. an import reference or an await/walk node). Other structural errors raise
    :class:`MalformedStaticError`.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, IRStatic] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise MalformedStaticError(
                    f"Static mapping key must be str; got {type(k).__name__}"
                )
            out[k] = decode_static(v)
        return out
    if isinstance(value, Sequence):
        return _decode_static_list(value)
    raise MalformedStaticError(f"Cannot decode {type(value).__name__!s} as IRStatic")


def decode_expression(value: JSONTypes) -> Expression[Any]:
    """Decode a JSON-serializable tree to an :class:`Expression`.

    The decoder produces the "obvious" shape for each wire form, with ``StaticExpr`` only at
    leaves whose wire form is inherently static (primitives / ``["bytes", ...]`` /
    ``["error", ...]``). Containers decode to their structural expression form
    (``ListExpr`` / ``DictExpr`` / ``IRTaggedExpr``) even when every child happens to be a
    ``StaticExpr`` — i.e. the decoder pushes ``StaticExpr`` in toward the leaves rather than
    pulling it out to the root. Consumers that want to ask "is this fully static?" can walk
    the tree themselves; the codec doesn't make that decision.

    Note that this means ``encode_expression`` and ``decode_expression`` are not a structural
    identity on all inputs: ``StaticExpr([1, 2])`` and
    ``ListExpr((StaticExpr(1), StaticExpr(2)))`` both encode to ``[[1, 2]]``, and both
    decode to the latter form. They remain semantically equivalent under the Cap-IR evaluator.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return StaticExpr(value)
    if isinstance(value, Mapping):
        children: dict[str, Expression[Any]] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise MalformedExpressionError(
                    f"Dict expression key must be str; got {type(k).__name__}"
                )
            children[k] = decode_expression(v)
        return DictExpr(children)
    if isinstance(value, Sequence):
        return _decode_expression_list(value)
    raise MalformedExpressionError(f"Cannot decode {type(value).__name__} as Expression")


def decode_message(value: JSONTypes) -> RPCMessage:
    """Decode a JSON-serializable tree to an :class:`RPCMessage`."""
    if not _is_list(value):
        raise MalformedMessageError(
            f"Top-level RPC message must be a list; got {type(value).__name__}"
        )
    lst = cast(Sequence[JSONTypes], value)
    if len(lst) == 0:
        raise MalformedMessageError("Empty list is not a valid RPC message")
    head = lst[0]
    if not isinstance(head, str):
        raise MalformedMessageError(f"RPC message tag must be a string; got {head!r}")
    handler = _MESSAGE_DECODERS.get(head)
    if handler is None:
        raise MalformedMessageError(f"Unknown RPC message tag: {head!r}")
    return handler(lst)


def decode(
    value: JSONTypes,
    *,
    mode: Literal["message", "expression", "static"],
) -> RPCMessage | Expression[Any] | IRStatic:
    """Decode ``value`` under the given ``mode`` (``"message"`` / ``"expression"`` / ``"static"``).

    Each mode dispatches to the corresponding ``decode_*`` function. Passing a ``mode`` not in
    that set raises :class:`ValueError`.
    """
    if mode == "message":
        return decode_message(value)
    if mode == "expression":
        return decode_expression(value)
    if mode == "static":
        return decode_static(value)
    raise ValueError(f"Unknown decode mode: {mode!r}")


####################################################################################################
# Encoder helpers
####################################################################################################


def _encode_static_expression(expr: Expression[Any]) -> JSONTypes:
    """Encode an expression required to be statically-evaluable (for reject/abort payloads).

    Only :class:`StaticExpr` is accepted; other expression variants raise
    :class:`MalformedMessageError`. The canonical form for reject/abort is
    ``StaticExpr(IRException(...))``; anything else indicates a bug in the caller.
    """
    if not isinstance(expr, StaticExpr):
        raise MalformedMessageError(
            f"reject/abort expression must be a StaticExpr; got {type(expr).__name__}"
        )
    return encode_static(expr.value)


def _encode_path_step(step: str | int) -> JSONTypes:
    if isinstance(step, bool) or not isinstance(step, (str, int)):
        raise MalformedExpressionError(f"Path step must be str or int; got {type(step).__name__}")
    return step


def _encode_method_or_attr(value: str | int | Sequence[str | int]) -> JSONTypes:
    if isinstance(value, bool):
        raise MalformedExpressionError("method/attr may not be a bool")
    if isinstance(value, (str, int)):
        return value
    if isinstance(value, Sequence):
        return [_encode_path_step(s) for s in value]
    raise MalformedExpressionError(
        f"method/attr must be str/int/Sequence; got {type(value).__name__}"
    )


def _encode_request_form(request: Call[Expression[Any]] | GetItem) -> JSONTypes:
    if isinstance(request, Call):
        return ["call", _encode_method_or_attr(request.method), encode_expression(request.arg)]
    if isinstance(request, GetItem):
        return ["get", _encode_method_or_attr(request.attr)]
    raise MalformedExpressionError(
        f"Request form must be Call or GetItem; got {type(request).__name__}"
    )


####################################################################################################
# Decoder helpers
####################################################################################################


def _is_list(value: JSONTypes) -> bool:
    """True if ``value`` is a list-like sequence (not a str / bytes)."""
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _require_int(value: JSONTypes, *, ctx: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedMessageError(f"{ctx}: expected int, got {type(value).__name__}")
    return value


def _require_str(value: JSONTypes, *, ctx: str) -> str:
    if not isinstance(value, str):
        raise MalformedMessageError(f"{ctx}: expected str, got {type(value).__name__}")
    return value


def _require_list(value: JSONTypes, *, ctx: str) -> Sequence[JSONTypes]:
    if not _is_list(value):
        raise MalformedExpressionError(f"{ctx}: expected list, got {type(value).__name__}")
    return cast(Sequence[JSONTypes], value)


def _decode_path_step(value: JSONTypes) -> str | int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MalformedExpressionError(f"Path step must be str or int; got {type(value).__name__}")
    return value


def _decode_method_or_attr(value: JSONTypes) -> str | int | Sequence[str | int]:
    if isinstance(value, bool):
        raise MalformedExpressionError("method/attr may not be a bool")
    if isinstance(value, (str, int)):
        return value
    if _is_list(value):
        return tuple(_decode_path_step(s) for s in cast(Sequence[JSONTypes], value))
    raise MalformedExpressionError(f"method/attr must be str/int/list; got {type(value).__name__}")


def _decode_request_form(value: JSONTypes) -> Call[Expression[Any]] | GetItem:
    lst = _require_list(value, ctx="request form")
    if len(lst) == 0:
        raise MalformedExpressionError("Empty request form")
    tag = lst[0]
    if not isinstance(tag, str):
        raise MalformedExpressionError(
            f"Request form tag must be a string; got {type(tag).__name__}"
        )
    if tag == "call":
        if len(lst) != 3:
            raise MalformedExpressionError(f"'call' form must have 3 elements; got {len(lst)}")
        return Call(method=_decode_method_or_attr(lst[1]), arg=decode_expression(lst[2]))
    if tag == "get":
        if len(lst) != 2:
            raise MalformedExpressionError(f"'get' form must have 2 elements; got {len(lst)}")
        return GetItem(attr=_decode_method_or_attr(lst[1]))
    raise MalformedExpressionError(f"Unknown request form tag: {tag!r}")


def _decode_static_list(value: Sequence[JSONTypes]) -> IRStatic:
    if len(value) == 0:
        raise MalformedStaticError("Empty list is not a valid static value")
    head = value[0]
    if _is_list(head):
        # Array-escape: [[inner...]]
        if len(value) != 1:
            raise MalformedStaticError(
                f"Array-escape list must have exactly 1 element; got {len(value)}"
            )
        inner = cast(Sequence[JSONTypes], head)
        return [decode_static(x) for x in inner]
    if not isinstance(head, str):
        raise MalformedStaticError(
            f"Static list tag must be a string or nested list; got {type(head).__name__}"
        )
    if head == "bytes":
        if len(value) != 2:
            raise MalformedStaticError(f"'bytes' form must have 2 elements; got {len(value)}")
        b64 = value[1]
        if not isinstance(b64, str):
            raise MalformedStaticError(f"'bytes' payload must be str; got {type(b64).__name__}")
        try:
            return base64.b64decode(b64, validate=True)
        except Exception as e:
            raise MalformedStaticError(f"Invalid base64 in 'bytes' payload: {e}") from e
    if head == "tag":
        if len(value) != 3:
            raise MalformedStaticError(f"'tag' form must have 3 elements; got {len(value)}")
        return IRTagged(tag=decode_static(value[1]), value=decode_static(value[2]))
    if head == "error":
        return _decode_error_static(value)
    if head in _EXPRESSION_ONLY_TAGS:
        raise NonStaticInStaticContextError(
            f"Encountered expression-only tag {head!r} inside a static context"
        )
    raise MalformedStaticError(f"Unknown static list tag: {head!r}")


def _decode_error_static(value: Sequence[JSONTypes]) -> IRException[Any]:
    if len(value) != 5:
        raise MalformedStaticError(f"'error' form must have 5 elements; got {len(value)}")
    message_raw = value[1]
    category_raw = value[2]
    data_raw = value[3]
    traceback_info_raw = value[4]
    if not isinstance(message_raw, str):
        raise MalformedStaticError(
            f"'error' message must be a string; got {type(message_raw).__name__}"
        )
    if category_raw is not None and not isinstance(category_raw, str):
        raise MalformedStaticError(
            f"'error' category must be str or null; got {type(category_raw).__name__}"
        )
    if traceback_info_raw is not None and not isinstance(traceback_info_raw, str):
        raise MalformedStaticError(
            f"'error' traceback_info must be a string or null; "
            f"got {type(traceback_info_raw).__name__}"
        )
    data = None if data_raw is None else decode_static(data_raw)
    return IRException(message_raw, category_raw, data, traceback_info=traceback_info_raw)


def _decode_expression_list(value: Sequence[JSONTypes]) -> Expression[Any]:
    if len(value) == 0:
        raise MalformedExpressionError("Empty list is not a valid expression")
    head = value[0]
    if _is_list(head):
        if len(value) != 1:
            raise MalformedExpressionError(
                f"Array-escape list must have exactly 1 element; got {len(value)}"
            )
        inner = cast(Sequence[JSONTypes], head)
        return ListExpr(tuple(decode_expression(x) for x in inner))
    if not isinstance(head, str):
        raise MalformedExpressionError(
            f"Expression list tag must be a string or nested list; got {type(head).__name__}"
        )
    handler = _EXPRESSION_DECODERS.get(head)
    if handler is None:
        raise MalformedExpressionError(f"Unknown expression tag: {head!r}")
    return handler(value)


def _decode_expr_bytes(value: Sequence[JSONTypes]) -> Expression[Any]:
    if len(value) != 2:
        raise MalformedExpressionError(f"'bytes' form must have 2 elements; got {len(value)}")
    b64 = value[1]
    if not isinstance(b64, str):
        raise MalformedExpressionError(f"'bytes' payload must be str; got {type(b64).__name__}")
    try:
        return StaticExpr(base64.b64decode(b64, validate=True))
    except Exception as e:
        raise MalformedExpressionError(f"Invalid base64 in 'bytes' payload: {e}") from e


def _decode_expr_tag(value: Sequence[JSONTypes]) -> Expression[Any]:
    if len(value) != 3:
        raise MalformedExpressionError(f"'tag' form must have 3 elements; got {len(value)}")
    tag = decode_static(value[1])
    inner = decode_expression(value[2])
    return IRTaggedExpr(tag=tag, value=inner)


def _decode_expr_error(value: Sequence[JSONTypes]) -> Expression[Any]:
    return StaticExpr(_decode_error_static(value))


def _decode_expr_import_token(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 2, "import_token")
    return ImportedLifetimeTokenExpr(import_id=_require_int(value[1], ctx="import_token.id"))


def _decode_expr_export_token(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 2, "export_token")
    return ExportedLifetimeTokenExpr(token_export_id=_require_int(value[1], ctx="export_token.id"))


def _decode_expr_import_lease(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 2, "import_lease")
    return ImportedLeaseExpr(
        grant_import_id=_require_int(value[1], ctx="import_lease.grant_import_id")
    )


def _decode_expr_export_lease(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 3, "export_lease")
    return ExportedLeaseExpr(
        grant_export_id=_require_int(value[1], ctx="export_lease.grant_export_id"),
        token_export_id=_require_int(value[2], ctx="export_lease.token_export_id"),
    )


def _decode_expr_import_handle(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 2, "import_handle")
    return ImportedIRHandleExpr(import_id=_require_int(value[1], ctx="import_handle.id"))


def _decode_expr_export_handle(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 3, "export_handle")
    return ExportedIRHandleExpr(
        handle_export_id=_require_int(value[1], ctx="export_handle.handle_export_id"),
        token_export_id=_require_int(value[2], ctx="export_handle.token_export_id"),
    )


def _decode_expr_import_future(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 2, "import_future")
    return ImportedIRFutureExpr(import_id=_require_int(value[1], ctx="import_future.id"))


def _decode_expr_export_future(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 3, "export_future")
    return ExportedIRFutureExpr(
        future_export_id=_require_int(value[1], ctx="export_future.future_export_id"),
        token_export_id=_require_int(value[2], ctx="export_future.token_export_id"),
    )


def _decode_expr_await(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 3, "await")
    future = decode_expression(value[1])
    lease = decode_expression(value[2])
    return AwaitFutureExpr(future=future, lease=lease)


def _decode_expr_walk(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 3, "walk")
    future = decode_expression(value[1])
    path_raw = _require_list(value[2], ctx="walk.attr_path")
    path = tuple(_decode_path_step(s) for s in path_raw)
    return WalkFutureExpr(future=future, attr_path=path)


def _decode_expr_request(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 5, "request")
    return HandleRequestExpr(
        handle=decode_expression(value[1]),
        lease=decode_expression(value[2]),
        request=_decode_request_form(value[3]),
        wait_for=_decode_wait_for(value[4]),
    )


def _decode_expr_dispatch(value: Sequence[JSONTypes]) -> Expression[Any]:
    _require_len(value, 5, "dispatch")
    return HandleDispatchExpr(
        handle=decode_expression(value[1]),
        lease=decode_expression(value[2]),
        request=_decode_request_form(value[3]),
        wait_for=_decode_wait_for(value[4]),
    )


def _decode_wait_for(value: JSONTypes) -> tuple[AwaitFutureExpr[Any], ...]:
    lst = _require_list(value, ctx="wait_for")
    result: list[AwaitFutureExpr[Any]] = []
    for i, elem in enumerate(lst):
        decoded = decode_expression(elem)
        if not isinstance(decoded, AwaitFutureExpr):
            raise MalformedExpressionError(
                f"wait_for[{i}] must decode to AwaitFutureExpr; got {type(decoded).__name__}"
            )
        result.append(decoded)
    return tuple(result)


def _require_len(value: Sequence[JSONTypes], expected: int, ctx: str) -> None:
    if len(value) != expected:
        raise MalformedExpressionError(
            f"{ctx!r} form must have {expected} elements; got {len(value)}"
        )


# Dispatch tables.
_EXPRESSION_DECODERS: dict[str, Callable[[Sequence[JSONTypes]], Expression[Any]]] = {
    "bytes": _decode_expr_bytes,
    "tag": _decode_expr_tag,
    "error": _decode_expr_error,
    "import_token": _decode_expr_import_token,
    "export_token": _decode_expr_export_token,
    "import_lease": _decode_expr_import_lease,
    "export_lease": _decode_expr_export_lease,
    "import_handle": _decode_expr_import_handle,
    "export_handle": _decode_expr_export_handle,
    "import_future": _decode_expr_import_future,
    "export_future": _decode_expr_export_future,
    "await": _decode_expr_await,
    "walk": _decode_expr_walk,
    "request": _decode_expr_request,
    "dispatch": _decode_expr_dispatch,
}


_EXPRESSION_ONLY_TAGS: frozenset[str] = frozenset(
    {
        "import_token",
        "export_token",
        "import_lease",
        "export_lease",
        "import_handle",
        "export_handle",
        "import_future",
        "export_future",
        "await",
        "walk",
        "request",
        "dispatch",
    }
)


####################################################################################################
# Message decoders
####################################################################################################


def _decode_msg_hello(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) != 2:
        raise MalformedMessageError(f"'fxtr_rpc_hello' form must have 2 elements; got {len(value)}")
    version = value[1]
    if isinstance(version, bool) or not isinstance(version, int):
        raise MalformedMessageError(
            f"'fxtr_rpc_hello' version must be an integer; got {type(version).__name__}"
        )
    if version != _PROTOCOL_VERSION:
        raise ProtocolVersionError(
            f"Unsupported protocol version: expected {_PROTOCOL_VERSION}, got {version}"
        )
    return HelloMessage()


def _decode_msg_push(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) != 2:
        raise MalformedMessageError(f"'push' form must have 2 elements; got {len(value)}")
    return PushMessage(expression=decode_expression(value[1]))


def _decode_msg_pull(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) == 2:
        fid = _require_int(value[1], ctx="pull.future_import_id")
        return PullMessage(future_import_id=fid, grant_import_id=fid + 1)
    if len(value) == 3:
        fid = _require_int(value[1], ctx="pull.future_import_id")
        gid = _require_int(value[2], ctx="pull.grant_import_id")
        return PullMessage(future_import_id=fid, grant_import_id=gid)
    raise MalformedMessageError(f"'pull' form must have 2 or 3 elements; got {len(value)}")


def _decode_msg_resolve(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) != 3:
        raise MalformedMessageError(f"'resolve' form must have 3 elements; got {len(value)}")
    return ResolveMessage(
        future_export_id=_require_int(value[1], ctx="resolve.future_export_id"),
        expression=decode_expression(value[2]),
    )


def _decode_msg_reject(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) != 3:
        raise MalformedMessageError(f"'reject' form must have 3 elements; got {len(value)}")
    static_value = decode_static(value[2])
    if not isinstance(static_value, IRException):
        raise MalformedMessageError(
            f"'reject' payload must decode to an IRException; got {type(static_value).__name__}"
        )
    return RejectMessage(
        future_export_id=_require_int(value[1], ctx="reject.future_export_id"),
        expression=StaticExpr(static_value),
    )


def _decode_msg_watch(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) != 2:
        raise MalformedMessageError(f"'watch' form must have 2 elements; got {len(value)}")
    return WatchMessage(token_import_id=_require_int(value[1], ctx="watch.token_import_id"))


def _decode_msg_closed(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) != 2:
        raise MalformedMessageError(f"'closed' form must have 2 elements; got {len(value)}")
    return ClosedMessage(token_export_id=_require_int(value[1], ctx="closed.token_export_id"))


def _decode_msg_break(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) != 3:
        raise MalformedMessageError(f"'break' form must have 3 elements; got {len(value)}")
    return BreakMessage(
        grant_export_id=_require_int(value[1], ctx="break.grant_export_id"),
        reason=_require_str(value[2], ctx="break.reason"),
    )


def _decode_msg_discard(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) != 3:
        raise MalformedMessageError(f"'discard' form must have 3 elements; got {len(value)}")
    return DiscardMessage(
        import_id=_require_int(value[1], ctx="discard.import_id"),
        refcount=_require_int(value[2], ctx="discard.refcount"),
    )


def _decode_msg_unpull(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) != 3:
        raise MalformedMessageError(f"'unpull' form must have 3 elements; got {len(value)}")
    return UnpullMessage(
        future_import_id=_require_int(value[1], ctx="unpull.future_import_id"),
        refcount=_require_int(value[2], ctx="unpull.refcount"),
    )


def _decode_msg_abort(value: Sequence[JSONTypes]) -> RPCMessage:
    if len(value) != 2:
        raise MalformedMessageError(f"'abort' form must have 2 elements; got {len(value)}")
    static_value = decode_static(value[1])
    if not isinstance(static_value, IRException):
        raise MalformedMessageError(
            f"'abort' payload must decode to an IRException; got {type(static_value).__name__}"
        )
    return AbortMessage(expression=StaticExpr(static_value))


_MESSAGE_DECODERS: dict[str, Callable[[Sequence[JSONTypes]], RPCMessage]] = {
    "fxtr_rpc_hello": _decode_msg_hello,
    "push": _decode_msg_push,
    "pull": _decode_msg_pull,
    "resolve": _decode_msg_resolve,
    "reject": _decode_msg_reject,
    "watch": _decode_msg_watch,
    "closed": _decode_msg_closed,
    "break": _decode_msg_break,
    "discard": _decode_msg_discard,
    "unpull": _decode_msg_unpull,
    "abort": _decode_msg_abort,
}


####################################################################################################
# Stream wrapper
####################################################################################################


class _JsonObjectStream(anyio.abc.ObjectStream[RPCMessage]):
    """Wraps an :class:`anyio.abc.ObjectStream` of JSON values as a stream of :class:`RPCMessage`.

    Each outbound send runs :func:`encode_message`; each inbound receive runs
    :func:`decode_message`. ``send_eof`` / ``aclose`` / ``extra_attributes`` delegate straight to
    the inner transport.
    """

    def __init__(self, inner: anyio.abc.ObjectStream[JSONTypes]) -> None:
        self._inner = inner

    async def send(self, item: RPCMessage) -> None:
        await self._inner.send(encode_message(item))

    async def receive(self) -> RPCMessage:
        return decode_message(await self._inner.receive())

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def send_eof(self) -> None:
        await self._inner.send_eof()

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return self._inner.extra_attributes


def wrap_json_stream(
    inner: anyio.abc.ObjectStream[JSONTypes],
) -> anyio.abc.ObjectStream[RPCMessage]:
    """Wrap a JSON-valued transport as a stream of :class:`RPCMessage`.

    The returned stream sends by encoding each :class:`RPCMessage` to JSON and decoding on
    receive. A framing-to-bytes layer (newline / length-prefix / …) is a separate concern and
    sits underneath this helper.
    """
    return _JsonObjectStream(inner)


__all__ = [
    "JSONTypes",
    "JsonCodecError",
    "MalformedMessageError",
    "MalformedExpressionError",
    "MalformedStaticError",
    "NonStaticInStaticContextError",
    "ProtocolVersionError",
    "encode_static",
    "encode_expression",
    "encode_message",
    "decode_static",
    "decode_expression",
    "decode_message",
    "decode",
    "wrap_json_stream",
]
