# Entity Serialization/Deserialization Design

This document describes the high-level design of entity serialization and deserialization in fxtr2, focusing on the relationship between `Entity`, `EntityRef`, `FxtrScope`, the entity load handler registry, and `DataclassEntity`.

## Core Concepts

### Entity

`Entity` is the abstract base class for all serializable objects in the fxtr graph. It defines a minimal serialization contract:

```python
class Entity(abc.ABC):
    @abc.abstractmethod
    def _fxtr_store_impl(self) -> EntityRef[Self]: ...

    @typing.final
    def store(self, scope: FxtrScope | None = None) -> EntityRef[Self]: ...
```

Key requirements:
- `_fxtr_store_impl` must serialize the entity to CBOR, store it via `scope.store_cbor()`, and return an `EntityRef` declaring a default type that can be used to load it again.
- Storing and then loading the returned `EntityRef` must not produce deserialization errors (though the loaded Python object need not be identical to the original).
- The serialized form must have exactly one `$fxtr.ctx` marker at the root and a `$fxtr.type` field.
- Equal entities must serialize identically.

### Entity Load Handler Registry

Deserialization is handled by a global registry of `EntityLoadHandler` objects, not by the entity class itself. Handlers are registered per type annotation (class, type alias, or NewType) and are looked up via `TypeAnnotationHandlerRegistry` which supports:
- MRO-based dispatch (handlers for base classes apply to subclasses)
- Union resolution (each branch resolved independently)
- Type alias unwrapping with generic parameter substitution
- `OverrideFxtrHandlers[T]` for explicit override of default dispatch

```python
class EntityLoadHandler[T: Entity](Protocol):
    async def supports_entity_type(
        self, type_id: FxtrID, target_type: EntityTypeAnnotation[T]
    ) -> bool: ...

    async def from_lowlevel_entity_data(
        self, cbor: FxtrCbor, target_type: EntityTypeAnnotation[T]
    ) -> DeserializedEntity[T]: ...
```

`supports_entity_type` checks whether a handler can deserialize a given `$fxtr.type` ID into the target annotation. A handler can support multiple type IDs (e.g., `UnknownEntity` supports all type IDs).

`from_lowlevel_entity_data` performs the actual deserialization. It runs in the active scope and may load child entities via `EntityRef.load()`. It returns a `DeserializedEntity` which includes the value and a `cacheable` flag.

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

`DataclassEntity` has a corresponding `DataclassEntityLoadHandler` registered in the entity load registry. The handler uses `BasicDecoder(target_type)` to properly substitute generic type parameters during deserialization.

### EntityRef[T]

`EntityRef[T]` is a lazy reference to an entity of type `T`. It holds:
- `entity_id: FxtrID` - the content-addressed ID of the referenced entity
- `target_type: EntityTypeAnnotation[T]` - the target type to deserialize to (can be a union type, generic alias, etc.)

```python
ref: EntityRef[MyEntity] = entity.store()
loaded: MyEntity = await ref.load()
```

`EntityRef` itself does not contain any caching logic. All load/cache operations delegate to `FxtrScope`.

### Scope-Local Entity Caching

When an entity handler declares a deserialized value as cacheable (immutable), the scope caches it to avoid redundant deserialization:

- **`_ref_loaded_value_cache`**: Each `FxtrScope` maintains its own cache mapping `(entity_id, target_type)` → `RefLoadCacheEntry` (entity value + side-effect log).
- **Cache lifetime**: Cache entries are tied to the scope's lifetime. When a scope exits, its cache is destroyed. Cache entries are copied from parent scopes on `lift()` or `copy_from_scope()`.
- **`_refs_loaded_since_abandon`**: A fast-path set tracking refs loaded since the last `abandon()` call. If a ref is in this set, the cached value can be returned immediately without replaying side effects.

### Side-Effect Tracking and Replay

When loading an entity, any recursive loads (child entities loaded via `EntityRef.load()` during deserialization) are tracked as "side effects" via the scope's `_load_tracer` context variable. The side-effect log records both cacheable and uncacheable recursive loads.

When using a cached entity, these side effects are replayed to ensure the scope properly tracks all referenced entities:
- The entity's own CBOR is loaded from the arena (ensuring the arena tracks it)
- For each side-effect load, `_load_for_side_effects` is called recursively

**Fast-path optimization**: If a ref is in `_refs_loaded_since_abandon`, all its side effects have already been applied in this scope, so replay is skipped entirely.

## Serialization Flow (Store)

```
Entity instance
    │
    ▼
scope.store(entity)
    │
    ├─► Sets self as active scope
    │
    └─► entity._fxtr_store_impl()
            │
            ├─► Serialize to CBOR (e.g., mashumaro to_dict with RecursiveStore context)
            │
            ├─► scope.store_cbor(cbor)
            │       │
            │       ├─► Lift all child IDs referenced in CBOR
            │       │
            │       └─► arena.store(cbor) → FxtrID (content hash)
            │
            └─► EntityRef[T](fxtr_id, default_target_type)
```

