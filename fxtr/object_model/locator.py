"""Protocol and implementations for locating entity data.

Locators are low-level objects that can be used to locate entity data for use in an application or
service. They operate directly on the DAG-CBOR representation without converting to the Python
representation.

In general, a locator is responsible for:
- retrieving entity data for a set of entities that it knows about, and
- identifying new locators responsible for the children of those entities

We say that a locator is "responsible for" an entity if it both (a) knows how to retrieve the
entity data, and also (b) knows some locator that is responsible for each of the children of that
entity (possibly itself).

We say that a locator is:
- "locally valid" if it can retrieve entity data for any entity that it is reponsible for (and
  this entity data actually has the requested CID),
- "transitively valid" if it is locally valid, and also, whenever it identifies a locator for the
  children of an entity it is responsible for, that locator is also transitively valid.

A family of locators is "closed" if they are each transitively valid, and also each locator in the
family only points to other locators that are within the family. In particular, a single locator
is closed if it is transitively valid and only ever returns itself.

Note that locators _may_ also know how to locate CIDs that are "externally referenced" by entities
they are responsible for (e.g. run data _may_ be able to locate the task definition for itself), but
this is not required in general. (This is useful if entities happen to colocate, which is often true
for entities that were created together. As one example, run log entries reference their data, and
usually that data will be colocated with the log entry, but if this isn't true then that's an
application-level configuration error rather than a format-level integrity error.)

Since it is fairly common for a locator to also be involved in loading entities, this module also
provides a CachingEntityLoader which can be used to load entities from a locator. The loader caches
the actual Python entity objects with a weak reference, so that it can cheaply return references
to entities that have already been loaded.
"""

from __future__ import annotations

import abc
import dataclasses
import os
import pathlib
import weakref
from typing import Any, Collection, TypeVar

import cbrrr
import lmdb

from fxtr.object_model import cbor_util, core, dag_util, entity_util

T = TypeVar("T")


class Locator(abc.ABC):
    """A locator for a set of entities."""

    @abc.abstractmethod
    def has_entity(self, cid: cbrrr.CID) -> bool:
        """Whether this locator is responsible for the entity with the given CID."""
        raise NotImplementedError

    @abc.abstractmethod
    def retrieve(self, cid: cbrrr.CID) -> tuple[cbrrr.DagCborTypes, Locator]:
        """Retrieve the entity data with the given CID and the locator that is responsible for it.

        May raise an error if the locator does not know how to retrieve the entity.

        Returns:
            A tuple of the entity data and the locator that is responsible for its children.
        """
        raise NotImplementedError

    def try_retrieve(
        self, cid: cbrrr.CID, default: T = None
    ) -> tuple[cbrrr.DagCborTypes, Locator] | T:
        """Try to retrieve the entity with the given CID and the locator that is responsible for it.

        May return None if the locator does not know how to retrieve the entity.

        Args:
            cid: The CID of the entity to retrieve.
            default: The default value to return if the locator does not know how to retrieve the
                entity.

        Returns:
            A tuple of the entity data and the locator that is responsible for its children.
        """
        # Default implementation: check if the locator is responsible for the entity, and if so,
        # retrieve it. Subclasses may want to override this for efficiency.
        if self.has_entity(cid):
            return self.retrieve(cid)
        else:
            return default

    def metadata(self) -> core.AnyIPLDable:
        """Retrieve the metadata for this locator.

        The metadata is a Python object that can be serialized to IPLD.
        """
        return {"type": type(self).__name__}

    def advertised_entities(self) -> Collection[cbrrr.CID]:
        """The entities that this locator advertises as being responsible for.

        Implementing advertised_entities is optional. If implemented, it should return a collection
        of entities that this locator is responsible for. This can be used to preemptively avoid
        serializing entities that this locator already has, when this process is the recipient of
        data being sent from another source that does not know about this locator (e.g. across an
        RPC boundary).

        Every entity that is returned by this method MUST be retrievable by this locator, and
        `has_entity` MUST return True for them.
        """
        return ()


