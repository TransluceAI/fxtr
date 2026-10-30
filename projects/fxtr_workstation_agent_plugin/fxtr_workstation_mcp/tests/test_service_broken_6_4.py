"""Service-break handling in the MCP `/client` reader (6.4).

The workstation pushes an out-of-band, sequence-less
:class:`ServiceBrokenNotification` when a service a session holds
breaks. The MCP routes it through the worker (not the reducer) and
surfaces it to the agent as a best-effort ``service_broken`` channel
notice, deduped per short_id so a reconnect (which re-derives the break
from current state) doesn't re-spam.
"""

from __future__ import annotations

import anyio
import pytest
from fxtr_workstation.client_protocol import ServiceBrokenNotification

from fxtr_workstation_mcp.ws_connection import (
    WorkstationConnection,
    _ServiceBrokenJob,
)

pytestmark = pytest.mark.anyio


def _make_connection() -> WorkstationConnection:
    conn = WorkstationConnection(
        rpc_url="ws://unused/rpc",
        client_url="ws://unused/client",
    )
    conn._active_subscription_id = 1
    return conn


async def test_route_service_broken_enqueues_job() -> None:
    conn = _make_connection()
    send, recv = anyio.create_memory_object_stream[object](max_buffer_size=8)
    conn._job_send = send  # type: ignore[assignment]

    conn._route_frame(ServiceBrokenNotification(subscription_id=1, service_short_id="s3"))

    job = recv.receive_nowait()
    assert isinstance(job, _ServiceBrokenJob)
    assert job.service_short_id == "s3"


async def test_route_service_broken_wrong_subscription_dropped() -> None:
    conn = _make_connection()
    send, recv = anyio.create_memory_object_stream[object](max_buffer_size=8)
    conn._job_send = send  # type: ignore[assignment]

    # subscription_id mismatch → dropped, no job enqueued.
    conn._route_frame(ServiceBrokenNotification(subscription_id=999, service_short_id="s3"))

    with pytest.raises(anyio.WouldBlock):
        recv.receive_nowait()


async def test_service_broken_job_notifies_once_and_names_holding_slots() -> None:
    conn = _make_connection()
    notifications: list[tuple[str, dict[str, str]]] = []

    async def capture(content: str, attrs: dict[str, str]) -> None:
        notifications.append((content, attrs))

    conn._notify_channel = capture  # type: ignore[assignment]
    conn._mirror.data_by_key = {
        "svc": ["exported_service", "s3"],
        "other": ["exported_service", "s4"],
        "inv": ["invoke_pending", 2],
    }

    await conn._do_service_broken_job(_ServiceBrokenJob(service_short_id="s3"))

    assert len(notifications) == 1
    content, attrs = notifications[0]
    assert attrs == {"kind": "service_broken", "service_short_id": "s3"}
    assert "s3" in content
    assert "svc" in content  # the slot_key holding the broken service
    assert "other" not in content  # holds a different service

    # Dedup: a second break notice for the same service is a no-op
    # (covers reconnect re-announce from current state).
    await conn._do_service_broken_job(_ServiceBrokenJob(service_short_id="s3"))
    assert len(notifications) == 1

    # A different broken service still notifies.
    await conn._do_service_broken_job(_ServiceBrokenJob(service_short_id="s4"))
    assert len(notifications) == 2


async def test_service_broken_dedup_clears_on_session_close() -> None:
    """The per-session dedup set is reset on session close so a fresh
    session can re-surface a (possibly different) break for the same
    short_id."""
    conn = _make_connection()
    notifications: list[tuple[str, dict[str, str]]] = []

    async def capture(content: str, attrs: dict[str, str]) -> None:
        notifications.append((content, attrs))

    conn._notify_channel = capture  # type: ignore[assignment]

    await conn._do_service_broken_job(_ServiceBrokenJob(service_short_id="s3"))
    assert len(notifications) == 1

    # Simulate the session-close reset (same line that resets
    # ``_notified_seqs``).
    conn._notified_broken_services = set()

    await conn._do_service_broken_job(_ServiceBrokenJob(service_short_id="s3"))
    assert len(notifications) == 2
