"""Database readers used by the viewer, independent of a runnable project.

The caller owns the database connection pool and schema. Constructing a reader neither imports
project modules nor creates tables. Source files and renderer bundles belong to the app that
composes these readers, not to the stored job data.
"""

from collections.abc import Collection
from datetime import timedelta

from sqlalchemy import func, select

from fxtr.core.entity_data import EntityID
from fxtr.runner.postgres.database import Database
from fxtr.runner.postgres.debug_web.scope import JobScope, reachable
from fxtr.runner.postgres.entities import PostgresEntityStorage
from fxtr.runner.postgres.jobs import Jobs
from fxtr.runner.postgres.schema import entities


class ViewerReader:
    """Read jobs and entities from one database schema.

    ``stale_after`` determines how job-attempt lease freshness is displayed; it does not alter
    execution or acquire a lease. ``scope`` is the set of jobs the reader may show
    (:mod:`.scope` has the rules), every job by default. The caller must authorize access to
    this schema and compute the scope before serving these readers: within the scope they are
    not an authorization boundary.
    """

    def __init__(self, database: Database, *, stale_after: timedelta, scope: JobScope) -> None:
        self.database = database
        self.stale_after = stale_after
        self.scope = scope
        self.entities = PostgresEntityStorage(database)
        self.jobs = Jobs(database, self.entities)

    async def reachable(self, entity_ids: Collection[EntityID]) -> set[bytes]:
        """The cids among ``entity_ids`` the scope may serve (:func:`.scope.reachable`), without
        a query under the unrestricted scope."""
        cids = [entity_id.cid for entity_id in entity_ids]
        if self.scope.unrestricted:
            return set(cids)
        async with self.database.transaction() as tx:
            return await reachable(tx, self.scope, cids)

    async def entity_sizes(self, cids: Collection[bytes]) -> dict[bytes, int]:
        """Read stored byte lengths without loading entity contents.

        The caller supplies only ids authorized by ``reachable``. Entity contents are
        immutable, so a size checked here also applies when the bytes are loaded.
        """
        async with self.database.transaction() as tx:
            rows = await tx.execute(
                select(entities.c.cid, func.octet_length(entities.c.data).label("byte_size")).where(
                    entities.c.cid.in_(cids)
                )
            )
            return {bytes(row.cid): int(row.byte_size) for row in rows}