For `DataclassEntity._fxtr_store_impl`:
1. Sets up `RecursiveStore` context for inline entity serialization
2. Uses mashumaro's `BasicEncoder(type(self))` to serialize
3. Calls `scope.store_cbor(cbor)` to store the CBOR
4. Calls `_infer_default_type_params(type(self))` to determine the default load type
5. Returns `EntityRef(fxtr_id, default_target_type)`

## Deserialization Flow (Load)

```
EntityRef[T]
    │
    ▼
scope.load(ref)
    │
    ├─► Check scope cache for (entity_id, target_type)
    │   └─► If found: replay side effects, return cached value
    │
    ├─► Lift ref from parent scopes (picks up parent cache entries too)
    │   └─► If cache hit after lift: replay side effects, return cached value
    │
    └─► No cache hit:
            │
            ▼
        arena.load(entity_id) → FxtrCbor dict
            │
            ▼
        Look up handler(s) in registry for target_type
            │
            ├─► Union type? → Check supports_entity_type for each variant
            │   └─► Exactly one handler must match
            │
            ├─► Check if concrete subtype already cached
            │
            └─► handler.from_lowlevel_entity_data(cbor, resolved_target_type)
                    │
                    ├─► Runs in active scope with _load_tracer tracking
                    │
                    └─► DeserializedEntity(value, cacheable)
                            │
                            ▼
                        If cacheable: cache in scope, add to fast-path set
```

For `DataclassEntityLoadHandler.from_lowlevel_entity_data`:
1. Phase 1 (collecting): Deserialize with mashumaro to find inline entity refs (`Child[T]`, `LinkedEntity[T]`)
2. If inline refs found: load all collected refs, then Phase 2 (rebuilding): re-deserialize with loaded entities substituted
3. Check if result is hashable to determine cacheability

## Type Parameter Preservation

The key challenge: when deserializing `WrapperWithRef[TypeA]` with a field `ref: EntityRef[T]`, how does the nested `EntityRef` know that `T = TypeA`?

### At Load Time

The caller specifies the full type annotation when creating the `EntityRef`:
```python
ref = EntityRef(some_id, WrapperWithRef[TypeA])
loaded = await ref.load()
```

The mashumaro-based `DataclassEntityLoadHandler` uses `BasicDecoder(WrapperWithRef[TypeA])` which properly substitutes `T → TypeA` for all nested fields.

### At Store Time

The entity's `_fxtr_store_impl` must return an `EntityRef` with a type that can successfully deserialize the entity. The entity does _not_ receive the caller's type annotation at store time — it must determine a suitable default type from its own runtime state.

`DataclassEntity` uses `_infer_default_type_params(type(self))`:
- For each type parameter, uses its bound as the default argument
- `T: Entity` → `UnknownEntity`
- `T: SomeConcreteType` → `SomeConcreteType`
- No bound → `typing.Any`

This means storing a `WrapperWithRef[TypeA](...)` and then loading it back _without_ specifying the type annotation will produce a `WrapperWithRef[UnknownEntity]` — the child entity's data is preserved but loaded as `UnknownEntity`. To get back a `WrapperWithRef[TypeA]`, the caller must specify the type when loading.

Subclasses can override `_fxtr_store_impl` to use runtime type introspection for more specific default types (e.g., inspecting `__orig_class__` or other runtime state). However, `__orig_class__` inspection is not part of the core API and is not explicitly supported.

NOTE: The `Entity` → `UnknownEntity` conversion happens at _store_ time, and ensures that stored entities can be re-loaded. If a user attempts to create `EntityRef(some_id, WrapperWithRef)` with no type parameters manually, the automatic inference of UnknownEntity does not trigger. Subclasses can opt-in to automatic `UnknownEntity` substitution by using the type alias `LoadAsUnknownByDefault` in their trait bound (see the `WrapperWithRefAndFallback` test for an example).

### Roundtrip Guarantee

The system guarantees that for any correctly-implemented entity `e`:
1. `ref = e.store()` succeeds
2. `loaded = await ref.load()` succeeds (no deserialization errors)
3. `ref2 = loaded.store()` produces `ref2.entity_id == ref.entity_id` (same content hash)

However, `loaded` may not be Python-equal to `e` (e.g., generic parameters may be lost). The guarantee is at the serialized CBOR level, not the Python object level.

## Union Type Handling

Union types are handled by the registry's union support. When loading an `EntityRef` with a union target type:

1. The registry resolves the union into per-branch handlers
2. `$fxtr.type` is extracted from the CBOR
3. `supports_entity_type` is called on each branch's handler
4. Exactly one handler must match (zero = error, multiple = error)
5. The matching handler deserializes the entity

```python
union_ref = EntityRef(some_id, TypeA | TypeB)
loaded = await union_ref.load()  # Dispatches based on $fxtr.type
```

If multiple handlers match the same type ID, use `OverrideFxtrHandlers` to define a custom handler that breaks ties:
```python
type MyUnion = OverrideFxtrHandlers[TypeA | TypeB]
register_entity_load_handler(MyUnion, CustomUnionHandler())
```

## Link Strength

