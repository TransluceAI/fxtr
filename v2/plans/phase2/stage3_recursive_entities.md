# Stage 3: Recursive Entity Loading and Storing

This plan describes changes to the entity serialization system to support **inline entities** - entities that are automatically loaded during deserialization (and automatically stored during serialization), rather than being left as `EntityRef` values that must be explicitly loaded later.

## Motivation

Currently, if an entity contains references to other entities, users must:
1. Store child entities explicitly to get `EntityRef` values
2. After loading a parent, explicitly call `.load()` on each child ref

This is verbose for cases where the child entities are logically "part of" the parent and should always be loaded together. The new `Child[T]` and `LinkedEntity[T]` type aliases enable automatic recursive loading/storing.

---

## Part 1: Core Entity System Changes

These changes apply to the `Entity` base class and `EntityRef` - the foundational types that any custom entity implementation must work with.

### 1.1 Expanded Contract for `_to_fxtr_cbor` and `_from_fxtr_cbor`

Previously, `_to_fxtr_cbor` and `_from_fxtr_cbor` were implicitly expected to be pure transformations between Python objects and CBOR data. We now **explicitly allow and document** that these methods may:

- **Load other entities** from the active scope during `_from_fxtr_cbor`
- **Store other entities** into the active scope during `_to_fxtr_cbor`

This enables entity implementations to recursively serialize/deserialize child entities as part of their own serialization.

The active scope (`ACTIVE_SCOPE.get()`) is guaranteed to be set correctly by the caller:
- When `entity.store(scope)` or `store_entity(entity, scope)` is called, `ACTIVE_SCOPE` is overridden to `scope` before calling `_to_fxtr_cbor`
- When `ref.load(scope)` is called, `ACTIVE_SCOPE` is overridden to `scope` before calling `_from_fxtr_cbor`

### 1.2 Async `_from_fxtr_cbor`

`_from_fxtr_cbor` may now return either:
- `Self` (synchronous) - for simple entities with no child loading
- `Awaitable[Self]` (asynchronous) - for entities that need to await child loads

```python
class Entity(abc.ABC):
    @classmethod
    @abc.abstractmethod
    def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> Self | Awaitable[Self]:
        """Convert the entity from its fxtr-CBOR representation.

        May load child entities from the active scope. If async loading is needed,
        return an Awaitable that resolves to the entity.

        The active scope (ACTIVE_SCOPE.get()) is guaranteed to be the scope
        that the load operation is targeting.
        """
        ...
```

Callers (converters, `EntityRef.load`) check with `inspect.isawaitable()` and await if needed.

### 1.3 Converter Signature Change

The `EntityRef.converter` signature changes to support async loading and explicit scope:

```python
# Old:
converter: Callable[[FxtrCbor], T_co]

# New:
converter: Callable[[FxtrCbor, FxtrScope], Awaitable[T_co]]
```

The converter is responsible for:
1. Overriding `ACTIVE_SCOPE` to the given scope
2. Calling `_from_fxtr_cbor(cbor, annotated_type)`
3. Awaiting the result if it's an Awaitable
4. Returning the entity

`build_converter_for(ty)` is updated to return async converters with this signature.

### 1.4 Updated `EntityRef.load()`

```python
async def load(self, scope: FxtrScope | None = None) -> T_co:
    if scope is None:
        scope = fxtr_scope.ACTIVE_SCOPE.get()

    # ... caching logic ...

    loaded_cbor = await scope.load_cbor(self.ref)
    entity = await self.converter(loaded_cbor, scope)  # Now async with scope

    # ... caching logic ...
    return entity
```

### 1.5 Custom Entity Example

A custom entity that loads a child entity during deserialization:

