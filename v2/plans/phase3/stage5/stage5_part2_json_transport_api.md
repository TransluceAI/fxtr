# Stage 5 Part 2: JSON Transport — Wire-Facing API Design

## Architecture Overview

The JSON transport is a single module (`fxtr2/rpc/json_transport.py`) that implements the
Cap-IR JSON wire protocol (specified in `fxtr2/rpc/PROTOCOL.md`) over a bidirectional JSON
message stream. It has two layers:

**Layer 1: `JSONTransportContext`** — the core connection. Takes an `ObjectStream[JSONTypes]` and
a local `RPCHandle` as the main interface, and implements the protocol: import/export tables,
expression evaluation/encoding, message processing, background tasks for teardown/resolution
watchers. Exposes `.remote_main` for the remote side's main interface.

**Layer 2: ObjectStream helper** — a thin wrapper that creates a "send receiver" handle (the
local main interface), wires it to `JSONTransportContext`, and produces an
`ObjectStream[IRValue[JSONLeaf]]`. This can be implemented and tested separately from the core.

The core transport never imports from `basic_local_caps.py`. It works purely through the
`cap_ir.py` ABCs (`RPCHandle`, `RPCPending`, `RPCDispatchable`). The only concrete types it
creates are its own transport-level handles and pendings.

## Ownership Model

The JSON transport must enforce the same ownership invariants as the local caps implementation
(see `stage5_part1post2_ir_correspondence.md`). The key principle: **serialization is the
ownership boundary for wire messages, and `local_refcount` is the ownership mechanism for
resolved values.**

**Outgoing dispatch (wire messages):** The expression encoder clones handles/pendings into
export entries when serializing. The user's original values are untouched. Serialization
naturally produces an independent copy on the remote side. No additional clone/discard is needed
on the sending side — the encoder's clone-on-export IS the ownership transfer.

**Incoming dispatch (expression evaluation):** The expression evaluator creates import entries
(new handles/pendings) when deserializing. These are owned by whoever consumes the evaluation
result. Intermediate values (e.g. evaluated call args) must be discarded after the dispatch
target has cloned what it needs.

**Pending value ownership:** `JSONTransportPending` owns its resolved value through the import
entry. The resolved value (produced by evaluating a resolve expression) may contain
`JSONTransportHandle` objects with their own independent import entries. These handles stay alive
as long as they are reachable through the pending. When the last clone of the pending is
discarded (`local_refcount` → 0), `discard_ir_value(resolved_value)` is called, recursively
discarding embedded handles (which decrements their own import entries' refcounts, potentially
cascading). `pull()` returns a deep copy via `clone_ir_value`, matching `LocalPending.pull()`.

**Dispatch result ownership:** `dispatch_and_wait()` (via submit) internally uses a `take()`
pattern: pull a copy of the resolved value, then discard the import entry. The caller owns the
returned value. `dispatch()` returns an `RPCPending` that owns its resolved value through the
same mechanism.

## JSONTransportContext

### Lifecycle

Created as an async context manager. On entry, exports the main interface as export 0, creates
import 0 for the remote's main interface, and starts the read loop. On exit, sends abort
(best-effort) and cleans up all state.

```
async with JSONTransportContext(json_stream, main_interface) as ctx:
    remote = ctx.remote_main  # RPCHandle for import 0
    ...
```

### State

- `_json_stream` — the underlying `ObjectStream[JSONTypes]`
- `_imports: dict[int, ImportEntry]` — import table (our imports = their exports)
- `_exports: dict[int, ExportEntry]` — export table (our exports = their imports)
- `_reverse_exports: dict[int, int]` — `id(handle)` → export ID for re-export deduplication
- `_next_positive_import_id: int` — counter for positive import IDs, incremented when we send
  push/submit (starts at 1)
- `_next_positive_export_id: int` — counter for positive export IDs, incremented when we receive
  push/submit (starts at 1)
- `_next_negative_export_id: int` — counter for negative export IDs, decremented when we export
  a handle or promise in the expression encoder (starts at -1)
- `_tg: TaskGroup` — for background tasks (read loop, teardown/resolution watchers)
- `_send_lock: Lock` — serializes "allocate ID + update tables + send message" sequences to
  prevent ID/message ordering mismatches when multiple tasks send concurrently (see Concurrency)
- `_aborted: bool` — whether the connection has been aborted
- `_abort_error: Exception | None` — the abort reason, if aborted
- `remote_main: JSONTransportHandle` — the remote's main interface (import 0)

### Positive ID allocation

Positive IDs are allocated by whoever initiates a push/submit. For **outgoing** pushes/submits,
we allocate the next positive **import** ID (from `_next_positive_import_id`). For **incoming**
pushes/submits, we allocate the next positive **export** ID (from `_next_positive_export_id`).
Both sides maintain independent counters that stay in sync because push/submit are the only
messages that allocate positive IDs and messages are ordered.

### Responsibilities

- **Read loop**: processes incoming messages (push, submit, pull, resolve, reject, release,
  teardown, broken, abort)
- **Message sending**: encode expressions, serialize sends through the lock
- **Export management**: create/release export entries, start teardown/resolution watchers
- **Import management**: create/release import entries, track refcounts
- **Abort**: on fatal error or context exit, reject all imports, dispose all exports, send abort

## Import Table

### ImportEntry

An import entry represents something the remote side has exported to us. All import types share
the same entry structure; behavior differences are captured by flags.

**State:**
- `id: int` — the import ID
- `local_refcount: int` — number of local `JSONTransportHandle`/`JSONTransportPending` instances
  pointing at this entry. When this reaches 0: if release has not been sent, send release and
  clean up. If release was already sent (release-on-resolve), just clean up the entry. In both
  cases, if the entry has a resolved value, call `discard_ir_value(resolved_value)` to clean up
  any handles/pendings embedded in the resolved value (this may cascade to other import entries).
  This is analogous to `_InternalFuture._release()` in `basic_local_caps.py`.
- `remote_refcount: int` — number of times this ID has been "introduced" (via export/promise
  expression, or by being a push/submit result). Sent as the refcount parameter in release.
