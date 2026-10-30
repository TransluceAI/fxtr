"""Supported indexer types."""

from __future__ import annotations

import dataclasses
from typing import Annotated, Literal

import mashumaro
import mashumaro.config
import mashumaro.types
from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import (
    DataclassEntity,
    FrozensetSerializationStrategy,
    FxtrIDSerializationStrategy,
)
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.system_entities import define_type

from .indexing_types import (
    STORE_INDEXING_UUID,
    Indexer,
    IndexerColumnType,
    IndexerColumnValue,
    IndexerConstraint,
)

# ---------------------------------------------------------------------------
# Path element discriminated union
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PathKey(mashumaro.DataClassDictMixin):
    """A dict key lookup in a path."""

    key: str
    _k: Literal["k"] = "k"


@dataclasses.dataclass(frozen=True)
class PathIndex(mashumaro.DataClassDictMixin):
    """A fixed list index lookup in a path."""

    index: int
    _k: Literal["i"] = "i"


@dataclasses.dataclass(frozen=True)
class VarMarker(mashumaro.DataClassDictMixin):
    """A variable marker in a path — fans out over all keys/indices of the current container."""

    _k: Literal["v"] = "v"


@dataclasses.dataclass(frozen=True)
class DerefMarker(mashumaro.DataClassDictMixin):
    """A dereference marker — the current value should be a FxtrID reference to load."""

    _k: Literal["d"] = "d"


@dataclasses.dataclass(frozen=True)
class TypedDerefMarker(mashumaro.DataClassDictMixin):
    """A typed dereference marker — dereference and check the entity's type.

    Attributes:
        supported_types: The entity types that this dereference allows.
        strict: Whether type checking is strict. If True, entities whose type is not in
            ``supported_types`` cause a ``ValueError``. If False, non-matching entities
            are silently skipped (the path yields no results for that branch).
    """

    supported_types: frozenset[FxtrID]
    strict: bool
    _k: Literal["td"] = "td"

    class Config(mashumaro.config.BaseConfig):
        serialization_strategy = {
            FxtrID: FxtrIDSerializationStrategy(strong=False),
            frozenset: FrozensetSerializationStrategy(),
        }


PathElement = Annotated[
    PathKey | PathIndex | VarMarker | DerefMarker | TypedDerefMarker,
    mashumaro.types.Discriminator(field="_k", include_supertypes=True),
]

SomeDerefMarker = Annotated[
    DerefMarker | TypedDerefMarker,
    mashumaro.types.Discriminator(field="_k", include_supertypes=True),
]

# ---------------------------------------------------------------------------
# Unpack helpers
# ---------------------------------------------------------------------------

_SENTINEL = object()


def _check_value_type(
    value: object,
    value_type: IndexerColumnType,
    strict: bool,
) -> IndexerColumnValue | None:
    """Check and coerce the final value against the expected type.

    Returns the coerced value, or None if the value doesn't match and we're in lax mode.
    Raises ValueError in strict mode on mismatch.
    """
    match value_type:
        case IndexerColumnType.INT:
            # int but not bool
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        case IndexerColumnType.STR:
            if isinstance(value, str):
                return value
        case IndexerColumnType.BOOL:
            if isinstance(value, bool):
                return value
        case IndexerColumnType.BYTES:
            if isinstance(value, bytes):
                return value
        case IndexerColumnType.FLOAT:
            if isinstance(value, float):
                return value
            # Coerce int → float
            if isinstance(value, int) and not isinstance(value, bool):
                return float(value)
        case IndexerColumnType.FXTR_ID:
            if isinstance(value, (RawWeakFxtrID, RawStrongFxtrID)):
                return value.inner_id
    # Type mismatch
    if strict:
        raise ValueError(
            f"Value type mismatch: expected {value_type.value}, got {type(value).__name__}"
        )
    return None


def _extract_fxtr_id(value: object) -> FxtrID | None:
    """Extract a FxtrID from a raw CBOR reference, or return None."""
    if isinstance(value, (RawWeakFxtrID, RawStrongFxtrID)):
        return value.inner_id
    return None


