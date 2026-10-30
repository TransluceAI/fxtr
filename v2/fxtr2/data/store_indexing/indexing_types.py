"""Basic types for store indexing."""

from __future__ import annotations

import abc
import dataclasses
import enum
import sqlite3
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence

from fxtr2.conversion.converters.mashumaro_entities import ChildFxtrID, DataclassEntity
from fxtr2.data import abstract_arena, entity_ref_scope
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.system_entities import define_type
from fxtr2.leases.lease_types import Lease, LifetimeBrand

STORE_INDEXING_UUID = uuid.UUID("cef8b5a0-7c0d-4378-93aa-971c634d3853")

type IndexerColumnValue = int | str | bool | bytes | float | FxtrID


class IndexerColumnType(enum.Enum):
    """The type of a column in an indexer. Only primitive types + FxtrID are supported."""

    INT = "int"
    STR = "str"
    BOOL = "bool"
    BYTES = "bytes"
    FLOAT = "float"
    FXTR_ID = "FxtrID"


class Indexer(entity_ref_scope.Entity):
    """Base class for a namespace indexer object.

    An indexer is responsible for encoding a property of an entity keyed by a tuple of keys, e.g.
    transforming a set of scores into a flat list of relationships like

        f(entity, $t, $m) = entity.turns[$t].metrics[$m].score

    e.g.

        f(entity, 0, "foo") = entity.turns[0].metrics["foo"].score = 4.6
        f(entity, 0, "bar") = entity.turns[0].metrics["bar"].score = 3.2
        f(entity, 1, "foo") = entity.turns[1].metrics["foo"].score = 2.1
        ...

    Indexers can be attached to IndexablePinningBackend namespaces to enable efficient queries of
    the contents of the namespace. Each indexer is itself a fxtr entity, and thus is an immutable
    representation of the logic needed to reconstruct the index.

    Note that this class is a convenience class which defines the operations a backend expects an
    indexer to have. However, the actual implementation of the indexer logic is an implementation
    detail of the backend currently. Users are not allowed to provide arbitrary subclasses of
    Indexer, only built-in types that the indexer already supports, and namespaces are expected to
    only persist the _serialized_ configuration of the indexer, not the implementation logic.
    """

    @abc.abstractmethod
    def index_key_types(self) -> Sequence[IndexerColumnType]:
        """Get the types of the key columns in the indexer."""
        raise NotImplementedError("Subclasses must override this method")

    @abc.abstractmethod
    def index_value_type(self) -> IndexerColumnType:
        """Get the type of the value column in the indexer."""
        raise NotImplementedError("Subclasses must override this method")

    @abc.abstractmethod
    async def unpack_entity(
        self, entity_id: FxtrID, scope: FxtrScope
    ) -> Sequence[tuple[tuple[IndexerColumnValue, ...], IndexerColumnValue]]:
        """Unpack an entity into a sequence of keytuple-value pairs.

        This method must be deterministic, and determines the meaning of the indexer. It should
        return a list of keytuple-value pairs, where the keytuple is a tuple of arguments to the
        property function f, and the value is the value of the indexed property for that keytuple.
        For instance, for the function f(entity, $t, $m) = entity.turns[$t].metrics[$m].score, the
        keytuple would be ($t, $m) and the value would be the score of the metric, so the result
        should be a list of all ((turn_index, metric), score) values in the whole entity.

        Strict indexers may raise ValueError on entities that are not compatible with the indexer.
        This error will then be bubbled up to the caller at the point where either (a) the invalid
        entity is pinned, or (b) the indexer is registered, whichever comes last. Since this affects
        the behavior of pinning values into the namespace, strict indexers should generally only
        be used when the namespace has been designated for a specific purpose.

        It is also possible to define a "lax" indexer that allows entities that are not compatible
        with the indexer. In this case, the indexer should return an empty list for entities it
        does not support. These entities will then never be returned when queried via this indexer.

        The entity's data is loaded internally via ``scope.load_frozen_cbor(entity_id)``, which
        returns cached frozen CBOR (immutable frozendict/tuple structure).

        Args:
            entity_id: The FxtrID of the entity to unpack.
            scope: A scope to use for loading the entity and any child entities.

        Returns:
            A list of keytuple-value pairs.

        Raises:
            ValueError: If the entity is not compatible with the indexer.
        """
        raise NotImplementedError("Subclasses must override this method")

    def bind(
        self,
        key_variables: tuple[str, ...],
        where: str = "TRUE",
    ) -> IndexerConstraint:
        """Bind the indexer expression to a set of key variables and a condition.

        Args:
            key_variables: The key variables to use. Must match the length of the indexer's key_types.
            where: The condition to apply to the indexer expression, as a SQLite conditional
                expression string, which can refer to the key variables (prefixed by "$"), and can also
                refer to the value of the indexer expression as `value`.
        """
        from fxtr2.data.store_indexing.sql_expr import parse_condition

        if len(key_variables) != len(self.index_key_types()):
            raise ValueError(
                "Key variables must match the length of the indexer's key_types. Expected"
                f" {len(self.index_key_types())}, got {len(key_variables)}."
            )
        if len(key_variables) != len(set(key_variables)):
            raise ValueError("Duplicate variable names in key_variables")
        # Eagerly validate the condition expression
        parse_condition(where, frozenset(key_variables), allow_value=True)
        return IndexerConstraint(self, key_variables, where)


