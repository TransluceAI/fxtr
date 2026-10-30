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
from typing import Any, AsyncGenerator, Self

import anyio

from . import entity_serdes, fxtr_scope
from .abstract_arena import PersistentPinningBackend
from .entity_core import FxtrID
from .git_store import GitNamespaceBackend
from .sqlite_store import sqlite_namespace


class FxtrNamespace(abc.ABC):
    """A namespace is a partition of a backing store that can be used to store entities.

    Each namespace is associated with its own fxtr scope, which controls the lifetime of all
    entities retrieved from it.
    """

    @property
    def scope(self) -> fxtr_scope.FxtrScope:
        """Get the scope for this namespace."""
        return self._get_scope()

    @abc.abstractmethod
    def _get_scope(self) -> fxtr_scope.FxtrScope:
        """Get the scope for this namespace."""
        raise NotImplementedError("Subclasses must override this method")

    @abc.abstractmethod
    def _get_backend(self) -> PersistentPinningBackend:
        """Get the backend for this namespace."""
        raise NotImplementedError("Subclasses must override this method")

    async def pin(
        self,
        key: str,
        entity: entity_serdes.Entity | FxtrID | entity_serdes.EntityRef,
        *,
        overwrite: bool = False,
        scope: fxtr_scope.FxtrScope | None = None,
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
            scope = fxtr_scope.ACTIVE_SCOPE.get()

        match entity:
            case entity_serdes.Entity():
                entity_id = entity_serdes.store_entity(entity, scope=scope).ref
            case FxtrID():
                entity_id = entity
            case entity_serdes.EntityRef():
                entity_id = entity.ref
            case _:
                raise ValueError(f"Invalid entity type: {type(entity)}")

        if not scope.is_tracked(entity_id):
            raise ValueError(f"Entity {entity_id} is not tracked in scope {scope}!")
        await self._get_backend().pin_entity_from_arena(
            key, entity_id, scope._main_arena, overwrite=overwrite
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
        into_scope: fxtr_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> FxtrID: ...

    @typing.overload
    async def retrieve[
        T: entity_serdes.Entity
    ](
        self,
        key: str,
        type: type[T],
        into_scope: fxtr_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> entity_serdes.EntityRef[T]: ...

    async def retrieve[
        T: entity_serdes.Entity
    ](
        self,
        key: str,
        type: type[T] | None = None,
        into_scope: fxtr_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> (FxtrID | entity_serdes.EntityRef[T]):
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
        into_scope: fxtr_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> FxtrID | None: ...

    @typing.overload
    async def try_retrieve[
        T: entity_serdes.Entity
    ](
        self,
        key: str,
        type: type[T],
        into_scope: fxtr_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> (entity_serdes.EntityRef[T] | None): ...

    async def try_retrieve[
        T: entity_serdes.Entity
    ](
        self,
        key: str,
        type: type[T] | None = None,
        into_scope: fxtr_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> (FxtrID | entity_serdes.EntityRef[T] | None):
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
        T: entity_serdes.Entity
    ](
        self,
        key: str,
        type: type[T] | None,
        into_scope: fxtr_scope.FxtrScope | None,
        copy_into_scope: bool,
        allow_not_found: bool,
    ) -> (FxtrID | entity_serdes.EntityRef[T] | None):
        if into_scope is None:
            if not copy_into_scope and not self.scope.included_in_active_scope():
                raise ValueError(
                    f"Active scope is not a child of scope for namespace {self}; cannot retrieve into it without copying"
                )
            into_scope = fxtr_scope.ACTIVE_SCOPE.get()

        else:
            if not copy_into_scope and not self.scope.included_in_scope(into_scope):
                raise ValueError(
                    f"Given scope {into_scope} is not a child of scope for namespace {self}; cannot retrieve into it without copying"
                )

        try:
            new_id, source_arena = await self._get_backend().retrieve_pinned(key)
        except KeyError:
            if allow_not_found:
                return None
            raise

        my_scope = self.scope
        try:
            my_scope._main_arena.unsafe_lazily_attach_from_arena(new_id, source_arena)
        finally:
            source_arena.close()
        if copy_into_scope:
            await into_scope.copy_from_scope(new_id)
        else:
            into_scope.lift(new_id)
        if type is None:
            return new_id
        else:
            return entity_serdes.EntityRef[type](new_id, entity_serdes.build_converter_for(type))

    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        """List all pins in the namespace, optionally with a given prefix.

        Args:
            prefix: Only return pins whose keys start with this prefix. Empty string (default)
                returns all pins.
        """
        return await self._get_backend().list_pins(prefix)


class SQLiteFxtrNamespace(FxtrNamespace, anyio.AsyncContextManagerMixin):
    """A namespace that is backed by a SQLite database."""

    def __init__(
        self,
        url: str,
        namespace_name: str,
        *,
        db_kwargs: dict[str, Any] | None = None,
        isolate_scope: bool = False,
    ):
        """Initialize a new SQLiteFxtrNamespace.

        Args:
            url: The URL of the SQLite database. Can be a SQLAlchemy URL
                (e.g., "sqlite+aiosqlite:///path/to/db.sqlite") or a plain
                SQLite path (e.g., "/path/to/db.sqlite" or "db.sqlite").
            namespace_name: The name of the namespace.
            db_kwargs: Deprecated, no longer used. Kept for backward compatibility.
            isolate_scope: If True, the scope will be isolated from the active scope.
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
        if db_kwargs:
            import warnings

            warnings.warn(
                "db_kwargs parameter is deprecated and no longer used with sqlite_store backend",
                DeprecationWarning,
                stacklevel=2,
            )
        self._backend: sqlite_namespace.SQLiteNamespaceBackend | None = None
        self._scope = fxtr_scope.FxtrScope(isolated=isolate_scope)

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        async with sqlite_namespace.SQLiteFxtrSession.open(self._db_path) as session:
            ns = await session.create_or_retrieve_user_namespace(self._namespace_name)
            self._backend = ns
            try:
                async with self._scope:
                    yield self
            finally:
                self._backend = None

    def _get_scope(self) -> fxtr_scope.FxtrScope:
        return self._scope

    def _get_backend(self) -> sqlite_namespace.SQLiteNamespaceBackend:
        if self._backend is None:
            raise RuntimeError("SQLiteFxtrNamespace must be used as a context manager!")
        return self._backend


class GitFxtrNamespace(FxtrNamespace, anyio.AsyncContextManagerMixin):
    """A namespace that is backed by a Git repository."""

    def __init__(
        self,
        store_root: pathlib.Path,
        *,
        auto_flush_seconds: float | None = 10.0,
        full_sync_staleness_threshold: float | None = 5.0,
        isolate_scope: bool = False,
    ):
        """Initialize a new GitFxtrNamespace.

        Args:
            store_root: The root of the Git repository.
            auto_flush_seconds: The interval at which to flush dirty TSVs to disk.
                Set to None to disable auto-flush. Default: 10.0
            full_sync_staleness_threshold: For list_pins(), try_lookup(), and
                backrefs(), skip full sync if one was performed within this many
                seconds. Set to None to always sync, or 0 to never auto-sync.
                Default: 5.0
            isolate_scope: If True, the scope will be isolated from the active scope.
        """
        self._store_root = store_root
        self._auto_flush_seconds = auto_flush_seconds
        self._full_sync_staleness_threshold = full_sync_staleness_threshold
        self._git_namespace: GitNamespaceBackend | None = None
        self._scope = fxtr_scope.FxtrScope(isolated=isolate_scope)

    async def create(
        self,
        name: str | None = None,
        description: str | None = None,
        reserved_prefix: str = "index",
    ) -> Self:
        """Create a new GitFxtrNamespace at the given store root.

        Args:
            name: The name of the namespace.
            description: The description of the namespace.
            reserved_prefix: The reserved prefix for keys without colon.
        """
        await GitNamespaceBackend.init(
            self._store_root, name=name, description=description, reserved_prefix=reserved_prefix
        )
        return self

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        async with GitNamespaceBackend.open(
            self._store_root,
            auto_flush_seconds=self._auto_flush_seconds,
            full_sync_staleness_threshold=self._full_sync_staleness_threshold,
        ) as ns:
            self._backend = ns
            try:
                async with self._scope:
                    yield self
            finally:
                self._backend = None

    def _get_scope(self) -> fxtr_scope.FxtrScope:
        return self._scope

    def _get_backend(self) -> GitNamespaceBackend:
        if self._backend is None:
            raise RuntimeError("SQLiteFxtrNamespace must be used as a context manager!")
        return self._backend