- `resolution_future: _InternalFuture | None` — for pending imports (push/submit/promise), the
  future that resolves when resolve/reject arrives. **None for handle imports** — this is the
  discriminator between handle and pending imports. After settlement, also serves as the store
  for the resolved value (used for local dispatch after release-on-resolve).
- `teardown_event: Event` — fires on teardown/broken message or connection abort.
- `teardown_reason: IRValue | None` — set on normal teardown.
- `teardown_error: Exception | None` — set on broken or abort.
- `auto_pulled: bool` — True for submit results and promise imports. False for push results
  and handle imports. Controls whether `pull()` sends a `["pull", ...]` message.
- `pull_sent: bool` — whether we've sent a `["pull", ...]` message for this entry. Shared
  across all clones (tracked on the entry, not the pending instance) to prevent sending pull
  twice.
- `auto_released: bool` — True for submit results. When True, the entry is cleaned up
  automatically on resolve/reject (no release message sent; see protocol spec).
- `released: bool` — whether we've sent release. For handle imports, set when local_refcount
  hits 0. For push result and promise imports, set on resolve/reject (release-on-resolve).
- `torn_down: bool` — whether we've received teardown/broken (implies released from their side).

### Import types

| Type | Created by | IDs | resolution_future | auto_pulled | auto_released | Teardown? |
|------|-----------|-----|-------------------|-------------|---------------|-----------|
| Handle | `["export", id]` expr | Negative | None | N/A | No | Yes |
| Push result | Our outgoing `push` | Positive | Yes | No | No | No |
| Submit result | Our outgoing `submit` | Positive | Yes | Yes | Yes | No |
| Promise | `["promise", id]` expr | Negative | Yes | Yes | No | No |

### Import lifecycle

**Handle imports:**
1. Created when evaluator encounters `["export", id]`.
2. User interacts via `JSONTransportHandle`: dispatch, clone, discard.
3. On teardown/broken message: fire teardown_event, mark torn_down. No release needed.
4. On last local reference discarded: send release (unless torn_down or aborted).
5. On connection abort: fire teardown_event with error.

**Push result imports:**
1. Created when we send a `push` message.
2. User interacts via `JSONTransportPending`: pull, dispatch (pipelining), as_handle, clone,
   discard.
3. **Before resolution**: dispatch/dispatch_and_wait send wire messages (push/submit) referencing
   this import ID — this is promise pipelining. When user calls `pull()`: send `["pull", id]`,
   then await resolution_future.
4. **On resolve/reject message**: settle resolution_future and **immediately send release**
   (release-on-resolve, matching Cap'n'Web). Set `released = True`. The import entry stays in
   the table holding the resolved value until all local clones are discarded.
5. **After resolution**: dispatch/dispatch_and_wait operate locally on the resolved value (via
   `_walk_and_dispatch_ir`), no wire messages sent. `pull()` returns
   `clone_ir_value(resolved_value)` — always a deep copy, matching `LocalPending.pull()`.
   Post-resolution `GetAttribute` dispatch also returns `clone_ir_value(target.value)`.
6. On last local reference discarded before resolution: send release, clean up the entry. If
   resolve/reject arrives later, it hits the "resolve/reject for already-released imports" path.
7. On last local reference discarded after resolution: call `discard_ir_value(resolved_value)`
   to clean up any handles/pendings in the resolved value, then remove the entry. Release was
   already sent on resolve, so no release message needed here. The `discard_ir_value` call may
   cascade — discarding embedded `JSONTransportHandle` objects decrements their own import
   entries' refcounts.
8. On connection abort: reject resolution_future, fire teardown_event.

**Submit result imports:**
1. Created when we send a `submit` message. Internally implemented using the same pending
   ownership model as push results, but consumed immediately via a `take()` pattern.
2. `dispatch_and_wait()` awaits the resolution future in a `try/finally` block. On success:
   calls `clone_ir_value(resolved_value)` to produce an independently-owned copy for the caller,
   then cleans up the import entry (which calls `discard_ir_value(resolved_value)` on the
   original). On cancellation: the `finally` block cleans up the entry regardless. The clone
   overhead is negligible and keeps the ownership model uniform across all pending import types.
3. On resolve/reject: settle the future. If auto_released: the entry cleanup is triggered by the
   `dispatch_and_wait` consumer (step 2), not by the resolve handler. No release sent.
4. On connection abort: reject the future. The entry is cleaned up by the `dispatch_and_wait`
   `finally` block (if still awaiting) or remains inert in the table (if already consumed).

**Promise imports:**
1. Created when evaluator encounters `["promise", id]`.
2. User interacts via `JSONTransportPending`: pull, dispatch, as_handle, clone, discard.
3. **Before resolution**: dispatch/dispatch_and_wait send wire messages referencing this import
   ID (pipelining). `pull()` just awaits resolution_future (no pull message — auto-pulled).
4. **On resolve/reject message**: settle resolution_future and **immediately send release**
   (release-on-resolve). Set `released = True`. Entry stays in the table until all local
   clones are discarded.
5. **After resolution**: dispatch/dispatch_and_wait operate locally on the resolved value.
   `pull()` returns `clone_ir_value(resolved_value)` — always a deep copy. Post-resolution
   `GetAttribute` dispatch also returns `clone_ir_value(target.value)`.
6. On last local reference discarded before resolution: send release, clean up the entry. If
   resolve/reject arrives later, it hits the "resolve/reject for already-released imports" path.
7. On last local reference discarded after resolution: call `discard_ir_value(resolved_value)`
   to clean up embedded handles/pendings, then remove the entry. Release was already sent on
   resolve. (Same cascading behavior as push result imports.)
8. On connection abort: reject resolution_future, fire teardown_event.

### Release-on-resolve

Matching Cap'n'Web behavior (`ImportTableEntry.resolve()` → `sendRelease()`), when a push result
or promise import receives resolve/reject, we immediately send release. This frees the
corresponding export on the remote side, which no longer needs to hold the value.

