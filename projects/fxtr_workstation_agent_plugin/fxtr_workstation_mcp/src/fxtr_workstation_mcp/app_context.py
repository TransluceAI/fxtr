"""``AppContext`` carried by the FastMCP lifespan and exposed to tools.

Tools fish this out via ``ctx.request_context.lifespan_context``.

The lifespan owns:

- ``task_group``: long-lived anyio task group for any tasks that
  outlive a tool call (notably the
  :class:`WorkstationConnection`'s supervisor).
- ``connection``: the workstation connection, set by the first
  ``connect`` tool call. The supervisor it spawns lives on
  ``task_group``; on lifespan exit the supervisor is signalled via
  ``connection.shutdown()`` and its drains complete before the
  task group exits.
- ``working_dir`` / ``config``: set by ``set_working_dir``.

Tools never enter context managers themselves — every long-lived
async resource is owned by the supervisor task in ``task_group``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anyio.abc import TaskGroup

from .config import WorkstationConfig
from .exception_log import ExceptionLog
from .outgoing_buffer import OutgoingBuffer
from .sandbox_env import SandboxEnvSupervisor
from .ws_connection import WorkstationConnection


@dataclass
class AppContext:
    """Server-wide state held by the FastMCP lifespan."""

    task_group: TaskGroup
    """Long-lived anyio task group. The :class:`WorkstationConnection`'s
    supervisor task is started here on first ``connect``; the
    :class:`SandboxEnvSupervisor`'s on first ``run_script``."""

    working_dir: Path | None = None
    """Set by ``set_working_dir``. ``None`` until then; other tools
    error if this is ``None``."""

    config: WorkstationConfig | None = None
    """Parsed contents of the working dir's ``.fxtr_workstation.toml``.
    Populated alongside ``working_dir``."""

    connection: WorkstationConnection | None = None
    """The workstation connection. Created + started by the first
    ``connect`` call; reused by subsequent calls. ``None`` indicates
    "not yet connected"."""

    sandbox_env: SandboxEnvSupervisor | None = None
    """The eryx sandbox environment. Lazy-bootstrapped by the first
    ``run_script`` call (the bootstrap takes ~3s, so we don't pay
    the cost for sessions that only ``connect`` / ``open_session``).
    """

    buffer: OutgoingBuffer | None = None
    """Lifespan-scoped outgoing-message buffer. Set in the
    ``lifespan`` body immediately after constructing the context,
    *before* ``yield``, so every tool callable can rely on its
    presence. The :class:`WorkstationConnection`'s supervisor pushes
    channel notifications into this buffer via a thin async adapter
    set up in ``_ensure_connected``; tools drain it via
    :meth:`OutgoingBuffer.tool_window`.
    """

    exception_log: ExceptionLog | None = None
    """Lifespan-scoped bounded LRU storing full script-exception
    tracebacks. ``run_script``'s host-side error formatter
    compresses the traceback to a two-frame summary and stashes
    the full text here; the response carries the resulting
    ``exc_id`` so the agent can fetch the full form via the
    ``get_full_exception(exc_id)`` sandbox builtin.

    Set in the ``lifespan`` body alongside ``buffer``, before
    ``yield``. ``None`` only in the brief window before
    construction (defensive — tools that touch it assert
    non-None)."""

    latest_session: Any | None = None
    """Most-recently-seen ``ServerSession`` from a tool call's
    ``ctx.session``. Captured by every tool callable on entry so
    that asynchronous channel notifications (fired by the
    supervisor's worker outside any tool-call request context)
    can still push frames through the client's transport.

    Stays ``None`` until the agent makes at least one tool call.
    **By construction, the connection's supervisor cannot fire a
    channel notification before this is set**: the supervisor is
    started inside ``_ensure_connected``, which only runs from a
    tool callable (e.g. ``connect()``), and the tool callable
    captures ``ctx.session`` here *before* it starts the
    supervisor. Consequently the notifier (see ``channel.py``)
    asserts the session is present rather than guarding against
    ``None`` — a ``None`` would indicate a structural bug.

    The SDK has no callback for ``ServerSession`` creation
    (``ServerSession`` is constructed inside the lowlevel
    ``Server.run`` *after* the lifespan is entered, and the
    lifespan signature is ``(server) -> AsyncContextManager`` with
    no session in scope), so the lazy capture-on-tool-call pattern
    is the only viable shape until upstream adds one.

    Typed ``Any`` to avoid importing the mcp library here just
    for the type annotation.
    """
