# Step 2a: `run_fallible`, `save_checkpoint`, `load_checkpoint`

Extends the core workflow system with two capabilities around cache slot behavior:
1. **Fallible runs** — cache errors as valid outcomes, replay them later
2. **Checkpoints** — crash recovery for partial executions

(`run_uncached` is deferred to step 2b as a separate feature.)

## 1. `run_fallible`

### Concept

`run_fallible` is a method on `CacheSlot` (not on `StepContext` or `TopLevelEntrypoint`). It runs a step where errors are treated as valid cached outcomes rather than workflow-aborting failures.

Inside a fallible step, everything works normally: inner `run()` calls are infallible and abort+poison as usual. But at the `run_fallible` boundary, the abort is "handled" — the exception is re-raised as a catchable `FallibleRunError` without poisoning the caller. The caller may catch it or let it propagate (in which case it becomes an abort at the next level up).

**No foreign cache-slot args.** `run_fallible` forbids passing cache slots as positional/keyword args. This simplifies error caching: no need to track or replay-check foreign slot deps when the cached outcome is an error. Internal cache slots via `ctx.cache_slot()` are still allowed, and those internal slots can be passed to inner `run()` calls normally.

### Error types

New error hierarchy in `errors.py`:

```python
class FallibleRunError(Exception):
    """Base for errors cached by run_fallible. Always catchable."""

class CustomFallibleRunError(FallibleRunError):
    """The step explicitly raised this with a string message."""
    # original_type_name is None for custom errors
    original_type_name = None
    message: str
    serialized_traceback: str  # stringified traceback from original raise

class WrappedFallibleRunError(FallibleRunError):
    """The step raised some other exception, lossily stringified."""
    original_type_name: str  # e.g. "ValueError"
    message: str
    serialized_traceback: str
```

These are fully serializable. On the first execution and on replay, the same error type is raised with the same data (no pickled exception state). The step can raise `CustomFallibleRunError("some message")` explicitly to control exactly what gets cached; any other exception type gets wrapped in `WrappedFallibleRunError`.

### Entity: `StepError`

New entity in `entities.py`, sibling to `StepOutput`:

```python
@dataclasses.dataclass(frozen=True)
class StepError(DataclassEntity, ...):
    """Cached error outcome from a run_fallible call."""
    original_type_name: str | None  # exception class name (e.g. "ValueError"), None for custom
    message: str
    serialized_traceback: str
```

### Storing errors in `SlotFinished`

`SlotFinished` stores its result as `EntityRef[StepOutput]`. To also support errors, change this to `EntityRef[StepOutput | StepError]`:

```python
@dataclasses.dataclass(frozen=True)
class SlotFinished(DataclassEntity, ...):
    cache_key_ref: EntityRef[CacheKeyEntity]
    result_ref: EntityRef[StepOutput | StepError]  # was EntityRef[StepOutput]
    slot_deps_ref: EntityRef[TrackedForeignSlotDeps]
```

When the result is a `StepError`, `slot_deps_ref` will contain an empty `TrackedForeignSlotDeps(runs=())` since no foreign cache-slot args are allowed.

### Interaction with `run` (infallible)

When `run()` encounters a `SlotFinished` whose `result_ref` loads to a `StepError`, it must abort the workflow with a clear error:

```
CacheSlotError: Cannot run infallible step in slot '<pin_key>': slot contains a
cached error from a prior run_fallible call. Clear the cache to retry.
```

This prevents silently continuing past a cached failure when the caller expected success.

When `run_fallible()` encounters `SlotFinished` whose result is a `StepError` with matching cache key, it re-raises the cached error (reconstruct `CustomFallibleRunError` or `WrappedFallibleRunError` from `StepError` entity).

When `run_fallible()` encounters `SlotFinished` with a `StepOutput` (success) and matching cache key, it returns the cached result normally — a fallible call to a step that succeeded is fine.

### Dispatch flow

Add a `fallible: bool = False` parameter to `dispatch_in_slot` and `_execute_in_slot`. The branches are small and localized:

