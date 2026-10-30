# Scratch Notes: Recursive Entity Implementation Details

## Contextvar Token Semantics

Each `contextvar.set()` returns a token that can `reset()` to the previous value. This allows proper nesting:

```python
state1 = RecursiveLoadCollecting(...)
token1 = RECURSIVE_SERDES_STATE.set(state1)
# ... nested call ...
state2 = RecursiveLoadCollecting(...)
token2 = RECURSIVE_SERDES_STATE.set(state2)
# ... do work ...
RECURSIVE_SERDES_STATE.reset(token2)  # Back to state1
# ... more work ...
RECURSIVE_SERDES_STATE.reset(token1)  # Back to None/previous
```

This is critical for nested inline entities to work correctly.

## Why Reset to None Before Storing Child?

During serialization of a `Child[T]` field:
```python
def serialize(self, value: T) -> RawStrongFxtrID | RawWeakFxtrID:
    state = RECURSIVE_SERDES_STATE.get()
    scope = state.scope

    # Why reset to None here?
    token = RECURSIVE_SERDES_STATE.set(None)
    try:
        ref = store_entity(value, scope=scope)
    finally:
        RECURSIVE_SERDES_STATE.reset(token)
```

The reset to None serves two purposes:
1. **Clarity**: Makes it explicit that we're starting a fresh serialization context for the child
2. **Safety**: If `store_entity` is called on something that ISN'T a DataclassEntity (custom Entity), it won't accidentally see the parent's RecursiveStore state

When `store_entity` calls `child._to_fxtr_cbor()` on a DataclassEntity, that will set up its own RecursiveStore context. So the None -> RecursiveStore transition happens naturally.

## ACTIVE_SCOPE Override Points

Places where ACTIVE_SCOPE must be overridden:

1. **`DataclassEntity.store(scope)`**: Before calling `_to_fxtr_cbor()`
2. **`store_entity(entity, scope)`**: Before calling `entity._to_fxtr_cbor()`
3. **Async converter**: Before calling `_from_fxtr_cbor()`
4. **`InlineEntitySerializationStrategy.serialize()`**: When calling `store_entity()` - passes scope explicitly

The key insight is that ACTIVE_SCOPE should always match the scope that owns the operation. Nested operations override it for their duration.

## Converter Change Details

Old signature and usage:
```python
# In EntityRef
converter: Callable[[FxtrCbor], T_co]

# In build_converter_for
def convert_entity(cbor: FxtrCbor) -> Any:
    return base._from_fxtr_cbor(cbor, ty)

# In load()
entity = self.converter(loaded)
```

New signature and usage:
```python
# In EntityRef
converter: Callable[[FxtrCbor, FxtrScope], Awaitable[T_co]]

# In build_converter_for
async def convert_entity(cbor: FxtrCbor, scope: FxtrScope) -> Any:
    token = ACTIVE_SCOPE.set(scope)
    try:
        result = base._from_fxtr_cbor(cbor, ty)
        if inspect.isawaitable(result):
            return await result
        return result
    finally:
        ACTIVE_SCOPE.reset(token)

# In load()
entity = await self.converter(loaded, scope)
```

## _from_fxtr_cbor Implementation Sketch

```python
@classmethod
def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> Self | Awaitable[Self]:
    scope = ACTIVE_SCOPE.get()

    # Phase 1: Collecting
    state = RecursiveLoadCollecting(scope, [])
    token = RECURSIVE_SERDES_STATE.set(state)
    try:
        if annotated_type is not None:
            obj = _build_mashumaro_converter(annotated_type)(cbor)
        else:
            obj = cls.from_dict(cbor)
        collected = state.collected[:]  # Copy before reset
    finally:
        RECURSIVE_SERDES_STATE.reset(token)

    if not collected:
        # No inline entities - return sync
        return obj

    # Has inline entities - return async loader
    async def load_and_rebuild() -> Self:
        # Load all refs in parallel
        entities: list[Entity] = [None] * len(collected)  # type: ignore

        async def load_one(i: int, ref: EntityRef[Any]) -> None:
            entities[i] = await ref.load(scope)

        async with anyio.create_task_group() as tg:
            for i, (ref, _) in enumerate(collected):
                tg.start_soon(load_one, i, ref)

        # Phase 2: Rebuilding
        rebuild_state = RecursiveLoadRebuilding(scope, entities, 0)
        rebuild_token = RECURSIVE_SERDES_STATE.set(rebuild_state)
        try:
            if annotated_type is not None:
                return _build_mashumaro_converter(annotated_type)(cbor)
            else:
                return cls.from_dict(cbor)
        finally:
            RECURSIVE_SERDES_STATE.reset(rebuild_token)

    return load_and_rebuild()
```

## Mashumaro Type Registration Challenge

For `Child[T]` and `LinkedEntity[T]` to work in mashumaro's `serialization_strategy`, we need mashumaro to recognize these type aliases. The challenge is that type aliases like:

```python
type Child[T: Entity] = Annotated[T, ChildMarker()]
```

May or may not be recognized depending on mashumaro version and how the type is used.

Fallback approaches:
1. Use `GenericSerializableType` mixin (like EntityRef does)
2. Register post-processors in mashumaro config
3. Use discriminated field approach

Need to verify mashumaro behavior with the specific type alias patterns we want to use.

## Alternative: Wrapper Class Instead of Type Alias

If type aliases don't work well with mashumaro, we could use wrapper classes:

```python
@dataclasses.dataclass(frozen=True)
class Child(Generic[T], mashumaro.types.GenericSerializableType):
    """A strong-linked inline entity."""
    _entity: T

    def _serialize(self, types) -> RawStrongFxtrID:
        # ... store and return ID

    @classmethod
    def _deserialize(cls, value: RawStrongFxtrID, types) -> Self:
        # ... load and return wrapper

    def __getattr__(self, name):
        # Proxy to inner entity
        return getattr(self._entity, name)
```

But this changes the API - users would access `parent.child._entity` or use proxy methods. Less ergonomic than the type alias approach.

## Testing Strategy

1. **Unit tests for contextvar state machine**: Verify state transitions work correctly
2. **Unit tests for serialization strategies**: Verify serialize/deserialize methods
3. **Integration tests for simple cases**: One-level inline children
4. **Integration tests for nested cases**: Multi-level inline children
5. **Integration tests for mixed cases**: Inline + ref in same entity
6. **Error case tests**: Load failures, invalid states
7. **Regression tests**: Ensure existing EntityRef/ChildRef behavior unchanged
