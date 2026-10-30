# Stage 2: Scope Freezing

## Motivation

We want to make it possible to freeze the state of a scope so that multiple child scopes can depend on it without worrying that the set of tracked nodes might change under them. Freezing guarantees immutability of the tracked node set, which is important for safe concurrent child scope usage.

The key invariant: a frozen scope's tracked node set never changes after freezing. This means no stores, loads (which track children), lifts, copies, abandons, or any other operation that could add or remove nodes from the scope.

## Design

### New instance variables on `FxtrScope`

- `_frozen: bool` — initialized to `False`
- `_is_root_scope: bool` — initialized to `False`, set to `True` only inside `_start_as_root_scope`

### Public API

- `FxtrScope.frozen` — read-only property returning `_frozen`
- `FxtrScope.freeze_scope()` — freezes the scope. Raises if:
  - The scope is a root scope (`_is_root_scope is True`) — root scopes can never be frozen because that would interfere with normal operation (e.g. `GLOBAL_SCOPE` needs to remain mutable for type registration etc.)
  - Any parent scope is not frozen — this ensures that no mutations to parent scopes can be visible through the frozen scope. Since root scopes can never be frozen, this means you can never freeze a scope that descends from a root scope. The only way to get a frozen scope is to create an isolated scope (no parents) and freeze that, or to inherit from an already-frozen isolated scope.

### `_check_not_frozen(method_name: str)`

Private helper that raises `RuntimeError` if `self._frozen` is `True`:

```
Scope has been frozen! `{method_name}` operations are not allowed on a frozen scope, as they affect the set of tracked nodes. To perform operations in a frozen scope, you should create a new FxtrScope that inherits from this one and perform your operations there.
```

### Methods that become disallowed when frozen

Each of these calls `_check_not_frozen("method_name")` at the top:

| Method | Why it mutates tracked set |
|--------|--------------------------|
| `store()` | Adds new node to arena |
| `store_cbor()` | Adds new node to arena |
| `load()` | Lifts node + tracks children |
| `load_cbor()` | Delegates to `load()` |
| `load_frozen_cbor()` | Delegates to `load()` |
| `deep_load()` | Delegates to `load()` |
| `lift()` | Attaches node from parent arena |
| `copy_from_scope()` | Copies node into arena |
| `abandon()` | Removes node from arena |
| `track_builtin()` | Adds builtin to arena |
| `clear_cache()` | See note below |

**Note on `clear_cache`:** We disallow `clear_cache` because `get_cached_value` relies on cache entries being present, and child scopes that depend on a frozen scope's cache entries shouldn't see them disappear. We may want to revisit this later — possibly by allowing `clear_cache` but instead disallowing `get_cached_value` on frozen scopes. However, there are scope inheritance questions to work through for that (child scopes look up parent caches), so for now we take the conservative approach.

### Methods that remain allowed when frozen

| Method | Why it's safe |
|--------|--------------|
| `get_cached_value()` | Only reads cache, doesn't mutate tracked set |
| `is_tracked()` | Pure read |
| `as_active_scope()` | Only changes context variable |
| `check_alive()` | Pure read |
| `is_alive()` | Pure read |
| `included_in_scope()` / `included_in_active_scope()` | Pure read |
| `get_context_marker_id()` | Pure read |
| `_deep_fetch()` and friends | Does not mutate the frozen scope's observable tracked interface. Note: `_deep_fetch_scan_sources` may create internal arenas on `self._store`, but this doesn't affect the scope's tracked set (the main arena). The memory store can change internally and this is OK. |

Child scope creation (`FxtrScope(include=[frozen_scope])`) is always fine — the child is a separate mutable scope that inherits from the frozen parent.

## Implementation Steps

### 1. ~~Add `_frozen` and `_is_root_scope` to `FxtrScope.__init__`~~ DONE

In `entity_ref_scope.py`, add to `__init__`:
```python
self._frozen = False
self._is_root_scope = False
```

### 2. ~~Add `frozen` property~~ DONE

```python
@property
def frozen(self) -> bool:
    return self._frozen
```

### 3. ~~Set `_is_root_scope` in `_start_as_root_scope`~~ DONE

Add `self._is_root_scope = True` inside `_start_as_root_scope`, before `self._evt_startup.set()`.

### 4. ~~Add `_check_not_frozen` helper~~ DONE