class WritableLocator(Locator, abc.ABC):
    """A locator that can also persist entities into the location it points to.

    This is the write-side counterpart to the read-only `Locator` protocol. Backends that store a
    complete subgraph on durable media (the CID-addressed CBOR file store, or the LMDB store)
    implement it, so callers that persist entities can be written against this interface rather than
    a specific backend.
    """

    @abc.abstractmethod
    def write_entity_data(self, data: cbrrr.DagCborTypes, cid: cbrrr.CID | None = None) -> None:
        """Write entity data into the location pointed to by this locator.

        Caller is responsible for also writing all children of the entity to maintain the
        closed property (or use `save_entity`, which does this for you).

        Args:
            data: The DAG-CBOR entity data to write.
            cid: The expected CID of the entity, if known. If provided, the locator verifies that the
                content hashes to this CID and skips the write if it already has the entity. If None,
                the CID is computed from the content.
        """
        raise NotImplementedError

    def save_entity(self, entity: core.AnyEntity) -> None:
        """Write an entity and all of its children to maintain the closed property.

        Walks the entity's subgraph (skipping entities already present) and writes each one via
        `write_entity_data`. Backends may override this to batch the writes (e.g. into a single
        transaction).
        """
        visitor = dag_util.RecursiveSubgraphCollector(
            include_if=lambda ent: (not self.has_entity(core.entity_cid(ent)))
        )
        visitor.visit_entity(entity)
        for entity in visitor.visited_entities:
            self.write_entity_data(
                core.typespec(entity).entity_data_to_ipld(entity), core.entity_cid(entity)
            )


class PythonObjectInMemoryLocator(Locator):
    """A locator for entities that are actually stored as Python objects in memory.

    This is a somewhat special locator, in that it owns a fixed set of entities, and dynamically
    walks those entities in order to find other entities.

    Note that this locator only advertises its roots (the entities explicitly provided to the
    constructor), and not their children, even though it does also know how to retrieve the
    children.
    """

    def __init__(self, entities: Collection[core.AnyEntity]):
        """Construct a locator that can locate a specific set of entities."""
        lookup_map: dict[cbrrr.CID, core.AnyEntity] = {}
        roots = set[cbrrr.CID]()
        child_collector = entity_util.EntityChildCollector()
        for entity in entities:
            entity_spec = core.typespec(entity)
            entity_spec.traverse_value(entity, child_collector)
            lookup_map = {
                core.entity_cid(childpath.entity): childpath.entity
                for childpath in child_collector.visited_entities
            }
            cid = core.entity_cid(entity)
            lookup_map[cid] = entity
            roots.add(cid)
        self._lookup_map = lookup_map
        self._roots = roots

    def has_entity(self, cid: cbrrr.CID) -> bool:
        return cid in self._lookup_map

    def retrieve(self, cid: cbrrr.CID) -> tuple[cbrrr.DagCborTypes, Locator]:
        ent = self._lookup_map[cid]
        data = core.typespec(ent).entity_data_to_ipld(ent)
        child_locator = PythonObjectInMemoryLocator(frozenset([ent]))
        return data, child_locator

    def advertised_entities(self) -> Collection[cbrrr.CID]:
        return self._roots


@dataclasses.dataclass(frozen=True)
class InMemoryCompleteSubgraphLocator(Locator):
    """A locator for a complete subgraph of entities in memory."""

    subgraph: dag_util.IPLDSubgraph

    def has_entity(self, cid: cbrrr.CID) -> bool:
        return cid in self.subgraph.nodes

    def retrieve(self, cid: cbrrr.CID) -> tuple[cbrrr.DagCborTypes, Locator]:
        return self.subgraph.nodes[cid], self

    def advertised_entities(self) -> Collection[cbrrr.CID]:
        return self.subgraph.nodes.keys()


