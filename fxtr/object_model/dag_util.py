"""Serialization and deserialization of entity graphs."""

from __future__ import annotations

import base64
import dataclasses
from typing import Any, Callable, Iterable, Mapping

import cbrrr

from . import core

type JSONTypes = int | float | str | bool | None | list["JSONTypes"] | dict[str, "JSONTypes"]


@dataclasses.dataclass(frozen=True)
class IPLDSubgraph:
    """A piece of a directed acyclic graph.

    Contains a set of roots, identified by their DAG-CBOR CIDs, and a collection of nodes, which map
    from CIDs to their IPLD representations.

    Attributes:
        named_roots: A mapping from entity names to their root entities.
        sequence_roots: A collection of root entities that are not named.
        nodes: A mapping from CIDs to their IPLD representations.
    """

    named_roots: dict[str, cbrrr.CID]
    sequence_roots: list[cbrrr.CID]
    nodes: dict[cbrrr.CID, cbrrr.DagCborTypes]


class RecursiveSubgraphCollector(core.HubValueVisitor):
    """Visitor for recursively collecting all entities accessible from a set of roots.

    Attributes:
        include_if: A predicate that determines whether an entity should be included in the subgraph.
        seen_cids: A set of CIDs that have been visited, INCLUDING entities that have been
            excluded.
        visited_entities: A list of entities that have been visited (including only those for which
            include_if returns True).
        excluded_entities: A list of entities that were seen but excluded. Children of excluded
            entities will NOT be included in this list.
    """

    def __init__(self, include_if: Callable[[core.EntityBase], bool] = lambda _: True):
        super().__init__()
        self.include_if = include_if
        self.seen_cids: set[cbrrr.CID] = set()
        self.visited_entities: list[core.EntityBase] = []
        self.excluded_entities: list[core.EntityBase] = []

    def visit_part_at_path(
        self,
        *,
        py_path: core.AccessPath,
        ipld_path: core.AccessPath,
        spec: core.TypeSpecBase,
        value: Any,
    ):
        del py_path, ipld_path
        self.visit_part(spec=spec, value=value)

    def visit_part(self, *, spec: core.TypeSpecBase, value: Any):
        if isinstance(value, core.EntityBase):
            self.visit_entity(value)
        else:
            spec.traverse_value(value, self)

    def visit_entity(self, entity: core.EntityBase):
        entity_cid = core.entity_cid(entity)
        if entity_cid in self.seen_cids:
            return
        self.seen_cids.add(entity_cid)
        if not self.include_if(entity):
            self.excluded_entities.append(entity)
            return
        self.visited_entities.append(entity)
        entity_typespec = core.typespec(entity)
        entity_typespec.traverse_value(entity, self)
        entity_typespec.traverse_self(self)


def entities_to_cid_ipld_map(
    entities: Iterable[core.EntityBase],
) -> dict[cbrrr.CID, cbrrr.DagCborTypes]:
    """Convert a set of entities to a map from CIDs to their IPLD representations."""
    return {
        core.entity_cid(entity): core.typespec(entity).entity_data_to_ipld(entity)
        for entity in entities
    }


def entity_subgraph_to_ipld_blocks(
    named_roots: Mapping[str, core.EntityBase],
    sequence_roots: Iterable[core.EntityBase],
    *,
    include_if: Callable[[core.EntityBase], bool] = lambda _: True,
) -> IPLDSubgraph:
    """Convert a subgraph of entities to a set of IPLD blocks.

    Args:
        named_roots: A mapping from entity names to their root entities.
        sequence_roots: A collection of root entities that are not named.
        include_if: A predicate that determines whether an entity should be included in the
            subgraph. You can customize this to exclude entities that are serialized elsewhere.

    Returns:
        An IPLD subgraph containing the roots and nodes of the subgraph.
    """
    visitor = RecursiveSubgraphCollector(include_if)
    for root in named_roots.values():
        visitor.visit_entity(root)
    for root in sequence_roots:
        visitor.visit_entity(root)

    entities = visitor.visited_entities
    entity_cid_to_ipld = entities_to_cid_ipld_map(entities)

    return IPLDSubgraph(
        named_roots={name: core.entity_cid(entity) for name, entity in named_roots.items()},
        sequence_roots=[core.entity_cid(entity) for entity in sequence_roots],
        nodes=entity_cid_to_ipld,
    )


