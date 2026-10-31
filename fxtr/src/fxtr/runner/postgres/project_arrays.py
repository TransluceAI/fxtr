"""Project arrays: the project's named, typed, versioned arrays, which the user edits and a launch
snapshots into a job's inputs.

A project array is the user's side of an experiment's data: the prompts, scenarios, or rubrics an
experiment runs over, kept in the database under a name so they can be filled and edited over
time from a script or notebook. The runner never reads one. The way an array reaches a job is a
**snapshot**: :meth:`ProjectArrays.snapshot` stores the current rows as a
:class:`~fxtr.entity_defns.array.Array` entity and returns its bound id, and a launch passes that
id to the job as a root input, so the job's record references the exact rows it ran on like any
other input and the job observes one version of the array by construction. Which array and version
an input came from is derivable from the snapshot records by cid; nothing else records it.

An array is declared once with a name and a :class:`~fxtr.entity_defns.array.ArrayType`
(:meth:`ProjectArrays.create_array`), then edited: :meth:`~ProjectArrays.upsert` merges an
in-memory :class:`~fxtr.entity_defns.array.Array` into it by key, :meth:`~ProjectArrays.replace`
makes it equal to one, and rows can be deleted by key. :meth:`~ProjectArrays.load` reads the
current rows back as an array of the *declared* type. Every mutation increments the array's
``version``, which later lets a snapshot be reused while nothing has changed.

The type an array is created with is the source of truth. An upserted array may have its dims in
any order (they are matched by name) and a schema assignable to the declared one (see
:func:`~fxtr.entity_defns.array.is_assignable_array`); its rows are stored, and later loaded,
under the declared type. Rows live in the ``project_array_rows`` table as JSON encoded by the
declared schema (see :mod:`fxtr.runner.postgres.json_values`).

Entity ids in an array's values must exist in the entity store: entities stored but not yet
flushed are flushed in the same transaction as the rows, before the references are checked
against the database, so persisting an array also persists what it references, atomically.
Conversely, the ids in a loaded array are guaranteed to exist, so :meth:`~ProjectArrays.load`
marks them known in the entity store.

A snapshot is recorded against the array's version in ``project_array_snapshots``. Taking a
snapshot when one already exists for the current version returns the existing one; identical
content at different versions is one entity (content addressing) under two records. Dropping an
array drops its rows and its snapshot records, not the snapshot entities, so a job that ran on one
keeps its input.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Row, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID, UnknownReferenceError
from fxtr.entity_defns.array import (
    Array,
    ArrayType,
    ArrayTypeSpec,
    Key,
    KeySpec,
    as_array_type,
    is_assignable_array,
)
from fxtr.project.client import ProjectClientError
from fxtr.runner.postgres.database import Database, Transaction
from fxtr.runner.postgres.entities import PostgresEntityStorage
from fxtr.runner.postgres.json_values import (
    array_type_from_json,
    array_type_to_json,
    key_from_json,
    key_to_json,
    value_from_json,
    value_to_json,
)
from fxtr.runner.postgres.schema import project_array_rows, project_array_snapshots, project_arrays


class ProjectArrayError(ProjectClientError, ValueError):
    """Base class for project array errors."""


class ProjectArrayNotFoundError(ProjectArrayError, KeyError):
    """No project array has the given name."""


class ProjectArrayExistsError(ProjectArrayError):
    """A project array with the given name exists with a different type."""


class ProjectArrayTypeError(ProjectArrayError):
    """An array does not fit the project array's declared type."""


class ProjectArrayEmptyError(ProjectArrayError):
    """A project array with no dims (a scalar) has no row, so it has no value to read."""


@dataclasses.dataclass(frozen=True)
class ProjectArrayInfo:
    """What the registry records about one project array.

    Attributes:
        name: The array's name.
        type: Its declared type.
        version: Incremented by every mutation of its rows; ``0`` when created.
        row_count: How many rows it currently holds.
        created_at: When it was created.
        updated_at: When it was last mutated (its creation time until then).
    """

    name: str
    type: ArrayType
    version: int
    row_count: int
    created_at: datetime
    updated_at: datetime


@dataclasses.dataclass(frozen=True)
class SnapshotInfo:
    """One snapshot of a project array.

    Attributes:
        array_name: The array.
        version: The array's version the snapshot captured.
        array: The snapshot entity, loadable as a :class:`~fxtr.entity_defns.array.Array`.
        created_at: When it was taken.
    """

    array_name: str
    version: int
    array: BoundID[Array[Any]]
    created_at: datetime