@dataclasses.dataclass(frozen=True)
class ClosedBinaryCBOROnDiskLocator(WritableLocator):
    """A locator for a complete subgraph of entities on disk.

    This locator references a directory containing CID-addressed blobs, where the CID is mapped to
    a path by taking the CID's base32 representation, chunking it into the constant "bafyrei" prefix
    (which identifies this as a SHA-256 hash of a DAG-CBOR object, true of every entity in our
    system), two two-character directory shards, and the final hash suffix with a .cbor extension.

    TODO(daniel): We should probably also have some locator that can allows the directory to only
    contain a partial set of entities. This would require two types of metadata: we would store data
    for some set of entities, and enough metadata to build a locator for the rest of the entities.
    """

    directory: pathlib.Path

    def _get_file_path(self, cid: cbrrr.CID) -> pathlib.Path:
        encoded = cid.encode(base="base32")
        constant_prefix = "bafyrei"
        assert encoded.startswith(constant_prefix)
        unique_part = encoded.removeprefix(constant_prefix)
        return (
            self.directory
            / constant_prefix
            / unique_part[:2]
            / unique_part[2:4]
            / f"{unique_part[4:]}.cbor"
        )

    def has_entity(self, cid: cbrrr.CID) -> bool:
        return self._get_file_path(cid).exists()

    def retrieve(self, cid: cbrrr.CID) -> tuple[cbrrr.DagCborTypes, Locator]:
        entity_data = cbor_util.decode_cbor(self._get_file_path(cid).read_bytes())
        return entity_data, self

    def metadata(self) -> core.AnyIPLDable:
        return {"type": type(self).__name__, "directory": self.directory.absolute().as_posix()}

    def write_entity_data(self, data: cbrrr.DagCborTypes, cid: cbrrr.CID | None = None) -> None:
        """Write entity data into the location pointed to by this locator.

        Caller is responsible for also writing all children of the entity to maintain the
        closed property.
        """
        if cid is not None and self.has_entity(cid):
            # already exists
            return

        encoded = cbor_util.encode_cbor(data)
        encoded_cid = cbrrr.CID.cidv1_dag_cbor_sha256_32_from(encoded)
        if cid is None:
            # Use encoded CID
            cid = encoded_cid
            if self.has_entity(cid):
                # already exists
                return
        elif cid != encoded_cid:
            # Check that the CID we got was the one we expected
            raise ValueError(f"CID mismatch: provided {cid} != actual content hash {encoded_cid}")

        file_path = self._get_file_path(cid)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(encoded)


# Default LMDB map size: the maximum the database can grow to. This is address space only (sparse on
# APFS / ext4), so over-provisioning is cheap; bump it (and reopen) if a store ever outgrows it.
DEFAULT_LMDB_MAP_SIZE = 512 * 1024**3  # 512 GiB

# Process-wide cache of open LMDB environments, keyed by resolved path. LMDB forbids opening one
# environment path more than once per process, but our factories hand out a fresh locator object for
# the same store constantly (e.g. every get_active_data_locator call). So every locator for a path
# shares one environment, opened on first use. Value is (env, default_db_handle).
_LMDB_ENV_CACHE: dict[str, tuple[lmdb.Environment, Any]] = {}


def _close_lmdb_envs_after_fork() -> None:
    """Drop inherited LMDB environments in a forked child.

    A forked child inherits the parent's open environments, which are unusable in the child AND block
    reopening the same path (LMDB raises "already open in this process"). Closing them here lets the
    next access reopen cleanly. Registered as an at-fork (child) hook; matters for the run-graph cache
    build's ProcessPoolExecutor when it uses the default `fork` start method (Linux).
    """
    for env, _db in _LMDB_ENV_CACHE.values():
        try:
            env.close()
        except Exception:
            pass
    _LMDB_ENV_CACHE.clear()


os.register_at_fork(after_in_child=_close_lmdb_envs_after_fork)