- **State check**: when loading a `SlotFinished`, inspect the loaded result entity. If it's a `StepError` and `fallible=False`, raise `CacheSlotError`. If it's a `StepError` and `fallible=True`, reconstruct and re-raise the `FallibleRunError`.
- **No-cache-slot-arg validation**: when `fallible=True`, assert no cache slots in resolved args (after resolution, before cache key computation).
- **Error handling**: when `fallible=True` and the step raises (or is poisoned), convert to `FallibleRunError`, persist `StepError` + `SlotFinished`, raise the `FallibleRunError`. When `fallible=False`, existing behavior (leave Partial, raise `StepAbortError`).
- **Tracing**: still set up normally — the step can use `ctx.cache_slot()` internally. `build_slot_deps()` will produce zero `TrackedForeignRun` entries (all purely internal), stored in an empty `TrackedForeignSlotDeps`.

### CacheSlot ABC change

Add `run_fallible` to `CacheSlot` ABC:

```python
@abc.abstractmethod
def run_fallible(self, step: StepHandle, *args, **kwargs) -> Pending[Any]:
    """Run a step, caching errors as valid outcomes.
    Cache-slot args are not allowed (raises TypeError).
    """
    ...
```

`LiveCacheSlot.run_fallible` validates no cache slots in args, then delegates to `self._backend.dispatch_in_slot(self, step, args, kwargs, fallible=True)`.

`TracingCacheSlot.run_fallible` routes through a `dispatch_fallible_from_step` method on the step context — similar to `dispatch_from_step` but asserts no foreign slots in args and passes `fallible=True` to the backend.

### Poisoning boundary behavior

Inside `_execute_in_slot` when `fallible=True`:
1. Execute the step normally (same tracing, same sub-step containment)
2. If the step raises (or is poisoned by sub-step abort):
   - Convert the exception to `FallibleRunError`: if it's already a `CustomFallibleRunError`, keep it; for anything else (including `StepAbortError`), wrap in `WrappedFallibleRunError`
   - Persist `StepError` entity + `SlotFinished` state (with empty slot_deps)
   - Delete checkpoint if present (slot is now finished)
   - Raise the `FallibleRunError` (set on the Pending)
3. The caller receives `FallibleRunError` — a normal `Exception`, not a `BaseException`. No poisoning occurs.

Important: if the fallible step itself catches a `StepAbortError` from an inner `run()` call and returns normally, that's still poisoning — the step claimed success but a sub-step failed. The existing poisoning check (`check_for_aborts()`) handles this: if any sub-step aborted, we force an error even if the step returned. For fallible runs, this forced error gets cached as a `StepError` rather than leaving the slot Partial.

## 2. `save_checkpoint` / `load_checkpoint`

### Concept

`StepContext` gets two new async methods for crash recovery. The underlying storage lives on the `LiveCacheSlot` as private methods; `LiveStepContext` delegates to these.

```python
# On StepContext ABC:
async def save_checkpoint(self, data: SerializableTypes) -> None:
    """Overwrite the checkpoint for this step's slot. Last-write-wins."""

async def load_checkpoint(self) -> SerializableTypes | None:
    """Load the most recent checkpoint, or None if no checkpoint exists.
    Works across executions: a checkpoint saved during a Partial run
    survives restart and is loadable on re-execution of the same slot.
    """
```

### Storage

Stored at `<slot_pin_key>/ckpt` as a pinned entity:

```python
@dataclasses.dataclass(frozen=True)
class StepCheckpoint(DataclassEntity, ...):
    """Checkpoint data saved by a running step."""
    value: SerializableTypes
```

### LiveCacheSlot private methods

```python
class LiveCacheSlot:
    async def _save_checkpoint(self, data: SerializableTypes, scope: FxtrScope) -> None:
        _validate_result(data)  # reuse the serializable-types validator
        ckpt = StepCheckpoint(value=data)
        ref = ckpt.store(scope=scope)
        await self._namespace.pin(
            f"{self._pin_key}/ckpt", ref, overwrite=True, scope=scope
        )

    async def _load_checkpoint(self) -> SerializableTypes | None:
        ref = await self._namespace.try_retrieve(
            f"{self._pin_key}/ckpt", type=StepCheckpoint
        )
        if ref is None:
            return None
        ckpt = await ref.load()
        return ckpt.value

    async def _delete_checkpoint(self) -> None:
        # Need to verify FxtrNamespace unpin API; may need try/except
        # for missing pins, or a check-before-delete pattern.
        await self._namespace.unpin(f"{self._pin_key}/ckpt")
```

### LiveStepContext delegation

