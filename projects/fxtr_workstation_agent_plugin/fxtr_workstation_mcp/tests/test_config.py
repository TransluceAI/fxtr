"""Round-trip tests for ``WorkstationConfig`` reading + writing."""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr_workstation_mcp.config import (
    CONFIG_FILENAME,
    DEFAULT_AGENT_MCP_STORE,
    ConfigError,
    WorkstationConfig,
    read_active_session,
    write_active_session,
)

pytestmark = pytest.mark.anyio


async def test_read_minimal(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILENAME).write_text('workstation_url = "ws://127.0.0.1:1234"\n')
    cfg = WorkstationConfig.from_working_dir(tmp_path)
    assert cfg.workstation_url == "ws://127.0.0.1:1234"
    # active_session is NOT a field on WorkstationConfig — it's
    # read via the free function.
    assert read_active_session(tmp_path) is None
    # Omitting the agent_mcp_store field falls back to the default name.
    assert cfg.agent_mcp_store == DEFAULT_AGENT_MCP_STORE


async def test_read_with_active_session(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILENAME).write_text(
        'workstation_url = "wss://example.test"\n'
        'active_session = "11111111-2222-3333-4444-555555555555"\n'
    )
    cfg = WorkstationConfig.from_working_dir(tmp_path)
    assert cfg.workstation_url == "wss://example.test"
    assert read_active_session(tmp_path) == "11111111-2222-3333-4444-555555555555"


async def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        WorkstationConfig.from_working_dir(tmp_path)


async def test_malformed_toml(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILENAME).write_text("workstation_url = !!!\n")
    with pytest.raises(ConfigError, match="failed to parse"):
        WorkstationConfig.from_working_dir(tmp_path)


async def test_bad_url(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILENAME).write_text('workstation_url = "http://nope"\n')
    with pytest.raises(ConfigError, match="must be a string starting"):
        WorkstationConfig.from_working_dir(tmp_path)


async def test_agent_mcp_store_custom_name(tmp_path: Path) -> None:
    """Explicit string overrides the default cap name."""
    (tmp_path / CONFIG_FILENAME).write_text(
        'workstation_url = "ws://127.0.0.1:1234"\nagent_mcp_store = "my_custom_store"\n'
    )
    cfg = WorkstationConfig.from_working_dir(tmp_path)
    assert cfg.agent_mcp_store == "my_custom_store"


async def test_agent_mcp_store_disabled(tmp_path: Path) -> None:
    """``agent_mcp_store = false`` disables persistence; field is ``None``."""
    (tmp_path / CONFIG_FILENAME).write_text(
        'workstation_url = "ws://127.0.0.1:1234"\nagent_mcp_store = false\n'
    )
    cfg = WorkstationConfig.from_working_dir(tmp_path)
    assert cfg.agent_mcp_store is None


async def test_agent_mcp_store_true_rejected(tmp_path: Path) -> None:
    """``true`` is ambiguous (already covered by field omission) — reject."""
    (tmp_path / CONFIG_FILENAME).write_text(
        'workstation_url = "ws://127.0.0.1:1234"\nagent_mcp_store = true\n'
    )
    with pytest.raises(ConfigError, match="agent_mcp_store"):
        WorkstationConfig.from_working_dir(tmp_path)


async def test_agent_mcp_store_empty_string_rejected(tmp_path: Path) -> None:
    """An empty external name is malformed — reject."""
    (tmp_path / CONFIG_FILENAME).write_text(
        'workstation_url = "ws://127.0.0.1:1234"\nagent_mcp_store = ""\n'
    )
    with pytest.raises(ConfigError, match="agent_mcp_store"):
        WorkstationConfig.from_working_dir(tmp_path)


async def test_agent_mcp_store_int_rejected(tmp_path: Path) -> None:
    """Non-string, non-boolean values are rejected."""
    (tmp_path / CONFIG_FILENAME).write_text(
        'workstation_url = "ws://127.0.0.1:1234"\nagent_mcp_store = 42\n'
    )
    with pytest.raises(ConfigError, match="agent_mcp_store"):
        WorkstationConfig.from_working_dir(tmp_path)


async def test_write_active_session_round_trip(tmp_path: Path) -> None:
    """Setting + clearing ``active_session`` survives a re-read and
    preserves comments/formatting."""
    src = '# top-level comment\nworkstation_url = "ws://127.0.0.1:1234"\n# another comment\n'
    (tmp_path / CONFIG_FILENAME).write_text(src)

    write_active_session(tmp_path, "abc-123")
    assert read_active_session(tmp_path) == "abc-123"

    on_disk = (tmp_path / CONFIG_FILENAME).read_text()
    assert "# top-level comment" in on_disk
    assert "# another comment" in on_disk

    write_active_session(tmp_path, None)
    assert read_active_session(tmp_path) is None
    final_disk = (tmp_path / CONFIG_FILENAME).read_text()
    assert "active_session" not in final_disk
    assert "# top-level comment" in final_disk


async def test_read_active_session_malformed_value(tmp_path: Path) -> None:
    """``active_session`` present but not a string raises a
    ``ConfigError`` (same shape as ``from_working_dir`` raises)."""
    (tmp_path / CONFIG_FILENAME).write_text(
        'workstation_url = "ws://127.0.0.1:1234"\nactive_session = 42\n'
    )
    with pytest.raises(ConfigError, match="active_session"):
        read_active_session(tmp_path)