class LMDBEntityLocator(WritableLocator):
    """A locator backed by a single LMDB database holding a complete subgraph of entities.

    This is the LMDB counterpart to `ClosedBinaryCBOROnDiskLocator`: it stores the same
    CID-addressed DAG-CBOR entity blobs, but in one memory-mapped B-tree instead of millions of
    individual files (which exhausts inodes on large stores). Keys are the raw CID bytes
    (`bytes(cid)`); values are the DAG-CBOR-encoded entity data, byte-identical to what the file
    locator writes into its `.cbor` files. Like the file locator, it owns a complete subgraph, so
    `retrieve` returns itself as the locator responsible for an entity's children.

    The instance itself holds no environment handle — only the path + open parameters — so it pickles
    trivially (needed because `CachingEntityLoader.from_locator` is pickled to `ProcessPoolExecutor`
    workers during the run-graph cache build). The actual `lmdb.Environment` lives in a process-wide
    cache (`_LMDB_ENV_CACHE`) shared by all locators for the same path, opened lazily on first use and
    dropped after a fork (see `_close_lmdb_envs_after_fork`).
    """

    # Name of the single named sub-database that holds entity data.
    _DB_NAME = b"entities"

    def __init__(
        self,
        path: pathlib.Path | str,
        *,
        map_size: int = DEFAULT_LMDB_MAP_SIZE,
        writemap: bool = False,
    ) -> None:
        """Construct a locator for the LMDB store at `path`.

        Args:
            path: Directory of the LMDB environment (holds `data.mdb` + `lock.mdb`).
            map_size: Maximum size of the memory map, in bytes. LMDB cannot grow past this without
                being reopened, so it must be at least as large as the data will ever grow. Only used
                when this process opens the environment for the first time; if another locator
                already opened the same path, its environment (and map_size) is reused.
            writemap: Whether to use a writeable memory map. Faster for bulk writes but less
                crash-safe; leave False in live processes and enable it only for offline bulk loads
                (the migration script). Like map_size, only honored on first open of the path.
        """
        self.path = pathlib.Path(path)
        self.map_size = map_size
        self.writemap = writemap

    def _get_env(self) -> tuple[lmdb.Environment, Any]:
        """Return the shared (env, db) for this path, opening it lazily on first use."""
        key = str(self.path.resolve())
        cached = _LMDB_ENV_CACHE.get(key)
        if cached is None:
            self.path.mkdir(parents=True, exist_ok=True)
            env = lmdb.Environment(
                str(self.path),
                map_size=self.map_size,
                writemap=self.writemap,
                subdir=True,
                max_dbs=1,
            )
            db = env.open_db(self._DB_NAME, create=True)
            cached = (env, db)
            _LMDB_ENV_CACHE[key] = cached
        return cached

    def close(self) -> None:
        """Close and forget the shared environment for this path (affects all locators on it)."""
        key = str(self.path.resolve())
        cached = _LMDB_ENV_CACHE.pop(key, None)
        if cached is not None:
            cached[0].close()

    def has_entity(self, cid: cbrrr.CID) -> bool:
        env, db = self._get_env()
        with env.begin(db=db, write=False) as txn:
            return txn.get(bytes(cid)) is not None

    def retrieve(self, cid: cbrrr.CID) -> tuple[cbrrr.DagCborTypes, Locator]:
        env, db = self._get_env()
        with env.begin(db=db, write=False) as txn:
            value = txn.get(bytes(cid))
        if value is None:
            raise ValueError(f"No entity found for CID {cid}")
        return cbor_util.decode_cbor(value), self

    def try_retrieve(
        self, cid: cbrrr.CID, default: T = None
    ) -> tuple[cbrrr.DagCborTypes, Locator] | T:
        # Single read txn, avoiding the has_entity + retrieve double lookup of the base class.
        env, db = self._get_env()
        with env.begin(db=db, write=False) as txn:
            value = txn.get(bytes(cid))
        if value is None:
            return default
        return cbor_util.decode_cbor(value), self

    def write_entity_data(self, data: cbrrr.DagCborTypes, cid: cbrrr.CID | None = None) -> None:
        if cid is not None and self.has_entity(cid):
            return
        encoded = cbor_util.encode_cbor(data)
        encoded_cid = cbrrr.CID.cidv1_dag_cbor_sha256_32_from(encoded)
        if cid is None:
            cid = encoded_cid
            if self.has_entity(cid):
                return
        elif cid != encoded_cid:
            raise ValueError(f"CID mismatch: provided {cid} != actual content hash {encoded_cid}")
        env, db = self._get_env()
        with env.begin(db=db, write=True) as txn:
            txn.put(bytes(cid), encoded, overwrite=True)

    def save_entity(self, entity: core.AnyEntity) -> None:
        """Write an entity and all of its children in a single write transaction."""
        visitor = dag_util.RecursiveSubgraphCollector(
            include_if=lambda ent: (not self.has_entity(core.entity_cid(ent)))
        )
        visitor.visit_entity(entity)
        if not visitor.visited_entities:
            return
        env, db = self._get_env()
        with env.begin(db=db, write=True) as txn:
            for ent in visitor.visited_entities:
                data = core.typespec(ent).entity_data_to_ipld(ent)
                txn.put(bytes(core.entity_cid(ent)), cbor_util.encode_cbor(data), overwrite=True)

    def metadata(self) -> core.AnyIPLDable:
        return {"type": type(self).__name__, "path": self.path.absolute().as_posix()}


