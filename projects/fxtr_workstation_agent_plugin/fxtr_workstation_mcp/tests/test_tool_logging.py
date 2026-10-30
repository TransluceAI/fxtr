"""Unit tests for the ``@logged_tool`` decorator.

Covers stage7post2 Part 2: when verbose-IO is on, every tool call
emits a ``=== [name] call ===`` fenced block on entry, a
``=== [name] result ===`` block on success, and a
``=== [name] raised ===`` block on ``BaseException`` (re-raised).
When verbose-IO is off, nothing is written.

Unit-style (no workstation): patches ``server._VERBOSE_IO`` and
calls the decorator directly on toy async functions, capturing
stderr via ``capsys``.
"""

from __future__ import annotations

from typing import Any

import pytest

from fxtr_workstation_mcp import server

pytestmark = pytest.mark.anyio


def _toggle_verbose(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    """Force ``server._VERBOSE_IO`` for the duration of one test."""
    monkeypatch.setattr(server, "_VERBOSE_IO", on)


async def test_logged_tool_logs_call_and_result_when_verbose(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verbose on: entry + result blocks land in stderr; ``ctx``
    is omitted from the logged inputs even though it's a parameter."""
    _toggle_verbose(monkeypatch, True)

    @server.logged_tool("toy_tool")
    async def toy(path: str, ctx: Any) -> str:
        return f"ok:{path}"

    result = await toy("hello", ctx=object())
    assert result == "ok:hello"
    err = capsys.readouterr().err
    assert "=== [toy_tool] call ===" in err
    assert "path='hello'" in err
    assert "ctx=" not in err  # ctx is filtered out of the call log
    assert "=== [toy_tool] result ===" in err
    assert "ok:hello" in err


async def test_logged_tool_logs_base_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verbose on: when the wrapped tool raises (any
    ``BaseException``), the decorator emits a
    ``[name] raised`` block with the type name + repr, then
    re-raises. The result block is NOT emitted."""
    _toggle_verbose(monkeypatch, True)

    class Boom(BaseException):
        pass

    @server.logged_tool("toy_raise")
    async def toy(ctx: Any) -> str:
        raise Boom("nope")

    with pytest.raises(Boom):
        await toy(ctx=object())

    err = capsys.readouterr().err
    assert "=== [toy_raise] call ===" in err
    assert "=== [toy_raise] raised ===" in err
    assert "Boom" in err
    assert "nope" in err
    assert "=== [toy_raise] result ===" not in err


async def test_logged_tool_no_overhead_when_verbose_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verbose off: zero output even though the decorator wraps
    the call. Pairs with the perf early-out: the wrapper skips
    the sig-bind entirely on this path."""
    _toggle_verbose(monkeypatch, False)

    @server.logged_tool("toy_silent")
    async def toy(path: str, ctx: Any) -> str:
        return f"ok:{path}"

    result = await toy("hello", ctx=object())
    assert result == "ok:hello"
    err = capsys.readouterr().err
    assert "[toy_silent]" not in err
    assert "===" not in err


async def test_logged_tool_preserves_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke check: ``inspect.signature`` on a decorated function
    returns the wrapped function's signature (so FastMCP's tool
    schema generation works against the real parameters, not the
    decorator's ``*args, **kwargs``).

    Verbose flag is irrelevant here — the assertion is on the
    decoration result, not the call behavior.
    """
    import inspect

    _toggle_verbose(monkeypatch, False)

    @server.logged_tool("toy_sig")
    async def toy(path: str, ctx: Any, count: int = 3) -> str:
        return f"{path}:{count}"

    sig = inspect.signature(toy)
    assert list(sig.parameters) == ["path", "ctx", "count"]
    assert sig.parameters["count"].default == 3