After release, all operations on the pending short-circuit to the locally-stored resolved value:
- `pull()` returns `clone_ir_value(resolved_value)` — always a deep copy. This clones any
  `JSONTransportHandle` objects in the resolved value, incrementing their import entries'
  `local_refcount`. The caller owns the cloned handles.
- `dispatch()` navigates the resolved value locally via `_walk_and_dispatch_ir`. If the path
  leads to an `RPCDispatchable` (e.g. a `JSONTransportHandle` within the resolved value), it
  delegates to that dispatchable — which may send its own wire messages. If the path leads to a
  concrete value for `GetAttribute`, it's cloned via `clone_ir_value` before returning. If the
  path leads to a concrete value for `Call`, the value must be `RPCDispatchable` (else error).
  In the `GetAttribute` case, the cloned value is wrapped in a trivially-resolved pending.
- `dispatch_and_wait()` same as dispatch but awaits the result.

The resolved value may contain `JSONTransportHandle` objects (created during expression
evaluation of the resolve message). These handles have their own independent import entries and
lifetimes, unaffected by the original pending's release. They stay alive as long as they are
reachable — either through the pending's stored resolved value, or through copies returned by
`pull()`. When the last clone of the pending is discarded, `discard_ir_value(resolved_value)`
recursively discards these handles, sending their own release messages.

**Concurrency with dispatch pipelining**: The check for "is this import resolved?" and the
decision to either send a wire message or dispatch locally must be consistent with the release
message ordering. The implementation must ensure that a push referencing an import ID is never
sent after the release for that import ID. One approach: check resolution inside the `_send_lock`
(see Concurrency section), since the release message is also sent through the lock.

### Re-introduction

If we've sent release for an import but then receive a new `["export", id]` or `["promise", id]`
for the same ID (the messages crossed), we re-create the import entry. This is the "re-acquire"
scenario from the design notes.

### Resolve/reject for already-released imports

If resolve/reject arrives for an import ID that is no longer in the table (e.g. the import was
already cleaned up after release-on-resolve and all local clones discarded), we must still
evaluate the resolve expression — it may contain `["export", id]` expressions that create new
import entries. After evaluation, immediately discard all created imports (call discard on any
handles/pendings produced). This prevents leaking exports on the sender's side.

For reject expressions arriving for missing imports, no evaluation is needed (reject expressions
cannot contain stubs per the protocol spec).

## Export Table

### ExportEntry

An export entry represents something we've exported to the remote side, or a computation result
from a push/submit they sent us.

**State:**
- `id: int` — the export ID
- `refcount: int` — for handle exports, how many times exported (via reverse-export
  deduplication). Decremented by release messages. For push/submit/promise exports this is
  always 1.
- `value: IRValue[JSONLeaf]` — the exported value. For handle exports, an RPCHandle. For
  push/submit results, the result of expression evaluation (often an RPCPending). For promise
  exports, the RPCPending we're proxying.
- `pull_requested: bool` — whether the remote has sent pull. Only meaningful for push exports.
  For submit/promise exports, the resolve process starts immediately regardless.
- `resolve_sent: bool` — whether we've sent resolve/reject. Used to prevent double-resolving
  (e.g. if a second pull message arrives — a protocol error — we ignore it).
- `auto_pull: bool` — True for submit and promise exports. When True, the resolve process
  starts immediately at creation (no pull message needed).
- `auto_release: bool` — True for submit exports.
- `released: bool` — whether the remote sent release or teardown was sent.

### Export types

| Type | Created by | IDs | Resolve? | Teardown? |
|------|-----------|-----|----------|-----------|
| Handle | Encoder exports an RPCHandle | Negative | No | Yes (we watch & send) |
| Push result | Incoming `push` message | Positive | On pull | No |
| Submit result | Incoming `submit` message | Positive | Immediately | No |
| Promise | Encoder exports an RPCPending | Negative | Immediately | No |

### Export lifecycle

**Handle exports:**
1. Created by the expression encoder when serializing an RPCHandle for sending.
2. The transport **clones** the handle and stores the clone (takes ownership).
3. Starts a **teardown watcher** task: awaits `clone.wait_until_teardown()`. On normal return,
   sends `["teardown", id, reason_expr]`. On exception, sends `["broken", id, error_expr]`.
   In both cases, marks the export as released and cleans it up.
4. On release message: decrement refcount; when 0, discard the clone and clean up.
5. On connection abort: discard the clone, cancel the watcher.

**Push result exports:**
1. Created when a `push` message arrives. The expression is evaluated and the result stored.
2. The result is typically an `RPCPending` (from dispatching a call via a pipeline expression).
3. On pull message: start the resolve process (see below).
4. On release message (before pull): clean up. The computation may still be running but the
   result is discarded.
5. Pipelining: if the remote references this export in a subsequent expression, the evaluator
   dispatches on the stored value (which may be a pending — dispatch queues behind it).

**Submit result exports:**
1. Created when a `submit` message arrives. Expression evaluated and result stored.
2. Immediately starts the resolve process (auto-pulled).
3. After resolve/reject is sent: auto-cleaned (auto-released).

**Promise exports:**
1. Created by the expression encoder when serializing an RPCPending for sending.
2. The transport **clones** the pending and stores the clone.
3. Immediately starts the resolve process (auto-pulled). Awaits `clone.pull()`, then sends
   resolve/reject.
4. On release message (before resolution): cancel the resolve watcher, discard the clone.
5. On connection abort: cancel the watcher.

### Resolve process (`ensure_resolving_export`)

Triggered by pull (for push results) or immediately (for submit and promise exports):

1. Await the export's value if it's an RPCPending (call `pull()` on it). Since `pull()` returns
   a deep copy, the pulled value is independently owned by the resolve process.
2. On success: encode the resolved IRValue as an expression, send `["resolve", id, expr]`.
3. **Discard the pulled value** via `discard_ir_value`. The encoding step already cloned any
   handles/pendings into export entries; the intermediate pulled value is no longer needed.
   Without this step, each resolve message leaks one copy of the resolved value tree.
