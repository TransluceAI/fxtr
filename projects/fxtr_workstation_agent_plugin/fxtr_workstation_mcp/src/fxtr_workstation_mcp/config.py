"""``.fxtr_workstation.toml`` reader/writer.

Schema (v1):

    workstation_url  = "ws://127.0.0.1:8123"     # required
    active_session   = "<uuid>"                   # optional
    agent_mcp_store  = "agent_mcp_store"          # optional, default
                                                  # shown; ``false`` disables

Reads use stdlib ``tomllib`` for parsing. Writes use ``tomlkit`` so we
preserve formatting + comments around the parts we don't touch.

**``active_session`` is intentionally not a field on
:class:`WorkstationConfig`.** It changes throughout a session's
lifecycle (open / close / external edits) and the
:class:`~fxtr_workstation_mcp.ws_connection.WorkstationConnection`
re-reads it from disk at the top of every cycle. Caching it as a
mutable dataclass field would introduce drift between the cached
copy and the runtime view (``connection.active_session_id``). Use
:func:`read_active_session` / :func:`write_active_session` to
read or update it explicitly.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import tomlkit

CONFIG_FILENAME = ".fxtr_workstation.toml"

#: Default ``external_name`` of the workstation persistence cap the
#: agent MCP looks up on session subscribe to persist its typed
#: event log. The user can override the name (or disable persistence
#: entirely) via the ``agent_mcp_store`` toml field — see
#: :class:`WorkstationConfig`.
DEFAULT_AGENT_MCP_STORE = "agent_mcp_store"


class ConfigError(ValueError):
    """Raised when the toml is missing, malformed, or fails validation."""


def _parse_agent_mcp_store(data: dict[str, Any], path: Path) -> str | None:
    """Parse the ``agent_mcp_store`` toml field.

    - Field absent → return :data:`DEFAULT_AGENT_MCP_STORE`.
    - ``false`` → return ``None`` (disable persistence).
    - Non-empty string → return that string.
    - Anything else → raise :class:`ConfigError`.

    ``true`` is rejected explicitly: ``true`` would mean "use the
    default name" but that's also what omitting the field means;
    silently accepting both forms would create two ways to say the
    same thing. Pick one: leave the field out for the default.
    """
    if "agent_mcp_store" not in data:
        return DEFAULT_AGENT_MCP_STORE
    raw = data["agent_mcp_store"]
    if raw is False:
        return None
    if isinstance(raw, str) and raw:
        return raw
    raise ConfigError(
        f"{path}: ``agent_mcp_store`` must be a non-empty string "
        f"(external_name of the persistence cap) or ``false`` to disable; "
        f"got {raw!r}."
    )


@dataclass(frozen=True)
class WorkstationConfig:
    """Parsed contents of the working dir's ``.fxtr_workstation.toml``,
    minus ``active_session`` (read/written via
    :func:`read_active_session` / :func:`write_active_session`).

    Carries only fields that are effectively immutable for the
    duration of an MCP process (URLs + persistence cap name).
    Frozen so callers can't accidentally rely on mutating it.
    """

    path: Path
    """Absolute path to the toml file (so we know where to write back)."""

    workstation_url: str
    """``ws://host:port`` base URL of the workstation server. Required."""

    agent_mcp_store: str | None = DEFAULT_AGENT_MCP_STORE
    """External name of the workstation persistence cap the agent MCP
    uses to persist its typed event log on every chain extension.

    Three values:

    - **Field absent / omitted from toml**: defaults to
      :data:`DEFAULT_AGENT_MCP_STORE` (``"agent_mcp_store"``). The
      MCP looks up a service with this name on the workstation at
      session-subscribe time; if no such service is registered, the
      supervisor pauses with a clear error.
    - **Explicit string**: same lookup behavior, just under a
      different external name.
    - **Explicit ``false``** (deserialized to ``None`` here): skip
      persistence entirely. The event log lives in memory only and
      is lost on MCP restart / reconnect.

    Read-only from the agent's perspective — the MCP doesn't write
    this back; it's an operator-controlled setting parallel to
    ``workstation_url``.
    """

    @classmethod
    def from_working_dir(cls, working_dir: Path) -> Self:
        """Read ``working_dir / .fxtr_workstation.toml`` and parse it.

        Validates ``active_session`` shape (if present) but does not
        store it — use :func:`read_active_session` for the live
        value.

        Raises:
            ConfigError: if the file is missing or malformed.
        """
        data, path = _parse_toml(working_dir)

        url = data.get("workstation_url")
        if not isinstance(url, str) or not url.startswith(("ws://", "wss://")):
            raise ConfigError(
                f"{path}: ``workstation_url`` must be a string starting with "
                f"``ws://`` or ``wss://`` (got {url!r})."
            )

        _validate_active_session_shape(data, path)
        agent_mcp_store = _parse_agent_mcp_store(data, path)

        return cls(
            path=path,
            workstation_url=url,
            agent_mcp_store=agent_mcp_store,
        )


def _parse_toml(working_dir: Path) -> tuple[dict[str, Any], Path]:
    """Read + parse ``.fxtr_workstation.toml`` from ``working_dir``.

    Shared between :meth:`WorkstationConfig.from_working_dir` and
    :func:`read_active_session` so the error messages stay
    identical.
    """
    path = working_dir / CONFIG_FILENAME
    if not path.is_file():
        raise ConfigError(
            f"{path} does not exist — create it with at minimum a "
            f'``workstation_url = "ws://..."`` line.'
        )
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: failed to parse TOML: {e}") from e
    return data, path


def _validate_active_session_shape(data: dict[str, Any], path: Path) -> None:
    """Raise :class:`ConfigError` if ``active_session`` is present
    in ``data`` but not a string. Otherwise return without error.
    Shared validation so the read-time and write-time error
    messages match."""
    active_session = data.get("active_session")
    if active_session is not None and not isinstance(active_session, str):
        raise ConfigError(f"{path}: ``active_session`` must be a string (got {active_session!r}).")


def read_active_session(working_dir: Path) -> str | None:
    """Return the current value of ``active_session`` from
    ``working_dir / .fxtr_workstation.toml``, or ``None`` if absent.

    Always hits disk — no caching, so a manual toml edit takes
    effect immediately. Used by the connection's per-cycle
    refresh and by tools that need to know the persisted-on-disk
    session ID (e.g. ``close_session(force=True)``, the
    ``set_working_dir`` display message).

    Raises:
        ConfigError: if the file is missing or malformed, or if
            ``active_session`` is present but not a string.
    """
    data, path = _parse_toml(working_dir)
    _validate_active_session_shape(data, path)
    raw = data.get("active_session")
    return raw if isinstance(raw, str) else None


def write_active_session(working_dir: Path, session_id: str | None) -> None:
    """Set or clear ``active_session`` in the toml at
    ``working_dir / .fxtr_workstation.toml``.

    Preserves any unrelated keys / comments via tomlkit. If
    ``session_id`` is ``None``, removes the field entirely.

    Used by ``open_session`` (persist a new UUID) and
    ``close_session`` (clear it). External callers that just want
    to know the current value should use :func:`read_active_session`
    instead.
    """
    path = working_dir / CONFIG_FILENAME
    if path.is_file():
        doc = tomlkit.parse(path.read_text())
    else:
        doc = tomlkit.document()
    if session_id is None:
        if "active_session" in doc:
            del doc["active_session"]
    else:
        doc["active_session"] = session_id
    path.write_text(tomlkit.dumps(doc))
