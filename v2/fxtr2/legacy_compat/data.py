"""Compatibility layer for storing fxtr v1 entities in fxtr v2's system.

This module provides `LegacyCompat[T]`, a wrapper that allows v1 entities to be stored
in v2's entity system. It handles the translation between v1's DAG-CBOR format (with CID
links) and v2's format (with FxtrID links), while preserving the original v1 CID for
integrity verification.

Typical usage:
    from fxtr.object_model.core import EntityBase as V1Entity
    from fxtr2.legacy_compat import LegacyCompat

    # Wrap and store a v1 entity
    v1_entity: V1Entity = ...
    ref = LegacyCompat(v1_entity).store()

    # Load it back
    loaded = await ref.load()
    original_v1_entity = loaded.value
"""

from __future__ import annotations

import abc
import dataclasses
from typing import Any, ClassVar, Generic, TypeVar

import cbrrr

from fxtr2.conversion.converters.legacy_entity_abc import SelfLoadingLegacyEntity
from fxtr2.data import entity_ref_scope, fxtr_context_api
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.entity_ref_scope import Entity, EntityRef
from fxtr.object_model import core as v1_core

# Type variable for the wrapped v1 entity type
T = TypeVar("T", bound=v1_core.EntityBase)

# Builtin type ID for legacy-wrapped entities
LEGACY_V1_TYPE_ID = FxtrID.builtin("compat.legacy_v1")


# =============================================================================
# CBOR Traversal Helpers
# =============================================================================


def walk_and_collect_cids(data: Any) -> list[cbrrr.CID]:
    """Recursively find all CID objects in an IPLD structure.

    Args:
        data: An IPLD-compatible data structure (dict, list, primitives, CIDs)

    Returns:
        A list of all cbrrr.CID objects found in the structure.
    """
    result: list[cbrrr.CID] = []

    def _walk(value: Any) -> None:
        if isinstance(value, cbrrr.CID):
            result.append(value)
        elif isinstance(value, dict):
            for k, v in value.items():
                # Keys can be CIDs in some v1 structures
                if isinstance(k, cbrrr.CID):
                    result.append(k)
                _walk(v)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _walk(item)
        # Primitives (str, int, float, bool, None, bytes) are ignored

    _walk(data)
    return result


def walk_and_replace_cids(data: Any, cid_to_fxtr_id: dict[cbrrr.CID, FxtrID]) -> Any:
    """Replace CID objects with RawStrongFxtrID wrappers in an IPLD structure.

    Args:
        data: An IPLD-compatible data structure
        cid_to_fxtr_id: Mapping from v1 CIDs to their corresponding v2 FxtrIDs

    Returns:
        A new structure with CIDs replaced by RawStrongFxtrID wrappers.

    Raises:
        ValueError: If a CID is not in the mapping.
    """

    def _replace(value: Any) -> Any:
        if isinstance(value, cbrrr.CID):
            if value in cid_to_fxtr_id:
                return RawStrongFxtrID(inner_id=cid_to_fxtr_id[value])
            # CID not in mapping - this shouldn't happen if we collected all entities
            raise ValueError(
                f"CID {value} not found in mapping. Available CIDs: {list(cid_to_fxtr_id.keys())}"
            )
        elif isinstance(value, dict):
            new_dict = {}
            for k, v in value.items():
                # Handle CID keys
                if isinstance(k, cbrrr.CID) and k in cid_to_fxtr_id:
                    new_k = RawStrongFxtrID(inner_id=cid_to_fxtr_id[k])
                else:
                    new_k = k
                new_dict[new_k] = _replace(v)
            return new_dict
        elif isinstance(value, list):
            return [_replace(item) for item in value]
        elif isinstance(value, tuple):
            return tuple(_replace(item) for item in value)
        else:
            # Primitives pass through unchanged
            return value

    return _replace(data)


