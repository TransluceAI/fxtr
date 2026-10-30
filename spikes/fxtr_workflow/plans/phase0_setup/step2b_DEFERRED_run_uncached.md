# Step 2b: `run_uncached`

Fire-and-forget step execution with no persistent cache. Available on both `StepContext` and `TopLevelEntrypoint`.

## Concept

`run_uncached` dispatches a step without durable caching. If the containing step is replayed, the uncached call is re-executed and may produce different results. Useful for sub-computations that don't need caching or where caching would be wasteful.

Implementation: allocate a hidden internal cache slot at `/u/<uuid>`, run the step there with fallible semantics, and clean up the slot's storage immediately when done (success or error).

## Design decisions

**Error semantics:** `run_uncached` always uses `run_fallible` semantics. Exceptions become `FallibleRunError` (a normal `Exception`), never `StepAbortError`. Rationale: since nothing is durably cached, there's no corruption risk from errors, so poisoning the caller is unnecessary. The caller can catch `FallibleRunError` or let it propagate naturally.

**Foreign slot tracking:** Uncached runs are NOT tracked in the parent step's `ForeignRunTracker`. Since the uncached slot is ephemeral and re-executed on every replay, there's nothing to verify. If the uncached step reads from a foreign cache slot or dispatches a `run()` into one, that inner run records and caches itself in the foreign slot as normal — the uncached wrapper doesn't need to worry about it.

**Cache slot args:** Allowed (unlike `run_fallible`). Since there's no cached result to invalidate, the safeguard doesn't apply. Tracing proxies in args are unwrapped before dispatch (just like a normal `dispatch_from_step` call), and `_execute_in_slot` re-wraps them with fresh tracing for the inner step.

**Internal cache slots inside uncached steps:** Ephemeral. `ctx.cache_slot()` inside an uncached step creates slots under `/u/<uuid>/c/<key>`, which are cleaned up along with the rest of the temp tree. These provide caching only during the single execution.

**Cleanup timing:** Immediately after the Pending resolves (success or error), not when the parent step finishes. This frees storage sooner and is safe because nothing references those pins after resolution.

**Sub-step abort behavior:** If a sub-step inside the uncached run aborts, the fallible semantics mean the abort is converted to `FallibleRunError` at the `run_uncached` boundary. The parent step sees `FallibleRunError`, not `StepAbortError`, so `check_for_aborts()` won't detect it — the parent decides whether to catch or propagate.

## StepContext.run_uncached

```python
# In StepContext ABC:
@abstractmethod
def run_uncached(self, step: StepHandle, *args, **kwargs) -> Pending[Any]:
    """Run a step without durable caching. Re-executes on every replay."""
    ...
```

Implementation in `LiveStepContext`:

```python
def run_uncached(self, step, *args, **kwargs):
    temp_key = f"{self._slot._pin_key}/u/{uuid4()}"
    temp_slot = LiveCacheSlot(
        self._slot._namespace, temp_key, self._backend,
        parent=self._slot, is_internal=True
    )

    # Unwrap tracing proxies (same as dispatch_from_step)
    unwrapped_args = tuple(_unwrap_tracing(a) for a in args)
    unwrapped_kwargs = {k: _unwrap_tracing(v) for k, v in kwargs.items()}

    # Dispatch with fallible=True and cleanup callback
    pending = self._backend.dispatch_uncached(
        temp_slot, step, unwrapped_args, unwrapped_kwargs
    )

    # Track for containment (parent waits for all sub-steps)
    self._spawned_pendings.append(pending)
    return pending
```

