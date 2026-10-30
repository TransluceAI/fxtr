"""End-to-end test that drives the actual MCP server as a subprocess
and speaks the MCP protocol over stdio.

Validates the full real-world stack:

    Subprocess: `uv run --directory plugin/scripts scheduler-mcp`
        ↓ stdio JSON-RPC
    Initialize handshake (with channel capability)
        ↓
    tools/call set_working_dir → sample_workdir
        ↓
    tools/call run_script → exercise declare_value, schedule_message,
                            host_info, and a `from demo_pkg import ...`
        ↓
    Asserts on the structured tool responses.

What this catches that in-process tests don't:

- The stdio transport / JSON-RPC framing actually works.
- The `scheduler-mcp` console script is wired correctly.
- `uv run --directory plugin/scripts` resolves all path deps as
  expected (fxtr2, fxtr-sandboxing, etc.) so cold-start works.
- The lifespan completes within a reasonable timeout (~30s for the
  factory build).

Slow: subprocess startup + factory build + sandbox spawn. One test
because each subprocess spawn is the expensive part.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.anyio

PLUGIN_SCRIPTS_DIR = Path(__file__).parent.parent.resolve()
SAMPLE_WORKDIR = (PLUGIN_SCRIPTS_DIR / "../../sample_workdir").resolve()


def _content_text(result: object) -> str:
    """Extract the text from a tool result. FastMCP returns
    `[TextContent(text=...)]`; we just grab the first one."""
    contents = result.content  # type: ignore[attr-defined]
    assert contents, f"empty tool result: {result!r}"
    first = contents[0]
    return first.text  # type: ignore[attr-defined]


async def test_mcp_subprocess_full_workflow():
    params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", str(PLUGIN_SCRIPTS_DIR), "scheduler-mcp"],
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # Factory bootstrap inside the server takes ~3s. The
            # initialize handshake + lifespan startup will block until
            # that completes.
            await session.initialize()

            # 1. set_working_dir
            result = await session.call_tool("set_working_dir", {"path": str(SAMPLE_WORKDIR)})
            text = _content_text(result)
            assert "Working dir set" in text
            # The tool reports discovered packages; sample_workdir has
            # demo_pkg.
            assert "demo_pkg" in text

            # 2. run_script — exercise all four builtins / plumbing
            # paths in one shot: preamble globals (ANSWER), local
            # package import via packages/ mount (doubler, GREETING),
            # host_info round-trip, declare_value + schedule_message.
            script = (
                "from _fxtr_mcp_builtin import declare_value, "
                "schedule_message, host_info\n"
                "info = await host_info()\n"
                "declare_value('host', info)\n"
                "declare_value('answer', ANSWER)\n"
                "declare_value('doubled', doubler(ANSWER))\n"
                "declare_value('greeting', GREETING)\n"
                "schedule_message(0.05, 'subprocess-test-msg')\n"
            )
            result = await session.call_tool("run_script", {"script": script})
            text = _content_text(result)

            # Plain-text summary (no JSON envelope) — agent reads directly.
            assert "answer = 42" in text
            assert "doubled = 84" in text
            assert "greeting = 'hello from demo_pkg'" in text
            assert "mcp_plugin_spike" in text  # from host_info()
            assert "Scheduled 1 follow-up message(s)" in text
            assert "subprocess-test-msg" in text

            # 3. Validation surfaces too — bad declare_value should
            # return an error string (not crash the server).
            bad = "from _fxtr_mcp_builtin import declare_value\ndeclare_value('x', object())\n"
            result = await session.call_tool("run_script", {"script": bad})
            text = _content_text(result)
            assert "not JSON-serializable" in text or "Error" in text