def walk_and_collect_fxtr_ids(data: Any) -> list[FxtrID]:
    """Find all FxtrID links in a modified CBOR structure.

    Args:
        data: A CBOR structure that may contain RawStrongFxtrID or RawWeakFxtrID wrappers

    Returns:
        A list of all FxtrIDs found in the structure.
    """
    result: list[FxtrID] = []

    def _walk(value: Any) -> None:
        if isinstance(value, (RawStrongFxtrID, RawWeakFxtrID)):
            result.append(value.inner_id)
        elif isinstance(value, dict):
            for k, v in value.items():
                if isinstance(k, (RawStrongFxtrID, RawWeakFxtrID)):
                    result.append(k.inner_id)
                _walk(v)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _walk(item)
        # Primitives, CIDs, RawFxtrEntityMarkerID are ignored

    _walk(data)
    return result


def walk_and_replace_fxtr_ids(data: Any, fxtr_id_to_cid: dict[FxtrID, cbrrr.CID]) -> Any:
    """Replace FxtrID links back to CID objects in a CBOR structure.

    Args:
        data: A CBOR structure containing RawStrongFxtrID/RawWeakFxtrID wrappers
        fxtr_id_to_cid: Mapping from v2 FxtrIDs back to their original v1 CIDs

    Returns:
        A new structure with FxtrID links replaced by cbrrr.CID objects.
    """

    def _replace(value: Any) -> Any:
        if isinstance(value, (RawStrongFxtrID, RawWeakFxtrID)):
            fxtr_id = value.inner_id
            if fxtr_id in fxtr_id_to_cid:
                return fxtr_id_to_cid[fxtr_id]
            raise ValueError(f"Unknown FxtrID during CID restoration: {fxtr_id}")
        elif isinstance(value, dict):
            new_dict = {}
            for k, v in value.items():
                if isinstance(k, (RawStrongFxtrID, RawWeakFxtrID)):
                    if k.inner_id in fxtr_id_to_cid:
                        new_k = fxtr_id_to_cid[k.inner_id]
                    else:
                        raise ValueError(f"Unknown FxtrID key during CID restoration: {k.inner_id}")
                else:
                    new_k = k
                new_dict[new_k] = _replace(v)
            return new_dict
        elif isinstance(value, list):
            return [_replace(item) for item in value]
        elif isinstance(value, tuple):
            return tuple(_replace(item) for item in value)
        else:
            # Primitives, CIDs, RawFxtrEntityMarkerID pass through
            return value

    return _replace(data)


# =============================================================================
# Post-Order Entity Collector
# =============================================================================


class PostOrderEntityCollector(v1_core.HubValueVisitor):
    """Visitor that collects entities in post-order (dependencies before dependents).

    Unlike RecursiveSubgraphCollector which uses pre-order traversal (adding entities
    before their children), this collector uses post-order traversal. This ensures
    that when an entity is added to the list, all entities it references (via CID)
    have already been added.

    This is important for LegacyCompat serialization where we need to store entities
    in dependency order so that CID→FxtrID mappings are available when processing
    each entity's IPLD.
    """

    def __init__(self) -> None:
        self.seen_cids: set[cbrrr.CID] = set()
        self.visited_entities: list[v1_core.EntityBase] = []

    def visit_part_at_path(
        self,
        *,
        py_path: v1_core.AccessPath,
        ipld_path: v1_core.AccessPath,
        spec: v1_core.TypeSpecBase,
        value: Any,
    ) -> None:
        del py_path, ipld_path
        self._visit_part(spec=spec, value=value)

    def _visit_part(self, *, spec: v1_core.TypeSpecBase, value: Any) -> None:
        if isinstance(value, v1_core.EntityBase):
            self.visit_entity(value)
        else:
            spec.traverse_value(value, self)

    def visit_entity(self, entity: v1_core.EntityBase) -> None:
        entity_cid = v1_core.entity_cid(entity)
        if entity_cid in self.seen_cids:
            return
        self.seen_cids.add(entity_cid)

        # POST-ORDER: First traverse all children
        entity_typespec = v1_core.typespec(entity)
        entity_typespec.traverse_value(entity, self)
        entity_typespec.traverse_self(self)

        # POST-ORDER: Then add this entity to the list
        self.visited_entities.append(entity)


# =============================================================================
# LegacyCompat Entity Class
# =============================================================================