```python
class ParentEntity(Entity):
    _TYPE_ID = FxtrID.builtin("parent")

    def __init__(self, name: str, child: ChildEntity):
        self.name = name
        self.child = child

    def _to_fxtr_cbor(self) -> FxtrCbor:
        # Store child into active scope, get its ref
        child_ref = store_entity(self.child)  # Uses ACTIVE_SCOPE
        return {
            "$fxtr.ctx": get_active_context_marker(),
            "$fxtr.type": RawWeakFxtrID(inner_id=self._TYPE_ID),
            "name": self.name,
            "child": RawStrongFxtrID(inner_id=child_ref.ref),
        }

    @classmethod
    async def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> Self:
        # Load child from active scope
        child_id = cbor["child"].inner_id
        child_ref = EntityRef[ChildEntity](child_id, build_converter_for(ChildEntity))
        child = await child_ref.load()  # Uses ACTIVE_SCOPE
        return cls(name=cbor["name"], child=child)

    @classmethod
    def _fxtr_type_id(cls) -> FxtrID:
        return cls._TYPE_ID
```

---

## Part 2: DataclassEntity Convenience Layer

These changes are specific to `DataclassEntity` and mashumaro integration, providing automatic handling of inline entities without manual serialization code.

### New Type Aliases

| Type | Link Strength | Loading | Description |
|------|--------------|---------|-------------|
| `EntityRef[T]` | weak | lazy (`.load()`) | Existing - reference to entity |
| `ChildRef[T]` | strong | lazy (`.load()`) | Renamed from `StrongLinkedEntityRef` |
| `LinkedEntity[T]` | weak | auto-loaded | **New** - field type is `T`, auto-loaded during deserialization |
| `Child[T]` | strong | auto-loaded | **New** - field type is `T`, auto-loaded during deserialization |

Similarly for raw FxtrID types:
| Type | Link Strength |
|------|--------------|
| `FxtrID` | weak (default) |
| `ChildFxtrID` | strong (renamed from `StrongLinkedFxtrID`) |

Removed (redundant with defaults):
- `WeakLinkedEntityRef` (same as `EntityRef`)
- `WeakLinkedFxtrID` (same as `FxtrID`)

Removed (fragile mashumaro behavior):
- `Annotated[EntityRef, FxtrIDChildStrengthTag(...)]` serialization strategies
- `Annotated[FxtrID, FxtrIDChildStrengthTag(...)]` serialization strategies

## Core Mechanism

### Context Variable for Recursive Serdes

A new module-level contextvar tracks the serialization/deserialization state:

```python
@dataclasses.dataclass
class DeferredLoadPlaceholder:
    """Sentinel returned during collecting phase. Wraps the index in collected list."""
    index: int

@dataclasses.dataclass
class RecursiveLoadCollecting:
    """Phase 1: collecting EntityRefs that need to be loaded."""
    scope: FxtrScope
    collected: list[tuple[EntityRef[Any], Any]]  # (ref, type_annotation)

@dataclasses.dataclass
class RecursiveLoadRebuilding:
    """Phase 2: rebuilding with loaded entities."""
    scope: FxtrScope
    entities: list[Entity]
    collected: list[tuple[EntityRef[Any], Any]]  # Original collected list for validation
    next_index: int = 0

@dataclasses.dataclass
class RecursiveStore:
    """State during serialization."""
    scope: FxtrScope

RECURSIVE_SERDES_STATE: ContextVar[
    RecursiveLoadCollecting | RecursiveLoadRebuilding | RecursiveStore | None
] = ContextVar("recursive_serdes_state", default=None)
```

### Two-Phase Deserialization (DataclassEntity)

When `_from_fxtr_cbor` is called on a DataclassEntity with `Child[T]` or `LinkedEntity[T]` fields:

**Phase 1 - Collecting (synchronous):**
1. Set `RECURSIVE_SERDES_STATE` to `RecursiveLoadCollecting(scope, [])`
2. Run mashumaro's `from_dict()` / `BasicDecoder`
3. When serialization strategy encounters `Child[T]`/`LinkedEntity[T]`:
   - Deserialize as `EntityRef` (get FxtrID, build converter)
   - Append `(ref, type_annotation)` to collected list
   - Return `DeferredLoadPlaceholder(index)` as placeholder
4. Mashumaro creates object with placeholder values (discarded)
5. Reset contextvar

**If nothing collected:** Return the object synchronously.

