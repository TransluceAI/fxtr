"""End-to-end integration test for the MCP server's sandbox path.

Validates the full pipeline before any MCP-tool wiring exists:

    bootstrap factory (prepare → site-packages → SandboxFactory)
        ↓
    open DirectSandboxEnvironment
        ↓
    open_sandbox(entry_point="_fxtr_mcp_runner.entry:run",
                 volumes=mount plugin/scripts at /sandbox-stub)
        ↓
    connect(McpRunnerConfig(preamble, script, host_ref))
        ↓
    runner.run() → {"declared": ..., "scheduled": ...}

If this passes, the MCP-tool layer (chunk 2 of step 3) is just
ergonomic glue — the heavy plumbing all works.

Slow test: factory build ~3 s, sandbox spawn + script exec a few
seconds more. Reuses the existing fxtr_sandboxing wheel cache to
avoid re-downloads.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from _fxtr_mcp_runner.protocols import McpRunnerConfig, McpRunnerProto

pytestmark = pytest.mark.anyio
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr_sandbox_types.protocols import SandboxConnectProto
from fxtr_sandboxing.direct_launcher import create_direct_sandbox_environment
from scheduler_mcp.host_impl import McpHostImpl
from scheduler_mcp.sandbox_factory import bootstrap_factory, build_runner_volumes

# plugin/scripts/tests/test_sandbox_integration.py
PLUGIN_SCRIPTS_DIR = Path(__file__).parent.parent.resolve()
# spikes/mcp_plugin_spike/sandbox_runtime/
SANDBOX_RUNTIME_DIR = (PLUGIN_SCRIPTS_DIR / "../../sandbox_runtime").resolve()
# Reuse the fxtr_sandboxing wheel cache to skip re-downloads.
CACHE_DIR = (
    PLUGIN_SCRIPTS_DIR / "../../../fxtr_sandboxing/tmp_files_gitignore/wheel_cache"
).resolve()


async def test_run_volume_mounted_runner_end_to_end():
    """Factory + direct env + volume-mounted entry point + bridged host ref +
    real script exec. The script just declares one value — proves the entire
    request/response cycle works."""
    async with FxtrScope():
        factory = await bootstrap_factory(
            sandbox_runtime_dir=SANDBOX_RUNTIME_DIR,
            cache_dir=CACHE_DIR,
        )

        async with FxtrScope(name="mcp_lifespan", mountable=True) as host_scope:
            host_impl = McpHostImpl("test-host v0")
            host_ref = host_scope.mount(host_impl)

            volumes, sys_path_additions = build_runner_volumes(
                plugin_scripts_dir=PLUGIN_SCRIPTS_DIR,
            )

            preamble = "ANSWER = 42\n"
            script = textwrap.dedent(
                """
                from _fxtr_mcp_builtin import declare_value, schedule_message
                declare_value("answer", ANSWER)
                schedule_message(1.5, "from-script")
                """
            )
            config = McpRunnerConfig(
                preamble_src=preamble,
                script_src=script,
                host_ref=host_ref,
            )

            async with create_direct_sandbox_environment(factory=factory) as env:
                async with env.open_sandbox(
                    entry_point="_fxtr_mcp_runner.entry:run",
                    volumes=volumes,
                    sys_path_additions=sys_path_additions,
                ) as sandbox_ref:
                    typed = sandbox_ref.replace_service_type(
                        SandboxConnectProto[McpRunnerConfig, McpRunnerProto]
                    )
                    sandbox = await typed.resolve()
                    runner_ref = await sandbox.connect(config)
                    runner = await runner_ref.resolve()
                    result = await runner.run()

            assert result == {
                "declared": {"answer": 42},
                "scheduled": [{"delay_seconds": 1.5, "message": "from-script"}],
            }


async def test_host_info_round_trips_through_user_script():
    """Full RPC round-trip: user script calls ``await host_info()``, which
    is the bridged ``McpHostImpl.host_info`` method on the host. Proves the
    whole bidirectional bridge works — sandbox-side script → entry's
    host-info closure → bridged ServiceRef → host's McpHostImpl → string
    back over the wire → declare_value → returned as wire result."""
    async with FxtrScope():
        factory = await bootstrap_factory(
            sandbox_runtime_dir=SANDBOX_RUNTIME_DIR,
            cache_dir=CACHE_DIR,
        )

        async with FxtrScope(name="mcp_lifespan", mountable=True) as host_scope:
            host_ref = host_scope.mount(McpHostImpl("test-host v0"))
            volumes, sys_path_additions = build_runner_volumes(
                plugin_scripts_dir=PLUGIN_SCRIPTS_DIR,
            )

            script = textwrap.dedent(
                """
                from _fxtr_mcp_builtin import declare_value, host_info
                info = await host_info()
                declare_value("host_says", info)
                """
            )
            config = McpRunnerConfig(
                preamble_src="",
                script_src=script,
                host_ref=host_ref,
            )

            async with create_direct_sandbox_environment(factory=factory) as env:
                async with env.open_sandbox(
                    entry_point="_fxtr_mcp_runner.entry:run",
                    volumes=volumes,
                    sys_path_additions=sys_path_additions,
                ) as sandbox_ref:
                    typed = sandbox_ref.replace_service_type(
                        SandboxConnectProto[McpRunnerConfig, McpRunnerProto]
                    )
                    sandbox = await typed.resolve()
                    runner_ref = await sandbox.connect(config)
                    runner = await runner_ref.resolve()
                    result = await runner.run()

            assert result == {
                "declared": {"host_says": "test-host v0"},
                "scheduled": [],
            }


async def test_declare_value_validation_surfaces_to_host():
    """A script that violates declare_value's JSON gate should raise
    out of runner.run() on the host side — proves entry.py's validation
    propagates across the RPC boundary instead of being silently swallowed."""
    async with FxtrScope():
        factory = await bootstrap_factory(
            sandbox_runtime_dir=SANDBOX_RUNTIME_DIR,
            cache_dir=CACHE_DIR,
        )

        async with FxtrScope(name="mcp_lifespan", mountable=True) as host_scope:
            host_ref = host_scope.mount(McpHostImpl("ignored"))
            volumes, sys_path_additions = build_runner_volumes(
                plugin_scripts_dir=PLUGIN_SCRIPTS_DIR,
            )

            script = textwrap.dedent(
                """
                from _fxtr_mcp_builtin import declare_value
                declare_value("bad", object())
                """
            )
            config = McpRunnerConfig(
                preamble_src="",
                script_src=script,
                host_ref=host_ref,
            )

            async with create_direct_sandbox_environment(factory=factory) as env:
                async with env.open_sandbox(
                    entry_point="_fxtr_mcp_runner.entry:run",
                    volumes=volumes,
                    sys_path_additions=sys_path_additions,
                ) as sandbox_ref:
                    typed = sandbox_ref.replace_service_type(
                        SandboxConnectProto[McpRunnerConfig, McpRunnerProto]
                    )
                    sandbox = await typed.resolve()
                    runner_ref = await sandbox.connect(config)
                    runner = await runner_ref.resolve()
                    with pytest.raises(Exception, match="not JSON-serializable"):
                        await runner.run()
