"""Pure-stdlib primitive: exec a preamble + a user script with a
caller-supplied set of names exposed via the ``_fxtr_mcp_builtin``
module.

No MCP-specific semantics. The caller (``entry.py``) defines what
``_fxtr_mcp_builtin`` exposes — typically ``declare_value``,
``schedule_message``, and any host-bridged callables. Each entry in
``builtins`` becomes an attribute on a fresh ``types.ModuleType``
installed at ``sys.modules['_fxtr_mcp_builtin']`` for the duration
of one call.

The script is compiled with ``ast.PyCF_ALLOW_TOP_LEVEL_AWAIT``; if
the resulting code object has ``CO_COROUTINE`` set (the body
contains ``await``/``async for``/``async with``) it's awaited as a
coroutine, otherwise plainly exec'd. This keeps line numbers honest
in tracebacks (no textual ``async def`` wrapping).

The preamble runs first as plain top-level Python (no top-level
await); its globals become the script's globals.
"""

from __future__ import annotations

import ast
import inspect
import sys
import types
from collections.abc import Mapping
from typing import Any

PRELUDE_FILENAME = "preamble.py"
SCRIPT_FILENAME = "<mcp_script>"
BUILTIN_MODULE_NAME = "_fxtr_mcp_builtin"


async def run_preamble_and_script(
    preamble_src: str,
    script_src: str,
    *,
    builtins: Mapping[str, Any],
) -> None:
    """Exec ``preamble_src`` then ``script_src`` with ``builtins``
    installed as the ``_fxtr_mcp_builtin`` module.

    Args:
        preamble_src: Source of the preamble file. Filename
            ``"preamble.py"``. No top-level await.
        script_src: Source of the user script. Filename
            ``"<mcp_script>"``. Top-level await allowed.
        builtins: Mapping of names to expose on
            ``sys.modules['_fxtr_mcp_builtin']``. Each value becomes
            an attribute on a fresh ``types.ModuleType``. Values are
            opaque to this module — typically callables (sync or
            async), but anything attribute-importable works.

    Raises:
        Whatever the preamble or script raises.
        ``_fxtr_mcp_builtin`` is removed from ``sys.modules`` even
        on error.
    """
    script_globals: dict[str, Any] = {"__name__": "__mcp_script__"}

    preamble_code = compile(preamble_src, PRELUDE_FILENAME, "exec")
    exec(preamble_code, script_globals)

    builtin_mod = types.ModuleType(BUILTIN_MODULE_NAME)
    for name, value in builtins.items():
        setattr(builtin_mod, name, value)

    sys.modules[BUILTIN_MODULE_NAME] = builtin_mod
    try:
        script_code = compile(
            script_src,
            SCRIPT_FILENAME,
            "exec",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
        if script_code.co_flags & inspect.CO_COROUTINE:
            # CPython: an exec-mode TLA-compiled code object is callable
            # via eval, which returns a coroutine when CO_COROUTINE is set.
            await eval(script_code, script_globals)
        else:
            exec(script_code, script_globals)
    finally:
        sys.modules.pop(BUILTIN_MODULE_NAME, None)