async def _walk_path(
    data: object,
    path: tuple[PathElement, ...],
    path_idx: int,
    scope: FxtrScope,
    var_bindings: list[IndexerColumnValue],
    strict: bool,
) -> list[tuple[tuple[IndexerColumnValue, ...], object]]:
    """Walk the path from path[path_idx] onward, returning (bindings, final_value) pairs.

    Operates on frozen CBOR data (frozendict/tuple instead of dict/list).

    Args:
        data: Current frozen CBOR value (frozendict, tuple, or primitive).
        path: The full path tuple.
        path_idx: Current position in the path.
        scope: Scope for loading dereferenced entities.
        var_bindings: Mutable list of variable bindings accumulated so far.
        strict: Indexer-level schema strictness.
    """
    if path_idx >= len(path):
        return [(tuple(var_bindings), data)]

    elem = path[path_idx]

    if isinstance(elem, PathKey):
        if not isinstance(data, frozendict):
            if strict:
                raise ValueError(
                    f"Expected dict for key lookup '{elem.key}', got {type(data).__name__}"
                )
            return []
        value = data.get(elem.key, _SENTINEL)
        if value is _SENTINEL:
            if strict:
                raise ValueError(f"Key '{elem.key}' not found in dict")
            return []
        return await _walk_path(value, path, path_idx + 1, scope, var_bindings, strict)

    elif isinstance(elem, PathIndex):
        if not isinstance(data, tuple):
            if strict:
                raise ValueError(
                    f"Expected tuple for index lookup [{elem.index}], got {type(data).__name__}"
                )
            return []
        if elem.index >= len(data) or elem.index < -len(data):
            if strict:
                raise ValueError(f"Index {elem.index} out of bounds (tuple length {len(data)})")
            return []
        return await _walk_path(data[elem.index], path, path_idx + 1, scope, var_bindings, strict)

    elif isinstance(elem, VarMarker):
        results: list[tuple[tuple[IndexerColumnValue, ...], object]] = []
        if isinstance(data, frozendict):
            for key in data:
                var_bindings.append(key)
                results.extend(
                    await _walk_path(data[key], path, path_idx + 1, scope, var_bindings, strict)
                )
                var_bindings.pop()
        elif isinstance(data, tuple):
            for i, item in enumerate(data):
                var_bindings.append(i)
                results.extend(
                    await _walk_path(item, path, path_idx + 1, scope, var_bindings, strict)
                )
                var_bindings.pop()
        else:
            if strict:
                raise ValueError(
                    f"VarMarker requires frozendict or tuple, got {type(data).__name__}"
                )
        return results

    elif isinstance(elem, TypedDerefMarker):
        fid = _extract_fxtr_id(data)
        if fid is None:
            if strict:
                raise ValueError(
                    f"Expected FxtrID reference for typed deref, got {type(data).__name__}"
                )
            return []
        loaded = await scope.load_frozen_cbor(fid)
        # Type check using deref's own strict flag
        if isinstance(loaded, frozendict):
            type_raw = loaded.get("$fxtr.type")
            if type_raw is not None:
                type_id = _extract_fxtr_id(type_raw)
                if type_id is not None and type_id not in elem.supported_types:
                    if elem.strict:
                        raise ValueError(
                            f"Entity type {type_id} not in supported types for typed deref"
                        )
                    return []
            else:
                # No type on entity
                if elem.strict:
                    raise ValueError("Entity has no type but typed deref requires a match")
                return []
        return await _walk_path(loaded, path, path_idx + 1, scope, var_bindings, strict)

    elif isinstance(elem, DerefMarker):
        fid = _extract_fxtr_id(data)
        if fid is None:
            if strict:
                raise ValueError(f"Expected FxtrID reference for deref, got {type(data).__name__}")
            return []
        loaded = await scope.load_frozen_cbor(fid)
        return await _walk_path(loaded, path, path_idx + 1, scope, var_bindings, strict)

    else:
        raise TypeError(f"Unknown path element type: {type(elem).__name__}")


def _check_initial_lookup(
    entity_data: FrozenFxtrCbor,
    initial_lookup: DerefMarker | TypedDerefMarker,
) -> bool:
    """Apply initial_lookup check. Returns True to proceed, False to skip, or raises ValueError.

    Returns None if no type check is needed (DerefMarker).
    """
    if isinstance(initial_lookup, DerefMarker):
        return True

    assert isinstance(initial_lookup, TypedDerefMarker)
    if not isinstance(entity_data, frozendict):
        if initial_lookup.strict:
            raise ValueError("Entity is not a dict")
        return False

    type_raw = entity_data.get("$fxtr.type")
    if type_raw is not None:
        type_id = _extract_fxtr_id(type_raw)
        if type_id is not None and type_id not in initial_lookup.supported_types:
            if initial_lookup.strict:
                raise ValueError(f"Entity type {type_id} not in supported types")
            return False
    else:
        # No type on entity
        if initial_lookup.strict:
            raise ValueError("Entity has no type but strict typed deref requires a match")
        return False

    return True