class ProjectArrays:
    """The project arrays of one database.

    Args:
        database: The database holding the ``project_arrays`` and ``project_array_rows`` tables.
        entities: The entity store the arrays' values may reference; flushed into every
            transaction that persists rows.
    """

    def __init__(self, database: Database, entities: PostgresEntityStorage) -> None:
        self._database = database
        self._entities = entities

    # ---- the registry ----

    async def create_array(self, name: str, type: ArrayTypeSpec) -> ProjectArrayInfo:
        """Declare a project array. A no-op if one of this name and type already exists.

        Args:
            name: The array's name, unique in the database.
            type: Its type (see :data:`~fxtr.entity_defns.array.ArrayTypeSpec`).

        Raises:
            ProjectArrayExistsError: If an array of this name exists with a different type.
        """
        ttype = as_array_type(type)
        async with self._database.transaction() as tx:
            await tx.execute(
                insert(project_arrays)
                .values(name=name, type=array_type_to_json(ttype))
                .on_conflict_do_nothing()
            )
            info = await _describe(tx, name)
        if info.type != ttype:
            raise ProjectArrayExistsError(
                f"Project array {name!r} exists with type {info.type}, not {ttype}"
            )
        return info

    async def describe(self, name: str) -> ProjectArrayInfo:
        """The registry's record of an array.

        Raises:
            ProjectArrayNotFoundError: If there is no such array.
        """
        async with self._database.transaction() as tx:
            return await _describe(tx, name)

    async def list_arrays(self) -> list[ProjectArrayInfo]:
        """Every project array, by name."""
        async with self._database.transaction() as tx:
            rows = (await tx.execute(_info_query().order_by(project_arrays.c.name))).all()
        return [_info_from_row(row) for row in rows]

    async def drop_array(self, name: str) -> None:
        """Delete an array, its rows, and its registry entry. Snapshots taken of it, being
        entities, remain.

        Raises:
            ProjectArrayNotFoundError: If there is no such array.
        """
        async with self._database.transaction() as tx:
            result = await tx.execute(delete(project_arrays).where(project_arrays.c.name == name))
            if result.rowcount == 0:
                raise ProjectArrayNotFoundError(name)

    # ---- rows ----

    async def upsert(self, name: str, array: Array[Any]) -> ProjectArrayInfo:
        """Merge an array's rows into a project array: new keys are added, existing keys take the
        new value, other rows are untouched.

        Args:
            name: The project array.
            array: The rows to merge; its type must fit the declared one (dims matched by name,
                schema assignable).

        Returns:
            The array's record after the change.

        Raises:
            ProjectArrayNotFoundError: If there is no such array.
            ProjectArrayTypeError: If the array's type does not fit.
            UnknownReferenceError: If a value references an entity that is not known.
        """
        return await self._write_rows(name, array, replace=False)

    async def replace(self, name: str, array: Array[Any]) -> ProjectArrayInfo:
        """Make a project array's rows equal to an array's, in one transaction. Arguments, result
        and errors as for :meth:`upsert`."""
        return await self._write_rows(name, array, replace=True)

    async def _write_rows(self, name: str, array: Array[Any], *, replace: bool) -> ProjectArrayInfo:
        async with self._database.transaction() as tx:
            declared = await _bump_version(tx, name)
            if not is_assignable_array(array.type, declared):
                raise ProjectArrayTypeError(
                    f"Array of type {array.type} does not fit project array {name!r} of type"
                    f" {declared}"
                )
            if array.dim_names != declared.dim_names:
                array = array.transpose(declared.dim_names)
            # Flushing first makes this process's pending entities visible to the reference
            # check, so the database alone decides whether every referenced entity exists.
            await self._entities.flush(tx)
            await self._check_references(tx, array)
            if replace:
                await tx.execute(
                    delete(project_array_rows).where(project_array_rows.c.array_name == name)
                )
            # A project array is one database row per array row, so the values become Python
            # here; iterating decodes an Arrow-built array a batch at a time and keeps none.
            rows = [
                {
                    "array_name": name,
                    "key": key_to_json(key),
                    "value": value_to_json(value, declared.schema, f"row {key}"),
                }
                for key, value in array.raw_items()
            ]
            if rows:
                statement = insert(project_array_rows).on_conflict_do_update(
                    index_elements=[project_array_rows.c.array_name, project_array_rows.c.key],
                    set_={"value": insert(project_array_rows).excluded.value},
                )
                await tx.execute(statement, rows)
            return await _describe(tx, name)

    async def _check_references(self, tx: Transaction, array: Array[Any]) -> None:
        ids = {EntityID(cid=cid) for cid in array.entity_cids()}
        known = await self._entities.confirm_known(ids, tx)
        if unknown := ids - known:
            raise UnknownReferenceError(
                f"The array references entities that are not known: {sorted(map(repr, unknown))}"
            )

    async def load(self, name: str) -> Array[Any]:
        """The current rows of a project array, as an array of its declared type. The entity ids
        it holds become known to the entity store.

        Raises:
            ProjectArrayNotFoundError: If there is no such array.
            ProjectArrayEmptyError: If the array is a scalar with no row.
        """
        async with self._database.transaction() as tx:
            declared = await _declared_type(tx, name)
            array = await _read_array(tx, name, declared)
        self._entities.assume_known(_array_ids(array))
        return array

    async def delete_rows(self, name: str, keys: Iterable[KeySpec]) -> int:
        """Delete rows by key. Keys that are not present are ignored.

        Args:
            name: The project array.
            keys: The keys, each as a tuple in declared dim order or a dim-name-to-value mapping.

        Returns:
            How many rows were deleted.

        Raises:
            ProjectArrayNotFoundError: If there is no such array.
        """
        async with self._database.transaction() as tx:
            declared = await _bump_version(tx, name)
            json_keys = [key_to_json(_key_tuple(key, declared)) for key in keys]
            if not json_keys:
                return 0
            result = await tx.execute(
                delete(project_array_rows).where(
                    project_array_rows.c.array_name == name,
                    project_array_rows.c.key.in_(json_keys),
                )
            )
            return result.rowcount

    async def clear(self, name: str) -> int:
        """Delete every row of a project array, keeping the array.

        Returns:
            How many rows were deleted.

        Raises:
            ProjectArrayNotFoundError: If there is no such array.
        """
        async with self._database.transaction() as tx:
            await _bump_version(tx, name)
            result = await tx.execute(
                delete(project_array_rows).where(project_array_rows.c.array_name == name)
            )
            return result.rowcount

    # ---- snapshots ----

    async def snapshot(self, name: str, tx: Transaction | None = None) -> SnapshotInfo:
        """Store the array's current rows as a :class:`~fxtr.entity_defns.array.Array` entity,
        recorded against the array's current version, or return the snapshot already recorded
        for that version.

        Args:
            name: The project array.
            tx: The transaction to work in, for callers recording the snapshot alongside their
                own writes; ``None`` opens one just for this snapshot.

        Raises:
            ProjectArrayNotFoundError: If there is no such array.
            ProjectArrayEmptyError: If the array is a scalar with no row.
        """
        if tx is None:
            async with self._database.transaction() as own:
                return await self.snapshot(name, own)
        # Sharing the registry row blocks mutations (which lock it exclusively) for the rest of
        # this transaction, so the version and the rows read below agree.
        row = (
            await tx.execute(
                select(project_arrays.c.version, project_arrays.c.type)
                .where(project_arrays.c.name == name)
                .with_for_update(read=True)
            )
        ).first()
        if row is None:
            raise ProjectArrayNotFoundError(name)
        version, declared = row.version, array_type_from_json(row.type)
        existing = await _snapshot_at(tx, name, version)
        if existing is not None:
            return existing
        array = await _read_array(tx, name, declared)
        # A store of its own: this transaction is its whole life. The array's ids come from
        # committed rows, so they are known to exist.
        store = PostgresEntityStorage(self._database)
        store.assume_known(_array_ids(array))
        bound = await store.store(array)
        await store.flush(tx)
        await tx.execute(
            insert(project_array_snapshots)
            .values(array_name=name, version=version, cid=bound.entity_id.cid)
            .on_conflict_do_nothing(
                index_elements=[
                    project_array_snapshots.c.array_name,
                    project_array_snapshots.c.version,
                ]
            )
        )
        info = await _snapshot_at(tx, name, version)
        assert info is not None
        return info

    async def list_snapshots(self, name: str) -> list[SnapshotInfo]:
        """Every snapshot of an array, oldest first.

        Raises:
            ProjectArrayNotFoundError: If there is no such array.
        """
        async with self._database.transaction() as tx:
            await _declared_type(tx, name)
            rows = (
                await tx.execute(
                    _snapshot_query()
                    .where(project_array_snapshots.c.array_name == name)
                    .order_by(project_array_snapshots.c.version)
                )
            ).all()
        return [_snapshot_from_row(row) for row in rows]


