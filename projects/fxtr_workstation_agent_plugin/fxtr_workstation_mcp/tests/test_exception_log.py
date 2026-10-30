"""Unit tests for :class:`ExceptionLog` and the host-side
traceback-compression helper (fix 4 of
stage3_mcp_backend_tweaks).

End-to-end behavior (script raises → compressed shape in tool
result → ``get_full_exception(exc_id)`` returns the full text)
is covered in :mod:`tests.test_exception_compression_e2e`.
"""

from __future__ import annotations

import pytest

from fxtr_workstation_mcp.exception_log import DEFAULT_MAX_ENTRIES, ExceptionLog
from fxtr_workstation_mcp.ws_connection import (
    _compress_script_traceback,
    _format_script_error,
)

pytestmark = pytest.mark.anyio


# --- ExceptionLog (in-memory LRU) -----------------------------------


async def test_store_returns_monotonic_ids() -> None:
    log = ExceptionLog()
    a = log.store("aaa")
    b = log.store("bbb")
    c = log.store("ccc")
    assert a == "exc_1"
    assert b == "exc_2"
    assert c == "exc_3"
    assert log.get(a) == "aaa"
    assert log.get(b) == "bbb"
    assert log.get(c) == "ccc"


async def test_get_returns_none_for_unknown_id() -> None:
    log = ExceptionLog()
    assert log.get("exc_1") is None
    assert log.get("nope") is None


async def test_lru_evicts_oldest_on_overflow() -> None:
    log = ExceptionLog(max_entries=3)
    ids = [log.store(str(i)) for i in range(5)]
    # ids 1 + 2 evicted; 3, 4, 5 retained.
    assert log.get(ids[0]) is None
    assert log.get(ids[1]) is None
    assert log.get(ids[2]) == "2"
    assert log.get(ids[3]) == "3"
    assert log.get(ids[4]) == "4"
    assert len(log) == 3


async def test_ids_never_recycle_across_eviction() -> None:
    log = ExceptionLog(max_entries=2)
    a = log.store("a")
    log.store("b")
    c = log.store("c")  # evicts a
    assert log.get(a) is None
    assert c == "exc_3"
    # Storing again advances the counter past 4 even though only 2
    # entries are retained — no reuse of the evicted id.
    d = log.store("d")
    assert d == "exc_4"


async def test_default_capacity_constant() -> None:
    assert DEFAULT_MAX_ENTRIES == 20
    assert ExceptionLog()._max_entries == DEFAULT_MAX_ENTRIES  # type: ignore[attr-defined]


async def test_invalid_capacity_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        ExceptionLog(max_entries=0)


# --- _compress_script_traceback --------------------------------------
#
# These tests build a synthetic ``error`` string in the same shape
# script_runner.py produces, then assert on the compressed form.


async def test_compress_single_user_frame_collapses() -> None:
    """Script raises directly: user frame and raise frame are the
    same; only one ``[user]`` line is emitted."""
    full = (
        "<mcp_script> raised ValueError: boom\n\n"
        "Traceback (most recent call last):\n"
        '  File "<mcp_script>", line 1, in <module>\n'
        '    raise ValueError("boom")\n'
        "ValueError: boom\n"
    )
    compressed = _compress_script_traceback(full)
    lines = compressed.split("\n")
    assert lines[0] == "<mcp_script> raised ValueError: boom"
    # Exactly one frame line; no [raise] (same frame).
    assert lines[1] == '  [user] <mcp_script>:1 in <module>: raise ValueError("boom")'
    assert len(lines) == 2
    # The trailing ValueError: boom line from format_exception is
    # dropped (the headline already says it).