# ---------------------------------------------------------------------------
# Concrete indexer types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TypedPathValueIndexer(
    DataclassEntity,
    Indexer,
    fxtr_type=define_type(STORE_INDEXING_UUID, "typed_path_value_indexer"),
):
    """An indexer that extracts a primitive value from an entity at a given path.

    The ``initial_lookup`` field controls how the entity is type-checked before path walking.
    Use ``DerefMarker()`` to index all entities regardless of type, or
    ``TypedDerefMarker(supported_types=..., strict=...)`` to filter (or require) specific types.

    The ``strict`` field (on the indexer itself) controls schema strictness: whether missing
    keys, out-of-bounds indices, variable type mismatches, and value type mismatches produce
    ``ValueError`` (strict) or are silently skipped (lax). This is independent of
    ``TypedDerefMarker.strict`` which controls type filtering strictness.

    Attributes:
        initial_lookup: Controls initial entity type checking. ``DerefMarker()`` means no type
            filtering. ``TypedDerefMarker(types, strict=True)`` requires the entity type to be
            in the set (ValueError if not). ``TypedDerefMarker(types, strict=False)`` filters
            to matching types, silently returning ``[]`` for non-matching entities.
        strict: Whether this indexer is strict about schema. If True, the indexer will raise
            ValueError when path elements don't match the entity structure (missing keys, bad
            indices, wrong variable types, wrong value type). If False, the indexer will return
            an empty list for non-matching paths.
        path: The path to the value to index in the CBOR-encoded entity. Elements are
            ``PathKey`` (dict key lookup), ``PathIndex`` (fixed list index), ``VarMarker``
            (fan-out variable), ``DerefMarker`` (load a FxtrID reference), or
            ``TypedDerefMarker`` (load and type-check a FxtrID reference).
        var_types: The types of the variables in the path (one per ``VarMarker``).
        value_type: The type of the value at the end of the path.
    """

    initial_lookup: SomeDerefMarker
    strict: bool
    path: tuple[PathElement, ...]
    var_types: tuple[IndexerColumnType, ...]
    value_type: IndexerColumnType

    def index_key_types(self) -> tuple[IndexerColumnType, ...]:
        """Get the types of the key columns in the indexer."""
        return self.var_types

    def index_value_type(self) -> IndexerColumnType:
        """Get the type of the value column in the indexer."""
        return self.value_type

    async def unpack_entity(
        self, entity_id: FxtrID, scope: FxtrScope
    ) -> list[tuple[tuple[IndexerColumnValue, ...], IndexerColumnValue]]:
        """Unpack an entity into (key_tuple, value) pairs.

        Loads frozen CBOR via the scope, applies ``initial_lookup`` type check first,
        then walks the ``path`` to extract values.
        """
        entity_data = await scope.load_frozen_cbor(entity_id)
        if not _check_initial_lookup(entity_data, self.initial_lookup):
            return []
        raw_results = await _walk_path(entity_data, self.path, 0, scope, [], self.strict)
        results: list[tuple[tuple[IndexerColumnValue, ...], IndexerColumnValue]] = []
        for bindings, raw_value in raw_results:
            checked = _check_value_type(raw_value, self.value_type, self.strict)
            if checked is not None:
                results.append((bindings, checked))
        return results

    @classmethod
    def parse(
        cls,
        expression: str,
        *,
        strict: bool = False,
    ) -> TypedPathValueIndexer:
        """Parse a string expression into a TypedPathValueIndexer.

        The expression must start with ``!`` (initial deref), optionally followed by a type
        filter, then ``.`` and the path. Examples::

            "!.name: str"
            "!(as '!my.type').turns[*: int].metrics[*: str].score: float"
            "!(as? '!my.type' | '!other.type').items[*: int].value: int"

        The initial ``!`` becomes the ``initial_lookup``:
        - ``!`` alone → ``DerefMarker()`` (no type filtering)
        - ``!(as ...)`` → ``TypedDerefMarker(strict=True)`` (require matching type)
        - ``!(as? ...)`` → ``TypedDerefMarker(strict=False)`` (filter, skip non-matching)

        Variables use ``*`` with a type annotation (e.g., ``[*: int]``). The expression must
        end with ``: <type>`` for the value type.

        Args:
            expression: The path expression to parse.
            strict: Whether the indexer should be strict about schema (missing keys, etc.).
        """
        # avoid circular import
        from .path_parse import parse_path_expression

        parsed = parse_path_expression(expression, named_vars=False, require_value_type=True)
        assert parsed.value_type is not None
        return cls(
            initial_lookup=parsed.initial_lookup,
            strict=strict,
            path=parsed.path,
            var_types=parsed.var_types,
            value_type=parsed.value_type,
        )

    @classmethod
    def query(
        cls,
        expression: str,
        where: str = "TRUE",
        *,
        strict: bool = False,
    ) -> IndexerConstraint:
        """Parse a string expression into an IndexerConstraint.

        Like ``parse()`` but variables use named ``$var`` syntax (e.g., ``[$t: int]``) and
        the result is an ``IndexerConstraint`` with the given ``where`` condition. Examples::

            "!(as '!my.type').turns[$t: int].metrics[$m: str].score: float"

        Args:
            expression: The path expression to parse (with named variables).
            where: A SQLite conditional expression that can refer to ``value`` and
                the named variables (prefixed by ``$``). Defaults to ``"TRUE"`` if not provided.
            strict: Whether the indexer should be strict about schema.
        """
        # avoid circular import
        from .path_parse import parse_path_expression

        parsed = parse_path_expression(expression, named_vars=True, require_value_type=True)
        assert parsed.value_type is not None
        indexer = cls(
            initial_lookup=parsed.initial_lookup,
            strict=strict,
            path=parsed.path,
            var_types=parsed.var_types,
            value_type=parsed.value_type,
        )
        assert parsed.var_names is not None
        return indexer.bind(parsed.var_names, where)