class LegacyCompat(SelfLoadingLegacyEntity, Generic[T]):
    """Wrapper for storing v1 entities in v2's system.

    This class wraps a v1 EntityBase and provides serialization/deserialization
    that translates between v1's DAG-CBOR format and v2's entity format.

    The serialized form includes:
    - $fxtr.ctx: The v2 context marker (ground context)
    - $fxtr.type: The builtin ID "!compat.legacy_v1"
    - data: The v1 entity's IPLD structure with CIDs replaced by FxtrID links
    - legacy_cid: The original v1 CID as raw bytes (for integrity verification)

    Type Parameters:
        T: The type of the wrapped v1 entity (for type hints only; actual type
           is determined by v1's typespec system during deserialization)
    """

    _TYPE_ID = LEGACY_V1_TYPE_ID

    _value: T

    def __init__(self, value: T):
        """Create a LegacyCompat wrapper for a v1 entity.

        Args:
            value: The v1 entity to wrap. Must be an instance of v1's EntityBase.
        """
        if not isinstance(value, v1_core.EntityBase):
            raise TypeError(f"LegacyCompat requires a v1 EntityBase, got {type(value).__name__}")
        super().__setattr__("_value", value)

    @property
    def value(self) -> T:
        """The wrapped v1 entity."""
        return self._value

    @property
    def legacy_cid(self) -> cbrrr.CID:
        """The v1 CID of the wrapped entity.

        v1 entities cache their CID in _fxtr_cid, so this is efficient.
        """
        return v1_core.entity_cid(self._value)

    def _to_fxtr_cbor(self, scope: entity_ref_scope.FxtrScope) -> FxtrCbor:
        """Serialize the wrapped v1 entity to v2's CBOR format.

        This method:
        1. Collects all entities in the v1 subgraph using post-order traversal
        2. Stores all non-root children via scope.store_cbor() (dependencies first)
        3. Returns the root's CBOR dict (caller stores it)

        Child entities are stored as side effects, following the Entity contract.
        """
        v1_entity = self._value

        # Collect ALL entities in subgraph using post-order traversal.
        # Post-order ensures dependencies come before dependents in the list.
        collector = PostOrderEntityCollector()
        collector.visit_entity(v1_entity)

        cid_to_fxtr_id: dict[cbrrr.CID, FxtrID] = {}

        # Store all NON-ROOT entities (already in dependency order from post-order)
        for entity in collector.visited_entities:
            entity_cid = v1_core.entity_cid(entity)

            if entity is v1_entity:
                continue  # skip root - we return its CBOR, caller stores it

            if entity_cid in cid_to_fxtr_id:
                continue  # already stored (handles shared children)

            # Get IPLD and replace child CIDs with FxtrIDs
            ipld_data = v1_core.typespec(entity).entity_data_to_ipld(entity)
            modified_data = walk_and_replace_cids(ipld_data, cid_to_fxtr_id)

            # Build and store wrapper for this child
            wrapper: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
                "$fxtr.type": RawWeakFxtrID(inner_id=scope.track_builtin(self._TYPE_ID)),
                "data": modified_data,
                "legacy_cid": bytes(entity_cid),
            }
            fxtr_id = scope.store_cbor(wrapper)
            cid_to_fxtr_id[entity_cid] = fxtr_id

        # Build and RETURN the root's CBOR (don't store it - caller does that)
        root_ipld = v1_core.typespec(v1_entity).entity_data_to_ipld(v1_entity)
        root_modified = walk_and_replace_cids(root_ipld, cid_to_fxtr_id)

        return {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
            "$fxtr.type": RawWeakFxtrID(inner_id=scope.track_builtin(self._TYPE_ID)),
            "data": root_modified,
            "legacy_cid": bytes(self.legacy_cid),
        }

    @classmethod
    async def _from_fxtr_cbor(
        cls,
        cbor: FxtrCbor,
        annotated_type: Any | None,
        scope: entity_ref_scope.FxtrScope,
    ) -> LegacyCompat[Any]:
        """Deserialize a LegacyCompat entity from v2's CBOR format.

        This method:
        1. Extracts the "data" and "legacy_cid" fields
        2. Loads all child LegacyCompat entities recursively
        3. Replaces FxtrID links back to CIDs
        4. Uses v1's deserialization to reconstruct the entity
        5. Verifies the computed CID matches the stored legacy_cid

        Returns an Awaitable because child loading is async.
        """

        assert isinstance(cbor, dict), f"Expected dict, got {type(cbor)}"
        data = cbor["data"]
        legacy_cid_bytes = cbor["legacy_cid"]
        assert isinstance(legacy_cid_bytes, bytes)
        stored_cid = cbrrr.CID(legacy_cid_bytes)

        # Collect all FxtrID links in data
        child_fxtr_ids = walk_and_collect_fxtr_ids(data)

        # Load all children, collecting both their CID and their v1 entity
        child_data: dict[FxtrID, tuple[cbrrr.CID, v1_core.EntityBase]] = {}
        for child_id in child_fxtr_ids:
            child_ref: EntityRef[LegacyCompat[Any]] = EntityRef(child_id, LegacyCompat)
            child_compat = await child_ref.load(scope)
            if not isinstance(child_compat, LegacyCompat):
                raise ValueError(
                    f"Child {child_id} is not a LegacyCompat entity, "
                    f"got {type(child_compat).__name__}"
                )
            child_data[child_id] = (child_compat.legacy_cid, child_compat.value)

        # Build both mappings from the collected data
        fxtr_id_to_cid = {fid: cid for fid, (cid, _) in child_data.items()}
        cid_to_entity = {cid: entity for _, (cid, entity) in child_data.items()}

        # Replace FxtrIDs with CIDs in data
        ipld_data = walk_and_replace_fxtr_ids(data, fxtr_id_to_cid)

        # v1 lookup function for reconstruction
        def lookup(cid: cbrrr.CID) -> v1_core.EntityBase:
            if cid in cid_to_entity:
                return cid_to_entity[cid]
            raise ValueError(f"Unknown CID during v1 deserialization: {cid}")

        # Reconstruct using v1's machinery
        v1_entity = v1_core.ipld_entity_data_to_entity(ipld_data, lookup)

        # Verify CID integrity
        computed_cid = v1_core.entity_cid(v1_entity)
        if computed_cid != stored_cid:
            raise ValueError(
                "CID mismatch during LegacyCompat deserialization: "
                f"stored {stored_cid} != computed {computed_cid}"
            )

        return cls(v1_entity)  # type: ignore[return-value]

    @classmethod
    def _fxtr_type_id(cls) -> FxtrID:
        """Return the type ID for LegacyCompat entities."""
        return LEGACY_V1_TYPE_ID

    def _fxtr_immutable(self) -> bool:
        """LegacyCompat wraps v1 entities, which are always immutable."""
        return True

    def __repr__(self) -> str:
        return f"LegacyCompat({self._value!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, LegacyCompat):
            return self.legacy_cid == other.legacy_cid
        return False

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("LegacyCompat entities are immutable")

    def __hash__(self) -> int:
        return hash(self.legacy_cid)

    # store() is inherited from Entity.store() and delegates to _fxtr_store_impl()
    # (provided by SelfLoadingLegacyEntity) which calls _to_fxtr_cbor().

    @classmethod
    def _get_type_name_for_cache(cls) -> str:
        """Get the type name of the entity for the cache."""
        return cls.__name__

    def _get_legacy_cid(self) -> cbrrr.CID:
        """Get the legacy CID of the entity."""
        return self.legacy_cid