```python
class LiveStepContext:
    async def save_checkpoint(self, data: SerializableTypes) -> None:
        await self._slot._save_checkpoint(data, self._backend.get_scope())

    async def load_checkpoint(self) -> SerializableTypes | None:
        return await self._slot._load_checkpoint()
```

### Cleanup

When a slot transitions to `SlotFinished` (success or error), the checkpoint pin is deleted. This happens at the end of `_execute_in_slot`, after persisting the final state. Use `slot._delete_checkpoint()`.

## Implementation Steps

- [x] 1. **Error types**: Add `FallibleRunError`, `CustomFallibleRunError`, `WrappedFallibleRunError` to `errors.py`
- [x] 2. **Entities**: Add `StepError`, `StepCheckpoint` to `entities.py`. Change `SlotFinished.result_ref` to `EntityRef[StepOutput | StepError]`. Update `SlotState` if needed.
- [x] 3. **CacheSlot ABC**: Add `run_fallible` abstract method
- [x] 4. **LiveCacheSlot**: Implement `run_fallible` (validate no cache-slot args, delegate with `fallible=True`). Add `_save_checkpoint`, `_load_checkpoint`, `_delete_checkpoint` private methods.
- [x] 5. **TracingCacheSlot**: Implement `run_fallible` routing through step context
- [x] 6. **_Backend / dispatch**: Add `fallible` parameter to `dispatch_in_slot`. Update `_execute_in_slot` to handle `StepError` in `SlotFinished`, fallible error caching, and checkpoint cleanup.
- [x] 7. **StepContext ABC**: Add `save_checkpoint`, `load_checkpoint` abstract methods
- [x] 8. **LiveStepContext**: Implement `save_checkpoint`, `load_checkpoint` (delegating to slot). Add `dispatch_fallible_from_step`.
- [x] 9. **Update `__init__.py`**: Export new error types
- [x] 10. **Tests: `run_fallible`**
    - [x] a. Basic fallible run: step raises, error cached, replayed on second call
    - [x] b. Fallible run success: step succeeds, result cached normally
    - [x] c. Cache-slot args rejected by `run_fallible`
    - [x] d. `run` on a slot with cached error → `CacheSlotError`
    - [x] e. `run_fallible` on a slot with cached success → returns cached result
    - [x] f. Inner `run()` abort bubbles up, cached as error in fallible slot
    - [x] g. Poisoning: inner abort caught by fallible step, still becomes error
    - [x] h. `CustomFallibleRunError`: explicit raise, same message on replay
    - [x] i. `WrappedFallibleRunError`: arbitrary exception, stringified on replay
    - [x] j. Nested: fallible step dispatches inner fallible step
    - [x] k. Caller catches `FallibleRunError`, continues without poisoning
    - [x] l. Caller doesn't catch `FallibleRunError`, becomes abort at next level
    - [x] m. `run_fallible` from inside a step (via TracingCacheSlot path)
- [x] 11. **Tests: `save_checkpoint` / `load_checkpoint`**
    - [x] a. Save and load within same execution
    - [x] b. Save, abort, resume — checkpoint survives across executions
    - [x] c. Overwrite: second save replaces first
    - [x] d. Load with no checkpoint → None
    - [x] e. Checkpoint cleaned up on successful finish
    - [x] f. Checkpoint cleaned up on fallible error finish
    - [x] g. Checkpoint data validation (rejects non-serializable types)
- [x] 12. **Update CODE_OVERVIEW.md**

## Deferred to step 2b: `run_uncached`

Open questions to resolve for `run_uncached`:
- **`is_internal` flag**: Currently used to skip parent split-state checks. Need to verify it doesn't affect pin key derivation or replay logic in ways that would cause issues if an uncached step uses `cache_slot()` internally and those slots get passed through replay tracking.
- **Top-level namespace**: User must provide namespace + prefix for top-level `run_uncached`, since lazily creating a `SQLiteFxtrNamespace` inside the task group has tricky async lifecycle issues (anyio deadlock risk with context managers across task boundaries). A worker-task approach (spawn into task group, signal start, wait for shutdown) would work but adds complexity.
- **Prefix cleanup**: `StepContext.run_uncached` needs `_cleanup_prefix` to delete all pins under the temporary `/u/<uuid>` prefix when the step finishes. Need to check `FxtrNamespace` API for prefix-based deletion or implement pin tracking.