def dag_cbor_to_escaped_atjson(obj: cbrrr.DagCborTypes) -> JSONTypes:
    r"""Transform DAG-CBOR data to JSON-serializable escaped ATJSON format.

    Recursively transforms:
    - bytes objects -> {"$bytes": <base64-encoded string>}
    - CID objects -> {"$link": <base32-encoded CID>}
    - Objects with single key "$bytes", "$link", or starting with "$\" are escaped by prefixing "$\"

    This is an extension of the "atjson" format described in
    https://atproto.com/specs/data-model#json-representation. The transformation will behave
    identically for any input that does not already have objects with single keys that are "$bytes",
    "$link", or start with "$\". The result will always be parseable as atjson, but the
    reverse-escaping may be necessary if the input had one of these reserved names.

    Args:
        obj: The DAG-CBOR data to transform.

    Returns:
        JSON-serializable object with escaped special values.
    """
    if isinstance(obj, bytes):
        return {"$bytes": base64.b64encode(obj).decode("ascii")}
    elif isinstance(obj, cbrrr.CID):
        return {"$link": obj.encode(base="base32")}
    elif isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            result[key] = dag_cbor_to_escaped_atjson(value)

        # Check if this is a single-key dict that needs escaping
        if len(result) == 1:
            single_key = list(result.keys())[0]
            if single_key in ("$bytes", "$link") or single_key.startswith("$\\"):
                return {"$\\" + single_key: result[single_key]}

        return result
    elif isinstance(obj, list):
        return [dag_cbor_to_escaped_atjson(item) for item in obj]
    else:
        # Primitive types (int, float, str, bool, None)
        return obj


def escaped_atjson_to_dag_cbor(obj: JSONTypes) -> cbrrr.DagCborTypes:
    r"""Transform escaped ATJSON data back to DAG-CBOR format.

    Recursively transforms:
    - {"$bytes": <base64-string>} -> bytes object
    - {"$link": <base32-string>} -> CID object
    - Removes one layer of "$\" escaping from single-key objects

    Args:
        obj: The escaped ATJSON data to transform.

    Returns:
        DAG-CBOR compatible data.
    """
    if isinstance(obj, dict):
        # Handle special escaped single-key objects
        if len(obj) == 1:
            key, value = next(iter(obj.items()))

            # Handle bytes objects
            if key == "$bytes":
                assert isinstance(value, str)
                return base64.b64decode(value.encode("ascii"))

            # Handle CID objects
            elif key == "$link":
                assert isinstance(value, str)
                return cbrrr.CID.decode(value)

            # Handle escaped keys (remove one layer of "$\" escaping)
            elif key.startswith("$\\"):
                unescaped_key = key[2:]  # Remove "$\"
                return {unescaped_key: escaped_atjson_to_dag_cbor(value)}

        # Regular dict processing
        result = {}
        for key, value in obj.items():
            result[key] = escaped_atjson_to_dag_cbor(value)
        return result

    elif isinstance(obj, list):
        return [escaped_atjson_to_dag_cbor(item) for item in obj]

    else:
        # Primitive types pass through unchanged
        return obj


@dataclasses.dataclass(frozen=True)
class IPLDSubgraphAsJSON:
    """A piece of a directed acyclic graph, with all values escaped to ATJSON format.

    Attributes:
        named_roots: A mapping from entity names to their root entities.
        sequence_roots: A collection of root entities that are not named.
        nodes: A mapping from CIDs to their IPLD representations.
    """

    named_roots: dict[str, str]
    sequence_roots: list[str]
    nodes: dict[str, JSONTypes]

    @classmethod
    def from_ipld_subgraph(cls, subgraph: IPLDSubgraph) -> IPLDSubgraphAsJSON:
        return cls(
            named_roots={
                name: cid.encode(base="base32") for name, cid in subgraph.named_roots.items()
            },
            sequence_roots=[cid.encode(base="base32") for cid in subgraph.sequence_roots],
            nodes={
                cid.encode(base="base32"): dag_cbor_to_escaped_atjson(data)
                for cid, data in subgraph.nodes.items()
            },
        )