# =============================================================================
# Transitional Upgraded Entity Class
# =============================================================================


class TransitionalUpgradedEntity(Entity):
    """Base class for transitional upgraded entities.

    These entities are always mixed with DataclassEntity, which provides the actual
    serialization. This base class provides the shared interface needed by UpgradeCache.

    It is a strict requirement that there be a _unique_ canonical TransitionalUpgradedEntity
    value for each TransitionalUpgradedEntity class and legacy CID.
    """

    # Set by DataclassEntity.__init_subclass__ when fxtr_type= is provided
    _fxtr_known_type_id: ClassVar[FxtrID | None]

    @abc.abstractmethod
    def _get_legacy_cid(self) -> cbrrr.CID:
        """Get the legacy CID of the entity."""
        raise NotImplementedError("Not implemented")

    @classmethod
    def _fxtr_type_id(cls) -> FxtrID:
        """Get the type ID for the entity (delegates to DataclassEntity's _fxtr_known_type_id)."""
        type_id = cls._fxtr_known_type_id
        assert (
            type_id is not None
        ), "TransitionalUpgradedEntity subclass must set fxtr_type via DataclassEntity"
        return type_id

    @classmethod
    def _get_type_name_for_cache(cls) -> str:
        """Get the type name of the entity for the cache."""
        return cls.__name__

    async def save_to_cache(self, cache: UpgradeCache) -> None:
        """Save the entity as an upgraded entity to the cache."""
        await cache.save_upgraded(self)


