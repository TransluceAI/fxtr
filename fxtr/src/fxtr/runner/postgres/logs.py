"""Per-attempt free-form logs: the ``attempt_logs`` table's write and read.

Logs are pure observability state. Nothing here is read by the ledger or the execution policy, no
rule depends on it, and it may be dropped without affecting correctness; this module is deliberately
small and self-contained so it is easy to remove when an event-streaming mechanism replaces it.

Lines are kept per attempt and never deduplicated across a replay: a later attempt of the same
invocation re-runs the body and appends its own lines under its own id. Workflow attempts write
their lines from the recorder's batch transaction (:mod:`fxtr.runner.postgres.recorder`); step
attempts write theirs from a background flush task (see
:class:`~fxtr.runner.postgres.attempts.StepAttempt`).
"""

from __future__ import annotations

import dataclasses
import itertools
import uuid
from collections.abc import Collection, Sequence
from datetime import datetime

from sqlalchemy import func, insert, select

from fxtr.runner.postgres.database import Transaction
from fxtr.runner.postgres.schema import attempt_logs
from fxtr.runner.session import AttemptID

_ATTEMPTS_PER_QUERY = 10_000
"""The most attempts one query of :func:`list_logs_many` reads the lines of."""


@dataclasses.dataclass(frozen=True)
class LogRecord:
    """One line of ``attempt_logs``.

    Attributes:
        seq: The line's position in the attempt's log, from 0.
        message: The line the body recorded.
        at: When it was recorded.
    """

    seq: int
    message: str
    at: datetime


def _attempt_uuid(attempt_id: AttemptID) -> uuid.UUID:
    return uuid.UUID(attempt_id)


async def record_logs(tx: Transaction, attempt_id: AttemptID, messages: Sequence[str]) -> None:
    """Append log lines to an attempt's log, numbered after its last.

    A no-op for an empty sequence. The lines are inserted in the given transaction; the caller
    decides the commit point (a recorder batch for a workflow attempt, a flush cycle for a step).

    Args:
        attempt_id: The attempt whose log to append to.
        messages: The lines, in order.
    """
    if not messages:
        return
    seq = await _next_seq(tx, attempt_id)
    await tx.execute(
        insert(attempt_logs),
        [
            {"attempt_id": _attempt_uuid(attempt_id), "seq": seq + i, "message": message}
            for i, message in enumerate(messages)
        ],
    )


async def list_logs(tx: Transaction, attempt_id: AttemptID) -> list[LogRecord]:
    """An attempt's log lines, in order."""
    rows = (
        await tx.execute(
            select(attempt_logs)
            .where(attempt_logs.c.attempt_id == _attempt_uuid(attempt_id))
            .order_by(attempt_logs.c.seq)
        )
    ).all()
    return [LogRecord(seq=row.seq, message=row.message, at=row.at) for row in rows]


async def list_logs_many(
    tx: Transaction, attempt_ids: Collection[AttemptID]
) -> dict[AttemptID, list[LogRecord]]:
    """:func:`list_logs` for many attempts, by attempt, in one query per
    :data:`_ATTEMPTS_PER_QUERY` attempts; an attempt with no lines maps to an empty list."""
    found: dict[AttemptID, list[LogRecord]] = {attempt_id: [] for attempt_id in attempt_ids}
    by_uuid = {_attempt_uuid(attempt_id): attempt_id for attempt_id in attempt_ids}
    for batch in itertools.batched(by_uuid, _ATTEMPTS_PER_QUERY):
        rows = await tx.execute(
            select(attempt_logs)
            .where(attempt_logs.c.attempt_id.in_(batch))
            .order_by(attempt_logs.c.attempt_id, attempt_logs.c.seq)
        )
        for row in rows:
            found[by_uuid[row.attempt_id]].append(
                LogRecord(seq=row.seq, message=row.message, at=row.at)
            )
    return found


async def _next_seq(tx: Transaction, attempt_id: AttemptID) -> int:
    """The sequence number the attempt's next log line gets."""
    row = (
        await tx.execute(
            select(func.coalesce(func.max(attempt_logs.c.seq) + 1, 0)).where(
                attempt_logs.c.attempt_id == _attempt_uuid(attempt_id)
            )
        )
    ).first()
    assert row is not None
    return int(row[0])


__all__ = ["LogRecord", "list_logs", "record_logs"]