# ---- operations (given a transaction) ----


async def _read_array(tx: Transaction, name: str, declared: ArrayType) -> Array[Any]:
    rows = (
        await tx.execute(
            select(project_array_rows.c.key, project_array_rows.c.value).where(
                project_array_rows.c.array_name == name
            )
        )
    ).all()
    if not declared.dims and not rows:
        raise ProjectArrayEmptyError(f"Project array {name!r} is a scalar with no value yet")
    return Array(
        dims=declared.dims,
        schema=declared.schema,
        raw_rows=tuple(
            (
                key_from_json(row.key, declared.dims),
                value_from_json(row.value, declared.schema, f"row {row.key}"),
            )
            for row in rows
        ),
    )


def _array_ids(array: Array[Any]) -> set[EntityID]:
    """Every entity id an array carries in its values."""
    return {EntityID(cid=cid) for cid in array.entity_cids()}


def _snapshot_query() -> Any:
    return select(
        project_array_snapshots.c.array_name,
        project_array_snapshots.c.version,
        project_array_snapshots.c.cid,
        project_array_snapshots.c.created_at,
    )


def _snapshot_from_row(row: Row[Any]) -> SnapshotInfo:
    return SnapshotInfo(
        array_name=row.array_name,
        version=row.version,
        array=EntityID(cid=bytes(row.cid)).bind_type(Array[Any]),
        created_at=row.created_at,
    )


