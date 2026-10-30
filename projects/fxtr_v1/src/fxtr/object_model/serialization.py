"""Serialization utilities for the hub entity dags."""

import dataclasses
import json
from typing import Any, Callable, Sequence, TypeVar

import cbrrr

from . import cbor_util, core, dag_util, locator

T = TypeVar("T")


@dataclasses.dataclass
class SerializedWithFallbackLocator(locator.Locator):
    """A locator that locates entities in a serialized format, with a fallback to another locator.

    This locator can be used to "open" a serialized representation of an entity subgraph and load
    entities from it, even if that serialized representation only contains a partial set of
    entities.

    Attributes:
        loader: A function that takes a CID and returns a serialized representation of the
            entity with that CID, or None if the entity is not present in the serialized format.
        fallback: A locator that is responsible for the rest of the entities in the subgraph.
    """

    loader: Callable[[cbrrr.CID], cbrrr.DagCborTypes]
    fallback: locator.Locator

    def try_retrieve(
        self, cid: cbrrr.CID, default: T = None
    ) -> tuple[cbrrr.DagCborTypes, locator.Locator] | T:
        result = self.loader(cid)
        if result is not None:
            return result, self
        else:
            return self.fallback.try_retrieve(cid, default)

    def has_entity(self, cid: cbrrr.CID) -> bool:
        return self.try_retrieve(cid) is not None

    def retrieve(self, cid: cbrrr.CID) -> tuple[cbrrr.DagCborTypes, locator.Locator]:
        result = self.loader(cid)
        if result is not None:
            return result, self
        else:
            try:
                return self.fallback.retrieve(cid)
            except Exception as exc:
                raise ValueError(
                    f"Entity {cid} not found in either serialized format or fallback locator"
                ) from exc


class SimpleJsonSerializer:
    """Entity serializer that uses simple JSON serialization.

    This serializer serializes an object or collection of objects into a simple human-readable
    JSON representation. This can be useful for small objects or when human-readability is
    important.

    Currently we make no guarantees about backwards compatibility of the serialized output.
    """

    def __init__(
        self, *, indent: int = 2, well_known_locator: locator.Locator = locator.EmptyLocator()
    ):
        """Create a new SimpleJsonSerializer.

        Args:
            indent: The number of spaces to use for indentation.
            well_known_locator: A locator that is responsible for a set of entities that are
                already known and do not need to be serialized. The same set of well-known entities
                must also be present when deserializing.
        """
        self.indent = indent
        self.well_known_locator = well_known_locator

    def _include_if(self, entity: core.EntityBase) -> bool:
        if self.well_known_locator.has_entity(core.entity_cid(entity)):
            return False
        return True

    def dumps(self, value: core.AnyEntity | Sequence[core.AnyEntity]) -> str:
        """Serialize an entity or collection of entities to a JSON string."""
        collector = dag_util.RecursiveSubgraphCollector(self._include_if)
        result: dict[str, Any] = {"$version": "0.1", "entities": {}}
        if isinstance(value, core.AnyEntity):
            collector.visit_entity(value)
            result["root"] = core.entity_cid(value)
        else:
            result["roots"] = []
            for entity in value:
                collector.visit_entity(entity)
                result["roots"].append(core.entity_cid(entity))

        for ent in collector.visited_entities:
            cid_str = core.entity_cid(ent).encode(base="base32")
            result["entities"][cid_str] = core.typespec(ent).entity_data_to_ipld(ent)
        return json.dumps(collector.visited_entities, indent=self.indent)

    def loads(self, s: str) -> core.AnyEntity | list[core.AnyEntity]:
        """Deserialize a JSON string to an entity or collection of entities."""
        json_loaded = json.loads(s)
        if json_loaded["$version"] != "0.1":
            raise ValueError(f"Unsupported version: {json_loaded['$version']}")

        def _load_from_str(cid: cbrrr.CID) -> cbrrr.DagCborTypes:
            cid_str = cid.encode(base="base32")
            try:
                return json_loaded["entities"][cid_str]
            except KeyError:
                return None

        loc = SerializedWithFallbackLocator(loader=_load_from_str, fallback=self.well_known_locator)

        if "root" in json_loaded:
            return locator.load_entity_from_locator(loc, cbrrr.CID.decode(json_loaded["root"]))
        else:
            return [
                locator.load_entity_from_locator(loc, cbrrr.CID.decode(cid_str))
                for cid_str in json_loaded["roots"]
            ]


class SimpleCBORSerializer:
    """Entity serializer that uses simple CBOR serialization.

    This serializer serializes an object or collection of objects into a basic CBOR binary
    representation. This can be used to store entities together or transmit them over a network.

    Currently we make no guarantees about backwards compatibility of the serialized output.
    """

    def __init__(
        self, *, indent: int = 2, well_known_locator: locator.Locator = locator.EmptyLocator()
    ):
        """Create a new SimpleCBORSerializer.

        Args:
            indent: The number of spaces to use for indentation.
            well_known_locator: A locator that is responsible for a set of entities that are
                already known and do not need to be serialized. The same set of well-known entities
                must also be present when deserializing.
        """
        self.indent = indent
        self.well_known_locator = well_known_locator

    def _include_if(self, entity: core.EntityBase) -> bool:
        if self.well_known_locator.has_entity(core.entity_cid(entity)):
            return False
        return True

    def dump_to_cbor(self, value: core.AnyEntity | Sequence[core.AnyEntity]) -> cbrrr.DagCborTypes:
        """Serialize an entity or collection of entities to a CBOR object."""
        collector = dag_util.RecursiveSubgraphCollector(self._include_if)
        result: dict[str, Any] = {"$version": "0.1", "entities": {}}
        if isinstance(value, core.AnyEntity):
            collector.visit_entity(value)
            result["root"] = core.entity_cid(value)
        else:
            result["roots"] = []
            for entity in value:
                collector.visit_entity(entity)
                result["roots"].append(core.entity_cid(entity))

        for ent in collector.visited_entities:
            cid_str = core.entity_cid(ent).encode(base="base32")
            result["entities"][cid_str] = core.typespec(ent).entity_data_to_ipld(ent)
        return result

    def dumps(self, value: core.AnyEntity | Sequence[core.AnyEntity]) -> bytes:
        """Serialize an entity or collection of entities to a CBOR bytestring."""
        result = self.dump_to_cbor(value)
        return cbor_util.encode_cbor(result)

    def loads(self, s: bytes) -> core.AnyEntity | list[core.AnyEntity]:
        """Deserialize a JSON string to an entity or collection of entities."""
        loaded = cbor_util.decode_cbor(s)
        return self.load_from_cbor(loaded)

    def load_from_cbor(self, loaded: cbrrr.DagCborTypes) -> core.AnyEntity | list[core.AnyEntity]:
        assert isinstance(loaded, dict)
        if loaded["$version"] != "0.1":
            raise ValueError(f"Unsupported version: {loaded['$version']}")

        def _load_from_str(cid: cbrrr.CID) -> cbrrr.DagCborTypes:
            cid_str = cid.encode(base="base32")
            try:
                entities = loaded["entities"]
                assert isinstance(entities, dict)
                return entities[cid_str]
            except KeyError:
                return None

        loc = SerializedWithFallbackLocator(loader=_load_from_str, fallback=self.well_known_locator)

        # TODO: actually check that they are CIDs
        if "root" in loaded:
            return locator.load_entity_from_locator(loc, loaded["root"])  # type: ignore
        else:
            return [locator.load_entity_from_locator(loc, cid) for cid in loaded["roots"]]  # type: ignore