```python
def _check_not_frozen(self, method: str) -> None:
    if self._frozen:
        raise RuntimeError(
            f"Scope has been frozen! `{method}` operations are not allowed on a "
            "frozen scope, as they affect the set of tracked nodes. To perform "
            "operations in a frozen scope, you should create a new FxtrScope that "
            "inherits from this one and perform your operations there."
        )
```

### 5. ~~Add `freeze_scope` method~~ DONE

```python
def freeze_scope(self) -> None:
    self.check_alive()
    if self._is_root_scope:
        raise RuntimeError("Root scopes cannot be frozen")
    if self._frozen:
        return  # already frozen, idempotent
    for parent in self._parents:
        if not parent.frozen:
            raise RuntimeError(
                f"Cannot freeze scope: parent {parent} is not frozen. "
                "All parent scopes must be frozen before freezing a child scope."
            )
    self._frozen = True
```

### 6. ~~Add `_check_not_frozen` calls to mutating methods~~ DONE

Add `self._check_not_frozen("method_name")` as the first meaningful line (after `self.check_alive()` where present) in:

- `store()` — `_check_not_frozen("store")`
- `store_cbor()` — `_check_not_frozen("store_cbor")`
- `load()` — `_check_not_frozen("load")`
- `deep_load()` — `_check_not_frozen("deep_load")`
- `lift()` — `_check_not_frozen("lift")`
- `copy_from_scope()` — `_check_not_frozen("copy_from_scope")`
- `abandon()` — `_check_not_frozen("abandon")`
- `track_builtin()` — `_check_not_frozen("track_builtin")`
- `clear_cache()` — `_check_not_frozen("clear_cache")`, with a comment explaining the rationale and that we may revisit

Note: `load_cbor` and `load_frozen_cbor` delegate to `load()`, so they'll get the check transitively. But for clarity and better error messages, we should add explicit checks to them too with their own method names.

### 7. ~~Update CODE_OVERVIEW.md~~ DONE

### 8. ~~Write tests~~ DONE (33 tests)

Tests go in `tests/test_entity_ref_scope/test_frozen_scope.py`. Test cases:

**Freezing mechanics:**
- [ ] Freezing an isolated scope with no parents succeeds
- [ ] Freezing an isolated scope that has a frozen parent succeeds
- [ ] `frozen` property returns False before freeze, True after
- [ ] Freezing is idempotent (calling `freeze_scope()` twice doesn't error)
- [ ] Freezing a root scope raises RuntimeError
- [ ] Freezing a non-isolated scope (which has the global root as parent) raises RuntimeError
- [ ] Freezing a scope with an unfrozen non-root parent raises RuntimeError
- [ ] Freezing a scope that hasn't been entered yet raises (via `check_alive`)

**Disallowed operations on frozen scope:**
- [ ] `store()` raises RuntimeError
- [ ] `store_cbor()` raises RuntimeError
- [ ] `load()` raises RuntimeError
- [ ] `load_cbor()` raises RuntimeError
- [ ] `load_frozen_cbor()` raises RuntimeError
- [ ] `deep_load()` raises RuntimeError
- [ ] `lift()` raises RuntimeError
- [ ] `copy_from_scope()` raises RuntimeError
- [ ] `abandon()` raises RuntimeError
- [ ] `track_builtin()` raises RuntimeError
- [ ] `clear_cache()` raises RuntimeError
- [ ] All error messages contain the method name and the guidance about creating a child scope

**Allowed operations on frozen scope:**
- [ ] `get_cached_value()` works (store + load before freezing, then freeze, then get_cached_value)
- [ ] `is_tracked()` works
- [ ] `as_active_scope()` works
- [ ] `check_alive()` works
- [ ] `is_alive()` works
- [ ] `included_in_scope()` / `included_in_active_scope()` works
- [ ] `get_context_marker_id()` works

**Inheritance patterns:**
- [ ] Creating a child scope that includes a frozen scope works; the child is mutable
- [ ] The child scope can store, load, lift, etc. normally
- [ ] Freezing a chain: freeze parent, then freeze child that includes it
- [ ] Cannot freeze child before parent is frozen

**Integration test:**
- [ ] Full workflow: create isolated scope → store entities → freeze → create child scopes → mutate children → freeze children → exit. Specifically:
  1. Create an isolated scope and set it as active
  2. Store some entities in it
  3. Freeze it
  4. Create child scope A (includes frozen scope), store more entities, freeze A
  5. Create child scope B (includes frozen scope), store different entities, freeze B
  6. Verify `get_cached_value` works on the frozen scopes for entities that were loaded before freezing
  7. Exit all scopes