4. On failure: encode the exception, send `["reject", id, error_expr]`.
5. If auto_release: clean up the export entry.

If the value is already a concrete IRValue (not pending), step 1 is skipped. Instead, the
value is encoded directly from the export's stored value. In this case, the encoding clones
handles into export entries, and the export's stored value remains owned by the export entry
(cleaned up on release). No separate discard is needed.

**Future optimization — promise chaining**: Cap'n'Web's `ensureResolvingExport()` has a loop
that skips intermediate resolves: if a push export's result is itself a promise (and not pointing
back at the peer), it follows the chain and waits for the final resolution rather than sending a
redundant intermediate resolve. Not required for correctness but worth considering later.

### Reverse export lookup

When the encoder encounters an RPCHandle it has already exported, it reuses the existing export
ID (incrementing refcount) instead of creating a new entry. This is tracked via
`_reverse_exports: dict[int, int]` mapping `id(handle_clone)` → export ID. The clone stored in
the export entry keeps the Python object alive, so `id()` remains stable.

RPCPending values always get a new export ID (never reused), because reusing an ID could cause
the recipient to miss the resolution.

## Expression Evaluator

Converts JSON expression trees (from received messages) into `IRValue[JSONLeaf]` values. Runs
on the **recipient** side.

### Evaluation rules

- Plain JSON values (string, number, bool, null, object) → pass through (leaf values, None,
  dicts). Object values have their properties recursively evaluated.
- `[[...]]` → evaluate each element, produce a list.
- `["inf"]`, `["-inf"]`, `["nan"]` → float special values.
- `["error", type, msg]` → construct an Exception.
- `["tag", tag_expr, val_expr]` → evaluate both, produce `RPCTagged`.
- `["export", id]` → create a new import entry, produce `JSONTransportHandle`.
  Increments remote_refcount if the entry already exists (re-introduction).
- `["promise", id]` → create a new import entry with resolution_future, produce
  `JSONTransportPending`. The entry is auto-pulled (no pull message needed).
