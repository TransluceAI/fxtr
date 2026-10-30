"""Tests for IRException.traceback_info propagation across wrap, copy, and
encode steps.

Stream 1 of substage 7 (see plans/phase3/stage8c_user_caps/part7_plan.md):
the field default + str format, ``wrap_exception``, ``copy_ir_value``
preservation. JSON-codec round-trip is tested separately in
``tests/test_rpc/test_json_serdes.py``.
"""

from __future__ import annotations

from typing import Any

from fxtr2.leases.leased_cap_common import copy_ir_value, wrap_exception
from fxtr2.leases.leased_cap_ir import IRException


class TestField:
    def test_default_is_none(self) -> None:
        exc = IRException("oops")
        assert exc.traceback_info is None

    def test_explicit_traceback_info_is_stored(self) -> None:
        exc = IRException("oops", traceback_info="abc\nxyz")
        assert exc.traceback_info == "abc\nxyz"


class TestStr:
    def test_no_category_no_traceback(self) -> None:
        exc = IRException("oops")
        assert str(exc) == "oops"

    def test_category_no_traceback(self) -> None:
        exc = IRException("oops", category="MyError")
        assert str(exc) == "[MyError] oops"

    def test_no_category_with_traceback(self) -> None:
        exc = IRException("oops", traceback_info="trace1\nline2")
        assert str(exc) == "oops\ntrace1\nline2"

    def test_category_and_traceback_info(self) -> None:
        exc = IRException("oops", category="MyError", traceback_info="trace content")
        assert str(exc) == "[MyError] oops\ntrace content"


class TestWrapException:
    def test_plain_exception_captures_traceback(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError as exc:
            wrapped = wrap_exception(exc)
        assert isinstance(wrapped, IRException)
        assert wrapped.message == "boom"
        assert wrapped.category == "ValueError"
        assert wrapped.traceback_info is not None
        # The captured traceback should mention this file + the line where we raised.
        assert "ValueError" in wrapped.traceback_info
        assert "boom" in wrapped.traceback_info
        assert __file__ in wrapped.traceback_info

    def test_plain_exception_with_explicit_category(self) -> None:
        try:
            raise RuntimeError("rt")
        except RuntimeError as exc:
            wrapped = wrap_exception(exc, category="CustomCategory")
        assert wrapped.category == "CustomCategory"
        assert wrapped.message == "rt"

    def test_irexception_passthrough_is_identity(self) -> None:
        original = IRException("oops", category="MyError", traceback_info="existing-content")
        result = wrap_exception(original)
        # Identity, not a copy: wrap_exception leaves IRException untouched.
        assert result is original
        assert result.traceback_info == "existing-content"

    def test_no_python_traceback_attached(self) -> None:
        # Python exception that was constructed but never raised: __traceback__ is None.
        # wrap_exception still produces an IRException; traceback_info is the
        # format_exception output, which is typically just "ValueError: boom\n".
        exc = ValueError("boom")
        wrapped = wrap_exception(exc)
        assert isinstance(wrapped, IRException)
        assert wrapped.message == "boom"
        assert wrapped.category == "ValueError"
        assert wrapped.traceback_info is not None
        # No file/line frames since __traceback__ is None.


class TestCopyIRValuePreservesTracebackInfo:
    def test_clone_preserves(self) -> None:
        exc: IRException[Any] = IRException("oops", category="MyError", traceback_info="a\nb")
        copied = copy_ir_value(exc, take=False)
        assert isinstance(copied, IRException)
        assert copied.traceback_info == "a\nb"
        # Fresh object, same field value.
        assert copied is not exc

    def test_take_preserves(self) -> None:
        exc: IRException[Any] = IRException("oops", traceback_info="x")
        copied = copy_ir_value(exc, take=True)
        assert isinstance(copied, IRException)
        assert copied.traceback_info == "x"

    def test_default_none_preserved(self) -> None:
        exc: IRException[Any] = IRException("oops")
        copied = copy_ir_value(exc, take=False)
        assert isinstance(copied, IRException)
        assert copied.traceback_info is None
