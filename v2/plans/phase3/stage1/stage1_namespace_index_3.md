# Part 3: User-facing API

Adds the user-facing `FxtrQueryableNamespace` class that wraps the backend's
`IndexablePinningBackend` interface, translating between `Indexer` objects / scopes
and the backend's `FxtrID` / `ReadableArena` API.

## Overview

Part 2 made `SQLiteNamespaceBackend` implement `IndexablePinningBackend`, with methods
that take `FxtrID` + `ReadableArena`. Part 3 adds a thin user-facing layer that:

- Accepts `Indexer | FxtrID | EntityRef` inputs (same pattern as `pin()`)
- Manages scope lifecycle for returned entities (same pattern as `retrieve()`)
- Exposes a clean async generator for query results without arena management

**Modified file:** `fxtr2/data/fxtr_context_api.py`
**Updated docs:** `CODE_OVERVIEW.md`

## Scope Convention

Consistent with existing `pin()` / `retrieve()` patterns:

- **Inputs** (register, unregister, iter_matches constraints): Use the **user's scope**
  (active scope or explicitly provided `scope` arg). The namespace's own scope is an
  internal detail — the user's scope has the tracked objects needed to store entities.
- **Outputs** (list_indexers, iter_matches results): Stage through the **namespace scope**
  (via `unsafe_lazily_attach_from_arena`), then deliver to the user via `into_scope` /
  `copy_into_scope` (lift or copy, same as `retrieve()`).

## Class Hierarchy

```
FxtrNamespace (abc)                    # pin, unpin, retrieve, list_pins
├── FxtrQueryableNamespace (abc)       # + register_indexer, unregister_indexer, list_indexers, iter_matches
│   └── SQLiteFxtrNamespace            # wraps SQLiteNamespaceBackend
└── GitFxtrNamespace                   # wraps GitNamespaceBackend (not indexable)
```

`FxtrQueryableNamespace` overrides `_get_backend()` (abstract) to return
`IndexablePinningBackend` instead of `PersistentPinningBackend`.

## Refactor: _resolve_into_scope helper

The scope validation logic for outputs is currently duplicated in `_retrieve_impl`. Since
`list_indexers` and `iter_matches` need the same checks, extract a helper on `FxtrNamespace`:

```python
def _resolve_into_scope(
    self,
    into_scope: FxtrScope | None,
    copy_into_scope: bool,
    method_name: str,
) -> FxtrScope:
    """Resolve and validate the target scope for output operations.

    Returns the resolved scope. Raises ValueError with a helpful message
    (including method_name) if scope inclusion checks fail.
    """
    if into_scope is None:
        if not copy_into_scope and not self.scope.included_in_active_scope():
            raise ValueError(
                f"Active scope is not a child of scope for namespace {self};"
                f" cannot {method_name} into it without copying"
            )
        return fxtr_scope.get_active_scope()
    else:
        if not copy_into_scope and not self.scope.included_in_scope(into_scope):
            raise ValueError(
                f"Given scope {into_scope} is not a child of scope for namespace {self};"
                f" cannot {method_name} into it without copying"
            )
        return into_scope
```

Then refactor `_retrieve_impl` to use it, and use it in `list_indexers` and `iter_matches`.

## FxtrQueryableNamespace API

### _get_backend() → IndexablePinningBackend

Overrides `FxtrNamespace._get_backend()` with a narrower return type. Subclasses implement
this. Since `IndexablePinningBackend` extends `PersistentPinningBackend`, all existing
`FxtrNamespace` methods (pin, retrieve, etc.) continue to work.

### register_indexer(indexer, key_prefix, *, scope)

```python
async def register_indexer(
    self,
    indexer: Indexer | FxtrID | entity_serdes.EntityRef,
    key_prefix: str = "",
    *,
    scope: fxtr_scope.FxtrScope | None = None,
) -> None:
```

Same input pattern as `pin()`:

