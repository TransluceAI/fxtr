"""Sanity-check the Claude Code plugin manifest files.

These are JSON shapes Claude Code's plugin loader consumes —
``plugin.json``, ``marketplace.json``, and the MCP-spawning
``.mcp.json``. We don't have a JSON Schema to validate against,
but we can at least confirm the files parse and carry the
fields downstream consumers rely on. Catches typos before
the agent tries to ``/plugin install`` and gets a vague
loader error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio

#: Plugin tree lives next to ``fxtr_workstation_mcp/``.
_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "claude_plugin"


def _load(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


async def test_plugin_json_shape() -> None:
    data = _load(_PLUGIN_DIR / ".claude-plugin" / "plugin.json")
    assert data["name"] == "fxtr_workstation"
    assert isinstance(data["version"], str) and data["version"]
    assert isinstance(data["description"], str) and data["description"]
    # Channels are required for ``notifications/claude/channel`` to
    # be delivered — the MCP fires these for invoke results.
    channels = data["channels"]
    assert isinstance(channels, list) and channels, "expected non-empty channels"
    assert any(
        c.get("server") == "workstation_mcp" for c in channels
    ), f"expected channels to declare server=workstation_mcp; got {channels!r}"


async def test_marketplace_json_shape() -> None:
    data = _load(_PLUGIN_DIR / ".claude-plugin" / "marketplace.json")
    assert isinstance(data["name"], str) and data["name"]
    assert isinstance(data.get("owner"), dict) and data["owner"].get("name")
    plugins = data["plugins"]
    assert isinstance(plugins, list) and plugins
    plugin_names = {p.get("name") for p in plugins}
    assert (
        "fxtr_workstation" in plugin_names
    ), f"marketplace should list fxtr_workstation; got {plugin_names!r}"


async def test_mcp_json_shape() -> None:
    data = _load(_PLUGIN_DIR / ".mcp.json")
    servers = data["mcpServers"]
    server = servers["workstation_mcp"]
    # The launcher wraps the uv invocation in ``sh -c …`` so that
    # the MCP's stderr can be redirected into a debug log file (see
    # README.md "Debug log" section). The actual MCP startup
    # command is embedded in the shell argument.
    assert server["command"] == "sh"
    args = server["args"]
    assert args[0] == "-c", args
    shell_script = args[1]
    # Sanity-check the embedded invocation.
    assert "uv run --directory" in shell_script
    assert "${CLAUDE_PLUGIN_ROOT}/../fxtr_workstation_mcp" in shell_script
    assert "fxtr-workstation-mcp" in shell_script
    # Stderr redirect is the whole point of this wrapper.
    assert "fxtr_workstation_mcp_debug.log" in shell_script
    # ``exec`` ensures signals propagate to the python process.
    assert "exec " in shell_script


async def test_skill_md_present() -> None:
    skill = _PLUGIN_DIR / "skills" / "use_fxtr_workstation" / "SKILL.md"
    assert skill.is_file()
    text = skill.read_text()
    # YAML frontmatter sanity-check (Claude Code uses ``name`` /
    # ``description`` to surface the skill).
    assert text.startswith("---")
    assert "name: use_fxtr_workstation" in text
    assert "description:" in text