**Phase 2 - Loading + Rebuilding (asynchronous):**
1. Load all collected EntityRefs in parallel using `anyio.create_task_group()`
2. Set `RECURSIVE_SERDES_STATE` to `RecursiveLoadRebuilding(scope, loaded_entities, collected, 0)`
3. Re-run mashumaro's `from_dict()` / `BasicDecoder`
4. When serialization strategy encounters `Child[T]`/`LinkedEntity[T]`:
   - Get entity from `entities[next_index]`
   - Increment `next_index`
   - Return the loaded entity
5. Reset contextvar
6. Return the final object

### Async `_from_fxtr_cbor` Return Type

`_from_fxtr_cbor` can now return `Self | Awaitable[Self]`:
- Returns `Self` synchronously if no refs to collect
- Returns `Awaitable[Self]` if refs need async loading

Callers (converters, `EntityRef.load`) check with `inspect.isawaitable()` and await if needed.

### Serialization (Storing)

When `_to_fxtr_cbor` is called on a DataclassEntity:
1. Set `RECURSIVE_SERDES_STATE` to `RecursiveStore(scope)` where scope = `ACTIVE_SCOPE.get()`
2. Run mashumaro's `to_dict()`
3. When serialization strategy encounters `Child[T]`/`LinkedEntity[T]`:
   - Temporarily reset `RECURSIVE_SERDES_STATE` to `None`
   - Call `store_entity(value, scope=scope)` to store the child
   - Restore contextvar
   - Return `RawStrongFxtrID` (for Child) or `RawWeakFxtrID` (for LinkedEntity)
4. Reset contextvar

### Scope Management

The scope flows through via `ACTIVE_SCOPE`:
- `entity.store(scope)` overrides `ACTIVE_SCOPE` before calling `_to_fxtr_cbor`
- `ref.load(scope)` passes scope to converter
- Converter overrides `ACTIVE_SCOPE` before calling `_from_fxtr_cbor`

This ensures all nested operations use the correct scope without adding scope parameters to `_to_fxtr_cbor`/`_from_fxtr_cbor`.

(The converter signature change is described in Part 1, section 1.3.)

## Serialization Strategies

### ChildSerializationStrategy / LinkedEntitySerializationStrategy

New mashumaro `SerializationStrategy` implementations for `Child[T]` and `LinkedEntity[T]`:

```python
@dataclasses.dataclass(frozen=True)
class InlineEntitySerializationStrategy(mashumaro.types.SerializationStrategy, Generic[T]):
    """Serialization strategy for Child[T] and LinkedEntity[T]."""
    strong: bool

    def serialize(self, value: T) -> RawStrongFxtrID | RawWeakFxtrID:
        state = RECURSIVE_SERDES_STATE.get()
        if not isinstance(state, RecursiveStore):
            raise RuntimeError("Cannot serialize inline entity outside of serialization context")

        scope = state.scope

        # Store child entity with fresh context
        token = RECURSIVE_SERDES_STATE.set(None)
        try:
            ref = store_entity(value, scope=scope)
        finally:
            RECURSIVE_SERDES_STATE.reset(token)

        return RawStrongFxtrID(inner_id=ref.ref) if self.strong else RawWeakFxtrID(inner_id=ref.ref)

    def deserialize(self, value: RawStrongFxtrID | RawWeakFxtrID, types: list[type]) -> T | DeferredLoadPlaceholder:
        state = RECURSIVE_SERDES_STATE.get()
        fxtr_id = value.inner_id
        target_type = types[0] if types else None

        if isinstance(state, RecursiveLoadCollecting):
            # Phase 1: collect ref
            converter = build_converter_for(target_type)
            ref = EntityRef(fxtr_id, converter)
            index = len(state.collected)
            state.collected.append((ref, target_type))
            return DeferredLoadPlaceholder(index=index)

        elif isinstance(state, RecursiveLoadRebuilding):
            # Phase 2: return loaded entity with validation
            index = state.next_index
            state.next_index += 1

            # Validate that we're returning the same ref/type we collected
            collected_ref, collected_type = state.collected[index]
            if collected_ref.ref != fxtr_id:
                raise RuntimeError(
                    f"Deserialization order mismatch at index {index}: "
                    f"collected {collected_ref.ref} but rebuilding got {fxtr_id}"
                )
            if collected_type != target_type:
                raise RuntimeError(
                    f"Type annotation mismatch at index {index}: "
                    f"collected {collected_type} but rebuilding got {target_type}"
                )

            return state.entities[index]

        else:
            raise RuntimeError("Cannot deserialize inline entity outside of deserialization context")
```

