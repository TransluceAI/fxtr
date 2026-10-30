"""Tests for Pending."""

import pytest

from fxtr_workflow.pending import Pending


async def test_set_result_and_await():
    p: Pending[int] = Pending()
    p.set_result(42)
    assert await p == 42


async def test_set_exception_and_await():
    p: Pending[int] = Pending()
    p.set_exception(ValueError("boom"))
    with pytest.raises(ValueError, match="boom"):
        await p


async def test_double_set_raises():
    p: Pending[int] = Pending()
    p.set_result(1)
    with pytest.raises(RuntimeError, match="already resolved"):
        p.set_result(2)


async def test_double_set_exception_raises():
    p: Pending[int] = Pending()
    p.set_exception(ValueError("a"))
    with pytest.raises(RuntimeError, match="already resolved"):
        p.set_exception(ValueError("b"))


async def test_is_resolved():
    p: Pending[str] = Pending()
    assert not p.is_resolved
    p.set_result("done")
    assert p.is_resolved
