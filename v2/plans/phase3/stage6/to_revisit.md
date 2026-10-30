# Stage 6: Items to Revisit

Issues and code smells identified during the initial implementation of the fxtr entity bridge.

## 1. ~~Circular dependency / lazy imports in `fxtr_rpc_types.py`~~ (FIXED)

`_dispatch_on_inner` and `_pull_from_inner` lived in `fxtr_rpc_types.py` but needed to import
from `fxtr_ir_conversion` and `fxtr_arena_cap`, which themselves import from `fxtr_rpc_types`.

**Fix**: Moved `_dispatch_on_inner`, `_pull_from_inner`, and `_resolve_scope` into
`fxtr_rpc_dispatch.py` which imports everything at the top level. The types file now
contains only type definitions, request types, tags, and the thin wrapper classes.
`FxtrRPCHandle.dispatch()`, `FxtrRPCPending.pull()`, and `FxtrRPCPending.dispatch()` use
local imports of `fxtr_rpc_dispatch` (one-liner, no circular dep since the dispatch module
is a leaf).

## 2. ~~`ArenaCapabilityHandle.dispatch()` is not implemented~~ (FIXED)

Only `dispatch_and_wait` was implemented; `dispatch()` raised `NotImplementedError`.

**Fix**: `ArenaCapabilityHandle` now accepts an optional `start_soon` callback (typically
`task_group.start_soon`). `dispatch()` eagerly spawns the async work into a background task
via `_run_dispatch` + `_InternalFuture`, matching the `LocalHandle.dispatch()` pattern.
`_ArenaCapPending` is a proper future-backed pending with `clone()` refcounting and
`pull()` returning a deep copy of the resolved value.

## 3. ~~Arena mounting creates adapters with the full FxtrID set~~ (FIXED)

In `_run_fxtr_call_in_background`, when mounting entities from multiple arena handles
(direct + pipelined), each `RemoteArenaAdapter` was initialized with the full set of decoded
FxtrIDs. This was NOT harmless: the first adapter (direct arg arena) would claim all IDs
and attach them all — including IDs that only exist in pipelined arenas. When the callee
tried to load a pipelined entity, it would dispatch on the wrong arena handle and fail.

**Fix**: `_unwrap_fxtr_results` now collects `(arena_handle, fxtr_ids)` pairs by walking
each `fxtr:result` wrapper's inner value for `fxtr:id` tags. The direct arena gets
`all_ids - pipelined_ids`, and each pipelined arena gets only its own IDs.

Also fixed a related bug: `release_scope` was only called on the happy path. If the handler
threw, `release_scope` was skipped, leaving `keep_alive` tokens unreleased and deadlocking
the caller's scope exit. Fixed by moving `release_scope` calls into a `finally` block.

## 4. ~~Pipelined arena handles' `release_scope` is never called~~ (FIXED)

In `_run_fxtr_call_in_background`, `release_scope` is only called on the direct arg arena
handle (`arena_handle`), not on the pipelined arena handles from resolved `fxtr:result`
wrappers. This means the dispatch scopes for those pipelined calls are held open (via
`keep_alive`) longer than necessary — until the arena cap is discarded rather than when
the callee finishes executing.

**Fix**: Call `release_scope` on all arena handles in `all_arena_handles`, not just the
direct one.

## 5. ~~Detached stores leak on context teardown~~ (FIXED)

When the callee detaches a store via `_detach_store()`, the store's lifetime is managed by
the return arena cap's `on_released` callback. But if the `LocalFxtrRPCContext` exits while
detached stores exist (return arena caps not yet discarded), those stores are never cleaned
up. The `on_released` callback doesn't fire because on context teardown, arena caps are torn
down (via `_FxtrHandleRefTracker.context_teardown()`), not discarded — and `on_released`
only fires on discard.

**Fix**: Track detached stores in the context. On context teardown, force-close any remaining
detached stores. Alternatively, make `context_teardown` on the return arena cap also trigger
the store cleanup callback.

## 6. `_FxtrHandleRefTracker` duplicates `_HandleRefTracker`

`_FxtrHandleRefTracker` in `fxtr_rpc_local.py` is nearly identical to `_HandleRefTracker` in
`basic_local_caps.py`. Both implement the same refcount + on_released + context_teardown
pattern.

**Fix**: Will be addressed by the local cap refactor (`local_cap_refactor.md`), which
unifies all three ref-tracking classes into one `HandleRefTracker` parameterized on a
`LocalCapContext` protocol.

## 7. ~~`_pull_from_inner` leaks arena handle on failure~~ (FIXED)

In `fxtr_rpc_dispatch.py`, after extracting `arena_handle` from the `fxtr:result` wrapper,
if `decode_to_fxtr_call_value` or scope validation threw, the arena handle was never
discarded.

**Fix**: Wrapped the decode + scope validation + mounting logic in a try/except that
discards the arena handle on failure.

## 8. ~~`import base64` inside `_collect_ir_fxtr_ids` body~~ (FIXED)

In `fxtr_rpc_local.py`, `_collect_ir_fxtr_ids` had `import base64` inside the function body.

**Fix**: Moved `import base64` to the top-level imports.

## 9. `_FxtrInternalCapIRRPCHandle.dispatch_and_wait` doesn't clone the arg

In `fxtr_rpc_local.py`, `dispatch()` clones via `clone_ir_value(request.arg)` (line 195),
but `dispatch_and_wait()` passes `arg.value` directly to the background task without cloning
(line 252). If the calling task gets cancelled after `await future.wait()` is interrupted,
the background task still runs with a shared reference. Compare with `LocalHandle` which
always clones.

**Fix**: Will be addressed by the local cap refactor — `ManagedHandle` clones the request
arg consistently for both `dispatch()` and `dispatch_and_wait()`.

## 10. `_run_fxtr_call_in_background` `finally` block can mask handler exception

In `fxtr_rpc_local.py`, if the handler throws and then `release_scope` in the `finally`
block also throws (e.g., arena cap already discarded), the `finally` exception replaces the
original. The `except Exception` catches the release_scope failure instead of the handler
error. Also, if there are multiple arena handles and one `release_scope` throws, the
remaining ones are skipped.

**Fix**: Will be addressed by the local cap refactor — `_run_fxtr_call` becomes a normal
async function (no future plumbing), and `_run_dispatch` in `ManagedHandle.dispatch()`
handles the try/except. The release_scope calls should also be wrapped to suppress errors
individually.