- `["import", id, path?, call?]` → look up **our export** `id`. Optionally navigate path
  and/or dispatch call. Produce `RPCHandle` (via `as_handle()` if dispatch produces a pending).
  **If no path and no call**: return `clone_ir_value(export_value)` coerced to handle (e.g.
  via `as_handle()` if it's a pending). The clone ensures the evaluation result is independently
  owned and does not alias the export table's value.
- `["pipeline", id, path?, call?]` → same as import, but produce `RPCPending`. If the value
  is already concrete (not pending), wrap it in a trivially-resolved pending. **If no path and
  no call**: `clone_ir_value(export_value)` and wrap in a trivially-resolved pending (if not
  already a pending). The clone is necessary because the trivially-resolved pending takes
  ownership of its value and would otherwise corrupt the export table on discard.

### Dispatch during evaluation

When an `import` or `pipeline` expression includes a path and/or call argument:

1. Look up our export entry for `id`, get its value.
2. If path specified and no call: dispatch `GetAttribute` on the value.
3. If call specified: evaluate the call argument expression, dispatch `Call(arg)` on the value.
4. The dispatch uses the value's `dispatch()` method if it's `RPCDispatchable`, or navigates
   structurally for plain IRValues (lists/dicts) similar to `_walk_and_dispatch_ir`.
5. **Arg cleanup**: after `dispatch()` returns, call `discard_ir_value(evaluated_arg)` on the
   evaluated call argument. The dispatch target is responsible for cloning what it needs (this is
   guaranteed by the ownership model — `RPCDispatchable.dispatch()` always copies the arg at the
   point of dispatch). This is analogous to `_execute_owned_call` in local caps discarding the
   resolved arg after the function returns.

The dispatch returns an `RPCPending`. For `import` expressions, `as_handle()` is called. For
`pipeline` expressions, the pending is returned directly. If no path/call is specified, the
export's value is cloned (see evaluation rules above) and coerced to the right type.

### Evaluation rollback

During evaluation, the evaluator may create new import entries (from `["export", id]` and
`["promise", id]` sub-expressions). If evaluation fails partway through (e.g. a malformed
expression, a dispatch failure, or an unevaluable sub-expression), the import entries already
created would be orphaned — the remote side holds corresponding exports that would never be
released.

To prevent this, the evaluator tracks all import entries created during a single evaluation pass.
On failure, it rolls them back: call `discard()` on each created handle/pending (which
decrements `local_refcount` → sends release if needed). This is the symmetric counterpart to
the encoder's export rollback mechanism.

The rollback covers the entire evaluation pass for a single top-level expression (e.g. the
expression in a `push`, `submit`, `resolve`, `teardown`, or `broken` message). All import
entries created during that evaluation are rolled back on failure.

## Expression Encoder

Converts `IRValue[JSONLeaf]` values into JSON expression trees for sending. Runs on the
**sender** side.

### Encoding rules

- Leaf values (int, float, str, bool) → pass through, except: `inf` → `["inf"]`, `-inf` →
  `["-inf"]`, `nan` → `["nan"]`.
- `None` → `null`.
- `list` → `[[encoded_elem_1, encoded_elem_2, ...]]`.
- `dict` → `{key: encoded_value, ...}`.
- `Exception` → `["error", type_name, message]`.
- `RPCTagged` → `["tag", encoded_tag, encoded_value]`.
- `RPCHandle` → see below.
- `RPCPending` → see below.

### Encoding RPCHandle

1. **Bounce-back check**: if it's a `JSONTransportHandle` on *this* connection, emit
   `["import", import_id]`. (They're getting back something they exported to us.)
2. **Re-export check**: if `id(handle)` is in `_reverse_exports`, reuse that export ID.
   Increment the export entry's refcount. Emit `["export", existing_id]`.
3. **New export**: clone the handle, allocate next negative export ID, create an export entry,
   start a teardown watcher, add to `_reverse_exports`. Emit `["export", new_id]`.

### Encoding RPCPending

1. **Bounce-back check**: if it's a `JSONTransportPending` on *this* connection, emit
   `["pipeline", import_id]`. (They're getting back a pending they gave us.)
2. **New export** (always — never reuses IDs): clone the pending, allocate next negative export
   ID, create a promise export entry, start the resolve process immediately.
   Emit `["promise", new_id]`.

### Encoding rollback

During encoding, the encoder may create new export entries (for handles and pendings encountered
in the value being encoded). If encoding fails partway through (e.g. an unencodable value type),
the exports already created would be orphaned — the remote side will never see them, and they'll
never be released.

To prevent this, the encoder tracks all export IDs created during a single encoding pass. On
failure, it rolls them back: decrement refcount on each, discard clones and remove from
`_reverse_exports` for any whose refcount reaches 0, and cancel any teardown/resolve watchers
that were started. This matches Cap'n'Web's `Exporter.unexport()` mechanism.

## User-Facing Types

### JSONTransportHandle

Implements `RPCHandle[JSONLeaf]`. Wraps a reference to an `ImportEntry`.

- **`dispatch(attr_path, request)`**: Encode the call argument (if Call). Send
  `["push", ["pipeline", import_id, path, arg_expr]]`. Allocate a new positive import ID for
  the push result. Return a `JSONTransportPending` for the new import.
- **`dispatch_and_wait(attr_path, request)`**: Encode the call argument. Send
  `["submit", ["pipeline", import_id, path, arg_expr]]`. Allocate a new positive import ID
  (auto-pulled, auto-released). Await the resolution future. Return
  `clone_ir_value(resolved_value)` — then clean up the import entry (which discards the
  original via `discard_ir_value`). This is effectively a `take()` (pull + discard) on the
  submit result import, keeping the ownership model uniform with push result `pull()`.
  **Cancellation safety**: the import entry cleanup must be in a `finally` block, so that if
  `dispatch_and_wait` is cancelled (e.g. timeout, task cancellation), the submit result import
  entry is still cleaned up. Without this, the entry (and its resolved value) would leak until
  connection abort. Since `clone_ir_value` and entry cleanup are both sync (no awaits), there
  is no cancellation window between "clone the result" and "clean up the entry."
- **`clone()`**: Increment local_refcount on the import entry. Return a new
  `JSONTransportHandle` pointing at the same entry.
- **`discard()`**: Mark the handle as discarded (subsequent operations raise
  `RPCResourceDiscardedError` synchronously). Decrement local_refcount. If 0 and not
  torn_down/aborted: **schedule** sending `["release", import_id, remote_refcount]`. The
  release send is deferred because `discard()` is sync (per the `RPCHandle` ABC) but sending
  requires the async send lock. The release is queued and sent by a background task or drained
  before the next send. The entry is cleaned up after the release is sent (or immediately if
  torn_down/aborted). See implementation note 7.
- **`wait_until_teardown()`**: Wait for the import entry's teardown_event (fires on
  teardown/broken message or connection abort). Return teardown_reason on normal teardown;
  raise RPCResourceBrokenError on broken/abort; raise RPCResourceDiscardedError if discarded.

### JSONTransportPending

Implements `RPCPending[JSONLeaf, IRValue[JSONLeaf]]`. Wraps a reference to an `ImportEntry`.
Used for both push result imports and promise imports — the behavioral difference is captured
by the entry's `auto_pulled` flag.

- **`pull()`**: If not `auto_pulled` and not `pull_sent` on the entry: send
  `["pull", import_id]`, set `entry.pull_sent = True`. Then await resolution_future. Return
  `clone_ir_value(resolved_value)` — always a deep copy, matching `LocalPending.pull()`. The
  import entry retains its internal copy; multiple pulls return independent copies. Cloning
  handles in the resolved value increments their import entries' `local_refcount`.
- **`dispatch(attr_path, request)`**: If `Call`, clone the arg synchronously (before any
  resolution check or await). Then check if the import has resolved.
  - **If not resolved**: send `["push", ["pipeline", import_id, path, arg_expr]]` (pipelining).
    Allocate a new positive import ID. Return a `JSONTransportPending` for the new import.
    Discard the cloned arg after encoding (the encoder takes ownership via clone-on-export).
  - **If resolved**: dispatch locally on the resolved value via `_walk_and_dispatch_ir`. If the
    path leads to an `RPCDispatchable`, delegate to its `dispatch()` (passing the cloned arg)
    and return its `RPCPending`.
    If the path leads to a concrete value for `GetAttribute`, return
    `clone_ir_value(target.value)` wrapped in a trivially-resolved `RPCPending`. For `Call` on a
    non-dispatchable, raise `TypeError` (matching `LocalPending` behavior).
- **`dispatch_and_wait(attr_path, request)`**: Same resolution check as dispatch.
  - **If not resolved**: send `["submit", ...]`, await the result. Return
    `clone_ir_value(resolved_value)`, then clean up the submit import entry (take pattern).
  - **If resolved**: dispatch locally on the resolved value. For `GetAttribute`, return
    `clone_ir_value(target.value)`. For `Call` on an `RPCDispatchable`, delegate to its
    `dispatch_and_wait()`. For `Call` on a non-dispatchable, raise `TypeError`.
- **`as_handle()`**: Return a `JSONTransportPendingHandle` pointing at the same import entry.
  Increment local_refcount.
- **`clone()`**: Increment local_refcount. Return new `JSONTransportPending` for same entry.
- **`discard()`**: Decrement local_refcount. If 0 and not released/aborted: send release and
  clean up the entry. If 0 and already released (release-on-resolve): clean up the entry. In
  both cases, if the entry has a resolved value, call `discard_ir_value(resolved_value)` to
  clean up embedded handles/pendings before removing the entry.
- **`wait_until_teardown()`**: Two phases (same pattern as `LocalPending`):
  1. Await resolution_future, racing against discard. If rejected, propagate the exception.
  2. Future resolved (or was already resolved). Wait for discard or connection abort.

### JSONTransportPendingHandle

Implements `RPCHandle[JSONLeaf]`. Wraps a reference to an `ImportEntry` (same entry as the
originating `JSONTransportPending`). Created by `JSONTransportPending.as_handle()`. Mirrors the
two-phase teardown pattern of `LocalPendingHandle` from `basic_local_caps.py`.

- **`dispatch(attr_path, request)`**: If `Call`, clone the arg synchronously (before any await).
  Then spawn a background task that: awaits resolution_future, asserts the resolved value is an
  `RPCHandle`, delegates to the resolved handle's `dispatch()` using the cloned arg. Return an
  `RPCPending` backed by the background task's result. This matches `LocalPendingHandle.dispatch()`
  in `basic_local_caps.py`, which also clones synchronously and spawns a background task — the
  caller must be free to mutate/discard the arg immediately after `dispatch()` returns.
- **`dispatch_and_wait(attr_path, request)`**: Same — await resolution, assert `RPCHandle`,
  delegate to the resolved handle's `dispatch_and_wait()`. (No sync cloning needed here since
  the method is async and holds onto the arg until delegation.)
- **`clone()`**: Increment local_refcount. Return new `JSONTransportPendingHandle` for same
  entry.
- **`discard()`**: Decrement local_refcount. Same cleanup as `JSONTransportPending.discard()`.
- **`wait_until_teardown()`**: Two-phase pattern matching `LocalPendingHandle`:
  1. Await resolution_future, racing against discard. If rejected, propagate the exception.
     Assert the resolved value is an `RPCHandle`.
  2. Await the resolved handle's `wait_until_teardown()`, racing against discard and connection
     abort. Return the resolved handle's teardown reason (or raise its exception).

### Shared dispatch logic

Both `JSONTransportHandle.dispatch()` and `JSONTransportPending.dispatch()` (pre-resolution)
need to encode a call and send a push. This shared logic can be a method on the context or
import entry:
1. Encode the request's call argument (or GetAttribute) as an expression.
2. Build the top-level expression: `["pipeline", import_id, path, arg_expr]` (or without arg_expr
   for GetAttribute).
3. Send `["push", expr]` (for dispatch) or `["submit", expr]` (for dispatch_and_wait).

Top-level push/submit expressions always use `"pipeline"` (consistent with Cap'n'Web, where
`sendCall` and `sendStream` both use `"pipeline"`). The `"import"` expression type is used only
inside value expressions — primarily for bounce-backs (when encoding a handle that points back at
the peer).

### Trivially-resolved RPCPending

When `dispatch()` on a resolved `JSONTransportPending` reaches a concrete value (not an
`RPCDispatchable`), we need to wrap it in an `RPCPending` to satisfy the return type. This
wrapper must support the full ownership model:

- **`pull()`**: returns `clone_ir_value(value)` — always a deep copy, consistent with all other
  pending types.
- **`dispatch()`**: navigates the value via `_walk_and_dispatch_ir`. For `GetAttribute`, returns
  `clone_ir_value(target.value)` wrapped in another trivially-resolved pending. For `Call` on an
  `RPCDispatchable`, delegates. For `Call` on a non-dispatchable, raises `TypeError`.
- **`dispatch_and_wait()`**: same as dispatch but awaits the result.
- **`as_handle()`**: asserts the value is an `RPCHandle`. Calls `value.clone()` and returns the
  clone. The caller owns the clone independently; the pending retains its own internal copy
  (which is discarded when the pending is discarded). This satisfies the `RPCPending.as_handle()`
  contract: "The returned handle must maintain its own copy of the pending value; this pending
  value can be discarded afterwards without affecting the handle."
- **`clone()`**: increments a refcount. Multiple clones share the same value.
- **`discard()`**: decrements refcount. When it hits 0, calls `discard_ir_value(value)` to clean
  up any handles/pendings in the value.
- **`wait_until_teardown()`**: waits for discard or connection abort.

The same type is used by the expression evaluator when `["pipeline", id]` references an
already-concrete value. This is essentially the `RPCConcretePayload` mentioned in the
correspondence doc's deferred items — now needed for ownership correctness.

## Read Loop

The read loop runs as a background task in the context's task group. It processes messages from
the JSON stream:

```
match msg[0]:
    "push"    → evaluate expression, store as next positive export
    "submit"  → evaluate expression, store as next positive export, start resolving immediately
    "pull"    → start resolving the referenced export
    "resolve" → settle the referenced import's future, send release (see below)
    "reject"  → settle the referenced import's future, send release (see below)
    "release" → decrement the referenced export's refcount; clean up if 0
    "teardown"→ evaluate reason expr, fire the referenced import's teardown_event (normal)
    "broken"  → evaluate error expr, fire the referenced import's teardown_event (error)
    "abort"   → abort the connection
```

**resolve/reject handling:**
1. Look up the import entry for the referenced ID.
2. **If found**: evaluate the expression (for resolve) or error expression (for reject). Settle
   the resolution_future. If not auto_released: send `["release", id, remote_refcount]`, set
   `released = True`. The import entry remains in the table until local_refcount reaches 0.
3. **If not found** (import already cleaned up — messages crossed): for resolve, evaluate the
   expression to process any `["export", id]` sub-expressions that create import entries, then
   immediately discard all produced handles/pendings. For reject, no evaluation needed (reject
   expressions cannot contain stubs).

ID mapping reminder: messages use the sender's perspective. "Their import" = our export.
"Their export" = our import. So:
- pull, release reference **our exports** (by their import ID)
- resolve, reject, teardown, broken reference **our imports** (by their export ID)

### Expression evaluation failures

- In push/submit: the export gets a rejected state. When pulled, reject is sent.
- In resolve: the import gets rejected (bad resolution expression).
- In other contexts: abort the connection (fatal protocol error).

### Malformed messages

If the read loop receives a message that does not match any known message type (e.g. unknown
opcode, wrong number of fields, wrong types), abort the connection with a protocol error.
This matches Cap'n'Web behavior (rpc.ts:847).

## Connection Abort

When the connection aborts (either we initiate or receive an abort), the following steps execute
**in this order** (ordering matters for consistency — e.g. resolution futures must be settled
before teardown events fire, so that `wait_until_teardown()` implementations that check
resolution state see a consistent view):

1. Set `_aborted = True`, store the error. (Prevents new sends.)
2. Try to send `["abort", error_expr]` (best-effort, ignore send failures).
3. Reject all **unresolved** imports' resolution futures with the abort error. (Wakes up pending
   `pull()` calls.) Already-resolved imports are left as-is — their resolved values remain
   pullable (see note below).
