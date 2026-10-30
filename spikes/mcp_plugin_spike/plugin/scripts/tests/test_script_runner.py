"""Unit tests for :mod:`_fxtr_mcp_runner.script_runner`.

Tests the bare primitive — caller supplies the builtins, script_runner
just installs them as ``_fxtr_mcp_builtin``, exec's preamble + script
(with TLA), and cleans up.

MCP-specific behaviors (declare_value/schedule_message validation,
host_info bridging) are entry.py's concern and are covered by
:mod:`tests.test_sandbox_integration`.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from _fxtr_mcp_runner.script_runner import BUILTIN_MODULE_NAME, run_preamble_and_script

pytestmark = pytest.mark.anyio


async def test_caller_supplied_builtins_visible_to_script():
    captured: list[tuple[str, int]] = []

    def record(key: str, value: int) -> None:
        captured.append((key, value))

    await run_preamble_and_script(
        preamble_src="",
        script_src=textwrap.dedent(
            """
            from _fxtr_mcp_builtin import record
            record("a", 1)
            record("b", 2)
            """
        ),
        builtins={"record": record},
    )
    assert captured == [("a", 1), ("b", 2)]


async def test_preamble_globals_visible_in_script():
    captured: list[int] = []
    await run_preamble_and_script(
        preamble_src="X = 7\n",
        script_src=textwrap.dedent(
            """
            from _fxtr_mcp_builtin import push
            push(X)
            """
        ),
        builtins={"push": captured.append},
    )
    assert captured == [7]


async def test_top_level_await_in_script():
    seen: list[bool] = []

    async def mark_async(value: bool) -> None:
        seen.append(value)

    preamble = "import asyncio\n"
    script = textwrap.dedent(
        """
        from _fxtr_mcp_builtin import mark_async
        await asyncio.sleep(0)
        await mark_async(True)
        """
    )
    await run_preamble_and_script(preamble, script, builtins={"mark_async": mark_async})
    assert seen == [True]


async def test_no_top_level_await_runs_via_plain_exec():
    """Sanity: the no-await branch is taken when CO_COROUTINE isn't set."""
    captured: list[int] = []
    await run_preamble_and_script(
        preamble_src="",
        script_src=textwrap.dedent(
            """
            from _fxtr_mcp_builtin import push
            for v in (1, 2, 3):
                push(v)
            """
        ),
        builtins={"push": captured.append},
    )
    assert captured == [1, 2, 3]


async def test_builtin_module_removed_after_run():
    assert BUILTIN_MODULE_NAME not in sys.modules
    await run_preamble_and_script(
        preamble_src="",
        script_src="from _fxtr_mcp_builtin import noop\nnoop()\n",
        builtins={"noop": lambda: None},
    )
    assert BUILTIN_MODULE_NAME not in sys.modules


async def test_builtin_module_removed_even_on_error():
    assert BUILTIN_MODULE_NAME not in sys.modules
    with pytest.raises(RuntimeError, match="boom"):
        await run_preamble_and_script(
            preamble_src="",
            script_src="raise RuntimeError('boom')\n",
            builtins={},
        )
    assert BUILTIN_MODULE_NAME not in sys.modules


async def test_fresh_builtin_module_per_call():
    """Two back-to-back runs each get their own _fxtr_mcp_builtin module
    populated from the call's `builtins` mapping — no leak across calls."""
    seen_first: list[str] = []
    await run_preamble_and_script(
        preamble_src="",
        script_src=textwrap.dedent(
            """
            from _fxtr_mcp_builtin import push
            push("first")
            """
        ),
        builtins={"push": seen_first.append},
    )

    seen_second: list[str] = []
    # Different name, different callable — second run must see this and
    # NOT see `push` from the first run.
    await run_preamble_and_script(
        preamble_src="",
        script_src=textwrap.dedent(
            """
            try:
                from _fxtr_mcp_builtin import push
            except ImportError:
                pass
            else:
                raise AssertionError("first run's push leaked")
            from _fxtr_mcp_builtin import other
            other("second")
            """
        ),
        builtins={"other": seen_second.append},
    )
    assert seen_first == ["first"]
    assert seen_second == ["second"]


async def test_traceback_filenames_match_sources():
    """Tracebacks point at <mcp_script>, not synthetic wrappers — confirms
    PyCF_ALLOW_TOP_LEVEL_AWAIT keeps line numbers honest."""
    with pytest.raises(ZeroDivisionError) as exc_info:
        await run_preamble_and_script(
            preamble_src="",
            script_src="x = 1 / 0\n",
            builtins={},
        )
    tb = exc_info.tb
    filenames = []
    while tb is not None:
        filenames.append(tb.tb_frame.f_code.co_filename)
        tb = tb.tb_next
    assert "<mcp_script>" in filenames