Key points:
- Uses `is_internal=True` to skip parent split-state checks (parent is in Partial state from the running step, can't be Split).
- Does NOT call `self._tracker.record_run()` — uncached runs are invisible to the parent's replay tracking.
- Unwraps tracing proxies before dispatch. Inside the uncached step, `_execute_in_slot` will create its own `LiveStepContext` with a fresh tracker and re-wrap cache slot args with new tracing proxies.
- The Pending is tracked for containment (`_spawned_pendings`) so `wait_for_all_substeps()` waits for it.

## TopLevelEntrypoint.run_uncached

The namespace for uncached runs is provided as a constructor argument:

```python
class TopLevelEntrypoint:
    def __init__(
        self,
        uncached_namespace: FxtrNamespace | None = None,
        uncached_prefix: str = "",
    ) -> None:
        self._uncached_namespace = uncached_namespace
        self._uncached_prefix = uncached_prefix
        ...

    def run_uncached(self, step: StepHandle, *args, **kwargs) -> Pending[Any]:
        if self._uncached_namespace is None:
            raise RuntimeError(
                "run_uncached requires an uncached_namespace; "
                "pass one to TopLevelEntrypoint()"
            )
        temp_key = f"{self._uncached_prefix}/u/{uuid4()}"
        temp_slot = LiveCacheSlot(
            self._uncached_namespace, temp_key, self._backend
        )
        return self._backend.dispatch_uncached(
            temp_slot, step, args, kwargs
        )
```

No parent slot, no tracing. The temp key is `<prefix>/u/<uuid>` if prefix is non-empty, or `u/<uuid>` if empty. (Might want to normalize this — if prefix is empty, just `u/<uuid>`; if non-empty, `<prefix>/u/<uuid>`. Use a small helper to avoid double-slash.)

## _Backend.dispatch_uncached

New method on `_Backend` ABC, implemented in `_EntrypointBackend`. Similar to `dispatch_in_slot` but always fallible and runs cleanup after completion:

```python
# In _Backend ABC:
@abstractmethod
def dispatch_uncached(
    self,
    slot: LiveCacheSlot,
    step: StepHandle,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Pending[Any]: ...

# In _EntrypointBackend:
def dispatch_uncached(self, slot, step, args, kwargs):
    pending = Pending()

    async def _run_step():
        try:
            result = await self._execute_in_slot(
                slot, step, args, kwargs, fallible=True
            )
            pending.set_result(result)
        except BaseException as exc:
            pending.set_exception(exc)
        finally:
            await _cleanup_prefix(slot._namespace, slot._pin_key)

    self._task_group.start_soon(_run_step)
    return pending
```

This reuses `_execute_in_slot` exactly as-is with `fallible=True`. The only addition is the `finally` cleanup.

## Cleanup: `_cleanup_prefix`

Module-level async helper (in `entrypoint.py`):

```python
async def _cleanup_prefix(namespace: FxtrNamespace, prefix: str) -> None:
    """Delete all pins under a prefix (inclusive)."""
    pins = await namespace.list_pins(prefix=prefix)
    for key in pins:
        try:
            await namespace.unpin(key)
        except Exception:
            pass  # Best-effort cleanup
```

`list_pins(prefix=temp_key)` returns all pins whose keys start with `temp_key`, which includes:
- The slot state itself (`temp_key`)
- Checkpoints (`temp_key/ckpt`)
- Internal cache slots (`temp_key/c/...`)
- Their checkpoints (`temp_key/c/.../ckpt`)

Individual `unpin` calls handle each one. Best-effort: if a pin was already unpinned or never created, we silently continue.

## Implementation steps

- [ ] 1. Add `dispatch_uncached` to `_Backend` ABC and `_EntrypointBackend`
    - New abstract method on `_Backend` in `cache_slot.py`
    - Implementation in `_EntrypointBackend` in `entrypoint.py` (wraps `_execute_in_slot` + cleanup)
    - Add `_cleanup_prefix` helper in `entrypoint.py`
- [ ] 2. Add `run_uncached` to `StepContext` ABC and `LiveStepContext`
    - Abstract method on `StepContext` in `step_context.py`
    - Implementation in `LiveStepContext`: create temp slot, unwrap tracing, dispatch via backend, track Pending
    - Import `uuid4` and `_unwrap_tracing` as needed
- [ ] 3. Add `run_uncached` to `TopLevelEntrypoint`
    - Add `uncached_namespace` and `uncached_prefix` constructor args (both optional)
    - Implement `run_uncached`: validate namespace exists, create temp slot, dispatch via backend
- [ ] 4. Update `__init__.py` exports (if any new public types)
- [ ] 5. Update CODE_OVERVIEW.md
- [ ] 6. Tests
    - [ ] a. Basic from StepContext: uncached sub-step runs and returns result
    - [ ] b. No persistent cache: same slot tree has no leftover pins after uncached run completes
    - [ ] c. Second call re-executes (not cached): run_uncached with side effects shows both execute
    - [ ] d. From TopLevelEntrypoint: uncached top-level call works
    - [ ] e. TopLevelEntrypoint without namespace raises RuntimeError
    - [ ] f. Error in uncached step raises FallibleRunError (not StepAbortError)
    - [ ] g. Uncached step can use `ctx.cache_slot()` internally (and those are cleaned up)
    - [ ] h. Cache slot args are allowed and work (passed through, inner step sees them)
    - [ ] i. Uncached run is NOT tracked in parent's foreign slot deps (replay check unaffected)
    - [ ] j. Sub-step abort inside uncached step becomes FallibleRunError at boundary
    - [ ] k. Cleanup happens even on error (verify no leftover pins)
- [ ] 7. Update CODE_OVERVIEW.md (final pass after tests)
