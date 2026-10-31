"""Connecting to the runner's Postgres database.

A :class:`Database` owns a SQLAlchemy async engine (and so a connection pool) for one experiment's
database and hands out transactions. The runner uses SQLAlchemy Core only: every query is an
explicit statement run through a :class:`Transaction`, and the unit of work is one
:meth:`Database.transaction` block, committed on normal exit and rolled back on an exception.
Transactions should stay short and never be held across slow external work.

Anything may hold a ``Database`` (it is a pool and a way to open transactions, with no state
attached); nothing holds a ``Transaction`` beyond one operation. Code that keeps in-memory state
reflecting a database write changes that state in a :meth:`Transaction.on_commit` callback, so a
rollback never leaves memory ahead of the database.

A database may be pointed at a *Postgres schema* (a namespace within the server's database) so
that several independent runner states can share one server. Tests use this to get a throwaway
namespace per test.

A ``Database`` also carries the process's **wake** signal (:meth:`Database.wake`,
:meth:`Database.wait_for`, :meth:`Database.wait_for_any`): named topics that a committing
transaction wakes (from an :meth:`Transaction.on_commit` callback) so that waiters in the same
process re-check the database at once instead of waiting out their poll interval. It is only an
optimization for co-located clients; every waiter also polls, since another process's commits do
not wake it.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Self

import anyio
import anyio.lowlevel
from sqlalchemy import CursorResult, Executable, Table, text
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from fxtr.project.config import DATABASE_URL_ENV_VAR
from fxtr.runner.postgres.schema import metadata

_PG_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]*")


class Transaction:
    """One database transaction: a connection whose work commits or rolls back as a unit.

    Handed out by :meth:`Database.transaction`, and passed down into any operation that should be
    part of the same commit. Never kept beyond the block that opened it.

    Attributes:
        connection: The underlying connection, for the rare operation that needs it directly
            (running synchronous metadata operations, say).
    """

    def __init__(self, connection: AsyncConnection, database: Database) -> None:
        self.connection = connection
        self._database = database
        self._on_commit: list[Callable[[], None]] = []

    def upsert(self, table: Table) -> Insert:
        """An ``INSERT`` on ``table`` with ``on_conflict_do_nothing`` / ``on_conflict_do_update``
        available on it: the one place the runner's execution operations pick a dialect-specific
        construct (Postgres's; another dialect would be chosen here, by the connection's
        dialect).
        """
        return pg_insert(table)

    async def execute(self, statement: Executable, parameters: Any = None) -> CursorResult[Any]:
        """Run one statement in this transaction.

        Args:
            statement: The Core statement (or ``text()``).
            parameters: Bound parameters: a mapping, or a list of mappings for an executemany.

        Returns:
            The result: rows for a query, ``rowcount`` for a data change.
        """
        return await self.connection.execute(statement, parameters)

    def on_commit(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` after this transaction has committed; never after a rollback.

        Callbacks run synchronously, in registration order, once the commit has succeeded. They
        are for bringing in-memory state in line with what the database now holds.
        """
        self._on_commit.append(callback)

    def wake(self, topic: str) -> None:
        """Wake ``topic`` (:meth:`Database.wake`) once this transaction has committed."""
        self.on_commit(lambda: self._database.wake(topic))

    def _committed(self) -> None:
        for callback in self._on_commit:
            callback()


def _application_name(pg_schema: str) -> str:
    """The ``application_name`` connections working in a schema carry."""
    return f"fxtr:{pg_schema}"