4. Fire all imports' teardown events with the abort error. (Wakes up `wait_until_teardown()`
   calls.)
5. Cancel all export watcher tasks (teardown watchers, resolve watchers).
6. Discard all exported handle/pending clones.
7. Close the JSON stream.

**Already-resolved imports survive abort.** If a pending import has already resolved before the
abort, its resolved value is not eagerly discarded. The user can still `pull()` it and get useful
data. This is a best-effort recovery: if the resolved value contains only data (no live handles),
it's fully usable. If it contains `JSONTransportHandle` objects, those handles' teardown events
have been fired (step 4), so operations on them will raise `RPCResourceBrokenError` — but the
data around them is still accessible. The resolved value is cleaned up normally when the user
eventually discards the pending (`local_refcount` → 0 triggers `discard_ir_value`).

Import entries for resolved imports remain in the table after abort. They are cleaned up when
the user discards their pending clones (the normal `local_refcount` mechanism). This means
the import table is not fully cleared on abort — some entries linger until the user's code
unwinds and discards its references. This is acceptable: the entries are inert (no wire messages
can be sent), and they will be garbage-collected when the `JSONTransportContext` is dropped.

## Concurrency

- **Send lock**: The `_send_lock` guards the entire "allocate ID + update local state + send
  message" sequence. This is necessary because positive IDs for push/submit are implicitly
  assigned by message order. If two tasks send pushes concurrently without the lock, their
  messages could arrive in a different order than their IDs were allocated, causing an ID
  mismatch. The lock ensures that message ordering on the wire matches ID allocation order.
  Expression encoding happens inside the lock (it's synchronous); the `stream.send()` call
  is the only await inside the locked section.
- **Send lock and release-on-resolve**: When a pending import has been resolved and released, its
  `dispatch()` must not send wire messages referencing the released import ID. The resolved-check
  inside `dispatch()` and any wire sends must be serialized through the send lock. The release
  message (sent by the read loop on resolve) also goes through the lock. This ensures: either
  dispatch sees the import as unresolved and sends a push (which goes out before the release on
  the wire), or it sees the import as resolved and dispatches locally (no wire message).
  **Clarification on lock scope**: Expression evaluation (in the read loop) happens *outside*
  the send lock. Only the "send release" step takes the lock. This avoids deadlocks — expression
  evaluation may trigger dispatch calls that themselves need the send lock.
- **Task group**: All background tasks (read loop, teardown watchers, resolution watchers) run
  in the context's task group. When the context exits, the group is cancelled.
- **Clone-on-export**: When exporting an RPCHandle or RPCPending, the transport clones it and
  takes ownership of the clone. This decouples the export's lifetime from the application's
  reference. The clone is discarded when the export is released or the connection aborts.

## Layer 2: ObjectStream Helper

A convenience wrapper that builds an `ObjectStream[IRValue[JSONLeaf]]` on top of
`JSONTransportContext`:

1. Creates a local handle (e.g. using `LocalRPCContext` + `LocalHandle`) with a `send(value)`
   method that puts values into an internal receive queue.
2. Passes this handle as the `main_interface` to `JSONTransportContext`.
3. `send(value)`: calls `ctx.remote_main.dispatch_and_wait(["send"], Call(value))` — this sends
   a submit to the remote's main interface.
4. `receive()`: reads from the internal receive queue.

This means `send()` blocks until the remote acknowledges (the submit resolves). This provides
natural backpressure.

**Note**: Delay implementation of this layer. Several edge cases need consideration before
implementing: queue bounding (for backpressure on the receive side), behavior on connection
abort while blocked on receive, and handle/pending lifetime management for values in the queue
after context exit. These should be designed carefully in a later stage.

## Resolved Questions

1. **Expression type for push/submit calls**: Always `"pipeline"` for top-level push/submit
   expressions. Consistent with Cap'n'Web. `"import"` is only used within value expressions
   (bounce-backs, handle arguments).

2. **GetAttribute deep copy**: For outgoing dispatch (wire messages), serialization naturally
   produces a snapshot — no explicit deep copy needed. For post-resolution local dispatch on
   `JSONTransportPending`, `GetAttribute` results are cloned via `clone_ir_value` before
   returning, matching `LocalPending._dispatch_and_wait_impl` behavior. This ensures the caller
   gets an independently-owned value that doesn't alias the pending's internal resolved value.

3. **Evaluation failures in resolve/reject expressions**: Scope the error — reject the specific
   import and log a warning, rather than aborting the whole connection.

4. **ObjectStream helper and LocalRPCContext**: The helper creates and owns its own
   `LocalRPCContext`, valid over its own lifetime.

5. **Release timing for pending imports**: Matching Cap'n'Web, pending imports (push results and
   promises) send release immediately on resolve/reject. Post-resolution operations use the
   locally-stored resolved value. This frees the remote export promptly without waiting for all
   local clones to be discarded.

6. **as_handle() teardown semantics**: `JSONTransportPendingHandle` mirrors `LocalPendingHandle`'s
   two-phase teardown: Phase 1 awaits resolution, Phase 2 delegates to the resolved handle's
   `wait_until_teardown()`. This ensures `.as_handle().wait_until_teardown()` follows through
   to the resolved value's lifetime, matching the local caps behavior.

## Implementation Notes

1. **Duplicate shared utilities rather than importing from `basic_local_caps`**: Helpers like
   `_InternalFuture` and `_walk_and_dispatch_ir` should be duplicated into `json_transport.py`
   rather than imported. This keeps the transport decoupled from the local caps implementation
   and allows independent tweaking if the transport's needs diverge. After implementation, if
   the copies turn out to be identical, we can refactor into a shared utility module.

   **Note on `_InternalFuture` simplification**: The transport's `_InternalFuture` does NOT need
   its own refcounting (unlike the one in `basic_local_caps.py`). The import entry's
   `local_refcount` already tracks how many user-visible objects reference the entry and triggers
   `discard_ir_value(resolved_value)` when it reaches 0. The transport's future just needs:
   store resolved value, wait event, store rejection. Cleanup of the resolved value is driven by
   the import entry lifecycle, not the future.

2. **Reject/broken expression encoding**: The protocol says reject expressions cannot contain
   stubs. The encoder should enforce this (or at minimum, exceptions should never contain
   handles in practice). When receiving a reject expression, add a defensive check that the
   evaluated result contains no stubs, and log a warning if it does.

3. **Idempotent export cleanup**: Export entries can be cleaned up by either a release message
   or a teardown watcher firing (for handle exports). These can race. Cleanup should check
   `released` first — if already set, skip. Otherwise set `released` before doing cleanup.
   This ensures the first path (teardown or release) does the real work and the second is a
   no-op. This is safe under cooperative concurrency as long as there is no `await` between
   the check and the set.

4. **Encoding rollback implementation**: The encoder should maintain a list of export IDs created
   during the current encoding pass. On exception, iterate the list and clean up each export
   (decrement refcount, dispose clones, cancel watchers). This can be implemented as a
   context manager or try/finally block wrapping the encode call.

5. **Evaluation rollback implementation**: Symmetric to encoding rollback. The evaluator should
   maintain a list of import entries (handles/pendings) created during a single evaluation pass.
   On exception, call `discard()` on each (which decrements `local_refcount` → sends release if
   needed). Also discard any intermediate values produced during evaluation (e.g. evaluated call
   args that were not yet consumed by dispatch). This can also be a context manager or
   try/finally block.

6. **Arg cleanup during evaluation dispatch**: When the evaluator dispatches a call during
   expression evaluation (for `["pipeline", id, path, arg_expr]` or `["import", id, path, arg_expr]`),
   it must call `discard_ir_value(evaluated_arg)` after `dispatch()` returns. The dispatch target
   is responsible for cloning what it needs (per the ownership model). If the dispatch itself
   fails, the evaluation rollback mechanism (note 5) handles cleanup of import entries created
   during arg evaluation.

7. **Deferred release sending**: `discard()` on `RPCHandle`/`RPCPending` is sync per the ABC, but
   sending a `["release", ...]` message requires the async send lock. The implementation should
   use a deferred-send pattern: `discard()` synchronously marks the object as discarded (so all
   subsequent operations fail immediately with `RPCResourceDiscardedError`), decrements
   `local_refcount`, and if a release needs to be sent, queues it for async delivery. The release
   queue can be drained: (a) by a dedicated background task, or (b) piggy-backed before the next
   outgoing send under the lock, or (c) via `tg.start_soon` to fire-and-forget the send. The key
   invariant is: after `discard()` returns, the user's object is immediately inert. The actual
   wire release is a transport-internal concern that happens asynchronously.

   **Ordering note**: Deferred releases can be sent in any order relative to other messages. The
   released import ID will never be referenced by our side again (IDs are never reused), so the
   remote side can process the release at any point without causing inconsistency.

8. **Evaluation must clone export values for `["import", id]` / `["pipeline", id]`**: When
   evaluating `["import", id]` or `["pipeline", id]` without a path or call, the evaluator must
   `clone_ir_value` the export entry's value before returning it. Without this, the evaluation
   result would alias the export table's own value. If the result ends up as a call argument in a
   larger expression (which gets discarded after dispatch), or gets wrapped in a trivially-resolved
   pending (which discards its value on cleanup), the export table would be corrupted. The clone
   ensures all evaluation results are independently owned. This matches Cap'n'Web's
   `ensureDeepCopied` semantics.

## Known Limitations

### Call ordering (no embargo mechanism)

Cap'n Proto includes an "embargo" mechanism to preserve call ordering when a promise resolves to
a capability that is already known to both sides. Without embargoes, pipelined calls sent before
a resolve arrives may be interleaved with direct calls sent after the resolve arrives. Cap'n'Web
has a TODO for this (rpc.ts:73-79) but does not implement it.

This transport inherits the same limitation. **Concurrent calls to a handle that is passed over
the transport are sequenced on a best-effort basis and may not arrive in order.** If call ordering
is important for correctness, callers should either:
- Wait for previous calls to complete before dispatching the next, or
- Use data-dependency / promise pipelining to directly enforce sequential execution (i.e. pass
  the result of one call as an argument to the next).

This limitation should be documented in:
- `PROTOCOL.md` (as a known limitation)
- `json_transport.py` module docstring
- `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md`

## Test Scenarios

In addition to running all existing transport-parameterized tests with the JSON loopback
fixture, the following scenarios should have dedicated tests:

- **Re-introduction**: release/export messages crossing — send release, then receive a new
  `["export", id]` for the same ID; verify the import entry is correctly recreated.
- **Evaluation rollback**: expression evaluation fails partway through after creating import
  entries; verify all created imports are cleaned up (released).
- **Encoding rollback**: expression encoding fails partway through after creating export
  entries; verify all created exports are cleaned up.
- **Teardown during pipelining**: a handle tears down while pipelined calls are in flight;
  verify pending results are properly rejected.
- **Abort while pull is in flight**: connection aborts while a pull is awaiting resolution;
  verify the pull raises `RPCResourceBrokenError`.
- **Already-resolved imports survive abort**: resolve arrives, then connection aborts; verify
  the resolved value is still pullable.
- **Submit result early cancellation**: `dispatch_and_wait` is cancelled before resolution;
  verify the import entry is cleaned up in the `finally` block.
- **Concurrent dispatch and resolve**: dispatch on a pending racing with resolve arrival;
  verify either wire pipelining or local dispatch is used consistently (no released-ID
  reference on the wire).