Entity references can be **strong** or **weak**:

- **Strong links** (`RawStrongFxtrID`, CBOR tag 33002): Ownership/containment. The referenced entity is considered a child.
- **Weak links** (`RawWeakFxtrID`, CBOR tag 33001): Informational reference. Not considered a child.

Both link types keep the referenced entity alive. The distinction is for semantic purposes (e.g., visualization, provenance).

Controlled via type annotations on `DataclassEntity` fields:
```python
child: ChildRef[ChildEntity]         # Strong link, lazy
related: EntityRef[OtherEntity]      # Weak link (default), lazy
inline_child: Child[ChildEntity]     # Strong link, auto-loaded
inline_related: LinkedEntity[Other]  # Weak link, auto-loaded
raw_child: ChildFxtrID               # Strong link, raw FxtrID
raw_related: FxtrID                  # Weak link (default), raw FxtrID
```

## Inline Entities (Auto-Loaded)

Inline entities (`Child[T]` and `LinkedEntity[T]`) are automatically loaded during deserialization, rather than being left as lazy `EntityRef` values.

### Type Reference Table

| Type | Link Strength | Loading | Description |
|------|---------------|---------|-------------|
| `EntityRef[T]` | weak | lazy | Reference, must call `.load()` |
| `ChildRef[T]` | strong | lazy | Strong reference, must call `.load()` |
| `LinkedEntity[T]` | weak | auto | Field type is `T`, auto-loaded |
| `Child[T]` | strong | auto | Strong field type is `T`, auto-loaded |

### Two-Phase Deserialization

When a `DataclassEntity` has inline entity fields, deserialization uses a two-phase process:

**Phase 1 - Collecting (synchronous):**
1. Set `RECURSIVE_SERDES_STATE` to `RecursiveLoadCollecting`
2. Run mashumaro's `BasicDecoder`
3. Inline entity fields return `DeferredLoadPlaceholder` sentinels
4. Collect all `EntityRef` objects that need to be loaded

**If nothing collected:** Return the object directly.

**Phase 2 - Loading + Rebuilding (asynchronous):**
1. Load all collected EntityRefs sequentially
2. Set `RECURSIVE_SERDES_STATE` to `RecursiveLoadRebuilding`
3. Re-run mashumaro's `BasicDecoder`
4. Inline entity fields return the pre-loaded entities

### Handling Placeholders in `__post_init__`

If your entity has `__post_init__` validation, you must handle `DeferredLoadPlaceholder` values during the collecting phase:

```python
@dataclass(frozen=True)
class ValidatedParent(DataclassEntity, fxtr_type=...):
    child: Child[Leaf]

    def __post_init__(self):
        super().__post_init__()
        if isinstance(self.child, DeferredLoadPlaceholder):
            return
        if self.child.value < 0:
            raise ValueError("Child value must be non-negative")
```

## Custom Entity Implementation

For entities that can't use `DataclassEntity`, there are two paths:

### New-Style: Implement `Entity._fxtr_store_impl` + Register Handler

```python
class CustomEntity(Entity):
    _TYPE_ID = FxtrID.builtin("custom")

    def __init__(self, field: str):
        self.field = field

    def _fxtr_store_impl(self) -> EntityRef[Self]:
        scope = get_active_scope()
        cbor = {
            "$fxtr.ctx": get_active_context_marker(),
            "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
            "field": self.field,
        }
        return EntityRef(scope.store_cbor(cbor), CustomEntity)

class CustomEntityLoadHandler(EntityLoadHandler[CustomEntity]):
    async def supports_entity_type(self, type_id, target_type) -> bool:
        return type_id == CustomEntity._TYPE_ID

    async def from_lowlevel_entity_data(self, cbor, target_type):
        entity = CustomEntity(field=cbor["field"])
        return DeserializedEntity(value=entity, cacheable=True)

register_entity_load_handler(CustomEntity, CustomEntityLoadHandler())
```

### Legacy-Style: Subclass `SelfLoadingLegacyEntity`

For backward compatibility with old entities that implement `_to_fxtr_cbor` / `_from_fxtr_cbor`:

```python
from fxtr2.conversion.converters.legacy_entity_abc import SelfLoadingLegacyEntity

class OldStyleEntity(SelfLoadingLegacyEntity):
    def _to_fxtr_cbor(self, scope) -> FxtrCbor: ...
    @classmethod
    def _from_fxtr_cbor(cls, cbor, annotated_type, scope) -> Self: ...
    @classmethod
    def _fxtr_type_id(cls) -> FxtrID: ...
    def _fxtr_immutable(self) -> bool: ...
```

`SelfLoadingLegacyEntity` adapts the old ABC to the new system automatically.

## Scope Management

All serialization and deserialization operates within the active scope:
- `Entity._fxtr_store_impl()` is called with `self` as the active scope during `scope.store(entity)`.
- `EntityLoadHandler.from_lowlevel_entity_data()` is called with the loading scope as the active scope during `scope.load(ref)`.

Custom entities should use `get_active_scope()` for scope operations rather than receiving an explicit scope parameter.