class Database:
    """One experiment's Postgres database: an engine, its pool, and the schema lifecycle.

    Long-lived and safe to share between tasks; the transactions it hands out are not. Use as an
    async context manager to dispose of the pool on exit, or call :meth:`dispose` explicitly.

    Args:
        url: A SQLAlchemy URL using the asyncpg driver, e.g.
            ``postgresql+asyncpg://user:password@localhost:5432/fxtr``.
        pg_schema: The Postgres schema (namespace) to work in; lowercase letters, digits, and
            underscores, not starting with a digit. Every connection's ``search_path`` is set to
            it, so the runner's tables live there and unqualified names resolve there, and its
            ``application_name`` is ``fxtr:<schema>``, so the schema's connections can be told
            apart in ``pg_stat_activity`` (which :meth:`drop_schema` relies on). ``None`` uses
            the server's default (normally ``public``).
        pool_size: Persistent connections the pool keeps open.
        max_overflow: Temporary connections allowed beyond ``pool_size`` when it is busy.

    Raises:
        ValueError: If ``pg_schema`` is not a valid name.
    """

    def __init__(
        self,
        url: str,
        *,
        pg_schema: str | None = None,
        pool_size: int = 5,
        max_overflow: int = 5,
    ) -> None:
        if pg_schema is not None and not _PG_SCHEMA_NAME.fullmatch(pg_schema):
            raise ValueError(f"pg_schema must match {_PG_SCHEMA_NAME.pattern!r}, got {pg_schema!r}")
        self.url = url
        self.pg_schema = pg_schema
        connect_args: dict[str, Any] = {}
        if pg_schema is not None:
            connect_args["server_settings"] = {
                "search_path": pg_schema,
                "application_name": _application_name(pg_schema),
            }
        self.engine: AsyncEngine = create_async_engine(
            url,
            connect_args=connect_args,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
        )
        self._wakes: dict[str, set[anyio.Event]] = {}

    # ---- the in-process wake ----

    def wake(self, topic: str) -> None:
        """Wake every task waiting on ``topic`` in this process. Call from a
        :meth:`Transaction.on_commit` callback after a change waiters care about."""
        for event in self._wakes.pop(topic, ()):
            event.set()

    async def wait_for(self, topic: str, timeout: float) -> bool:
        """Wait until ``topic`` is woken or ``timeout`` seconds pass.

        Returns:
            Whether a wake happened (``False`` on timeout). Either way the caller should re-check
            the database: wakes from other processes never arrive here.
        """
        return await self.wait_for_any((topic,), timeout)

    async def wait_for_any(self, topics: Iterable[str], timeout: float) -> bool:
        """Wait until any of ``topics`` is woken or ``timeout`` seconds pass: one event subscribed
        under every topic, so a wait on thousands of topics costs a set insertion each rather than
        a task each. The subscriptions last only for this wait; a wake, a timeout, or a
        cancellation releases all of them.

        Returns:
            Whether a wake happened (``False`` on timeout); see :meth:`wait_for`.
        """
        event = anyio.Event()
        names = set(topics)
        for name in names:
            self._wakes.setdefault(name, set()).add(event)
        try:
            with anyio.move_on_after(timeout):
                await event.wait()
                return True
            return False
        finally:
            for name in names:
                waiting = self._wakes.get(name)
                if waiting is not None:
                    waiting.discard(event)
                    if not waiting:
                        del self._wakes[name]

    @classmethod
    def from_env(cls, *, pg_schema: str | None = None, **kwargs: Any) -> Database:
        """Connect to the URL in the :data:`DATABASE_URL_ENV_VAR` environment variable.

        Args:
            pg_schema: As for the constructor.
            **kwargs: Further constructor arguments.

        Raises:
            RuntimeError: If the variable is not set.
        """
        url = os.environ.get(DATABASE_URL_ENV_VAR)
        if url is None:
            raise RuntimeError(f"{DATABASE_URL_ENV_VAR} is not set")
        return cls(url, pg_schema=pg_schema, **kwargs)

    @asynccontextmanager
    async def transaction(self, *, snapshot: bool = False) -> AsyncIterator[Transaction]:
        """One transaction on a pooled connection: committed when the block exits normally (then
        its :meth:`Transaction.on_commit` callbacks run), rolled back when it raises.

        Checking out the connection, the commit, and the rollback and return of the connection to
        the pool run shielded from cancellation, so no cancel leaves a connection checked out
        until the garbage collector finds it (whose late failure, once the connection is gone,
        surfaces in whatever task runs next). A task cancelled while opening the transaction
        finishes opening it, then rolls back and gives the connection back before the block
        runs. One cancelled inside the block still rolls back and gives its connection back. One
        cancelled while committing still finishes the commit, so its callbacks run and the state
        it wrote is known.

        Args:
            snapshot: Run at ``REPEATABLE READ`` instead of the default ``READ COMMITTED``, so
                every statement sees the same snapshot. For readers that compose several queries
                into one view of the state (a job's status); under ``READ COMMITTED`` each
                statement sees its own snapshot, and a commit between two of them shows an
                impossible combination. Writers use the default: the locking model in the
                execution module's docstring is built on ``READ COMMITTED``.
        """
        begin = self.engine.begin()
        with anyio.CancelScope(shield=True):
            entered = await begin.__aenter__()
        # A shielded scope is never cancelled, so its exit never swallows an exception; pyright
        # cannot tell, and would call a name bound inside it possibly unbound.
        connection: AsyncConnection = entered  # pyright: ignore[reportPossiblyUnboundVariable]
        try:
            await anyio.lowlevel.checkpoint_if_cancelled()
            if snapshot:
                await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            tx = Transaction(connection, self)
            yield tx
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                await begin.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        with anyio.CancelScope(shield=True):
            await begin.__aexit__(None, None, None)
        tx._committed()

    async def create_schema(self) -> None:
        """Create the Postgres schema (if one was named) and every table registered on
        :data:`~fxtr.runner.postgres.schema.metadata` that does not exist yet."""
        async with self.transaction() as tx:
            if self.pg_schema is not None:
                await tx.execute(text(f"CREATE SCHEMA IF NOT EXISTS {self.pg_schema}"))
            await tx.connection.run_sync(metadata.create_all)

    async def drop_schema(self, *, lock_timeout: str = "10s") -> None:
        """Drop everything: the named Postgres schema with all its contents, or, with no schema
        named, every table registered on :data:`~fxtr.runner.postgres.schema.metadata`.

        Dropping needs an exclusive lock on every table, which any connection with an open
        transaction on them blocks, and Postgres would wait for it indefinitely. So this first
        closes this instance's idle pooled connections, then terminates every *other* backend
        working in the schema (by the ``application_name`` the constructor sets: a leftover
        transaction from an interrupted task, a client that was never closed), and runs the drop
        under ``lock_timeout``, so it fails loudly instead of hanging if something still holds on.

        Args:
            lock_timeout: How long the drop waits for its locks, as a Postgres interval string.

        Raises:
            sqlalchemy.exc.OperationalError: If the locks were not available within
                ``lock_timeout``.
        """
        await self.dispose()
        async with self.transaction() as tx:
            if self.pg_schema is not None:
                await tx.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND application_name = :app"
                        " AND pid <> pg_backend_pid()"
                    ),
                    {"app": _application_name(self.pg_schema)},
                )
                await tx.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
                await tx.execute(text(f"DROP SCHEMA IF EXISTS {self.pg_schema} CASCADE"))
            else:
                await tx.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
                await tx.connection.run_sync(metadata.drop_all)

    async def dispose(self) -> None:
        """Close every pooled connection."""
        await self.engine.dispose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.dispose()
