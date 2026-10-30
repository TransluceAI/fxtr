# Stage 5 Part 2: JSON Transport — Implementation Review

Review of `fxtr2/rpc/json_transport.py` against the API design
(`stage5_part2_json_transport_api.md`). Covers what's implemented, what's missing, bugs found,
and improvement opportunities.

## What's implemented and matches the design

The core architecture closely follows the API design:

- **JSONTransportContext**: lifecycle (using `AsyncContextManagerMixin`), import/export tables, ID
  allocation, read loop, abort, send lock, deferred send queues
- **ImportEntry / ExportEntry**: all fields match the design spec
- **JSONTransportHandle**: clone, dispatch (sync push), dispatch_and_wait (submit), discard
  (deferred release), wait_until_teardown (race discard vs teardown)
- **JSONTransportPending**: clone, dispatch (pipelining pre-resolution, local post-resolution),
  dispatch_and_wait (submit pre-resolution, local post-resolution with lock-checked resolution
  race), pull (with pull message), discard, wait_until_teardown (two-phase), as_handle
- **JSONTransportPendingHandle**: clone, dispatch (wire pipelining), dispatch_and_wait (wire
  submit), discard, wait_until_teardown (two-phase with resolved handle delegation)
- **Expression encoder**: all encoding rules, bounce-back for handles and pendings, re-export
  deduplication for handles, rollback on failure
- **Expression evaluator**: all evaluation rules including `export`, `promise`, `import`,
  `pipeline`, dispatch during evaluation, arg cleanup, rollback on failure
- **Read loop**: all message types (push, submit, pull, resolve, reject, release, teardown,
  broken, abort)
- **Release-on-resolve**: implemented for non-auto-released imports
- **Resolve process** (`_resolve_export`): pull pending value, encode, send resolve/reject,
  discard pulled copy, auto-release
- **Teardown/resolve watchers**: background tasks for handle exports and promise/submit exports
- **Reverse export lookup**: deduplication for handles, always-new for pendings
- **Ownership model**: clone-on-export, clone-on-pull, discard resolved values on entry cleanup

## What's planned but NOT implemented

1. **Layer 2 (ObjectStream helper)** — explicitly deferred per design. The
   `ObjectStream[IRValue[JSONLeaf]]` convenience wrapper is not implemented.

2. **Promise chaining optimization** — the design mentions "future optimization" where
   `ensureResolvingExport()` could follow a chain of promises to skip intermediate resolves.
   Not implemented.

3. **Reject expression stub check** — design note 2 says to add a defensive check that reject
   expressions contain no stubs. Not implemented (reject expressions are just evaluated normally).