async def test_compress_two_distinct_frames() -> None:
    """Raise frame is in library code, user frame is shallower:
    both lines are emitted."""
    full = (
        "<mcp_script> raised KeyError: 'missing'\n\n"
        "Traceback (most recent call last):\n"
        '  File "<mcp_script>", line 3, in <module>\n'
        "    d[key]\n"
        '  File "/path/to/lib/helper.py", line 42, in __getitem__\n'
        "    raise KeyError(key)\n"
        "KeyError: 'missing'\n"
    )
    compressed = _compress_script_traceback(full)
    lines = compressed.split("\n")
    assert lines[0] == "<mcp_script> raised KeyError: 'missing'"
    assert lines[1] == "  [user] <mcp_script>:3 in <module>: d[key]"
    assert lines[2] == "  [raise] /path/to/lib/helper.py:42 in __getitem__: raise KeyError(key)"
    assert len(lines) == 3


async def test_compress_preamble_frame_counts_as_user() -> None:
    """``preamble.py`` frames also qualify as user code for the
    ``[user]`` tag."""
    full = (
        "preamble.py raised RuntimeError: bad preamble\n\n"
        "Traceback (most recent call last):\n"
        '  File "preamble.py", line 2, in <module>\n'
        "    bad_call()\n"
        '  File "preamble.py", line 5, in bad_call\n'
        '    raise RuntimeError("bad preamble")\n'
        "RuntimeError: bad preamble\n"
    )
    compressed = _compress_script_traceback(full)
    lines = compressed.split("\n")
    assert lines[0] == "preamble.py raised RuntimeError: bad preamble"
    # User frame = deepest preamble.py frame (the raise); raise
    # frame = same. Collapse to a single line.
    assert lines[1] == '  [user] preamble.py:5 in bad_call: raise RuntimeError("bad preamble")'
    assert len(lines) == 2


async def test_compress_no_user_frame_emits_only_raise() -> None:
    """Defensive: traceback whose every frame is library code
    (shouldn't happen in practice, since script_runner always
    runs user code) renders just the ``[raise]`` line."""
    full = (
        "<mcp_script> raised OSError: no\n\n"
        "Traceback (most recent call last):\n"
        '  File "/path/lib_a.py", line 10, in outer\n'
        "    inner()\n"
        '  File "/path/lib_b.py", line 20, in inner\n'
        '    raise OSError("no")\n'
        "OSError: no\n"
    )
    compressed = _compress_script_traceback(full)
    lines = compressed.split("\n")
    assert lines[0] == "<mcp_script> raised OSError: no"
    assert lines[1] == '  [raise] /path/lib_b.py:20 in inner: raise OSError("no")'
    assert len(lines) == 2


async def test_compress_empty_input_yields_empty() -> None:
    assert _compress_script_traceback("") == ""


async def test_compress_no_frames_returns_headline_only() -> None:
    """Headline with no traceback section: just return the headline."""
    assert _compress_script_traceback("oops only headline\n") == "oops only headline"


# --- _format_script_error (compressed traceback + exc_id) ------------


async def test_format_script_error_with_log_emits_exc_id() -> None:
    """When an ExceptionLog is supplied, the full traceback is
    stored and the rollback message includes the exc_id."""
    log = ExceptionLog()
    full = (
        "<mcp_script> raised ValueError: hi\n\n"
        "Traceback (most recent call last):\n"
        '  File "<mcp_script>", line 1, in <module>\n'
        '    raise ValueError("hi")\n'
        "ValueError: hi\n"
    )
    out = _format_script_error(full, stdout="", stderr="", exception_log=log)
    # Compressed traceback inside the fence.
    assert "--- traceback ---" in out
    assert "[user] <mcp_script>:1 in <module>" in out
    assert "--- end traceback ---" in out
    # The exc_id is referenced for retrieval.
    assert "exc_id='exc_1'" in out, out
    assert "get_full_exception('exc_1')" in out, out
    # And the full text is in the log under that id.
    assert log.get("exc_1") == full


async def test_format_script_error_without_log_signals_unavailable() -> None:
    """When no log is configured, the rollback message says so
    instead of pointing at a missing id."""
    full = "<mcp_script> raised ValueError: hi\n\n"
    out = _format_script_error(full, stdout="", stderr="", exception_log=None)
    assert "no exception log configured" in out, out
    assert "exc_id=" not in out, out
