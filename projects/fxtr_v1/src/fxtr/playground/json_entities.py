"""New system involving JSON-based entities.

A simplified design for how entities should work in fxtr. Key idea
"""

from __future__ import annotations

from typing import Annotated, Any

import cbrrr
import msgspec


class BuiltinEntityStruct(msgspec.Struct, tag_field="$type"):
    pass


class SchemaEntity(BuiltinEntityStruct, tag="!schema^1"):
    """A schema entity."""

    json_schema: dict[str, Any]


def forward_declare_entity(type_id: str, namespace: str) -> Any:
    """Forward declare an entity type."""
    return Annotated[
        Any,
        msgspec.Meta(
            extra_json_schema={
                "fxtr-entity": True,
                "fxtr-type-id": type_id,
                "fxtr-namespace": namespace,
            },
            extra={"fxtr_forward_declared_entity": (type_id, namespace)},
        ),
    ]


def msgspec_fxtr_json_schema(
    cls: type[msgspec.Struct], include_entity_defs: bool = False
) -> dict[str, Any]:
    """Compute a fxtr-aware JSON schema for a msgspec class.

    This schema will be similar to the normal msgspec JSON schema, but where entities are
    represented using special fxtr annotations.

    Args:
        cls: The msgspec class to compute a schema for.
        include_entity_defs: Whether to include definitions for entities that are referenced in the
            schema, rather than just identifying them with fxtr annotations.

    Returns:
        A JSON schema for the msgspec class, with fxtr annotations.
    """
    type_info = msgspec.inspect.type_info(cls)

    # TODO: follow https://github.com/jcrist/msgspec/blob/main/src/msgspec/_json_schema.py with
    # a few modifications:
    # - if we encounter a StructType that is an instance of EntityStruct or BuiltinEntityStruct,
    #   it should be encoded as an object with a "$type" that is a string, and also have
    #   "fxtr-entity": True, "fxtr-type-id": <type_id>, "fxtr-namespace": <namespace>
    # - if we encounter a forward_declare_entity it should be processed similarly (although
    #   msgspec may already handle this)
    # - if include_entity_defs=False, then we should NOT include any recursive specs of entities
    #   in the defs, we should simply skip them. the
    #   "fxtr-entity": True, "fxtr-type-id": <type_id>, "fxtr-namespace": <namespace> parts should
    #   be directly inlined into the schema where they are needed.
    # - if include_entity_defs=True, then every entity we find should be a StructType, and we should
    #   put it into the $defs section. The "fxtr-entity": True, "fxtr-type-id": <type_id>,
    #   "fxtr-namespace": <namespace> details should be in the def. it is illegal to encounter a
    #   forward_declare_entity if include_entity_defs=True.
    raise NotImplementedError("Not implemented")


type AnyEntity = Annotated[Any, msgspec.Meta(extra_json_schema={"fxtr-entity": True})]
type EntityLink[T] = Annotated[str, msgspec.Meta(extra_json_schema={"fxtr-link": True})]


class EntityStructMeta(msgspec.StructMeta):
    """A struct representing an entity."""

    schema: SchemaEntity

    def __new__(cls, name, bases, namespace, type_id: str | None = None, **kwargs):
        assert "tag" not in kwargs
        assert "tag_field" not in kwargs

        if type_id is None:
            return super().__new__(cls, name, bases, namespace, **kwargs)
        else:

            fake_cls = super().__new__(
                cls, name, bases, namespace, tag_field="$type", tag="!NOT_SET", **kwargs
            )
            json_schema = msgspec.json.schema(fake_cls)
            schema_entity = SchemaEntity(json_schema=json_schema)
            tag = cid_of(schema_entity)

            new_cls = super().__new__(
                cls, name, bases, namespace, tag_field="$type", tag=tag, **kwargs
            )
            new_cls.schema = schema_entity
            return new_cls


class EntityStruct(msgspec.Struct, metaclass=EntityStructMeta):
    """A struct representing an entity."""


def cid_of(entity: EntityStruct | BuiltinEntityStruct | cbrrr.DagCborTypes) -> str:
    """Get the CID of an entity."""
    if isinstance(entity, (EntityStruct, BuiltinEntityStruct)):
        cbor_obj = msgspec.to_builtins(entity)
    else:
        raise NotImplementedError(
            f"Cannot compute CID of {type(entity)}; convert to JSON-serializable object first"
        )

    # TODO: should entity IDs depend on hashes of the sub entities, or on their values? maybe it
    # is simpler to depend on their values. we will need to pull out entity ids from the result
    # at some later date.

    cbor = cbrrr.encode_dag_cbor(cbor_obj)
    return cbrrr.CID.cidv1_dag_cbor_sha256_32_from(cbor).encode(base="base32")


class Foo(EntityStruct, type_id="Foo^1"):
    """A person entity."""

    foo: str


Bar = forward_declare_entity("Bar^1")


class Baz(EntityStruct, type_id="Baz^1"):
    """A baz entity."""

    foo: Foo
    bar: Bar


class Bar(EntityStruct, type_id="Bar^1"):
    """A bar entity."""

    bar: Foo


class Qux(EntityStruct, type_id="Qux^1"):
    """A qux entity."""

    foo: Foo
    bar: Bar


class ExtractedEntities(msgspec.Struct):
    entities: list[Any]
    entity_links: list[str]


def validate_fxtr_schema(fxtr_schema: dict[str, Any]) -> None:
    """Validate a fxtr-aware JSON schema.

    A JSON schema is fxtr-aware if it follows the basic rules of a JSON schema, and also has the
    property that:
    - it does not reference any external schemas (so all refs are internal to the schema)
    - the special annotations "fxtr-entity" and "fxtr-link" NEVER appear nested inside a "oneOf" or
      "anyOf", UNLESS that "oneOf" or "anyOf" has a "discriminator" field (OpenAPI-style)
      identifying which variant it is. (References to defs still count, so if you are inside an
      anyOf/oneOf, it is illegal to refer to any definition that uses "fxtr-entity" or "fxtr-link".)
    """
    raise NotImplementedError("Not implemented yet")


def extract_contained_entities(item: Any, fxtr_schema: dict[str, Any]) -> ExtractedEntities:
    """Extract all entities contained in an item.

    This function walks the `item` according to `fxtr_schema`, and extracts all parts of the item
    where `"fxtr-entity": True` or `"fxtr-link": True` is set in the schema.

    This function should simply give up if it encounters an "anyOf" or "oneOf" that does not have
    a "discriminator" field identifying which variant it is.

    Args:
        item: The item to extract entities from.
        fxtr_schema: The fxtr-aware JSON schema to use.

    Returns:
        A list of entities contained in the item.
    """
    raise NotImplementedError("Not implemented yet")
