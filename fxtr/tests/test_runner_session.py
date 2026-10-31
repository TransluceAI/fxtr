"""Tests for the worker session vocabulary: error descriptions."""

from __future__ import annotations

from fxtr.runner.session import AttemptError


def test_attempt_error_from_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError as exc:
        error = AttemptError.from_exception(exc)
    assert error.type_name == "ValueError"
    assert error.message == "boom"
    assert "ValueError: boom" in error.traceback