### Updates to DataclassEntity.Config

```python
class Config(mashumaro.config.BaseConfig):
    serialization_strategy = {
        # ... existing strategies ...

        # Remove: Annotated[FxtrID, FxtrIDChildStrengthTag(...)] entries
        # Remove: Annotated[EntityRef, FxtrIDChildStrengthTag(...)] entries

        # Keep: FxtrID, EntityRef, ChildFxtrID, ChildRef
        FxtrID: FxtrIDSerializationStrategy(strong=False),
        ChildFxtrID: FxtrIDSerializationStrategy(strong=True),
        EntityRef: EntityRefSerializationStrategy(strong=False),
        ChildRef: EntityRefSerializationStrategy(strong=True),

        # New: Inline entity types
        Child: InlineEntitySerializationStrategy(strong=True),
        LinkedEntity: InlineEntitySerializationStrategy(strong=False),
    }
```

## Example Usage

```python
@dataclasses.dataclass(frozen=True)
class Leaf(DataclassEntity, fxtr_type=...):
    value: int

@dataclasses.dataclass(frozen=True)
class Parent(DataclassEntity, fxtr_type=...):
    name: str
    child: Child[Leaf]  # Strong link, auto-loaded
    related: LinkedEntity[Leaf]  # Weak link, auto-loaded
    lazy_ref: ChildRef[Leaf]  # Strong link, lazy (must call .load())

# Storing - children are automatically stored
parent = Parent(
    name="test",
    child=Leaf(value=1),
    related=Leaf(value=2),
    lazy_ref=Leaf(value=3).store(),
)
ref = parent.store()

# Loading - child and related are automatically loaded
loaded = await ref.load()
assert loaded.child.value == 1  # Already loaded!
assert loaded.related.value == 2  # Already loaded!
loaded_lazy = await loaded.lazy_ref.load()  # Must explicitly load
```

## Implementation Steps

### Step 1: Core Entity System Changes (Part 1)

These implement the foundational changes that affect all Entity implementations.

- [x] Update `Entity._from_fxtr_cbor` docstring to document that it may return `Self | Awaitable[Self]`
- [x] Update `Entity._to_fxtr_cbor` and `_from_fxtr_cbor` docstrings to document that they may load/store entities from/to `ACTIVE_SCOPE`
- [x] Change `EntityRef.converter` signature to `Callable[[FxtrCbor, FxtrScope], Awaitable[T_co]]`
- [x] Update `build_converter_for()` to return async converters that override `ACTIVE_SCOPE` and handle `Awaitable` returns
- [x] Update `EntityRef.load()` to pass scope to converter and await result
- [x] Update `store_entity()` to override `ACTIVE_SCOPE` before calling `_to_fxtr_cbor`
- [x] Update `DataclassEntity.store()` to override `ACTIVE_SCOPE` before calling `_to_fxtr_cbor`

### Step 2: Tests for Core Changes

- [x] Add tests for custom Entity that stores child in `_to_fxtr_cbor`
- [x] Add tests for custom Entity with async `_from_fxtr_cbor` that loads children
- [x] Verify existing tests still pass (EntityRef, DataclassEntity basics)

### Step 3: Type Aliases and Renames (Part 2 prep)

- [x] Rename `StrongLinkedEntityRef` to `ChildRef` (kept backwards compatibility alias)
- [x] Rename `StrongLinkedFxtrID` to `ChildFxtrID`
- [x] Remove `WeakLinkedEntityRef` alias
- [x] Remove `WeakLinkedFxtrID` alias
- [x] Remove `Annotated[..., FxtrIDChildStrengthTag(...)]` serialization strategies from `DataclassEntity.Config`
- [x] Update existing tests to use new type names