4. [fixed] **Submit early cancellation** — the `finally` block in `dispatch_and_wait` already
   handles this correctly. The wire test was xfailed due to two test bugs: a stale `xfail`
   marker and an incorrect `pytest.raises(BaseException)` (anyio's `cancel_scope.cancel()`
   doesn't propagate an exception out of the task group block). Test fixed and passing.

## Bugs

### B1: [fixed] Evaluation rollback — re-introduction doesn't bump `local_refcount`

When `_create_import_entry` (line 396) encounters an existing entry (re-introduction), it bumps
`remote_refcount` but NOT `local_refcount`. However, a new `JSONTransportHandle` is created at
line 745, meaning two handles now point at the entry but only one is counted.

Consequences:
- Without rollback: discarding the original handle decrements `local_refcount` to 0 and cleans
  up the entry, even though the re-introduced handle still exists.
- With rollback: `_decrement_import_refcount` decrements `local_refcount` (which might hit 0 and
  prematurely clean up the entry for the original handle).

Fix: `_create_import_entry` should bump `local_refcount` on re-introduction (since a new handle
referencing the entry will be created by the caller).

### B2: [fixed] Evaluation rollback — doesn't undo `remote_refcount` bump

For re-introduced imports, rollback calls `_decrement_import_refcount` which decrements
`local_refcount`. But the re-introduction bumped `remote_refcount`, and that bump is not undone.
The `remote_refcount` is sent in the release message — if it's too high, we release "too many"
references from the remote's perspective.

Fix: rollback for re-introduced entries needs to also decrement `remote_refcount`. This suggests
the rollback tracking should distinguish between "new entry" and "re-introduction refcount bump"
so it knows what to undo.

### B3: [fixed] Encoding rollback — re-export refcount bump not tracked

When `_encode_handle` (line 648) re-exports an already-exported handle, it bumps the existing
export entry's `refcount` but does NOT add it to `created_exports`. If encoding fails later in
the same pass, this refcount increment is not rolled back.

The design explicitly says rollback should "decrement refcount on each" created/bumped export.

Fix: track re-export refcount bumps in `created_exports` (or a separate list) and decrement on
rollback. Need to be careful not to call `_release_export_entry` for a re-export (which would
fully tear down the entry), just decrement the refcount.

### B4: [fixed] `_do_abort` stream close not wrapped in try/except

`_do_abort` called `await self._json_stream.aclose()` without a try/except. If `aclose()`
raised, the exception propagated out of `_do_abort`. [fixed: wrapped in try/except]

### B5: [fixed] Encoder silently passes unknown types through

`_encode_value` had a fallback `return value` for types not matching any `isinstance` check,
silently passing arbitrary Python objects to the JSON stream. [fixed: raises `TypeError`]

### B6: [fixed] Evaluator silently passes unknown types through

`_eval_expr` returned `expr` for any non-list, non-dict type without validating it was a valid
JSON leaf (str/int/float/bool/None). [fixed: validates type, raises `ValueError` for unknowns]

### B7: [fixed] `_release_import_entry` swallows exceptions via `get_value_or_raise`

Used `get_value_or_raise()` inside a bare `except Exception: pass` to access the resolved value
for cleanup. This masked potential bugs — `get_value_or_raise` re-raises rejections, and the
bare except swallowed them silently. [fixed: use new `resolved_value` property that returns
None for rejected/unsettled futures instead of raising]

## Improvement opportunities

### I1: [fixed] `_TrivialPending.clone()` was messy

Had visible "thinking out loud" comments and a needless refcount increment/decrement. Each
clone is fully independent via `clone_ir_value`. [fixed: simplified to 3-line method]

### I2: [not a bug] `_TrivialPending` doesn't detect connection abort

Its `wait_until_teardown()` only waits on discard, not connection abort. This is by design:
a `_TrivialPending` holds a concrete, already-resolved value that doesn't depend on the
connection. All operations (`pull`, `dispatch`, `clone`) work regardless of connection state.
Documented in the class docstring.

### I3: Duplicated local dispatch logic

`JSONTransportPending._dispatch_local`, `JSONTransportPending._dispatch_and_wait_local`,
`_TrivialPending.dispatch`, and `_TrivialPending.dispatch_and_wait` all have nearly identical
walk-and-dispatch logic. The design mentions "shared dispatch logic" as a consideration. Could
factor into a shared helper function.

### I4: [fixed] `_encode_handle` duplicated `_create_handle_export`

`_create_handle_export` and `_create_promise_export` were dead code — the encoder handles all
export creation inline with rollback tracking. [fixed: removed dead methods]

### I5: Two dispatch paths with different concurrency guarantees

`_sync_dispatch_push` allocates an ID and queues a message synchronously without the send lock,
relying on cooperative concurrency (no `await` between allocation and queuing). The async
`_send_push` / `_send_submit` hold the send lock. This creates two dispatch paths with
subtly different concurrency semantics. The sync path is correct under cooperative concurrency
but could be a footgun if the code evolves.

### I6: File size

`json_transport.py` is ~1900 lines. The cross-cutting dependencies (context ↔ handle ↔ pending
↔ encoder ↔ evaluator) make splitting hard, but the duplicated dispatch logic (I3) is a
candidate for extraction.