async def _snapshot_at(tx: Transaction, name: str, version: int) -> SnapshotInfo | None:
    row = (
        await tx.execute(
            _snapshot_query().where(
                project_array_snapshots.c.array_name == name,
                project_array_snapshots.c.version == version,
            )
        )
    ).first()
    return None if row is None else _snapshot_from_row(row)


def _info_query() -> Any:
    row_count = (
        select(func.count())
        .select_from(project_array_rows)
        .where(project_array_rows.c.array_name == project_arrays.c.name)
        .scalar_subquery()
    )
    return select(
        project_arrays.c.name,
        project_arrays.c.type,
        project_arrays.c.version,
        row_count.label("row_count"),
        project_arrays.c.created_at,
        project_arrays.c.updated_at,
    )


def _info_from_row(row: Row[Any]) -> ProjectArrayInfo:
    return ProjectArrayInfo(
        name=row.name,
        type=array_type_from_json(row.type),
        version=row.version,
        row_count=row.row_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _describe(tx: Transaction, name: str) -> ProjectArrayInfo:
    row = (await tx.execute(_info_query().where(project_arrays.c.name == name))).first()
    if row is None:
        raise ProjectArrayNotFoundError(name)
    return _info_from_row(row)


async def _declared_type(tx: Transaction, name: str) -> ArrayType:
    row = (
        await tx.execute(select(project_arrays.c.type).where(project_arrays.c.name == name))
    ).first()
    if row is None:
        raise ProjectArrayNotFoundError(name)
    return array_type_from_json(row[0])


async def _bump_version(tx: Transaction, name: str) -> ArrayType:
    """Increment an array's version (locking its registry row for the rest of the transaction, so
    mutations of one array are serialized) and return its declared type."""
    row = (
        await tx.execute(
            update(project_arrays)
            .where(project_arrays.c.name == name)
            .values(version=project_arrays.c.version + 1, updated_at=func.now())
            .returning(project_arrays.c.type)
        )
    ).first()
    if row is None:
        raise ProjectArrayNotFoundError(name)
    return array_type_from_json(row[0])


def _key_tuple(key: KeySpec, ttype: ArrayType) -> Key:
    names = ttype.dim_names
    if isinstance(key, Mapping):
        if set(key) != set(names):
            raise KeyError(f"Expected dims {list(names)}, got {sorted(key)}")
        return tuple(key[n] for n in names)
    if len(key) != len(names):
        raise KeyError(f"Expected {len(names)} key values, got {len(key)}")
    return tuple(key)