### Step 4: DataclassEntity Inline Entity Support (Part 2)

- [x] Add `DeferredLoadPlaceholder`, `RecursiveLoadCollecting`, `RecursiveLoadRebuilding`, `RecursiveStore` dataclasses
- [x] Add `RECURSIVE_SERDES_STATE` contextvar
- [x] Add `InlineEntitySerializationStrategy` class (with `_InlineEntitySerializationHelper` for type preservation)
- [x] Add `Child[T]` type alias
- [x] Add `LinkedEntity[T]` type alias
- [x] Update `DataclassEntity.Config.serialization_strategy` with new types
- [x] Update `DataclassEntity._to_fxtr_cbor()` to set `RecursiveStore` context
- [x] Update `DataclassEntity._from_fxtr_cbor()` to implement two-phase loading
- [x] Update `_build_mashumaro_converter()` to work with new async converter signature

### Step 5: Tests for Inline Entities

- [x] Add tests for `Child[T]` basic store/load
- [x] Add tests for `LinkedEntity[T]` basic store/load
- [x] Add tests for nested inline entities (A contains Child[B] contains Child[C])
- [x] Add tests for mixed inline and ref fields in same entity
- [x] Add tests for error handling (parallel load, multiple children)
- [x] Add tests for `DeferredLoadPlaceholder` in `__post_init__` (users handle it)
- [x] Add tests for generic entities with inline children (`Wrapper[T]` where T has Child fields)

**Note on "load failure propagates" test:** The original plan included testing that errors during child loading propagate correctly. We decided not to add an explicit test for this because: (1) error propagation is handled automatically by `anyio.create_task_group()` - we're not writing custom error handling code, and (2) it's difficult to create a "missing entity" scenario since `store_cbor()` validates that all referenced IDs exist. The parallel loading tests verify the happy path works, and error propagation is standard anyio/Python behavior that doesn't need explicit coverage.

### Step 6: Documentation

- [x] Update CODE_OVERVIEW.md with new types and mechanism
- [x] Update notes/roundtrip_serdes.md with inline entity documentation and updated examples
- [x] Add docstrings to new classes and functions (inline in implementation)

## Edge Cases and Considerations

### `__post_init__` with Placeholders
Users who use `Child[T]` or `LinkedEntity[T]` fields and have `__post_init__` validation must handle `DeferredLoadPlaceholder` values gracefully during the collecting phase. The simplest approach is to check `isinstance(field, DeferredLoadPlaceholder)` and skip validation for those fields.

### Nested Inline Entities
When entity A has `Child[B]` and B has `Child[C]`:
1. Loading A triggers phase 1, collecting B's ref
2. Loading B (during A's phase 2) triggers B's own two-phase loading
3. B's phase 1 collects C's ref
4. B's phase 2 loads C and rebuilds B
5. A's phase 2 completes with the fully loaded B

This is recursive but each level manages its own contextvar state via tokens.

### Error Handling
If any ref fails to load during the parallel load phase, the error propagates immediately (fail-fast). The `anyio.create_task_group()` will cancel other pending loads and raise the first exception.

### Thread Safety
The contextvar mechanism is async-safe but the `next_index` mutation in `RecursiveLoadRebuilding` assumes single-threaded access during the rebuild phase. This is fine because mashumaro's `from_dict()` is synchronous and single-threaded.

## Testing Notes

Key scenarios to test:
1. Simple inline child (one level) ✓
2. Deeply nested inline children (3+ levels) ✓
3. Mixed inline and lazy refs in same entity ✓
4. Union types with inline children (not explicitly tested, but union handling is tested elsewhere)
5. Generic entities with inline children (`WrapperWithChild[T]` where T has Child fields) ✓
6. Custom Entity implementations using inline children (covered by Part 1 custom entity tests)
7. ~~Error propagation when child load fails~~ (see note above - relies on anyio, not custom code)
8. Parallel loading of multiple children ✓
