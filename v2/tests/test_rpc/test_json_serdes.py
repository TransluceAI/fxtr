"""Unit tests for fxtr2.rpc.json_serdes — the JSON wire codec.

Parameterized roundtrip coverage across every message / expression / static variant,
plus a separate parameterized suite of malformed-input cases that each assert the
correct :class:`JsonCodecError` subclass fires.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from fxtr2.leases.leased_cap_ir import Call, GetItem, IRException, IRStatic, IRTagged
from fxtr2.rpc import json_serdes
from fxtr2.rpc.json_serdes import (
    JSONTypes,
    MalformedExpressionError,
    MalformedMessageError,
    MalformedStaticError,
    NonStaticInStaticContextError,
    ProtocolVersionError,
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

####################################################################################################
# Structural equality helper
####################################################################################################


def _ir_equal(a: Any, b: Any) -> bool:
    """Compare two values for structural equality.

    ``IRException`` inherits from ``Exception`` and uses identity-based equality, so we need
    a custom walker that unpacks the ``(message, category, data)`` triple.
    """
    if isinstance(a, IRException) or isinstance(b, IRException):
        if not (isinstance(a, IRException) and isinstance(b, IRException)):
            return False
        return a.message == b.message and a.category == b.category and _ir_equal(a.data, b.data)
    # Tolerate list vs tuple (tuples only appear on the decode side for attr paths).
    if type(a) is not type(b) and not (
        isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))
    ):
        return False
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(_ir_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_ir_equal(a[k], b[k]) for k in a)
    if dataclasses.is_dataclass(a) and not isinstance(a, type):
        for f in dataclasses.fields(a):
            if not _ir_equal(getattr(a, f.name), getattr(b, f.name)):
                return False
        return True
    return a == b


####################################################################################################
# Static round-trip
####################################################################################################

STATIC_ROUNDTRIP_CASES: list[tuple[str, IRStatic, JSONTypes]] = [
    ("none", None, None),
    ("bool_true", True, True),
    ("bool_false", False, False),
    ("int", 42, 42),
    ("float", 3.25, 3.25),
    ("str_empty", "", ""),
    ("str_nonempty", "hello", "hello"),
    ("bytes_empty", b"", ["bytes", ""]),
    ("bytes_nonempty", b"hello", ["bytes", "aGVsbG8="]),
    ("list_empty", [], [[]]),
    ("list_primitives", [1, "x", True, None], [[1, "x", True, None]]),
    ("list_nested", [[1, 2], [3]], [[[[1, 2]], [[3]]]]),
    ("dict_empty", {}, {}),
    (
        "dict_primitives",
        {"a": 1, "b": "x", "c": None},
        {"a": 1, "b": "x", "c": None},
    ),
    (
        "dict_nested",
        {"outer": {"inner": [1, 2]}},
        {"outer": {"inner": [[1, 2]]}},
    ),
    (
        "tagged_primitive",
        IRTagged(tag="date", value=1234567),
        ["tag", "date", 1234567],
    ),
    (
        "tagged_nested",
        IRTagged(tag=5, value=IRTagged(tag="inner", value="hi")),
        ["tag", 5, ["tag", "inner", "hi"]],
    ),
    (
        "exception_no_category_no_data",
        IRException("boom", None, None),
        ["error", "boom", None, None, None],
    ),
    (
        "exception_with_category",
        IRException("boom", "value", None),
        ["error", "boom", "value", None, None],
    ),
    (
        "exception_with_data",
        IRException("boom", "cat", {"k": 7}),
        ["error", "boom", "cat", {"k": 7}, None],
    ),
    (
        "exception_with_list_data",
        IRException("boom", None, [1, 2, 3]),
        ["error", "boom", None, [[1, 2, 3]], None],
    ),
    (
        "exception_with_traceback_info",
        IRException("boom", "MyError", None, traceback_info="trace content\nline2"),
        ["error", "boom", "MyError", None, "trace content\nline2"],
    ),
]


@pytest.mark.parametrize(
    "name,value,wire",
    STATIC_ROUNDTRIP_CASES,
    ids=[c[0] for c in STATIC_ROUNDTRIP_CASES],
)
def test_static_roundtrip(name: str, value: IRStatic, wire: JSONTypes) -> None:
    encoded = json_serdes.encode_static(value)
    assert encoded == wire, f"encode mismatch: {encoded!r} vs {wire!r}"
    decoded = json_serdes.decode_static(encoded)
    assert _ir_equal(decoded, value), f"decode mismatch: {decoded!r} vs {value!r}"


####################################################################################################
# IRException traceback_info enrichment
####################################################################################################


def test_encode_static_prepends_fresh_segment_for_raised_exception() -> None:
    """When an IRException has a Python ``__traceback__`` attached (e.g. it was raised
    and caught), :func:`encode_static` builds a fresh ``format_exception`` segment and
    emits it as the wire-side ``traceback_info`` (replacing the previous None).
    """
    exc: IRException[Any] = IRException("boom", category="MyError")
    try:
        raise exc
    except IRException:
        pass
    assert exc.__traceback__ is not None
    encoded = json_serdes.encode_static(exc)
    assert isinstance(encoded, list)
    assert encoded[0] == "error"
    assert encoded[1] == "boom"
    assert encoded[2] == "MyError"
    assert encoded[3] is None
    wire_tb = encoded[4]
    assert isinstance(wire_tb, str)
    assert "MyError" in wire_tb
    assert "boom" in wire_tb
    assert __file__ in wire_tb


def test_encode_static_no_local_state_passes_through() -> None:
    """An IRException constructed without being raised has no Python ``__traceback__``
    or ``__notes__`` to capture — :func:`encode_static` emits ``traceback_info``
    unchanged.
    """
    exc: IRException[Any] = IRException(
        "boom", category="MyError", traceback_info="existing-content"
    )
    encoded = json_serdes.encode_static(exc)
    assert isinstance(encoded, list)
    assert encoded[4] == "existing-content"


def test_encode_static_prepends_with_quoted_prior_chain() -> None:
    """If an IRException already carries ``traceback_info`` from a prior hop AND has
    fresh local Python state, the new segment is prepended at the top with the prior
    content quoted one ``>`` level deeper.

    Regression: the new fresh segment (top, no prefix) must NOT contain the prior
    content as a substring (would happen if ``__str__`` were folding it into
    ``format_exception``'s output). The prior content shows up only inside the
    quoted block at the bottom.
    """
    PRIOR_MARKER = "earlier-hop-marker-string-zzz"
    exc: IRException[Any] = IRException("boom", traceback_info=PRIOR_MARKER)
    try:
        raise exc
    except IRException:
        pass
    encoded = json_serdes.encode_static(exc)
    assert isinstance(encoded, list)
    wire_tb = encoded[4]
    assert isinstance(wire_tb, str)
    # Header separates new segment from quoted prior chain.
    assert "Earlier (pre-RPC) version of this exception:" in wire_tb
    # Prior marker survives, line-quoted with "> ".
    assert f"> {PRIOR_MARKER}" in wire_tb
    # The new segment (everything before the header) must NOT contain the
    # prior marker — that's the encode-time clearing in action.
    new_segment, _, _ = wire_tb.partition("Earlier (pre-RPC) version of this exception:")
    assert PRIOR_MARKER not in new_segment


def test_encode_static_truncates_long_message_in_formatted_segment() -> None:
    """Very long ``message`` strings get truncated *only inside the formatted
    segment* — the wire-side ``message`` field still carries the full value.
    """
    long_message = "x" * 5000
    exc: IRException[Any] = IRException(long_message, category="Big")
    try:
        raise exc
    except IRException:
        pass
    encoded = json_serdes.encode_static(exc)
    assert isinstance(encoded, list)
    # message field on the wire is the full value.
    assert encoded[1] == long_message
    # but the formatted traceback_info segment contains a truncated version.
    wire_tb = encoded[4]
    assert isinstance(wire_tb, str)
    assert "(truncated)" in wire_tb
    # Bound: the formatted segment shouldn't contain the full 5000-char message.
    assert long_message not in wire_tb


def test_encode_decode_roundtrip_preserves_traceback_info() -> None:
    """A simple roundtrip on an IRException with no Python ``__traceback__`` preserves
    the ``traceback_info`` field exactly.
    """
    original: IRException[Any] = IRException(
        "boom",
        category="MyError",
        data=None,
        traceback_info="entry one\nentry two",
    )
    encoded = json_serdes.encode_static(original)
    decoded = json_serdes.decode_static(encoded)
    assert isinstance(decoded, IRException)
    assert decoded.message == "boom"
    assert decoded.category == "MyError"
    assert decoded.traceback_info == "entry one\nentry two"


####################################################################################################
# Expression round-trip
####################################################################################################

EXPRESSION_ROUNDTRIP_CASES: list[tuple[str, Expression[Any], JSONTypes]] = [
    # Leaf StaticExprs — roundtrip-stable since the wire form is inherently static.
    ("static_int", StaticExpr(7), 7),
    ("static_str", StaticExpr("hi"), "hi"),
    ("static_none", StaticExpr(None), None),
    ("static_bytes", StaticExpr(b"ab"), ["bytes", "YWI="]),
    (
        "static_exception",
        StaticExpr(IRException("boom", None, None)),
        ["error", "boom", None, None, None],
    ),
    # Containers use their structural expression form. ``StaticExpr([1, 2])``,
    # ``ListExpr((StaticExpr(1), StaticExpr(2)))``, etc. all encode to the same wire bytes,
    # but the decoder always produces the structural (non-collapsed) form. Roundtrip-stable
    # cases are therefore the ones that are already in that form.
    (
        "list_all_static",
        ListExpr((StaticExpr(1), StaticExpr(2))),
        [[1, 2]],
    ),
    (
        "dict_all_static",
        DictExpr({"k": StaticExpr(1)}),
        {"k": 1},
    ),
    (
        "tagged_static_inner",
        IRTaggedExpr(tag="t", value=StaticExpr(5)),
        ["tag", "t", 5],
    ),
    # Imports / exports.
    ("import_token", ImportedLifetimeTokenExpr(import_id=3), ["import_token", 3]),
    ("export_token", ExportedLifetimeTokenExpr(token_export_id=-4), ["export_token", -4]),
    ("import_lease", ImportedLeaseExpr(grant_import_id=5), ["import_lease", 5]),
    (
        "export_lease",
        ExportedLeaseExpr(grant_export_id=-7, token_export_id=-3),
        ["export_lease", -7, -3],
    ),
    ("import_handle", ImportedIRHandleExpr(import_id=6), ["import_handle", 6]),
    (
        "export_handle",
        ExportedIRHandleExpr(handle_export_id=-2, token_export_id=-1),
        ["export_handle", -2, -1],
    ),
    ("import_future", ImportedIRFutureExpr(import_id=9), ["import_future", 9]),
    (
        "export_future",
        ExportedIRFutureExpr(future_export_id=-5, token_export_id=-4),
        ["export_future", -5, -4],
    ),
    # Operations.
    (
        "await",
        AwaitFutureExpr(
            future=ImportedIRFutureExpr(import_id=3),
            lease=ImportedLeaseExpr(grant_import_id=4),
        ),
        ["await", ["import_future", 3], ["import_lease", 4]],
    ),
    (
        "walk_single_step",
        WalkFutureExpr(future=ImportedIRFutureExpr(import_id=3), attr_path=("a",)),
        ["walk", ["import_future", 3], ["a"]],
    ),
    (
        "walk_mixed_path",
        WalkFutureExpr(future=ImportedIRFutureExpr(import_id=3), attr_path=("a", 0, "b")),
        ["walk", ["import_future", 3], ["a", 0, "b"]],
    ),
    (
        "request_call",
        HandleRequestExpr(
            handle=ImportedIRHandleExpr(import_id=1),
            lease=ImportedLeaseExpr(grant_import_id=2),
            request=Call(method="foo", arg=StaticExpr(42)),
            wait_for=(),
        ),
        ["request", ["import_handle", 1], ["import_lease", 2], ["call", "foo", 42], []],
    ),
    (
        "request_getitem",
        HandleRequestExpr(
            handle=ImportedIRHandleExpr(import_id=1),
            lease=ImportedLeaseExpr(grant_import_id=2),
            request=GetItem(attr=("x", "y")),
            wait_for=(),
        ),
        ["request", ["import_handle", 1], ["import_lease", 2], ["get", ["x", "y"]], []],
    ),
    (
        "dispatch_with_wait_for",
        HandleDispatchExpr(
            handle=ImportedIRHandleExpr(import_id=1),
            lease=ImportedLeaseExpr(grant_import_id=2),
            request=Call(method=("a", "b"), arg=StaticExpr(None)),
            wait_for=(
                AwaitFutureExpr(
                    future=ImportedIRFutureExpr(import_id=7),
                    lease=ImportedLeaseExpr(grant_import_id=8),
                ),
            ),
        ),
        [
            "dispatch",
            ["import_handle", 1],
            ["import_lease", 2],
            ["call", ["a", "b"], None],
            [["await", ["import_future", 7], ["import_lease", 8]]],
        ],
    ),
    # Tagged expression with a dynamic child.
    (
        "tagged_expr_dynamic_inner",
        IRTaggedExpr(tag="x", value=ImportedIRHandleExpr(import_id=1)),
        ["tag", "x", ["import_handle", 1]],
    ),
    # Dict with a mix of static and dynamic children => DictExpr.
    (
        "dict_expr_mixed",
        DictExpr({"a": StaticExpr(1), "h": ImportedIRHandleExpr(import_id=1)}),
        {"a": 1, "h": ["import_handle", 1]},
    ),
    # List with a mix of static and dynamic children => ListExpr.
    (
        "list_expr_mixed",
        ListExpr((StaticExpr(1), ImportedIRHandleExpr(import_id=2))),
        [[1, ["import_handle", 2]]],
    ),
]


@pytest.mark.parametrize(
    "name,expr,wire",
    EXPRESSION_ROUNDTRIP_CASES,
    ids=[c[0] for c in EXPRESSION_ROUNDTRIP_CASES],
)
def test_expression_roundtrip(name: str, expr: Expression[Any], wire: JSONTypes) -> None:
    encoded = json_serdes.encode_expression(expr)
    assert encoded == wire, f"encode mismatch: {encoded!r} vs {wire!r}"
    decoded = json_serdes.decode_expression(encoded)
    assert _ir_equal(decoded, expr), f"decode mismatch: {decoded!r} vs {expr!r}"


####################################################################################################
# Encode / decode asymmetry at containers
####################################################################################################
#
# ``StaticExpr([1, 2])`` and ``ListExpr((StaticExpr(1), StaticExpr(2)))`` both encode to the
# same wire form ``[[1, 2]]``. The decoder always picks the structural (``ListExpr``) form —
# i.e. ``StaticExpr`` is pushed in toward the leaves rather than pulled out to the root. These
# tests pin the asymmetry down so nobody accidentally re-introduces collapse later.


def test_static_expr_with_list_encodes_inline_but_decodes_to_list_expr() -> None:
    """Encoding ``StaticExpr([1, 2])`` matches the inline array-escape, but decoding that
    wire form produces ``ListExpr((StaticExpr(1), StaticExpr(2)))`` — the decoder does not
    collapse all-static subtrees back to a single ``StaticExpr``."""
    original = StaticExpr([1, 2])
    wire = json_serdes.encode_expression(original)
    assert wire == [[1, 2]]
    decoded = json_serdes.decode_expression(wire)
    assert decoded == ListExpr((StaticExpr(1), StaticExpr(2)))


def test_static_expr_with_dict_encodes_inline_but_decodes_to_dict_expr() -> None:
    original = StaticExpr({"k": 1})
    wire = json_serdes.encode_expression(original)
    assert wire == {"k": 1}
    decoded = json_serdes.decode_expression(wire)
    assert decoded == DictExpr({"k": StaticExpr(1)})


def test_static_expr_with_tagged_encodes_inline_but_decodes_to_tagged_expr() -> None:
    original = StaticExpr(IRTagged(tag="t", value=5))
    wire = json_serdes.encode_expression(original)
    assert wire == ["tag", "t", 5]
    decoded = json_serdes.decode_expression(wire)
    assert decoded == IRTaggedExpr(tag="t", value=StaticExpr(5))


####################################################################################################
# Message round-trip
####################################################################################################

MESSAGE_ROUNDTRIP_CASES: list[tuple[str, RPCMessage, JSONTypes]] = [
    ("hello", HelloMessage(), ["fxtr_rpc_hello", 1]),
    (
        "push_simple",
        PushMessage(expression=StaticExpr(42)),
        ["push", 42],
    ),
    (
        "push_with_request",
        PushMessage(
            expression=HandleRequestExpr(
                handle=ImportedIRHandleExpr(import_id=1),
                lease=ImportedLeaseExpr(grant_import_id=2),
                request=Call(method="do", arg=StaticExpr(42)),
                wait_for=(),
            ),
        ),
        [
            "push",
            ["request", ["import_handle", 1], ["import_lease", 2], ["call", "do", 42], []],
        ],
    ),
    (
        "pull_elided_grant_id",
        PullMessage(future_import_id=5, grant_import_id=6),
        ["pull", 5],
    ),
    (
        "pull_explicit_grant_id",
        PullMessage(future_import_id=5, grant_import_id=9),
        ["pull", 5, 9],
    ),
    (
        "resolve",
        ResolveMessage(future_export_id=-3, expression=StaticExpr("ok")),
        ["resolve", -3, "ok"],
    ),
    (
        "reject",
        RejectMessage(
            future_export_id=-3,
            expression=StaticExpr(IRException("no", "cat", None)),
        ),
        ["reject", -3, ["error", "no", "cat", None, None]],
    ),
    ("watch", WatchMessage(token_import_id=7), ["watch", 7]),
    ("closed", ClosedMessage(token_export_id=-2), ["closed", -2]),
    ("break", BreakMessage(grant_export_id=-4, reason="down"), ["break", -4, "down"]),
    ("discard", DiscardMessage(import_id=5, refcount=3), ["discard", 5, 3]),
    ("unpull", UnpullMessage(future_import_id=5, refcount=2), ["unpull", 5, 2]),
    (
        "abort",
        AbortMessage(expression=StaticExpr(IRException("bye", None, None))),
        ["abort", ["error", "bye", None, None, None]],
    ),
]


@pytest.mark.parametrize(
    "name,msg,wire",
    MESSAGE_ROUNDTRIP_CASES,
    ids=[c[0] for c in MESSAGE_ROUNDTRIP_CASES],
)
def test_message_roundtrip(name: str, msg: RPCMessage, wire: JSONTypes) -> None:
    encoded = json_serdes.encode_message(msg)
    assert encoded == wire, f"encode mismatch: {encoded!r} vs {wire!r}"
    decoded = json_serdes.decode_message(encoded)
    assert _ir_equal(decoded, msg), f"decode mismatch: {decoded!r} vs {msg!r}"


def test_pull_without_grant_id_parses_to_elided_default() -> None:
    """``["pull", fid]`` without an explicit grant_import_id defaults to fid + 1."""
    decoded = json_serdes.decode_message(["pull", 10])
    assert isinstance(decoded, PullMessage)
    assert decoded.future_import_id == 10
    assert decoded.grant_import_id == 11


####################################################################################################
# Malformed inputs
####################################################################################################

# Each case: (name, decoder-function-key, wire, expected-error)
# decoder-function-key is one of "message", "expression", "static" and selects which
# decode path to exercise.

MALFORMED_CASES: list[tuple[str, str, Any, type[Exception]]] = [
    # Hello version errors.
    (
        "hello_wrong_version",
        "message",
        ["fxtr_rpc_hello", 2],
        ProtocolVersionError,
    ),
    (
        "hello_version_not_int",
        "message",
        ["fxtr_rpc_hello", "1"],
        MalformedMessageError,
    ),
    (
        "hello_wrong_arity",
        "message",
        ["fxtr_rpc_hello"],
        MalformedMessageError,
    ),
    # Unknown top-level tag.
    ("unknown_message_tag", "message", ["nope", 1], MalformedMessageError),
    # Message isn't a list.
    ("message_not_list", "message", {"tag": "push"}, MalformedMessageError),
    ("message_empty_list", "message", [], MalformedMessageError),
    # Pull with wrong arity.
    ("pull_too_many", "message", ["pull", 1, 2, 3], MalformedMessageError),
    ("pull_no_id", "message", ["pull"], MalformedMessageError),
    # Non-int ID fields.
    ("discard_non_int_id", "message", ["discard", "abc", 1], MalformedMessageError),
    ("discard_bool_id", "message", ["discard", True, 1], MalformedMessageError),
    ("watch_non_int_id", "message", ["watch", "abc"], MalformedMessageError),
    ("break_non_str_reason", "message", ["break", -1, 5], MalformedMessageError),
    # Reject / abort static-only enforcement.
    (
        "reject_with_import",
        "message",
        ["reject", -3, ["import_handle", 1]],
        # The static decoder sees "import_handle" and raises NonStaticInStaticContextError.
        NonStaticInStaticContextError,
    ),
    (
        "reject_static_but_not_exception",
        "message",
        ["reject", -3, 42],
        MalformedMessageError,
    ),
    (
        "abort_with_import",
        "message",
        ["abort", ["import_handle", 1]],
        NonStaticInStaticContextError,
    ),
    (
        "abort_static_but_not_exception",
        "message",
        ["abort", "oops"],
        MalformedMessageError,
    ),
    # Unknown expression tag.
    ("unknown_expr_tag", "expression", ["nope", 1], MalformedExpressionError),
    # Expression arity errors.
    ("await_wrong_arity", "expression", ["await", ["import_future", 1]], MalformedExpressionError),
    ("walk_wrong_arity", "expression", ["walk"], MalformedExpressionError),
    (
        "walk_path_not_list",
        "expression",
        ["walk", ["import_future", 1], "a"],
        MalformedExpressionError,
    ),
    (
        "walk_path_bad_step",
        "expression",
        ["walk", ["import_future", 1], [True]],
        MalformedExpressionError,
    ),
    (
        "request_wrong_arity",
        "expression",
        ["request", ["import_handle", 1], ["import_lease", 2]],
        MalformedExpressionError,
    ),
    (
        "request_bad_form",
        "expression",
        [
            "request",
            ["import_handle", 1],
            ["import_lease", 2],
            ["unknown_form", "x"],
            [],
        ],
        MalformedExpressionError,
    ),
    (
        "request_wait_for_not_list",
        "expression",
        [
            "request",
            ["import_handle", 1],
            ["import_lease", 2],
            ["get", "a"],
            "not_a_list",
        ],
        MalformedExpressionError,
    ),
    (
        "request_wait_for_not_await",
        "expression",
        [
            "request",
            ["import_handle", 1],
            ["import_lease", 2],
            ["get", "a"],
            [42],
        ],
        MalformedExpressionError,
    ),
    # Array-escape arity.
    ("array_escape_extra_elements", "expression", [[1, 2], 3], MalformedExpressionError),
    # Bytes payload is not a string.
    ("bytes_not_str", "expression", ["bytes", 123], MalformedExpressionError),
    ("bytes_invalid_base64", "expression", ["bytes", "!!!not_base64!!!"], MalformedExpressionError),
    # Static decoding with expression-only tag.
    (
        "static_import_ref",
        "static",
        ["import_handle", 1],
        NonStaticInStaticContextError,
    ),
    (
        "static_await",
        "static",
        ["await", 1, 2],
        NonStaticInStaticContextError,
    ),
    ("static_empty_list", "static", [], MalformedStaticError),
    ("static_unknown_tag", "static", ["nope"], MalformedStaticError),
    (
        "static_error_wrong_arity_too_few",
        "static",
        ["error", "m", None],
        MalformedStaticError,
    ),
    (
        "static_error_legacy_4_element",
        "static",
        # Legacy v1 4-element form (no traceback_info slot) is no longer accepted.
        ["error", "m", None, None],
        MalformedStaticError,
    ),
    (
        "static_error_bad_message",
        "static",
        ["error", 5, None, None, None],
        MalformedStaticError,
    ),
    (
        "static_error_bad_category",
        "static",
        ["error", "m", 5, None, None],
        MalformedStaticError,
    ),
    (
        "static_error_traceback_info_not_str_or_null",
        "static",
        ["error", "m", None, None, ["not", "a", "string"]],
        MalformedStaticError,
    ),
    (
        "static_error_traceback_info_int",
        "static",
        ["error", "m", None, None, 42],
        MalformedStaticError,
    ),
    (
        "static_tag_wrong_arity",
        "static",
        ["tag", "x"],
        MalformedStaticError,
    ),
    (
        "static_non_str_dict_key",
        "static",
        {1: "a"},  # Non-string key
        MalformedStaticError,
    ),
]


@pytest.mark.parametrize(
    "name,mode,wire,error_type",
    MALFORMED_CASES,
    ids=[c[0] for c in MALFORMED_CASES],
)
def test_malformed_input(
    name: str,
    mode: str,
    wire: Any,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        json_serdes.decode(wire, mode=mode)  # type: ignore[arg-type]


####################################################################################################
# Unified decode entry-point
####################################################################################################


def test_unified_decode_dispatch() -> None:
    assert json_serdes.decode(["fxtr_rpc_hello", 1], mode="message") == HelloMessage()
    expr = json_serdes.decode(["import_handle", 3], mode="expression")
    assert isinstance(expr, ImportedIRHandleExpr) and expr.import_id == 3
    assert json_serdes.decode([[1, 2, 3]], mode="static") == [1, 2, 3]


def test_unified_decode_rejects_bad_mode() -> None:
    with pytest.raises(ValueError):
        json_serdes.decode(42, mode="bogus")  # type: ignore[arg-type]


####################################################################################################
# Encoder rejects bad inputs
####################################################################################################


def test_encode_reject_message_requires_static_expr() -> None:
    """A RejectMessage whose expression is a non-StaticExpr fails to encode."""
    msg = RejectMessage(
        future_export_id=-3,
        expression=ImportedIRHandleExpr(import_id=1),  # type: ignore[arg-type]
    )
    with pytest.raises(MalformedMessageError):
        json_serdes.encode_message(msg)


def test_encode_abort_message_requires_static_expr() -> None:
    msg = AbortMessage(
        expression=ImportedIRHandleExpr(import_id=1),  # type: ignore[arg-type]
    )
    with pytest.raises(MalformedMessageError):
        json_serdes.encode_message(msg)
