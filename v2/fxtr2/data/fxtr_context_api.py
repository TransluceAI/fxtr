"""User-facing API for working with the fxtr context.

Includes:
- Namespaces: partitions of a backing store that can be used to store entities. The Namespace API
wraps the lower level abstract PersistentPinningBackend interface, and assumes that all operations
should occur in the active arena from fxtr_context.
- Scoped sub-contexts: independent contexts where operations can be executed in a clone of the
  parent context, and can return only a subset of entities to their parent.
"""

from __future__ import annotations

import abc
import contextlib
import pathlib
import typing
from typing import Any, AsyncGenerator, Mapping, Self

import anyio

from ..leases.lease_bridges import IndependentLocalBridge
from ..leases.lease_types import Lease
from . import entity_ref_scope
from .abstract_arena import PersistentPinningBackend
from .entity_core import FxtrID
from .sqlite_store import sqlite_namespace
from .store_indexing.indexers import WellKnownBuiltinIndexer
from .store_indexing.indexing_types import (
    BindingConstraint,
    IndexablePinningBackend,
    Indexer,
    IndexerColumnValue,
    IndexerConstraint,
    IndexerConstraintByID,
    ParsedView,
    ParsedViewColumn,
    QueryView,
)
from .store_indexing.sql_expr import parse_condition, validate_user_query
from .tsv_namespace import TsvNamespaceBackend


