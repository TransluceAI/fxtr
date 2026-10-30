# Step 1 Post-3: Split State Integrity

## Problem

`_ensure_split` currently accepts Partial/Finished as "fine" with a comment about internal cache slots. This masks a real integrity violation: if you access a subslot of a slot that has a run (Partial/Finished), that's a bug — you're mixing the "split into subslots" and "contains a run" modes on the same slot.

The two child-creation paths have different parent-state requirements:

| Parent state | subslot child (`/s/`) | internal cache slot child (`/c/`) |
|---|---|---|
| Empty | OK → transition to Split | Shouldn't happen |
| Split | OK | Shouldn't happen |
| Partial | **Error** — can't subslot a running slot | OK — step is executing |
| Finished | **Error** — can't subslot a finished slot | **Error** — step is done, shouldn't write |

For internal cache slot children, the parent's Partial state is established by `_execute_in_slot` *before* the step runs, so the ancestor chain is already valid by construction. No lazy ancestor check is needed.

## Plan

### 1. Track child kind on `LiveCacheSlot`

Add an `_is_internal: bool` field (default `False`), set to `True` when created via `_internal_cache_slot`. This distinguishes the two child-creation paths.

```python
def __init__(self, ..., is_internal: bool = False) -> None:
    ...
    self._is_internal = is_internal
```

`_internal_cache_slot` passes `is_internal=True`. `subslot` leaves the default.

### 2. Make `_ensure_split` strict

`_ensure_split` should only accept Empty (→ Split) and Split. Partial/Finished are errors:

```python
async def _ensure_split(self) -> None:
    state = await self._load_state()
    if isinstance(state, SlotSplit):
        return
    if state is not None:
        raise CacheSlotError(
            f"Cannot use slot '{self._pin_key}' for subslots: "
            f"already has state {type(state).__name__}"
        )
    # Empty → Split (ensure ancestors first)
    if self._parent is not None and not self._is_internal:
        await self._parent._ensure_split()
    await self._persist_state(SlotSplit())
```

### 3. Skip ancestor check for internal children in `_save_state`

`_save_state` currently unconditionally calls `parent._ensure_split()`. Internal cache slot children should skip this, since their parent's state is managed by the execution lifecycle:

```python
async def _save_state(self, state: SlotState) -> EntityRef[SlotState]:
    if self._parent is not None and not self._is_internal:
        await self._parent._ensure_split()
    return await self._persist_state(state)
```

### 4. Update CODE_OVERVIEW.md

- [x] Step 1: Track child kind on `LiveCacheSlot`
- [x] Step 2: Make `_ensure_split` strict
- [x] Step 3: Skip ancestor check for internal children in `_save_state`
- [x] Step 4: Verify tests pass
- [x] Step 5: Update CODE_OVERVIEW.md
