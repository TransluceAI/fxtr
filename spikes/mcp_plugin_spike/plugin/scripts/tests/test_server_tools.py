"""End-to-end test of the MCP server's `set_working_dir` + `run_script`
tools, driven through the real lifespan (factory bootstrap, host impl
mount, direct env). Stops short of going through stdio JSON-RPC — the
tools are called as async functions with a hand-rolled fake Context.

What this proves on top of `test_sandbox_integration`:

- Lifespan resource ordering works (task_group → FxtrScope → host
  mount → factory → direct env) without deadlocks.
- `set_working_dir` validates and stores the path.
- `run_script` reads `preamble.py`, builds volumes (including the
  optional `packages/` mount), drives the sandbox, and returns the
  declared values as JSON.
- Scheduled messages collected inside the sandbox are handed to the
  channel-delivery task group (we capture `_deliver_after`
  invocations via the fake session's write stream).
- A user package under `<working_dir>/packages/` is reachable from
  the sandboxed script.

Slow test: factory build is ~3s; we pay it once per test using
module-scoped lifespan via a fixture.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest

pytestmark = pytest.mark.anyio

from scheduler_mcp.server import (
    AppContext,
    lifespan,
    mcp,
    run_script,
    set_working_dir,
)


class _FakeWriteStream:
    """Captures whatever `_send_channel_notification` would push."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, msg: Any) -> None:
        self.sent.append(msg)


class _FakeSession:
    """Just enough of an MCP session for `_send_channel_notification` to
    write through `session._write_stream.send(...)`."""

    def __init__(self) -> None:
        self._write_stream = _FakeWriteStream()


def _make_ctx(app_ctx: AppContext, session: _FakeSession) -> Any:
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=app_ctx),
        session=session,
    )


@pytest.fixture
async def app_ctx_and_lifespan() -> AsyncIterator[AppContext]:
    """Open the real server lifespan per test. The lifespan's
    ``mcp_lifespan`` FxtrScope is `isolated=True, include=[GLOBAL_SCOPE]`,
    so ``ACTIVE_SCOPE``-based test interference can't reach into it."""
    async with lifespan(mcp) as app_ctx:
        yield app_ctx


def _write_working_dir(tmp_path: Path, *, with_packages: bool = False) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "preamble.py").write_text("ANSWER = 42\n")
    if with_packages:
        pkg = wd / "packages" / "userpkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("def shout(msg: str) -> str:\n    return msg.upper()\n")
    return wd


async def test_set_working_dir_validates_and_stores(
    tmp_path: Path,
    app_ctx_and_lifespan: AppContext,
) -> None:
    app_ctx = app_ctx_and_lifespan
    wd = _write_working_dir(tmp_path)
    ctx = _make_ctx(app_ctx, _FakeSession())

    msg = await set_working_dir(str(wd), ctx)
    assert "Working dir set" in msg
    assert app_ctx.working_dir == wd


async def test_set_working_dir_rejects_missing_preamble(
    tmp_path: Path,
    app_ctx_and_lifespan: AppContext,
) -> None:
    app_ctx = app_ctx_and_lifespan
    wd = tmp_path / "no-preamble"
    wd.mkdir()
    ctx = _make_ctx(app_ctx, _FakeSession())
    msg = await set_working_dir(str(wd), ctx)
    assert "preamble.py does not exist" in msg


async def test_run_script_returns_declared_values(
    tmp_path: Path,
    app_ctx_and_lifespan: AppContext,
) -> None:
    app_ctx = app_ctx_and_lifespan
    wd = _write_working_dir(tmp_path)
    session = _FakeSession()
    ctx = _make_ctx(app_ctx, session)
    await set_working_dir(str(wd), ctx)

    result = await run_script(
        textwrap.dedent(
            """
            from _fxtr_mcp_builtin import declare_value
            declare_value("answer", ANSWER)
            """
        ),
        ctx,
    )
    # Plain-text summary (no JSON). One declared value, no schedules.
    assert "answer = 42" in result
    assert "Scheduled follow-ups: (none)" in result


async def test_run_script_replays_scheduled_messages(
    tmp_path: Path,
    app_ctx_and_lifespan: AppContext,
) -> None:
    """Scheduled messages collected in the sandbox must be handed to the
    channel task group — we observe this by checking that the fake
    session's write stream eventually sees the notification."""
    import anyio

    app_ctx = app_ctx_and_lifespan
    wd = _write_working_dir(tmp_path)
    session = _FakeSession()
    ctx = _make_ctx(app_ctx, session)
    await set_working_dir(str(wd), ctx)

    result = await run_script(
        textwrap.dedent(
            """
            from _fxtr_mcp_builtin import schedule_message
            schedule_message(0.05, "wake up")
            """
        ),
        ctx,
    )
    assert "Declared values: (none)" in result
    assert "Scheduled 1 follow-up message(s)" in result
    assert "wake up" in result

    # Wait long enough for the 0.05s deferred delivery to fire.
    with anyio.fail_after(2.0):
        while not session._write_stream.sent:
            await anyio.sleep(0.01)
    sent = session._write_stream.sent[0]
    # SessionMessage(message=JSONRPCMessage(JSONRPCNotification(...)))
    notif = sent.message.root
    assert notif.method == "notifications/claude/channel"
    assert notif.params["content"] == "wake up"


async def test_run_script_picks_up_user_packages_dir(
    tmp_path: Path,
    app_ctx_and_lifespan: AppContext,
) -> None:
    """`<working_dir>/packages/` is mounted and added to sys.path so user
    packages are importable from the sandboxed script."""
    app_ctx = app_ctx_and_lifespan
    wd = _write_working_dir(tmp_path, with_packages=True)
    ctx = _make_ctx(app_ctx, _FakeSession())
    await set_working_dir(str(wd), ctx)

    result = await run_script(
        textwrap.dedent(
            """
            from _fxtr_mcp_builtin import declare_value
            from userpkg import shout
            declare_value("loud", shout("hi"))
            """
        ),
        ctx,
    )
    assert "loud = 'HI'" in result


async def test_run_script_without_working_dir_errors(
    app_ctx_and_lifespan: AppContext,
) -> None:
    """run_script with no working_dir returns an error string instead of
    crashing. We temporarily clear the shared fixture's working_dir
    rather than spinning up a second lifespan (anyio cancel scopes
    don't survive cross-task entry/exit, which a nested lifespan
    inside a module-scoped fixture trips over)."""
    app_ctx = app_ctx_and_lifespan
    saved = app_ctx.working_dir
    app_ctx.working_dir = None
    try:
        ctx = _make_ctx(app_ctx, _FakeSession())
        result = await run_script("", ctx)
        assert "working_dir not set" in result
    finally:
        app_ctx.working_dir = saved