@dataclasses.dataclass(frozen=True)
class IndexerConstraint:
    """A constraint for an indexer.

    An indexer constraint represents a selection of a set of entities and binding variables that
    satisfy some constraint.

    Attributes:
        indexer: The indexer to use.
        key_variables: The key variables to use. Must match the length of the indexer's key_types.
            Should not include dollar sign prefixes.
        condition: The condition to apply to the indexer expression, as a SQLite conditional
            expression string, which can refer to the key variables (prefixed by "$"), and can also
            refer to the value of the indexer expression as `value`.
    """

    indexer: Indexer
    key_variables: tuple[str, ...]
    condition: str


@dataclasses.dataclass(frozen=True)
class IndexerConstraintByID:
    """Version of IndexerConstraint that references an indexer by ID."""

    indexer_id: FxtrID
    key_variables: tuple[str, ...]
    condition: str


@dataclasses.dataclass(frozen=True)
class BindingConstraint:
    """A constraint for binding variables alone.

    Attributes:
        condition: The condition to apply to the binding variables, as a SQLite conditional
            expression string, which can refer to the binding variables (prefixed by "$").
    """

    condition: str


@dataclasses.dataclass(frozen=True)
class ParsedViewColumn:
    """A single column in a view, parsed and validated by the user-facing layer.

    Contains the resolved indexer entity ID and the rewritten SQL condition,
    but NOT the database table info (which is resolved by the backend).
    """

    column_name: str  # user-chosen name (the kwarg key in QueryView)
    indexer_id: FxtrID  # resolved entity ID
    key_variables: tuple[str, ...]  # e.g., ("t", "m")
    condition: str  # rewritten SQL from parse_condition


@dataclasses.dataclass(frozen=True)
class ParsedView:
    """A view parsed and validated by the user-facing layer, ready for backend execution.

    Produced by ``FxtrQueryableNamespace.sql()`` and consumed by
    ``IndexablePinningBackend.run_direct_sql()``.
    """

    view_name: str
    prefix: str
    columns: list[ParsedViewColumn]


_QUERY_VIEW_RESERVED_NAMES = frozenset({"pin_name", "entity_id"})


class QueryView:
    """A view definition for ``FxtrQueryableNamespace.sql()``.

    Groups a key prefix with one or more named indexer constraints that together
    define the columns of a virtual table (CTE) in the generated SQL query.

    Columns of the resulting view:

    - **Binding variables**: The union of all ``key_variables`` across the constraints.
      Named after the ``$var`` names in the indexer path expressions.
    - **Value columns**: One per constraint, named by the keyword argument.
    - ``pin_name``: The pin key string (always present).
    - ``entity_id``: The raw FxtrID bytes of the entity (always present, via LEFT JOIN).

    Args:
        prefix: Key prefix filter — only pins whose keys start with this prefix
            are included. Defaults to ``""`` (all pins).
        **columns: Named indexer constraints. Each keyword becomes a column in the view
            whose value is the indexer's output. At least one is required.

    Raises:
        ValueError: If no columns are provided, if column names collide with binding
            variable names or reserved names, or if binding variable names collide
            with reserved names.
    """

    __slots__ = ("prefix", "columns")

    prefix: str
    columns: dict[str, IndexerConstraint]

    def __init__(self, prefix: str = "", /, **columns: IndexerConstraint) -> None:
        if not columns:
            raise ValueError("QueryView requires at least one column")

        column_names = set(columns.keys())

        # Check reserved name collisions
        reserved_collision = column_names & _QUERY_VIEW_RESERVED_NAMES
        if reserved_collision:
            raise ValueError(
                f"Column names conflict with reserved names: {sorted(reserved_collision)}"
            )

        # Collect all binding variable names
        all_binding_vars: set[str] = set()
        for constraint in columns.values():
            all_binding_vars.update(constraint.key_variables)

        # Check binding var vs column name collisions
        var_col_collision = all_binding_vars & column_names
        if var_col_collision:
            raise ValueError(
                f"Binding variable names conflict with column names: {sorted(var_col_collision)}"
            )

        # Check binding var vs reserved name collisions
        var_reserved_collision = all_binding_vars & _QUERY_VIEW_RESERVED_NAMES
        if var_reserved_collision:
            raise ValueError(
                "Binding variable names conflict with reserved names:"
                f" {sorted(var_reserved_collision)}"
            )

        object.__setattr__(self, "prefix", prefix)
        object.__setattr__(self, "columns", columns)

    def __repr__(self) -> str:
        cols = ", ".join(f"{k}=..." for k in self.columns)
        return f"QueryView({self.prefix!r}, {cols})"


