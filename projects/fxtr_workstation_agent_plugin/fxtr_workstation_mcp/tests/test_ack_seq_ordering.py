"""Stage-3a unit test for the ack-seq ordering invariant.

The ``_AckCommitJob`` design ensures we never ack a commit's seq
to the workstation before any foreign events queued before our
response have been processed. This test exercises the queue
ordering directly: simulate a worker scenario where a foreign
``_EventJob(seq=N)`` was queued before our response landed, then
we enqueue our ``_AckCommitJob(seq=M)``. After draining, the
final ``_last_processed_seq`` is ``max(N, M)``.

This is a lightweight test that constructs the connection's
state-mutating helpers directly without touching websockets or
the workstation. We don't need a real server for this — just
exercise the mutation paths.
"""

from __future__ import annotations

import pytest
from fxtr_workstation.client_protocol import (
    SessionEventFrame,
    StateUpdateEvent,
)

from fxtr_workstation_mcp.ws_connection import (
    ProtocolError,
    WorkstationConnection,
    _AckCommitJob,
    _EventJob,
)

pytestmark = pytest.mark.anyio


def _make_connection() -> WorkstationConnection:
    """Build a never-started connection for direct method calls.
    The supervisor task is not needed for these tests since we
    only exercise the per-job state mutations."""
    conn = WorkstationConnection(
        rpc_url="ws://unused/rpc",
        client_url="ws://unused/client",
    )
    # Pretend we have an active subscription so _do_event_job
    # accepts state_update events.
    conn._active_subscription_id = 1
    return conn


def _state_update_event(seq: int) -> _EventJob:
    """Build a synthetic foreign state_update event job."""
    return _EventJob(
        frame=SessionEventFrame(
            subscription_id=1,
            event=StateUpdateEvent(
                seq=seq,
                ack_seq=0,
                state={"data_by_key": {}},
                tracked_entities=[],
                tracked_services=[],
            ),
        )
    )


async def test_ack_commit_job_does_not_jump_past_lower_seq() -> None:
    """A foreign event at seq=4 followed by an ack-commit at seq=5
    yields _last_processed_seq=5 — the foreign event advanced to
    4, then the ack-commit advanced to 5."""
    conn = _make_connection()
    await conn._do_event_job(_state_update_event(seq=4))
    assert conn._last_processed_seq == 4
    await conn._do_ack_commit_job(_AckCommitJob(seq=5))
    assert conn._last_processed_seq == 5


async def test_ack_commit_job_no_op_when_event_seq_already_higher() -> None:
    """If a higher-seq event was processed first (e.g. workstation
    processed our commit then a foreign one, and the foreign event
    drained before our ack-commit fired), the ack-commit is a no-op."""
    conn = _make_connection()
    await conn._do_event_job(_state_update_event(seq=10))
    assert conn._last_processed_seq == 10
    await conn._do_ack_commit_job(_AckCommitJob(seq=5))
    assert conn._last_processed_seq == 10  # unchanged


async def test_event_job_advances_seq_monotonically() -> None:
    """Receiving state_update events advances _last_processed_seq.
    Out-of-order delivery (a regression) is now a ProtocolError per
    the 3-post-2 hard-error invariants."""
    conn = _make_connection()
    await conn._do_event_job(_state_update_event(seq=3))
    assert conn._last_processed_seq == 3
    await conn._do_event_job(_state_update_event(seq=7))
    assert conn._last_processed_seq == 7
    # Regression — workstation guarantees state_update seqs strictly
    # monotonic; surface as ProtocolError so the supervisor exits
    # the cycle and a fresh subscribe rebuilds local state.
    with pytest.raises(ProtocolError, match="regressed"):
        await conn._do_event_job(_state_update_event(seq=5))