class FxtrNamespace(abc.ABC):
    """A namespace is a partition of a backing store that can be used to store entities.

    Each namespace is associated with its own fxtr scope, which controls the lifetime of all
    entities retrieved from it.
    """

    @property
    def scope(self) -> entity_ref_scope.FxtrScope:
        """Get the scope for this namespace."""
        return self._get_scope()

    @abc.abstractmethod
    def _get_scope(self) -> entity_ref_scope.FxtrScope:
        """Get the scope for this namespace.

        Important internal invariant: If the backend does not implement ``DeepFetchable``,
        the returned scope must be marked as not-deeply-fetchable. If the backend _does_
        implement ``DeepFetchable``, the namespace is expected to register it on the
        scope via ``scope.register_deep_fetch_source(backend, _backend_lease())``.
        """
        raise NotImplementedError("Subclasses must override this method")

    @abc.abstractmethod
    def _get_backend(self) -> PersistentPinningBackend[Any]:
        """Get the backend for this namespace."""
        raise NotImplementedError("Subclasses must override this method")

    @abc.abstractmethod
    def _backend_lease(self) -> Lease[Any]:
        """Produce a fresh clone of the backend's lifetime lease.

        Called by every backend-invoking method on this namespace. The clone is consumed
        on the backend side; callers that need to retain access obtain a fresh clone
        each time. Typically backed by ``session.root_lease.clone()``.
        """
        raise NotImplementedError("Subclasses must override this method")

    def _resolve_into_scope(
        self,
        into_scope: entity_ref_scope.FxtrScope | None,
        copy_into_scope: bool,
        method_name: str,
    ) -> entity_ref_scope.FxtrScope:
        """Resolve and validate the target scope for output operations.

        Returns the resolved scope. Raises ValueError with a helpful message
        (including method_name) if scope inclusion checks fail.
        """
        return entity_ref_scope.resolve_retrieve_into_scope(
            self.scope,
            into_scope,
            copy_into_scope,
            type_name=type(self).__name__,
            method_name=method_name,
        )

    async def pin(
        self,
        key: str,
        entity: entity_ref_scope.Entity | FxtrID | entity_ref_scope.EntityRef,
        *,
        overwrite: bool = False,
        scope: entity_ref_scope.FxtrScope | None = None,
    ) -> None:
        """Pin an entity into the namespace with a given key.

        Args:
            key: The key to pin the entity under.
            entity: The entity to pin, or a reference/ID to that entity. Must already be saved into
                the active (memory) arena.
            overwrite: If False (the default), raises an error if a pin with the given key
                already exists. If True, replaces the existing pin with the new entity.
            scope: The scope to retrieve the entity from. If None, the active scope will be used.

        Raises:
            KeyError: If a pin with the given key already exists and overwrite is False.
        """
        if scope is None:
            scope = entity_ref_scope.get_active_scope()

        match entity:
            case entity_ref_scope.Entity():
                entity_id = entity.store(scope=scope).entity_id
            case entity_ref_scope.EntityRef():
                entity_id = entity.entity_id
            case FxtrID():
                entity_id = entity
            case _:
                raise ValueError(f"Invalid entity type: {type(entity)}")

        try:
            scope.lift(entity_id)
        except ValueError as exc:
            raise ValueError(
                f"Could not find entity {entity_id} in scope {scope}. Either run `pin` in a scope"
                " that includes the entity to pin, or pass the explicit `scope` parameter."
            ) from exc

        await self._get_backend().pin_entity_from_arena(
            key,
            entity_id,
            scope._main_arena,
            scope._current_auth_lease().clone(),
            overwrite=overwrite,
        )

    async def unpin(self, key: str) -> None:
        """Unpin an entity from the namespace with a given key.

        Args:
            key: The key to unpin the entity from.
        """
        await self._get_backend().unpin_entity(key)

    @typing.overload
    async def retrieve(
        self,
        key: str,
        type: None = None,
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> FxtrID: ...

    @typing.overload
    async def retrieve[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T],
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> entity_ref_scope.EntityRef[T]: ...

    async def retrieve[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T] | None = None,
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> (FxtrID | entity_ref_scope.EntityRef[T]):
        """Retrieve an entity from the namespace with a given key.

        Args:
            key: The key to retrieve the entity from.
            type: The expected type of the entity, which will be used to deserialize it. If None,
                the entity will be returned as a generic FxtrID.
            into_scope: The scope to retrieve the entity into. If None, the active scope will be
                used.
            copy_into_scope: If True, the entity will be copied into the given scope. If False,
                the entity will be _lifted_ into the given scope, which requires that the given
                scope be a child of the namespace's scope.

        Returns:
            If type is provided, returns an EntityRef[type] that deserializes to the given type when
            loaded. Otherwise, returns the raw FxtrID of the entity. The entity is always bound
            to the namespace's own scope.
        """
        res = await self._retrieve_impl(
            key, type, into_scope, copy_into_scope, allow_not_found=False
        )
        assert res is not None
        return res

    @typing.overload
    async def try_retrieve(
        self,
        key: str,
        type: None = None,
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> FxtrID | None: ...

    @typing.overload
    async def try_retrieve[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T],
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> (entity_ref_scope.EntityRef[T] | None): ...

    async def try_retrieve[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T] | None = None,
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> (FxtrID | entity_ref_scope.EntityRef[T] | None):
        """Retrieve an entity from the namespace with a given key, or None if not found.

        Args:
            key: The key to retrieve the entity from.
            type: The expected type of the entity, which will be used to deserialize it. If None,
                the entity will be returned as a generic FxtrID.
            into_scope: The scope to retrieve the entity into. If None, the active scope will be
                used.
            copy_into_scope: If True, the entity will be copied into the given scope. If False,
                the entity will be _lifted_ into the given scope, which requires that the given
                scope be a child of the namespace's scope.

        Returns:
            If type is provided, returns an EntityRef[type] that deserializes to the given type when
            loaded. Otherwise, returns the raw FxtrID of the entity. The entity is always bound
            to the namespace's own scope. Returns None if the entity is not found.
        """
        return await self._retrieve_impl(
            key, type, into_scope, copy_into_scope, allow_not_found=True
        )

    async def _retrieve_impl[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T] | None,
        into_scope: entity_ref_scope.FxtrScope | None,
        copy_into_scope: bool,
        allow_not_found: bool,
    ) -> (FxtrID | entity_ref_scope.EntityRef[T] | None):
        method_name = "try_retrieve" if allow_not_found else "retrieve"
        into_scope = self._resolve_into_scope(into_scope, copy_into_scope, method_name)

        try:
            new_id, source_arena = await self._get_backend().retrieve_pinned(key)
        except KeyError:
            if allow_not_found:
                return None
            raise

        my_scope = self.scope
        try:
            # Safe attach: the namespace's backend lease is a Lease[Y]
            # directly (case 1 in lifetime_conversion_mechanics.md), so we
            # wrap it in an IndependentLocalBridge.
            my_scope._main_arena.lazily_attach_from_arena(
                my_scope._current_auth_lease().clone(),
                new_id,
                source_arena,
                IndependentLocalBridge(lease=self._backend_lease()),
            )
        finally:
            source_arena.close()
        if copy_into_scope:
            await into_scope.copy_from_scope(new_id, source_scope=my_scope)
        else:
            into_scope.lift(new_id)
        if type is None:
            return new_id
        else:
            return entity_ref_scope.EntityRef(new_id, type)

    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        """List all pins in the namespace, optionally with a given prefix.

        Args:
            prefix: Only return pins whose keys start with this prefix. Empty string (default)
                returns all pins.
        """
        return await self._get_backend().list_pins(prefix)


class FxtrQueryableNamespace(FxtrNamespace):
    """A namespace that supports indexing and querying pinned entities.

    Extends FxtrNamespace with register_indexer, unregister_indexer, list_indexers,
    and iter_matches. Subclasses must override _get_backend() to return an
    IndexablePinningBackend.
    """

    @abc.abstractmethod
    def _get_backend(self) -> IndexablePinningBackend[Any]:
        """Get the backend for this namespace."""
        raise NotImplementedError("Subclasses must override this method")

    def _resolve_entity_id(
        self,
        indexer: Indexer | FxtrID | entity_ref_scope.EntityRef[Indexer],
        scope: entity_ref_scope.FxtrScope,
    ) -> FxtrID:
        """Resolve an indexer input to an entity ID, storing into scope if needed."""
        match indexer:
            case entity_ref_scope.Entity():
                return indexer.store(scope=scope).entity_id
            case entity_ref_scope.EntityRef():
                return indexer.entity_id
            case FxtrID():
                return indexer
            case _:
                raise ValueError(f"Invalid indexer type: {type(indexer)}")

    async def register_indexer(
        self,
        indexer: Indexer | FxtrID | entity_ref_scope.EntityRef[Indexer],
        key_prefix: str = "",
        *,
        scope: entity_ref_scope.FxtrScope | None = None,
    ) -> None:
        """Register an indexer on this namespace at a given key prefix.

        The indexer must be registered before it can be used in queries. Registration
        builds the index for all existing pins matching the key prefix.

        Args:
            indexer: The indexer to register. Can be an Indexer object, an EntityRef,
                or a raw FxtrID. If an Indexer object is given, it will be stored into
                the scope to compute its content-addressed ID.
            key_prefix: Only index entities pinned under keys starting with this prefix.
                Empty string (default) indexes all entities.
            scope: The scope containing the indexer entity. If None, the active scope
                will be used.
        """
        if scope is None:
            scope = entity_ref_scope.get_active_scope()
        entity_id = self._resolve_entity_id(indexer, scope)
        try:
            scope.lift(entity_id)
        except ValueError as exc:
            raise ValueError(
                f"Could not find entity {entity_id} in scope {scope}. Either run `pin` in a scope"
                " that includes the entity to pin, or pass the explicit `scope` parameter."
            ) from exc
        await self._get_backend().register_indexer(
            entity_id,
            scope._main_arena,
            scope._current_auth_lease().clone(),
            key_prefix,
        )

    async def unregister_indexer(
        self,
        indexer: Indexer | FxtrID | entity_ref_scope.EntityRef,
        key_prefix: str = "",
        *,
        scope: entity_ref_scope.FxtrScope | None = None,
    ) -> None:
        """Unregister an indexer from this namespace.

        Marks the registration for cleanup. The index data is not deleted inline
        but will be cleaned up by GC.

        Args:
            indexer: The indexer to unregister. Can be an Indexer object, an EntityRef,
                or a raw FxtrID.
            key_prefix: The key prefix to unregister. Must exactly match the prefix
                used during registration.
            scope: The scope to use for resolving the indexer if an Indexer object
                is given (needs to be stored to compute its ID). Ignored for FxtrID
                and EntityRef inputs. If None, the active scope will be used.
        """
        match indexer:
            case entity_ref_scope.Entity():
                if scope is None:
                    scope = entity_ref_scope.get_active_scope()
                entity_id = indexer.store(scope=scope).entity_id
            case entity_ref_scope.EntityRef():
                entity_id = indexer.entity_id
            case FxtrID():
                entity_id = indexer
            case _:
                raise ValueError(f"Invalid indexer type: {type(indexer)}")
        await self._get_backend().unregister_indexer(entity_id, key_prefix)

    async def list_indexers(
        self,
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> list[tuple[entity_ref_scope.EntityRef[WellKnownBuiltinIndexer], str]]:
        """List all registered (INDEXED) indexers in this namespace.

        Args:
            into_scope: The scope to deliver indexer entities into. If None, the
                active scope will be used.
            copy_into_scope: If True, entities are copied into the target scope.
                If False, entities are lifted (requires scope inclusion).

        Returns:
            A list of (indexer_ref, key_prefix) tuples. Each indexer_ref is an
            EntityRef that can be loaded to get the concrete indexer entity.
        """
        into_scope = self._resolve_into_scope(into_scope, copy_into_scope, "list_indexers")

        registrations, source_arena = await self._get_backend().list_indexers()
        my_scope = self.scope
        try:
            for fxtr_id, _prefix in registrations:
                my_scope._main_arena.lazily_attach_from_arena(
                    my_scope._current_auth_lease().clone(),
                    fxtr_id,
                    source_arena,
                    IndependentLocalBridge(lease=self._backend_lease()),
                )
        finally:
            source_arena.close()

        result: list[tuple[entity_ref_scope.EntityRef[WellKnownBuiltinIndexer], str]] = []
        for fxtr_id, prefix in registrations:
            if copy_into_scope:
                await into_scope.copy_from_scope(fxtr_id, source_scope=my_scope)
            else:
                into_scope.lift(fxtr_id)
            result.append((entity_ref_scope.EntityRef(fxtr_id, WellKnownBuiltinIndexer), prefix))
        return result

    async def iter_matches(
        self,
        first: IndexerConstraint,
        /,
        *rest: IndexerConstraint,
        key_prefix: str = "",
        where: BindingConstraint | None = None,
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> AsyncGenerator[tuple[str, FxtrID, Mapping[str, IndexerColumnValue]], None]:
        """Iterate over all matches for a given set of indexer constraints.

        Args:
            first: The first indexer constraint.
            rest: Additional indexer constraints. Shared variable names are unified
                (joined) across constraints.
            key_prefix: Restrict matches to pins with keys starting with this prefix. This key
                prefix must be covered by a registration for each indexer used in an indexer
                constraint.
            where: An optional global condition on binding variables.
            into_scope: The scope to deliver matched entities into. If None, the
                active scope will be used. Also used to resolve indexer entities if needed.
            copy_into_scope: If True, entities are copied into the target scope.
                If False, entities are lifted (requires scope inclusion).

        Yields:
            (key, entity_id, bindings) tuples where key is the pin key, entity_id
            is the matched entity's FxtrID, and bindings maps variable names to values.
        """
        into_scope = self._resolve_into_scope(into_scope, copy_into_scope, "iter_matches")

        # Convert IndexerConstraint → IndexerConstraintByID
        input_scope = entity_ref_scope.get_active_scope()

        def _to_by_id(c: IndexerConstraint) -> IndexerConstraintByID:
            entity_id = self._resolve_entity_id(c.indexer, input_scope)
            return IndexerConstraintByID(
                indexer_id=entity_id,
                key_variables=c.key_variables,
                condition=c.condition,
            )

        first_by_id = _to_by_id(first)
        rest_by_id = tuple(_to_by_id(c) for c in rest)

        my_scope = self.scope
        async for key, entity_id, bindings, arena in self._get_backend().iter_matches(
            first_by_id,
            *rest_by_id,
            key_prefix=key_prefix,
            where=where,
        ):
            try:
                my_scope._main_arena.lazily_attach_from_arena(
                    my_scope._current_auth_lease().clone(),
                    entity_id,
                    arena,
                    IndependentLocalBridge(lease=self._backend_lease()),
                )
            finally:
                arena.close()
            if copy_into_scope:
                await into_scope.copy_from_scope(entity_id, source_scope=my_scope)
            else:
                into_scope.lift(entity_id)
            yield (key, entity_id, bindings)

    async def sql(
        self,
        query: str,
        *,
        views: dict[str, QueryView],
        dialect: str = "sqlite",
    ) -> list:
        """Run an arbitrary SQL query against named index views.

        Each entry in *views* defines a virtual table (CTE) composed of one or more
        indexer constraints joined on shared variables. The user's *query* can
        reference these tables freely.

        Results are returned as a list of ``sqlite3.Row`` objects (supporting both
        index and key-based access). No entity resolution or scope management is
        performed — this is a low-level analytical interface. (Return format may change for new
        dialects in the future.)

        Args:
            query: The SQL query to execute. Substituted verbatim after the
                generated ``WITH`` clause. Should reference the view names defined
                in *views*.
            views: A mapping of view name → ``QueryView``. Each view becomes a
                CTE in the generated query.
            dialect: The SQL dialect. Currently only ``"sqlite"`` is supported.

        Returns:
            All result rows as ``sqlite3.Row`` objects.

        Raises:
            ValueError: If dialect is not ``"sqlite"`` or views is empty.
            KeyError: If an indexer in a view is not registered at a prefix covering
                the view's prefix.
        """
        if dialect != "sqlite":
            raise ValueError(f"Unsupported dialect: {dialect!r} (only 'sqlite' is supported)")
        if not views:
            raise ValueError("At least one view is required")

        # Lightweight syntax check — catches obvious errors before hitting SQLite
        validate_user_query(query)

        input_scope = entity_ref_scope.get_active_scope()

        # Parse each view: resolve indexer entities to FxtrIDs and rewrite conditions.
        # Table resolution, query generation, and execution are handled by the backend.
        parsed_views: list[ParsedView] = []
        for view_name, view in views.items():
            parsed_columns: list[ParsedViewColumn] = []
            for col_name, constraint in view.columns.items():
                indexer_id = self._resolve_entity_id(constraint.indexer, input_scope)
                rewritten = parse_condition(
                    constraint.condition,
                    frozenset(constraint.key_variables),
                    allow_value=True,
                )
                parsed_columns.append(
                    ParsedViewColumn(
                        column_name=col_name,
                        indexer_id=indexer_id,
                        key_variables=constraint.key_variables,
                        condition=rewritten,
                    )
                )
            parsed_views.append(
                ParsedView(
                    view_name=view_name,
                    prefix=view.prefix,
                    columns=parsed_columns,
                )
            )

        return await self._get_backend().run_direct_sql(query, parsed_views)


class SQLiteFxtrNamespace(FxtrQueryableNamespace, anyio.AsyncContextManagerMixin):
    """A namespace that is backed by a SQLite database."""

    def __init__(
        self,
        url: str,
        namespace_name: str,
        *,
        isolate_scope: bool = False,
        max_segment_size: int | None = None,
        read_only: bool = False,
    ):
        """Initialize a new SQLiteFxtrNamespace.

        Args:
            url: The URL of the SQLite database. Can be a SQLAlchemy URL
                (e.g., "sqlite+aiosqlite:///path/to/db.sqlite") or a plain
                SQLite path (e.g., "/path/to/db.sqlite" or "db.sqlite").
            namespace_name: The name of the namespace.
            isolate_scope: If True, the scope will be isolated from the active scope.
            max_segment_size: Soft limit in bytes for segment file rotation. When the active
                segment exceeds this size after a commit, it is sealed and a new segment is
                created. Defaults to 512 MB if not specified.
            read_only: If True, open the store in read-only mode. No writes will be
                performed. Pin, unpin, register_indexer, and unregister_indexer will
                raise RuntimeError. The session is invisible to other processes.
        """
        # Convert SQLAlchemy URL to plain SQLite path if needed
        if url.startswith("sqlite+aiosqlite:///"):
            # Extract path from SQLAlchemy URL
            self._db_path = url.replace("sqlite+aiosqlite:///", "")
        elif url.startswith("sqlite:///"):
            self._db_path = url.replace("sqlite:///", "")
        else:
            # Assume it's already a plain path
            self._db_path = url

        self._namespace_name = namespace_name
        self._max_segment_size = max_segment_size
        self._read_only = read_only
        self._session: sqlite_namespace.SQLiteFxtrSession[Any] | None = None
        self._backend: sqlite_namespace.SQLiteNamespaceBackend[Any] | None = None
        self._scope: entity_ref_scope.FxtrScope | None = None
        self._isolate_scope = isolate_scope

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        async with sqlite_namespace.SQLiteFxtrSession.open(
            self._db_path, max_segment_size=self._max_segment_size, read_only=self._read_only
        ) as session:
            ns = await session.create_or_retrieve_user_namespace(self._namespace_name)
            self._session = session
            self._backend = ns
            try:
                self._scope = entity_ref_scope.FxtrScope(isolated=self._isolate_scope)
                async with self._scope:
                    # Register backend as a deep-fetch source on our scope (default
                    # converter — the key lease is session-branded and the source
                    # arena produced by try_lookup also carries the session brand).
                    self._scope.register_deep_fetch_source(
                        self._backend,
                        IndependentLocalBridge(lease=session.root_lease.clone()),
                    )
                    yield self
            finally:
                self._backend = None
                self._session = None

    def _get_scope(self) -> entity_ref_scope.FxtrScope:
        if self._scope is None:
            raise RuntimeError("SQLiteFxtrNamespace must be used as a context manager!")
        return self._scope

    def _get_backend(self) -> sqlite_namespace.SQLiteNamespaceBackend[Any]:
        if self._backend is None:
            raise RuntimeError("SQLiteFxtrNamespace must be used as a context manager!")
        return self._backend

    def _backend_lease(self) -> Lease[Any]:
        if self._session is None:
            raise RuntimeError("SQLiteFxtrNamespace must be used as a context manager!")
        return self._session.root_lease.clone()


class TsvFxtrNamespace(FxtrQueryableNamespace, anyio.AsyncContextManagerMixin):
    """A TSV-file fxtr namespace: pin mappings live in git-tracked TSV files,
    backed by a repo-level entity store (SQLite)."""

    def __init__(
        self,
        namespace_root: pathlib.Path,
        *,
        auto_flush_seconds: float | None = 10.0,
        full_sync_staleness_threshold: float | None = 5.0,
        isolate_scope: bool = False,
    ):
        """Initialize a new TsvFxtrNamespace.

        Args:
            namespace_root: The root directory of the tsv namespace (containing
                fxtr_namespace.toml, inside a git repository).
            auto_flush_seconds: The interval at which to flush dirty TSVs to disk.
                Set to None to disable auto-flush. Default: 10.0
            full_sync_staleness_threshold: For list_pins(), try_lookup(), and
                backrefs(), skip full sync if one was performed within this many
                seconds. Set to None to always sync, or 0 to never auto-sync.
                Default: 5.0
            isolate_scope: If True, the scope will be isolated from the active scope.
        """
        self._namespace_root = namespace_root
        self._auto_flush_seconds = auto_flush_seconds
        self._full_sync_staleness_threshold = full_sync_staleness_threshold
        self._backend: TsvNamespaceBackend | None = None
        self._session: sqlite_namespace.SQLiteFxtrSession[Any] | None = None
        self._scope: entity_ref_scope.FxtrScope | None = None
        self._isolate_scope = isolate_scope

    async def create(
        self,
        name: str | None = None,
        description: str | None = None,
        reserved_prefix: str = "index",
    ) -> Self:
        """Create a new TsvFxtrNamespace at the given store root.

        Args:
            name: The name of the namespace.
            description: The description of the namespace.
            reserved_prefix: The reserved prefix for keys without colon.
        """
        await TsvNamespaceBackend.init(
            self._namespace_root,
            name=name,
            description=description,
            reserved_prefix=reserved_prefix,
        )
        return self

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        async with TsvNamespaceBackend.open(
            self._namespace_root,
            auto_flush_seconds=self._auto_flush_seconds,
            full_sync_staleness_threshold=self._full_sync_staleness_threshold,
        ) as backend:
            self._backend = backend
            self._session = backend._session
            try:
                self._scope = entity_ref_scope.FxtrScope(isolated=self._isolate_scope)
                async with self._scope:
                    self._scope.register_deep_fetch_source(
                        backend,
                        IndependentLocalBridge(lease=self._session.root_lease.clone()),
                    )
                    yield self
            finally:
                self._backend = None
                self._session = None

    def _get_scope(self) -> entity_ref_scope.FxtrScope:
        if self._scope is None:
            raise RuntimeError("TsvFxtrNamespace must be used as a context manager!")
        return self._scope

    def _get_backend(self) -> TsvNamespaceBackend:
        if self._backend is None:
            raise RuntimeError("TsvFxtrNamespace must be used as a context manager!")
        return self._backend

    def _backend_lease(self) -> Lease[Any]:
        if self._session is None:
            raise RuntimeError("TsvFxtrNamespace must be used as a context manager!")
        return self._session.root_lease.clone()