# DAG-CBOR CIDs look like "bafyreiXXXXXX..." and we want to take the first 2 non-constant chars as
# the shard (roughly 32 * 32 = 1024 shards).
DAG_CBOR_CID_SHARD_OFFSET = 9


@dataclasses.dataclass(eq=True, frozen=True)
class UpgradeCache:
    """Cache object used to upgrade legacy entities to v2 equivalents.

    Two UpgradeCaches are equal if they have the same namespace (by identity).
    """

    cache_namespace: fxtr_context_api.FxtrNamespace
    check_consistency: bool = False

    # Internal resolution cache used to avoid lookups into the cache namespace. This is OK because
    # any well-behaved upgrade map will be a deterministic function.
    _cache_cache: dict[str, FxtrID] | None = dataclasses.field(default=None, compare=False)

    def _get_pin_key(
        self, transitional_type_name: str, transitional_type_id: str, cid: cbrrr.CID
    ) -> str:
        """Get the pin key for a CID."""
        cid_str = cid.encode()
        prefix, suffix = cid_str[:DAG_CBOR_CID_SHARD_OFFSET], cid_str[DAG_CBOR_CID_SHARD_OFFSET:]
        return f"as_{transitional_type_name}_{transitional_type_id}/{prefix}:{suffix}"

    async def save_upgraded(self, entity: TransitionalUpgradedEntity | LegacyCompat[Any]) -> None:
        """Save an upgraded entity to the cache.

        Args:
            entity: The entity to save. Must be storeable in the active scope.
        """
        if self._cache_cache is None:
            object.__setattr__(self, "_cache_cache", await self.cache_namespace.list_pins())
        assert self._cache_cache is not None

        transitional_type_name = type(entity)._get_type_name_for_cache()
        transitional_type_id = type(entity)._fxtr_type_id().id_string()
        pin_key = self._get_pin_key(
            transitional_type_name, transitional_type_id, entity._get_legacy_cid()
        )

        id_in_cache = self._cache_cache.get(pin_key)
        if id_in_cache is None:
            id_in_cache = await self.cache_namespace.try_retrieve(pin_key)
            if id_in_cache is not None:
                self._cache_cache[pin_key] = id_in_cache

        if id_in_cache is not None:
            if self.check_consistency:
                entity_id = entity.store().entity_id
                if id_in_cache != entity_id:
                    raise ValueError(
                        f"Entity {entity_id} (legacy CID {entity._get_legacy_cid()}) already exists"
                        f" in cache as {id_in_cache}!"
                    )
            # It's already in the cache, no need to store.
            return

        # Not in cache. Store it and copy it into the cache.
        entity_id = entity.store().entity_id
        try:
            await self.cache_namespace.pin(pin_key, entity_id, overwrite=False)
        except KeyError as exc:
            # Entity already exists! Maybe a race and someone else stored it at the same time.
            if self.check_consistency:
                val = await self.cache_namespace.retrieve(pin_key)
                if val != entity_id:
                    raise ValueError(
                        f"Entity {entity_id} (legacy CID {entity._get_legacy_cid()}) already exists"
                        f" in cache as {val}!"
                    ) from exc
            else:
                # If not checking consistency, ignore the conflict.
                pass

    async def lookup_upgraded[
        T: TransitionalUpgradedEntity | LegacyCompat[Any]
    ](self, legacy_cid: cbrrr.CID, as_type: type[T]) -> EntityRef[T]:
        """Load an upgraded entity from the cache as a ref."""
        transitional_type_name = as_type.__name__
        transitional_type_id = as_type._fxtr_type_id().id_string()
        pin_key = self._get_pin_key(transitional_type_name, transitional_type_id, legacy_cid)
        val = await self.cache_namespace.retrieve(pin_key, type=as_type)
        # Copy the entity out of the namespace's scope into our own active scope.
        return await val.copy_from(self.cache_namespace.scope)