1. Resolve scope: `scope = scope or ACTIVE_SCOPE.get()`
2. Resolve entity_id from `indexer`:
   - `Indexer` → `store_entity(indexer, scope=scope).entity_id`
   - `EntityRef` → `entity.entity_id`
   - `FxtrID` → use directly
3. Verify tracked: `scope.is_tracked(entity_id)`
4. Delegate: `await self._get_backend().register_indexer(entity_id, scope._main_arena, key_prefix)`

### unregister_indexer(indexer, key_prefix, *, scope)

```python
async def unregister_indexer(
    self,
    indexer: Indexer | FxtrID | entity_serdes.EntityRef,
    key_prefix: str = "",
    *,
    scope: fxtr_scope.FxtrScope | None = None,
) -> None:
```

Same input resolution as `register_indexer`:

1. Resolve scope: `scope = scope or ACTIVE_SCOPE.get()`
2. Resolve entity_id (same match as above — store into user's scope if given an Indexer)
3. Delegate: `await self._get_backend().unregister_indexer(entity_id, key_prefix)`

Note: storing the Indexer into the user's scope (not namespace scope) is intentional.
The namespace scope is an internal detail; the user's scope has the tracked objects
needed to serialize the entity and compute its content-addressed ID.

### list_indexers(into_scope, copy_into_scope)

```python
async def list_indexers(
    self,
    into_scope: fxtr_scope.FxtrScope | None = None,
    copy_into_scope: bool = False,
) -> list[tuple[entity_serdes.EntityRef[WellKnownBuiltinIndexer], str]]:
```

Same output pattern as `retrieve()`:

1. Call backend: `registrations, source_arena = await self._get_backend().list_indexers()`
2. For each `(fxtr_id, prefix)` in registrations:
   - Attach to namespace scope: `self.scope._main_arena.unsafe_lazily_attach_from_arena(fxtr_id, source_arena)`
3. Close the source arena
4. Resolve target scope (default: active scope, with scope inclusion check)
5. For each fxtr_id: lift or copy into target scope
6. Return `[(EntityRef(fxtr_id, WellKnownBuiltinIndexer), prefix) for ...]`

Uses `EntityRef[WellKnownBuiltinIndexer]` (the type alias for
`TypedPathValueIndexer | TypedPathExistsIndexer` from `indexers.py`) so the loaded
entity is typed as a concrete indexer type.

### iter_matches(first, /, *rest, key_prefix, where, into_scope, copy_into_scope)

```python
async def iter_matches(
    self,
    first: IndexerConstraint,
    /,
    *rest: IndexerConstraint,
    key_prefix: str = "",
    where: BindingConstraint | None = None,
    into_scope: fxtr_scope.FxtrScope | None = None,
    copy_into_scope: bool = False,
) -> AsyncGenerator[tuple[str, FxtrID, dict[str, IndexerColumnValue]], None]:
```

Takes `IndexerConstraint` (with `Indexer` objects), not `IndexerConstraintByID`.
Returns `(key, entity_id, bindings)` — no arena. Entities tracked in `into_scope`.

**Input conversion** — for each constraint, resolve the indexer's FxtrID. Since these
indexers must already be registered, their entities are already in the store. We store
them into the active scope to compute the content-addressed ID:

```python
scope = fxtr_scope.get_active_scope()

def _to_by_id(c: IndexerConstraint) -> IndexerConstraintByID:
    match c.indexer:
        # In most cases the user has an Indexer object from constructing
        # the constraint. Store it to compute its content-addressed ID.
        case _:
            entity_id = entity_serdes.store_entity(c.indexer, scope=scope).entity_id
    return IndexerConstraintByID(
        indexer_id=entity_id,
        key_variables=c.key_variables,
        condition=c.condition,
    )
```

**Output handling** — same pattern as `retrieve()`:

1. Resolve target scope (default: active scope, with scope inclusion check)
2. Call backend `iter_matches` (returns an async iterator)
3. For each yielded `(key, entity_id, bindings, arena)`:
   a. Attach entity to namespace scope: `self.scope._main_arena.unsafe_lazily_attach_from_arena(entity_id, arena)`
   b. Close the arena
   c. Lift or copy entity into target scope
   d. Yield `(key, entity_id, bindings)`

Usage:
```python
async for key, entity_id, bindings in ns.iter_matches(constraint):
    ref = EntityRef(entity_id, MyEntityType)
    entity = await ref.load()
```

## SQLiteFxtrNamespace Changes

Minimal:

- [ ] Change parent class from `FxtrNamespace` to `FxtrQueryableNamespace`
- [ ] `_get_backend()` return type annotation changes to `SQLiteNamespaceBackend`
      (which implements `IndexablePinningBackend` — no runtime change, just the type
      narrows to satisfy `FxtrQueryableNamespace`'s return type)

No other changes needed — `SQLiteNamespaceBackend` already implements `IndexablePinningBackend`.

## Testing

### Unit-level tests

- [ ] `register_indexer` with Indexer object stores and delegates
- [ ] `register_indexer` with FxtrID delegates directly
- [ ] `register_indexer` with EntityRef extracts ID and delegates
- [ ] `unregister_indexer` with Indexer stores into user's scope (not namespace scope)
- [ ] `unregister_indexer` with FxtrID delegates directly
- [ ] `list_indexers` attaches entities to namespace scope and lifts to active scope
- [ ] `list_indexers` with `copy_into_scope=True` copies instead of lifting
- [ ] `list_indexers` returns `EntityRef[WellKnownBuiltinIndexer]` that deserializes correctly
- [ ] `iter_matches` converts IndexerConstraint → IndexerConstraintByID
- [ ] `iter_matches` results tracked in into_scope (default: active scope)
- [ ] `iter_matches` with `copy_into_scope=True` copies results
- [ ] `iter_matches` with no matches yields nothing

### Integration tests (end-to-end with SQLiteFxtrNamespace)

- [ ] Full cycle: open namespace → register indexer → pin entities → query → verify results
- [ ] Multiple indexers: register two indexers, query with both, verify join results
- [ ] Unregister: register, query succeeds, unregister, verify query fails
- [ ] Scope isolation: results from iter_matches accessible in correct scope
- [ ] list_indexers returns loadable EntityRefs
- [ ] Pin with overwrite: overwrite a pin, verify index updates
- [ ] Error cases:
  - [ ] iter_matches with unregistered indexer
  - [ ] register_indexer with strict indexer on incompatible existing pin
  - [ ] list_indexers on empty namespace
  - [ ] scope mismatch: namespace scope not included in active scope

## Implementation Checklist

- [x] Add `_resolve_into_scope()` helper to `FxtrNamespace`
- [x] Refactor `_retrieve_impl` to use `_resolve_into_scope("retrieve")` / `_resolve_into_scope("try_retrieve")`
      (note: `_retrieve_impl` has an `allow_not_found` flag — use `"try_retrieve"` vs `"retrieve"` accordingly)
- [x] Add `FxtrQueryableNamespace` class to `fxtr_context_api.py`
  - [x] Override `_get_backend()` returning `IndexablePinningBackend`
  - [x] `register_indexer()` (Indexer | FxtrID | EntityRef, same pattern as pin)
  - [x] `unregister_indexer()` (Indexer | FxtrID | EntityRef)
  - [x] `list_indexers()` (uses `_resolve_into_scope("list_indexers")`)
  - [x] `iter_matches()` (uses `_resolve_into_scope("iter_matches")`, constraint conversion)
- [x] Update `SQLiteFxtrNamespace`:
  - [x] Change parent to `FxtrQueryableNamespace`
  - [x] Narrow `_get_backend()` return type
- [x] Add imports for indexing types in `fxtr_context_api.py`
- [x] Write tests
- [x] Update `CODE_OVERVIEW.md`
  - [x] Add `store_indexing/` to file structure
  - [x] Add section on indexing concepts and FxtrQueryableNamespace
  - [x] Update `fxtr_context_api.py` section
  - [x] Update test counts