@dataclasses.dataclass(frozen=True)
class EmptyLocator(Locator):
    """A locator that contains no entities."""

    def has_entity(self, cid: cbrrr.CID) -> bool:
        return False

    def retrieve(self, cid: cbrrr.CID) -> tuple[cbrrr.DagCborTypes, Locator]:
        raise ValueError(f"No entity found for CID {cid}")

    def metadata(self) -> core.AnyIPLDable:
        return {"type": type(self).__name__}


@dataclasses.dataclass(frozen=True)
class UnionLocator(Locator):
    """A locator that chains together multiple locators.

    Locators will be tried in order, so that the first locator that finds an entity is used for
    it and all its children. (Note that this locator will NEVER return itself as the next-round
    locator, so all locators in the list must be transitively valid for this one to be transitively
    valid.)
    """

    locators: list[Locator]

    def has_entity(self, cid: cbrrr.CID) -> bool:
        return any(locator.has_entity(cid) for locator in self.locators)

    def retrieve(self, cid: cbrrr.CID) -> tuple[cbrrr.DagCborTypes, Locator]:
        for locator in self.locators:
            result = locator.try_retrieve(cid)
            if result is not None:
                return result
        raise ValueError(f"No locator found for CID {cid}")

    def try_retrieve(
        self, cid: cbrrr.CID, default: T = None
    ) -> tuple[cbrrr.DagCborTypes, Locator] | T:
        for locator in self.locators:
            result = locator.try_retrieve(cid)
            if result is not None:
                return result
        return default

    def metadata(self) -> core.AnyIPLDable:
        return {
            "type": type(self).__name__,
            "locators": [locator.metadata() for locator in self.locators],
        }

    def advertised_entities(self) -> Collection[cbrrr.CID]:
        return set(cid for locator in self.locators for cid in locator.advertised_entities())


@dataclasses.dataclass(frozen=True)
class CachingEntityLoader:
    """Loader that can load an entity or its children from a locator, and caches the results.

    Works by asking the current locator for the location of the entity, then asking that new
    locator for the entity data, and registering that new locator as the lookup for any other
    entities we encounter while loading.
    """

    cache: weakref.WeakValueDictionary[cbrrr.CID, core.AnyEntity]
    locator: Locator

    def __call__(self, cid: cbrrr.CID) -> core.AnyEntity:
        if cid in self.cache:
            return self.cache[cid]
        else:
            # Look up the entity and the next responsible locator.
            entity_data, locator_for_children = self.locator.retrieve(cid)
            # Convert the entity data into a Python object. During conversion, use the new locator
            # to locate and load children of the entity that this entity requires.
            lookup_for_children = CachingEntityLoader(
                cache=self.cache, locator=locator_for_children
            )
            entity = core.ipld_entity_data_to_entity(entity_data, lookup_for_children)
            # Save this entity to the cache
            self.cache[cid] = entity
            return entity

    @classmethod
    def from_locator(cls, locator: Locator) -> CachingEntityLoader:
        """Construct a loader from a locator, with an empty cache."""
        return cls(cache=weakref.WeakValueDictionary(), locator=locator)

    @classmethod
    def from_entities(cls, entities: Collection[core.AnyEntity]) -> CachingEntityLoader:
        """Construct a loader from a collection of entities, also caching those entities."""
        cache = weakref.WeakValueDictionary()
        visitor = dag_util.RecursiveSubgraphCollector()
        for entity in entities:
            visitor.visit_entity(entity)
        for entity in visitor.visited_entities:
            cache[core.entity_cid(entity)] = entity
        return cls(cache=cache, locator=PythonObjectInMemoryLocator(entities))

    @classmethod
    def empty(cls) -> CachingEntityLoader:
        """Construct an empty loader."""
        return cls(cache=weakref.WeakValueDictionary(), locator=EmptyLocator())


def load_entity_from_locator(locator: Locator, cid: cbrrr.CID) -> core.AnyEntity:
    """Load an entity from a locator.

    This helper function recursively uses the locator to rehydrate the Python representation of the
    entity.

    Args:
        locator: The locator to load the entity from. Must own the entity with the given CID.
        cid: The CID of the entity to load.

    Returns:
        The loaded entity.
    """
    return CachingEntityLoader.from_locator(locator)(cid)
