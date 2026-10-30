"""Protocol for fxtr remote entity transfer.

Any remote backend (hub server, S3, local sync, etc.) implements this
protocol to support push/pull of entities from a git-backed store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fxtr2.data.abstract_arena import FxtrEntityFlatData
from fxtr2.data.entity_core import FxtrID


class RemoteError(Exception):
    """Base error for remote operations."""


@dataclass(frozen=True)
class PushResult:
    """Result of a push operation."""

    entities_stored: int


@runtime_checkable
class RemoteClient(Protocol):
    """Interface for remote entity transfer.

    Four methods: negotiate (check which entities the remote already
    has), push (upload entities), pull_plan (get transitive closure
    IDs with sizes), and pull_data (download specific entities).
    """

    async def negotiate(
        self,
        entity_ids: list[FxtrID],
    ) -> tuple[list[FxtrID], list[FxtrID]]:
        """Check which entity IDs the remote knows about.

        Returns (known, missing) ID lists.
        """
        ...

    async def push_entities(
        self,
        entities: list[FxtrEntityFlatData],
    ) -> PushResult:
        """Upload entities to the remote."""
        ...

    async def pull_plan(
        self,
        entity_ids: list[FxtrID],
    ) -> list[tuple[FxtrID, int]]:
        """Get the transitive closure with sizes, in topological order.

        Returns (entity_id, size_bytes) pairs, deps before dependents.
        """
        ...

    async def pull_data(
        self,
        entity_ids: list[FxtrID],
    ) -> list[FxtrEntityFlatData]:
        """Download specific entities by ID. No graph walking."""
        ...