@dataclasses.dataclass(frozen=True)
class TypedPathExistsIndexer(
    DataclassEntity,
    Indexer,
    fxtr_type=define_type(STORE_INDEXING_UUID, "typed_path_exists_indexer"),
):
    """An indexer that checks if a given path exists in an entity.

    Like ``TypedPathValueIndexer`` but does not require the path to end at a primitive. It
    always produces a boolean value of ``True`` for every valid path traversal. Useful for
    type/presence checks.

    See ``TypedPathValueIndexer`` for documentation of the ``initial_lookup`` and ``strict``
    fields, which work identically here.

    Attributes:
        initial_lookup: Controls initial entity type checking (see TypedPathValueIndexer).
        strict: Whether this indexer is strict about schema (see TypedPathValueIndexer).
        path: The path to check existence of in the CBOR-encoded entity.
        var_types: The types of the variables in the path (one per ``VarMarker``).
    """

    initial_lookup: SomeDerefMarker
    strict: bool
    path: tuple[PathElement, ...]
    var_types: tuple[IndexerColumnType, ...]

    def index_key_types(self) -> tuple[IndexerColumnType, ...]:
        """Get the types of the key columns in the indexer."""
        return self.var_types

    def index_value_type(self) -> IndexerColumnType:
        """Get the type of the value column in the indexer."""
        return IndexerColumnType.BOOL

    async def unpack_entity(
        self, entity_id: FxtrID, scope: FxtrScope
    ) -> list[tuple[tuple[IndexerColumnValue, ...], IndexerColumnValue]]:
        """Unpack an entity into (key_tuple, True) pairs for every valid path traversal.

        Loads frozen CBOR via the scope, applies ``initial_lookup`` type check first,
        then walks the ``path``. At the end of the path, yields ``True`` instead of
        extracting a primitive value.
        """
        entity_data = await scope.load_frozen_cbor(entity_id)
        if not _check_initial_lookup(entity_data, self.initial_lookup):
            return []
        raw_results = await _walk_path(entity_data, self.path, 0, scope, [], self.strict)
        return [(bindings, True) for bindings, _ in raw_results]

    @classmethod
    def parse(
        cls,
        expression: str,
        *,
        strict: bool = False,
    ) -> TypedPathExistsIndexer:
        """Parse a string expression into a TypedPathExistsIndexer.

        Like ``TypedPathValueIndexer.parse()`` but the expression does not end with a value
        type annotation. Examples::

            "!(as '!my.type').items[*: int]"
            "!(as '!my.type')"

        Args:
            expression: The path expression to parse (no value type at end).
            strict: Whether the indexer should be strict about schema.
        """
        # avoid circular import
        from .path_parse import parse_path_expression

        parsed = parse_path_expression(expression, named_vars=False, require_value_type=False)
        return cls(
            initial_lookup=parsed.initial_lookup,
            strict=strict,
            path=parsed.path,
            var_types=parsed.var_types,
        )

    @classmethod
    def query(
        cls,
        expression: str,
        where: str = "TRUE",
        *,
        strict: bool = False,
    ) -> IndexerConstraint:
        """Parse a string expression into an IndexerConstraint.

        Like ``TypedPathValueIndexer.query()`` but for exists indexers. The default ``where``
        condition is ``"TRUE"`` since the value is always ``True``. Examples::

            "!(as '!my.type').items[$i: int]"

        Args:
            expression: The path expression to parse (with named variables, no value type).
            where: A SQLite conditional expression (default "TRUE").
            strict: Whether the indexer should be strict about schema.
        """
        # avoid circular import
        from .path_parse import parse_path_expression

        parsed = parse_path_expression(expression, named_vars=True, require_value_type=False)
        indexer = cls(
            initial_lookup=parsed.initial_lookup,
            strict=strict,
            path=parsed.path,
            var_types=parsed.var_types,
        )
        assert parsed.var_names is not None
        return indexer.bind(parsed.var_names, where)


type WellKnownBuiltinIndexer = TypedPathValueIndexer | TypedPathExistsIndexer
"""Type alias useable as an EntityRef target defining the set of supported built-in indexers."""
