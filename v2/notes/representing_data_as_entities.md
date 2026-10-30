# Representing Data as Entities

A guide to defining and structuring fxtr2 entity types.

## Defining an entity type

Every entity type is a frozen dataclass that extends `DataclassEntity`. Each type is registered with a namespace UUID (pick one per project, reuse it) and a name:

```python
import dataclasses
import uuid
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.system_entities import define_type

MY_NS = uuid.UUID("e47ac10b-58cc-4372-a567-0e02b2c3d479")

@dataclasses.dataclass(frozen=True)
class MyEntity(DataclassEntity, fxtr_type=define_type(MY_NS, "my_entity")):
    name: str
    score: float
```

Calling `.store()` serializes it to CBOR, computes a content hash, and returns an `EntityRef[MyEntity]`. The entity ID is deterministic — identical data always produces the same ID.

## Subparts: structured data inside entities

When you have structured data that is always serialized inline (no independent identity needed), use `DataclassFxtrSubpart`:

```python
from fxtr2.conversion.converters.mashumaro_entities import DataclassFxtrSubpart

@dataclasses.dataclass(frozen=True)
class Address(DataclassFxtrSubpart):
    street: str
    city: str

@dataclasses.dataclass(frozen=True)
class Person(DataclassEntity, fxtr_type=define_type(MY_NS, "person")):
    name: str
    address: Address  # serialized inline, no separate entity
```

`DataclassFxtrSubpart` inherits the fxtr2 serialization strategies from `DataclassEntity`, so it can contain `FxtrID`, `EntityRef`, and other fxtr types in its fields. Do **not** use plain mashumaro `DataClassDictMixin` for types that contain fxtr-specific fields — mashumaro's basic codecs don't know how to serialize them and you'll get `UnserializableField` errors at class definition time.

## References: `EntityRef[T]` vs `FxtrID`

Use `EntityRef[T]` when you know the type of the referenced entity:

```python
@dataclasses.dataclass(frozen=True)
class Experiment(DataclassEntity, fxtr_type=define_type(MY_NS, "experiment")):
    config_ref: EntityRef[Config]   # typed, self-documenting, lazy-loadable
    result_ref: EntityRef[Result]
```

Use bare `FxtrID` only for opaque references where the type isn't known or controlled by your code (e.g., user-provided identity keys).

**Always preserve real links.** Don't convert entity IDs to bytes or strings for storage — this breaks fxtr's reference tracking and makes the data opaque. If you need to store an entity reference inside a `DataclassFxtrSubpart`, use `EntityRef[T]` or `FxtrID` directly.

## Immutability

Default to immutable collections in entity and subpart fields:
- `list[T]` → `tuple[T, ...]`
- `dict[str, T]` → `frozendict[str, T]` (from the `frozendict` package)

This makes entity objects hashable and safe to cache. For fields behind an `EntityRef`, the referenced entity's Python-level mutability doesn't affect the referencing entity's hashability, so mutable types are acceptable there if convenient.

## Avoid `Any` in field type annotations

Don't use `Any` for entity or subpart fields. mashumaro performs no type conversion on `Any` fields — the value is passed through as-is, which will cause errors when fxtr2 tries to store or load the entity (e.g., `visit_ids` encountering an unexpected type, or values silently losing structure during serialization).

Instead, define a type alias that explicitly enumerates the allowed types. This gives mashumaro enough information to correctly serialize and deserialize each variant. When using a union type alias, remember that mashumaro tries union members in order — make sure that for any value you might store, the first matching member in the union is the correct one (see the `frozendict`/`tuple` ordering note below).

## Encoding user-defined data structures

When an entity needs to store an open-ended tree of data (e.g., serialized arguments, configuration), define a recursive type alias using frozen collections and `EntityRef` for custom leaf types:

```python
from frozendict import frozendict

@dataclasses.dataclass(frozen=True)
class MyMarker(DataclassEntity, fxtr_type=define_type(MY_NS, "my_marker")):
    """A domain-specific leaf value in the data tree."""
    identity: FxtrID

type MyData = (
    int | str | bool | bytes | float | None
    | EntityRef[MyMarker]
    | frozendict[str, MyData]
    | tuple[MyData, ...]
)
```

Using `EntityRef` for custom leaves (rather than encoding them as magic dict keys like `{"$type": "marker"}`) avoids collision with user data — entity references serialize as CBOR-tagged links, which are structurally distinct from any primitive data.

**Important: `frozendict` must come before `tuple` in the union.** mashumaro tries union members in order during serialization. Iterating a `frozendict` yields its keys (like any Python mapping), so `tuple[T, ...]` will match a `frozendict` first and serialize it as just the keys, silently losing the values. Putting `frozendict` first ensures it's tried before `tuple`.

## Discriminated unions of subparts

When a field is a union of `DataclassFxtrSubpart` types, mashumaro needs a discriminator to deserialize correctly. Add a literal `_k` field with a default to each variant:

```python
from typing import Annotated, Literal
import mashumaro.types

@dataclasses.dataclass(frozen=True)
class ForeignRef(DataclassFxtrSubpart):
    arg_index: int
    path: tuple[str, ...]
    _k: Literal["foreign"] = "foreign"

@dataclasses.dataclass(frozen=True)
class InternalRef(DataclassFxtrSubpart):
    slot_key: str
    path: tuple[str, ...]
    _k: Literal["internal"] = "internal"

SlotRef = Annotated[
    ForeignRef | InternalRef,
    mashumaro.types.Discriminator(field="_k", include_supertypes=True),
]
```

Then use the `SlotRef` type alias in entity fields. The `_k` field is serialized into the dict and used to pick the right variant during deserialization.
