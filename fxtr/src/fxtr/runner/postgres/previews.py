"""Step attempts' latest previews: the ``attempt_previews`` table's write and read.

A step body publishes previews of the result it is producing through ``ctx.preview``; a step
attempt keeps only the latest and writes it from its background flush task, at most once per
:data:`~fxtr.runner.postgres.attempts.PREVIEW_INTERVAL` (see
:class:`~fxtr.runner.postgres.attempts.StepAttempt`). Each write replaces the row, so the table
holds at most one preview per attempt. An attempt that fails or is abandoned keeps its last one;
one that completes has its row deleted in the transaction that records its result
(:func:`delete_preview`), since the result takes the preview's place.

Like logs, previews are pure observability state: nothing here is read by the ledger or the
execution policy, and no rule depends on it. The row's ``seq`` counts the previews the body had
published when it was written and only grows, which is how a reader polling for changes tells a
newer preview from the one it has. Metadata and bytes are read separately, so describing a step
never loads a preview's bytes.
"""

from __future__ import annotations

import dataclasses
import itertools
import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from fxtr.runner.postgres.database import Transaction
from fxtr.runner.postgres.schema import attempt_previews
from fxtr.runner.session import AttemptID

_ATTEMPTS_PER_QUERY = 10_000
"""The most attempts one query of :func:`list_previews_many` reads."""


@dataclasses.dataclass(frozen=True)
class PreviewRecord:
    """What is known about an attempt's latest preview without loading it.

    Attributes:
        seq: How many previews the body had published when this one was written, from 1.
        at: When it was written.
    """

    seq: int
    at: datetime


def _attempt_uuid(attempt_id: AttemptID) -> uuid.UUID:
    return uuid.UUID(attempt_id)


async def record_preview(tx: Transaction, attempt_id: AttemptID, seq: int, data: bytes) -> None:
    """Write an attempt's latest preview, replacing the one it had.

    Args:
        attempt_id: The step attempt.
        seq: How many previews the body had published, this one included.
        data: The preview's canonical DAG-CBOR bytes.
    """
    statement = insert(attempt_previews).values(
        attempt_id=_attempt_uuid(attempt_id), seq=seq, data=data
    )
    await tx.execute(
        statement.on_conflict_do_update(
            index_elements=[attempt_previews.c.attempt_id],
            set_={"seq": statement.excluded.seq, "data": statement.excluded.data, "at": func.now()},
        )
    )


async def delete_preview(tx: Transaction, attempt_id: AttemptID) -> None:
    """Delete an attempt's preview, if it has one: what a completing attempt does, in the
    transaction that records its result."""
    await tx.execute(
        delete(attempt_previews).where(attempt_previews.c.attempt_id == _attempt_uuid(attempt_id))
    )


async def get_preview(tx: Transaction, attempt_id: AttemptID) -> tuple[PreviewRecord, bytes] | None:
    """An attempt's latest preview and its bytes, or ``None`` if it published none."""
    row = (
        await tx.execute(
            select(attempt_previews).where(
                attempt_previews.c.attempt_id == _attempt_uuid(attempt_id)
            )
        )
    ).first()
    if row is None:
        return None
    return PreviewRecord(seq=row.seq, at=row.at), bytes(row.data)


async def list_previews_many(
    tx: Transaction, attempt_ids: Collection[AttemptID]
) -> dict[AttemptID, PreviewRecord]:
    """The latest preview's record of each attempt that published one, without the bytes, in one
    query per :data:`_ATTEMPTS_PER_QUERY` attempts."""
    found: dict[AttemptID, PreviewRecord] = {}
    by_uuid = {_attempt_uuid(attempt_id): attempt_id for attempt_id in attempt_ids}
    columns = (attempt_previews.c.attempt_id, attempt_previews.c.seq, attempt_previews.c.at)
    for batch in itertools.batched(by_uuid, _ATTEMPTS_PER_QUERY):
        rows = await tx.execute(select(*columns).where(attempt_previews.c.attempt_id.in_(batch)))
        for row in rows:
            found[by_uuid[row.attempt_id]] = PreviewRecord(seq=row.seq, at=row.at)
    return found


__all__ = [
    "PreviewRecord",
    "delete_preview",
    "get_preview",
    "list_previews_many",
    "record_preview",
]
