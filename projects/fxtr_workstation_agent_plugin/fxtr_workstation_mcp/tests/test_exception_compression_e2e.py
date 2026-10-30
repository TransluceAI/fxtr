"""End-to-end tests for the compressed-traceback + retrieval flow
(fix 4 of stage3_mcp_backend_tweaks).

When ``run_script`` raises:

1. The host-side ``_format_script_error`` compresses the
   traceback to a two-frame summary (``[user]`` deepest
   script/preamble frame + ``[raise]`` innermost frame) and
   stashes the full :func:`traceback.format_exception` text in
   ``AppContext.exception_log``.
2. The tool result includes the ``exc_id`` so the agent can
   fetch the full form via the sandbox builtin
   ``get_full_exception(exc_id)`` from a subsequent
   ``run_script``.

The unit-level shape of compression + LRU is covered in
:mod:`tests.test_exception_log`. This file walks the full path:
a real script raises, the agent retrieves the full text via the
builtin, and a third script verifies the LRU evicts old entries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fxtr_workstation import WorkstationServer

from fxtr_workstation_mcp.app_context import AppContext
from fxtr_workstation_mcp.config import CONFIG_FILENAME
from fxtr_workstation_mcp.exception_log import ExceptionLog
from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import (
    close_session,
    connect,
    make_app_ctx,
    open_session,
    run_script,
    set_working_dir,
)

pytestmark = pytest.mark.anyio


class _FakeRequestContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.lifespan_context = app_ctx


class _FakeContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.request_context = _FakeRequestContext(app_ctx)
        self.session = None


async def _swd(ctx: _FakeContext, p: str) -> str:
    return await set_working_dir(p, ctx)  # type: ignore[arg-type]


async def _conn(ctx: _FakeContext) -> str:
    return await connect(ctx)  # type: ignore[arg-type]


async def _open(ctx: _FakeContext, md: dict[str, Any] | None = None) -> str:
    return await open_session(ctx, md)  # type: ignore[arg-type]


async def _close(ctx: _FakeContext) -> str:
    return await close_session(ctx)  # type: ignore[arg-type]


async def _run(ctx: _FakeContext, src: str) -> str:
    return await run_script(src, ctx)  # type: ignore[arg-type]


@pytest.mark.slow
async def test_compressed_traceback_includes_exc_id_and_retrieval_works(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """First script raises → tool result has compressed shape +
    exc_id. Second script calls ``get_full_exception(exc_id)`` →
    receives the full traceback, which is longer than the
    compressed form and contains the full ``File "..."`` block."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# fix4 compression e2e\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"phase": "fix4-compression"})

            # Script that raises through library code so the
            # compressed form has distinct ``[user]`` (<mcp_script>)
            # and ``[raise]`` (json internals) frames.
            raising_script = "import json\n" "json.loads('not valid json')\n"
            result = await _run(ctx, raising_script)

            # Compressed form has both anchors; the rollback line
            # carries exc_id='exc_1'.
            assert "[user] <mcp_script>" in result, result
            assert "[raise] " in result, result
            assert "[raise] <mcp_script>" not in result, (
                "raise frame should be the json internal frame, not the script: " f"got: {result}"
            )
            assert "JSONDecodeError" in result or "ValueError" in result, result
            assert "exc_id='exc_1'" in result, result
            assert "get_full_exception('exc_1')" in result, result
            # The compressed traceback inside the fence is short —
            # roughly headline + two frame lines, not the full
            # multi-line traceback.
            compressed_block = result.split("--- traceback ---", 1)[1].split(
                "--- end traceback ---", 1
            )[0]
            assert compressed_block.count("[user]") == 1, compressed_block
            assert compressed_block.count("[raise]") == 1, compressed_block

            # Pull the full traceback in a follow-up script.
            retrieve_script = (
                "from _fxtr_mcp_builtin import get_full_exception\n"
                "full = await get_full_exception('exc_1')\n"
                "print(full)\n"
            )
            r2 = await _run(ctx, retrieve_script)
            assert "Script ran" in r2, r2
            # stdout block captures the full traceback the agent
            # printed. Full traceback includes the standard header
            # + at least the <mcp_script> frame + the json frame.
            assert "Traceback (most recent call last):" in r2, r2
            assert "<mcp_script>" in r2, r2
            assert "json" in r2, r2  # library frame appears in full

            # Unknown id → LookupError surfaced as a script-level
            # raise (which the host then compresses).
            missing_script = (
                "from _fxtr_mcp_builtin import get_full_exception\n"
                "await get_full_exception('exc_9999')\n"
            )
            r3 = await _run(ctx, missing_script)
            assert "Error: script raised" in r3, r3
            # The headline mentions LookupError (per the bridge's
            # category re-raise).
            assert "LookupError" in r3, r3

            assert (await _close(ctx)).startswith("closed session ")


@pytest.mark.slow
async def test_lru_evicts_old_exc_ids(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Configure a tiny LRU on the AppContext so we can exercise
    eviction without running 21 scripts. The first exc_id is
    evicted by the second; ``get_full_exception('exc_1')`` raises
    LookupError."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# fix4 LRU eviction e2e\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            # Replace the default ExceptionLog with a single-entry one
            # so we can trigger eviction in two scripts.
            app_ctx.exception_log = ExceptionLog(max_entries=1)

            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"phase": "fix4-lru"})

            r1 = await _run(ctx, "raise RuntimeError('first')\n")
            assert "exc_id='exc_1'" in r1, r1

            r2 = await _run(ctx, "raise RuntimeError('second')\n")
            assert "exc_id='exc_2'" in r2, r2

            # exc_1 has been evicted.
            assert app_ctx.exception_log.get("exc_1") is None
            assert app_ctx.exception_log.get("exc_2") is not None

            # Agent's get_full_exception('exc_1') hits a clean
            # LookupError.
            check_script = (
                "from _fxtr_mcp_builtin import get_full_exception\n"
                "await get_full_exception('exc_1')\n"
            )
            r3 = await _run(ctx, check_script)
            assert "LookupError" in r3, r3

            assert (await _close(ctx)).startswith("closed session ")