class IndexablePinningBackend[B: LifetimeBrand](abstract_arena.PersistentPinningBackend[B]):
    """A pinning backend that can be indexed.

    Brand ``B`` is the backend's own lifetime brand. Returned arenas share the backend's
    brand; callers obtain ``Lease[B]`` via a concrete ``get_lease()`` (or similar).
    """

    @abc.abstractmethod
    async def register_indexer[
        B_Src: LifetimeBrand
    ](
        self,
        indexer_id: FxtrID,
        source: abstract_arena.ReadableArena[B_Src],
        src_lease: Lease[B_Src],
        key_prefix: str = "",
    ) -> None:
        """Register an indexer with the backend at a given key prefix.

        Indexers must be registered and attached to a given key prefix before they can be queried.

        Args:
            indexer_id: The ID of the indexer to register.
            source: An arena containing the indexer entity and any other entities it references.
            src_lease: A lease for ``source``'s token, consumed by the call.
            key_prefix: The key prefix to index using the indexer. If provided, queries must also
                be restricted to this key prefix.
        """
        raise NotImplementedError("Subclasses must override this method")

    @abc.abstractmethod
    async def unregister_indexer(self, indexer_id: FxtrID, key_prefix: str = "") -> None:
        """Unregister an indexer from the backend.

        Args:
            indexer_id: The ID of the indexer to unregister.
            key_prefix: The key prefix to unindex using the indexer. Must exactly match the key
                prefix that was used for registration.
        """
        raise NotImplementedError("Subclasses must override this method")

    @abc.abstractmethod
    async def list_indexers(
        self,
    ) -> tuple[list[tuple[FxtrID, str]], abstract_arena.ReadableArena[B]]:
        """List all indexers registered with the backend.

        Returns:
            A tuple of (registrations, arena) where registrations is a list of
            (indexer_id, key_prefix) pairs and arena contains all the indexer
            entities. The arena shares this backend's brand ``B``. The arena may
            be empty if there are no registrations.
        """
        raise NotImplementedError("Subclasses must override this method")

    @abc.abstractmethod
    async def iter_matches(
        self,
        first: IndexerConstraintByID,
        /,
        *rest: IndexerConstraintByID,
        key_prefix: str = "",
        where: BindingConstraint | None = None,
    ) -> AsyncIterator[
        tuple[str, FxtrID, Mapping[str, IndexerColumnValue], abstract_arena.ReadableArena[B]]
    ]:
        """Iterate over all matches for a given set of indexer constraints.

        Args:
            first: The first indexer constraint to apply.
            rest: The rest of the indexer constraints to apply.
            key_prefix: The key prefix to restrict the matches to. This key prefix (or a key prefix
                containing it) must be indexed in the namespace for this indexer.
            where: An optional additional condition to apply to the matches. Every variable in this
                must appear in at least one of the indexer constraints.

        Returns:
            An asynchronous generator of matches. Each match is a tuple
            (key, entity_id, bindings, arena) where:
                - key is the key string where the entity was pinned.
                - entity_id is the ID of the entity that matched the constraints.
                - bindings is a mapping from the binding variable name to the value of the binding
                variable.
                - arena is an arena sharing this backend's brand ``B`` that can be used to load the
                  entity (which may be shared across multiple matches or may be distinct).
        """
        raise NotImplementedError("Subclasses must override this method")
        yield  # pyright: ignore[reportUnreachable]

    @abc.abstractmethod
    async def run_direct_sql(
        self,
        query: str,
        views: list[ParsedView],
    ) -> list[sqlite3.Row]:
        """Run an ad-hoc SQL query against named index views.

        Used by ``FxtrQueryableNamespace.sql()``. The caller (user-facing layer)
        handles entity ID resolution and condition rewriting; this method handles
        table resolution, query generation, and execution in a read-only context.

        Args:
            query: The user's SQL query (substituted after the generated WITH clause).
            views: Parsed views with resolved indexer IDs and rewritten conditions.

        Returns:
            All result rows as ``sqlite3.Row`` objects.

        Raises:
            KeyError: If an indexer in a view is not registered at a prefix covering
                the view's prefix.
        """
        raise NotImplementedError("Subclasses must override this method")


@dataclasses.dataclass(frozen=True)
class NamespaceIndexMetadata(
    DataclassEntity,
    fxtr_type=define_type(STORE_INDEXING_UUID, "index_metadata"),
):
    """Metadata entity for exporting/importing indexer registrations.

    Stored in the "metadata" namespace of an archive during export.
    Strong links to indexer entities keep them (and their transitive
    dependencies like type entities) alive in the archive.

    Each registration is a tuple of (indexer_id, key_prefix).
    """

    registrations: tuple[tuple[ChildFxtrID, str], ...]
