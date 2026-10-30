"""Pure-stdlib primitive: exec a preamble + a user script with a
caller-supplied set of names exposed via the ``_fxtr_mcp_builtin``
module.

No MCP-specific semantics. The caller (``entry.py``) defines what
``_fxtr_mcp_builtin`` exposes. Each entry in ``builtins`` becomes an
attribute on a fresh :class:`types.ModuleType` installed at
``sys.modules['_fxtr_mcp_builtin']`` for the duration of one call.
The builtin module is installed *before* the preamble runs so
preamble authors can ``from _fxtr_mcp_builtin import …`` at the top
level and have those names live in the per-script globals for free.

The script is compiled with ``ast.PyCF_ALLOW_TOP_LEVEL_AWAIT``; if the
resulting code object has ``CO_COROUTINE`` set (the body contains
``await`` / ``async for`` / ``async with``) it's awaited as a
coroutine, otherwise plainly exec'd. This keeps line numbers honest
in tracebacks (no textual ``async def`` wrapping).

The preamble runs first as plain top-level Python (no top-level
await); its globals become the script's globals.

Stdout + stderr are captured for the duration of preamble + script
execution; any user ``Exception`` raised by the preamble or script
is caught and returned as a formatted traceback string in the
result. ``BaseException`` (cancellation, ``SystemExit``,
``KeyboardInterrupt``) still propagates — those signal teardown
events that the surrounding RPC/sandbox runtime needs to see, and
they're rare in normal script flow.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import linecache
import sys
import traceback
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PRELUDE_FILENAME = "preamble.py"
SCRIPT_FILENAME = "<mcp_script>"
BUILTIN_MODULE_NAME = "_fxtr_mcp_builtin"
#: Module name set as ``__name__`` on the script's globals. Classes
#: defined in the script (e.g. via ``@dataclasses.dataclass``) get
#: ``__module__`` set to this string. Python 3.14's
#: :mod:`dataclasses` looks the class's module up in :data:`sys.modules`
#: (``sys.modules.get(cls.__module__).__dict__``) during field-type
#: introspection — so we register a fresh :class:`types.ModuleType`
#: under this name in ``sys.modules`` for the duration of each run.
SCRIPT_MODULE_NAME = "__mcp_script__"


@dataclass
class RunOutcome:
    """Result of one ``run_preamble_and_script`` call.

    On success, ``error is None`` and ``stdout`` / ``stderr`` carry
    whatever the preamble + script wrote. On error, ``error`` is a
    headline-plus-traceback string (the agent's primary surface) and
    ``stdout`` / ``stderr`` still carry anything that was emitted
    before the raise.
    """

    stdout: str
    stderr: str
    error: str | None  # headline + formatted traceback; None on success


async def run_preamble_and_script(
    preamble_src: str,
    script_src: str,
    *,
    builtins: Mapping[str, Any],
) -> RunOutcome:
    """Exec ``preamble_src`` then ``script_src`` with ``builtins``
    installed as the ``_fxtr_mcp_builtin`` module.

    Args:
        preamble_src: Source of the preamble file. Filename
            ``"preamble.py"``. No top-level await.
        script_src: Source of the user script. Filename
            ``"<mcp_script>"``. Top-level await allowed.
        builtins: Mapping of names to expose on
            ``sys.modules['_fxtr_mcp_builtin']``. Each value becomes
            an attribute on a fresh :class:`types.ModuleType`. Values
            are opaque to this module — typically callables (sync or
            async), but anything attribute-importable works. The
            module is installed before the preamble runs so preamble
            imports work.

    Returns:
        A :class:`RunOutcome` carrying captured stdout/stderr and an
        optional formatted-traceback string. A returned error means
        the caller should *not* apply any queued mutations the
        script may have populated — script semantics are atomic.

    Raises:
        ``BaseException`` (cancellation, ``SystemExit``,
        ``KeyboardInterrupt``) — propagates so the surrounding
        sandbox runtime can shut down cleanly.
        ``_fxtr_mcp_builtin`` is removed from ``sys.modules`` on
        every exit path.
    """
    # ``script_mod.__dict__`` doubles as the globals for the preamble +
    # script execs, so any class defined at the script's top level lands
    # as an attribute of ``script_mod`` and gets ``__module__ =
    # SCRIPT_MODULE_NAME``. Registering ``script_mod`` in
    # ``sys.modules`` makes that ``__module__`` resolvable — required
    # by Python 3.14's dataclasses introspection.
    script_mod = types.ModuleType(SCRIPT_MODULE_NAME)
    script_globals: dict[str, Any] = script_mod.__dict__

    builtin_mod = types.ModuleType(BUILTIN_MODULE_NAME)
    for name, value in builtins.items():
        setattr(builtin_mod, name, value)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    error: str | None = None
    # Tracks which phase is currently executing so the error
    # headline can attribute the raise — much more useful for the
    # agent than just the filename in the traceback.
    phase = PRELUDE_FILENAME
    sys.modules[BUILTIN_MODULE_NAME] = builtin_mod
    sys.modules[SCRIPT_MODULE_NAME] = script_mod
    # Populate linecache so ``traceback.format_exception`` can show
    # the actual source lines for our two synthetic filenames
    # (``preamble.py`` / ``<mcp_script>``). Without this, the
    # rendered traceback shows the filename + line number but the
    # source line itself is blank. Format per linecache's contract:
    # ``(size, mtime, lines, fullname)``; ``mtime=None`` disables
    # the freshness check. ``splitlines(keepends=True)`` preserves
    # the original line endings byte-for-byte. Cleared in the
    # ``finally`` below so the next run installs fresh contents
    # (these filenames are reused across calls; the script body
    # changes per call).
    linecache.cache[PRELUDE_FILENAME] = (
        len(preamble_src),
        None,
        preamble_src.splitlines(keepends=True),
        PRELUDE_FILENAME,
    )
    linecache.cache[SCRIPT_FILENAME] = (
        len(script_src),
        None,
        script_src.splitlines(keepends=True),
        SCRIPT_FILENAME,
    )
    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            try:
                preamble_code = compile(preamble_src, PRELUDE_FILENAME, "exec")
                exec(preamble_code, script_globals)

                phase = SCRIPT_FILENAME
                script_code = compile(
                    script_src,
                    SCRIPT_FILENAME,
                    "exec",
                    flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
                )
                if script_code.co_flags & inspect.CO_COROUTINE:
                    await eval(script_code, script_globals)
                else:
                    exec(script_code, script_globals)
            except Exception as exc:
                headline = f"{phase} raised {type(exc).__name__}: {exc}"
                error = headline + "\n\n" + "".join(traceback.format_exception(exc))
    finally:
        sys.modules.pop(BUILTIN_MODULE_NAME, None)
        sys.modules.pop(SCRIPT_MODULE_NAME, None)
        linecache.cache.pop(PRELUDE_FILENAME, None)
        linecache.cache.pop(SCRIPT_FILENAME, None)
    return RunOutcome(
        stdout=stdout_buf.getvalue(),
        stderr=stderr_buf.getvalue(),
        error=error,
    )
