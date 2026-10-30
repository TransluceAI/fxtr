# Entity Serialization/Deserialization Design

This document describes the high-level design of entity serialization and deserialization in fxtr2, focusing on the relationship between `Entity`, `EntityRef`, and `DataclassEntity`.

## Core Concepts

### Entity

`Entity` is the abstract base class for all serializable objects in the fxtr graph. It defines the contract for serialization:

```python
class Entity(abc.ABC):
    @abc.abstractmethod
    def _to_fxtr_cbor(self) -> FxtrCbor: ...

    @classmethod
    @abc.abstractmethod
    def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> Self: ...

    @classmethod
    @abc.abstractmethod
    def _fxtr_type_id(cls) -> FxtrID: ...

    def _fxtr_immutable(self) -> bool: ...
```

Key requirements:
- Every entity has a **type ID** (`_fxtr_type_id`) that uniquely identifies its class
- Serialized form must have exactly one `$fxtr.ctx` marker at the root
- Serialized form must include `$fxtr.type` for type discrimination
- Equal entities must serialize identically

### DataclassEntity

`DataclassEntity` is the convenient base class for most entities. It uses [mashumaro](https://github.com/Fatal1ty/mashumaro) for automatic serialization:

```python
@dataclasses.dataclass(frozen=True)
class MyEntity(DataclassEntity, fxtr_type=FxtrID.builtin("my_type")):
    name: str
    value: int
```

Features:
- Automatic `to_dict()` / `from_dict()` via mashumaro
- Discriminated union support via `$fxtr.type` field
- Automatic handling of `EntityRef` fields with proper link strength
- Frozen by default (immutable, hashable)

### EntityRef[T]

`EntityRef[T]` is a lazy reference to an entity of type `T`. It holds:
- `ref: FxtrID` - the content-addressed ID of the referenced entity
- `converter: Callable[[FxtrCbor], T]` - function to deserialize the entity

```python
ref: EntityRef[MyEntity] = entity.store()
loaded: MyEntity = await ref.load()
```

The converter is built by `build_converter_for(T)` and knows how to deserialize CBOR data into type `T`.

## Serialization Flow (Store)

```
Entity instance
    │
    ▼
entity._to_fxtr_cbor()  →  FxtrCbor dict
    │                       (includes $fxtr.ctx, $fxtr.type)
    ▼
arena.store(cbor)  →  FxtrID (content hash)
    │
    ▼
EntityRef[T](fxtr_id, converter)
```

For `DataclassEntity`:
1. `entity.store()` calls `_store_into(arena)`
2. `_to_fxtr_cbor()` uses mashumaro's `to_dict()`
3. Nested `EntityRef` fields are serialized as `RawStrongFxtrID` or `RawWeakFxtrID`
4. The arena stores the CBOR and returns a content-addressed ID
5. A converter is built from `__orig_class__` (if available) or `type(entity)`

## Deserialization Flow (Load)

```
EntityRef[T]
    │
    ▼
ref.load()  →  arena.load(fxtr_id)  →  FxtrCbor dict
    │
    ▼
ref.converter(cbor)
    │
    ▼
build_converter_for(T) dispatches to:
    │
    ├─► Union type? → Check $fxtr.type, dispatch to correct variant
    │
    ├─► Entity type? → base._from_fxtr_cbor(cbor, annotated_type)
    │
    └─► Other? → mashumaro BasicDecoder fallback
```

For `DataclassEntity._from_fxtr_cbor(cbor, annotated_type)`:
1. If `annotated_type` is provided (e.g., `WrapperWithRef[TypeA]`):
   - Use `_build_mashumaro_converter(annotated_type)`
   - This creates a `BasicDecoder` that properly substitutes type parameters
2. Otherwise: fall back to `cls.from_dict(cbor)`

## Type Parameter Preservation

The key challenge: when deserializing `WrapperWithRef[TypeA]` with a field `ref: EntityRef[T]`, how does the nested `EntityRef` know that `T = TypeA`?

### The Problem

Mashumaro doesn't automatically substitute type parameters in nested generic fields. When it sees `EntityRef[T]` where `T` is a TypeVar bounded by `Entity`, it passes the bound (`Entity`) as the type argument, not the actual instantiated type.

### The Solution

1. **At store time**: Capture the annotated type via `__orig_class__` or explicit `as_type` parameter
2. **At converter build time**: `build_converter_for(WrapperWithRef[TypeA])` creates a converter that passes the full annotated type to `_from_fxtr_cbor`
3. **At load time**: `DataclassEntity._from_fxtr_cbor` uses `_build_mashumaro_converter(annotated_type)` which creates a `BasicDecoder(WrapperWithRef[TypeA])`. This decoder properly substitutes `T → TypeA` for all nested fields.

### Two Paths for Type Information

```
Path 1: __orig_class__ (automatic)
─────────────────────────────────────
wrapper = WrapperWithRef[TypeA](...)  # Python sets __orig_class__
ref = wrapper.store()                  # Reads __orig_class__ for converter

Path 2: Explicit as_type
─────────────────────────────────────
wrapper = WrapperWithRef(...)
ref = store_entity(wrapper, as_type=WrapperWithRef[TypeA])
```

## Union Type Handling

`build_converter_for` handles unions by building a dispatch table from type IDs:

```python
converter = build_converter_for(TypeA | TypeB | TypeC)
```

At deserialization:
1. Extract `$fxtr.type` from the CBOR dict
2. Look up the corresponding (annotated_type, base_class) pair
3. Call `base_class._from_fxtr_cbor(cbor, annotated_type)`

This works for both `DataclassEntity` subclasses and custom `Entity` implementations.

## Link Strength

Entity references can be **strong** or **weak**:

- **Strong links** (`StrongLinkedEntityRef`, `RawStrongFxtrID`): The referenced entity is considered a child and will be included when copying/persisting
- **Weak links** (default): The reference is informational; the target may or may not exist

Controlled via type annotations:
```python
child: StrongLinkedEntityRef[ChildEntity]  # Strong link
related: EntityRef[OtherEntity]            # Weak link (default)
```

## Custom Entity Implementation

For entities that can't use `DataclassEntity` (e.g., need custom serialization):

```python
class CustomEntity(Entity):
    _TYPE_ID = FxtrID.builtin("custom")

    def _to_fxtr_cbor(self) -> FxtrCbor:
        return {
            "$fxtr.ctx": get_active_context_marker(),
            "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
            "field": self.field,
        }

    @classmethod
    def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> Self:
        # Can use annotated_type to get type parameters if generic
        return cls(field=cbor["field"])

    @classmethod
    def _fxtr_type_id(cls) -> FxtrID:
        return cls._TYPE_ID
```

Custom entities can be freely mixed with `DataclassEntity` in unions - the `$fxtr.type` discriminator ensures correct dispatch.

## Inline Entities (Auto-Loaded)

Inline entities (`Child[T]` and `LinkedEntity[T]`) are automatically loaded during deserialization, rather than being left as lazy `EntityRef` values.

### Type Aliases

| Type | Link Strength | Loading | Description |
|------|---------------|---------|-------------|
| `EntityRef[T]` | weak | lazy | Reference, must call `.load()` |
| `ChildRef[T]` | strong | lazy | Strong reference, must call `.load()` |
| `LinkedEntity[T]` | weak | auto | Field type is `T`, auto-loaded |
| `Child[T]` | strong | auto | Strong field type is `T`, auto-loaded |

### Usage

```python
@dataclass(frozen=True)
class Parent(DataclassEntity, fxtr_type=...):
    name: str
    child: Child[Leaf]           # Auto-loaded, strong link
    related: LinkedEntity[Leaf]  # Auto-loaded, weak link
    lazy: ChildRef[Leaf]         # Lazy, strong link, must call .load()

# Store - children are automatically stored
parent = Parent(
    name="test",
    child=Leaf(value=1),
    related=Leaf(value=2),
    lazy=Leaf(value=3).store(),
)
ref = parent.store()

# Load - child and related are automatically loaded
loaded = await ref.load()
assert loaded.child.value == 1      # Already loaded!
assert loaded.related.value == 2    # Already loaded!
lazy_loaded = await loaded.lazy.load()  # Must explicitly load
```

### Two-Phase Deserialization

When a `DataclassEntity` has inline entity fields, deserialization uses a two-phase process:

**Phase 1 - Collecting (synchronous):**
1. Set `RECURSIVE_SERDES_STATE` to `RecursiveLoadCollecting`
2. Run mashumaro's `from_dict()` / `BasicDecoder`
3. Inline entity fields return `DeferredLoadPlaceholder` sentinels
4. Collect all `EntityRef` objects that need to be loaded
5. Reset state

**If nothing collected:** Return the object synchronously.

**Phase 2 - Loading + Rebuilding (asynchronous):**
1. Load all collected EntityRefs in parallel using `anyio.create_task_group()`
2. Set `RECURSIVE_SERDES_STATE` to `RecursiveLoadRebuilding`
3. Re-run mashumaro's `from_dict()` / `BasicDecoder`
4. Inline entity fields return the pre-loaded entities
5. Reset state and return the final object

### Handling Placeholders in `__post_init__`

If your entity has `__post_init__` validation, you must handle `DeferredLoadPlaceholder` values during the collecting phase:

```python
@dataclass(frozen=True)
class ValidatedParent(DataclassEntity, fxtr_type=...):
    child: Child[Leaf]

    def __post_init__(self):
        super().__post_init__()
        # Skip validation during collecting phase
        if isinstance(self.child, DeferredLoadPlaceholder):
            return
        # Actual validation when child is loaded
        if self.child.value < 0:
            raise ValueError("Child value must be non-negative")
```

### Async `_from_fxtr_cbor`

For custom entities (not using DataclassEntity) that need to load children, `_from_fxtr_cbor` can return `Self | Awaitable[Self]`:

```python
class ParentEntity(Entity):
    @classmethod
    async def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> Self:
        # Load child from active scope
        child_id = cbor["child"].inner_id
        child_ref = EntityRef[ChildEntity](child_id, build_converter_for(ChildEntity))
        child = await child_ref.load()  # Uses ACTIVE_SCOPE
        return cls(name=cbor["name"], child=child)
```

Callers (converters, `EntityRef.load`) check with `inspect.isawaitable()` and await if needed.

### Scope Management

During serialization/deserialization, `ACTIVE_SCOPE` is guaranteed to be set correctly:
- `entity.store(scope)` overrides `ACTIVE_SCOPE` before calling `_to_fxtr_cbor`
- `ref.load(scope)` passes scope to converter, which overrides `ACTIVE_SCOPE` before calling `_from_fxtr_cbor`

This allows custom entities to use `store_entity()` and `EntityRef.load()` without explicitly passing scope parameters.
