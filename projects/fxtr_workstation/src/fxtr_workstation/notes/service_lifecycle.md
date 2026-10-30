# Service registration lifecycle

This doc traces what actually happens when a service is registered,
looked up, and released, so we can be precise about what each piece
of code is responsible for and which "defensive" branches are load-
bearing vs. paranoia. It supersedes the brief lifetime story in
`service_impl.py`'s module docstring.

Stage 1 covers services only; entities will get the same treatment in
a later step.

## Components

The moving pieces, with their owners and lifetimes:

| Object | Owner | Lifetime |
|---|---|---|
| `WorkstationState.service_records[key]` (a `TrackedServiceRecord`) | `WorkstationState` | While at least one `Registration` is in `tracked.registrations`. |
| `tracked.service_scope` | `service_record_worker` task | Worker's `async with FxtrScope(...)` block. Drain triggered by `tracked.shutdown_event`. |
| Bridge inside `service_scope` of the source service ref | `service_scope` | Drains with `service_scope`. **Pins the scope it was bridged from.** |
| `Registration` | `tracked.registrations` + `record.registrations` | From `register_service` until `release_registration` runs. |
| `ReleaseCallbackImpl` | The `register_service` call's dispatch scope (`call_scope`) | Until the chain-bridged `release_cb_ref` is released by the peer. |
| `state.short_ids` mapping for the key | `ShortIDAllocator` | Mapping kept for run lifetime; "active" set membership tracks whether `lookup_active_service` resolves it. |
| `RPCConnectionRecord` + `record.scope` | `state.add_rpc_connection` async CM | One per `/rpc` connection; drains when `rpc_endpoint` exits. |

### Two scopes that matter for bridges

- **`call_scope`**: the per-call dispatch scope inside the typed-
  service framework. Owns the peer's input args (decoded via the wire
  arena) and the values mounted for chain-bridged outputs. Drains when
  *every chain-bridged ref it produced has been released by the peer*.
  For `register_service`, that means the `release_cb_ref`.
- **`tracked.service_scope`**: the long-lived workstation-side scope
  that holds the bridge to the registered service. The bridge source
  is `call_scope` of the *first* `register_service` call for this
  `ServiceKey`. `service_scope` drains when `tracked.shutdown_event`
  is set.

## Setup

### First registration of a `ServiceKey` (`register_service` on conn A)

1. The typed-service framework decodes `service: ServiceRef[Any]`
   into A's `call_scope`.
2. `register_service` derives `ServiceKey(handle, type_id)`.
3. Under `state.lock`:
   - `short_ids.get_or_assign_service(key)` allocates `s{N}` and
     marks the key active.
   - No record yet, so we construct a `TrackedServiceRecord` and
     insert it in `state.service_records`.
4. We spawn `service_record_worker` on `state.background_tg`.
5. The worker:
   - Opens `service_scope` (`isolated=True, mountable=True,
     include=[root_scope]`).
   - Calls `service_scope.bridge_service_from_scope(service_ref,
     call_scope)`. This **pins `call_scope` for as long as
     `service_scope` is alive** (per the docstring on
     `bridge_service_from_scope`).
   - Stores `tracked.service_scope`, sets `bridge_ready`, and parks
     on `tracked.shutdown_event`.
6. `register_service` awaits `bridge_ready`, then:
   - Constructs `Registration(service_key, source=record,
     external_name)` and appends it to both `tracked.registrations`
     and `record.registrations`.
   - Mounts a `ReleaseCallbackImpl` on `call_scope`, getting `cb_ref`.
   - Registers an end-callback on `call_scope._token` that calls
     `release_registration(state, registration)`.
   - Returns `(short_id, cb_ref)`. The framework chain-bridges
     `cb_ref` back to A.

### Subsequent registration of the same `ServiceKey` (e.g. from conn B)

Same as above except `service_records.get(key)` finds the existing
record, so steps 3-5 collapse to "append a new `Registration`".
A new `ReleaseCallbackImpl` is mounted on B's `call_scope` with its
own end-callback. The bridge in `service_scope` is the one created by
the *first* registration; it pins A's `call_scope`, not B's.

> **Implication.** Releasing all of A's registrations does not drain
> `service_scope` if B still has a registration; the bridge keeps A's
> `call_scope` alive too. Only when *every* registration is released
> does `shutdown_event` fire. This is a deliberate consequence of the
> "first registration owns the bridge" model and is why bounceback
> works without re-bridging.

### External names

External names are exclusive: at most one active registration per
name across the whole workstation. The check happens in
`register_service`'s sync block — if `state.service_external_names`
already has an entry for the name, we raise `ValueError` referencing
the existing registration's connection ID. Releasing the holding
registration frees the name. There is no "stale entry" state to
reason about.

`release_registration` only clears the index entry when the released
registration *is* the current entry (a defensive identity check that
no longer matters under the exclusive-names rule, but cheap to keep).
The combination guarantees: the index either points at a live
registration or is absent.

### `get_service(short_id)` on conn C

1. Under `state.lock`: resolve key via `lookup_active_service`,
   fetch `tracked` from `service_records`.
2. Read `tracked.service_scope` (must be non-None — see "Race
   windows" below).
3. Reconstruct a `ServiceRef` from the key.
4. `await call_scope.bridge_service_from_scope(ref, service_scope)`.
   This **pins `service_scope` for as long as C's `call_scope` is
   alive**. Once the framework chain-bridges the result to C, C's
   `call_scope` lives until C drops its chain-bridged ref.
5. Return the ref; framework chain-bridges to C.

So `service_scope`'s drain is gated on:
- All `tracked.registrations` released *and*
- Every `get_service` consumer dropping its chain-bridged ref (so
  each consumer's `call_scope` drains, releasing the bridge into it
  from `service_scope`).

The latter is implicit — `bridge_service_from_scope`'s pin is what
keeps `service_scope` alive past `shutdown_event` if a consumer is
still using it.

## Teardown signals

There are exactly three signals that drop a `Registration`:

1. **Peer calls `release()` on the chain-bridged ref.** This is a
   normal RPC call that reaches `ReleaseCallbackImpl.release`, which
   calls `release_registration` synchronously. The chain-bridged
   `release_cb_ref` may still be alive on the peer side after this;
   that's fine — `release_registration` is idempotent.
2. **Peer drops the chain-bridged ref.** The lease ends, the
   workstation-side `call_scope` of `register_service` starts
   draining, the end-callback registered on `call_scope._token`
   fires, calling `release_registration`.
3. **Peer's RPC connection drops.** `rpc_endpoint` exits
   `open_rpc_scope`. As `open_rpc_scope` unwinds, every leased
   import the peer was holding (including the chain-bridged
   `release_cb_ref`) is broken via `lease_cancel_scope`. This
   cascades into signal 2 for each registration sourced by this
   connection, with the end-callbacks firing as each `call_scope`
   drains.

In addition, `state.add_rpc_connection`'s `finally` walks
`record.registrations` and calls `release_registration` for each.
**This is currently defensive** — under signal 3 the end-callbacks
already fire — but it pins down the invariant that no registration
outlives the connection that sourced it, regardless of whether the
underlying scope-drain machinery delivers the end-callbacks in time.
See "Open questions" below.

## Teardown flow (cascade once `release_registration` is called)

`release_registration` is sync. It is called from:

- `ReleaseCallbackImpl.release` (an async dispatch method)
- The end-callback on `call_scope._token` (sync, fires during scope
  drain)
- `add_rpc_connection.finally` (sync inside an async CM)

The flow:

1. If `registration.released`, return (idempotency).
2. Set `registration.released = True`.
3. Look up `tracked = service_records.get(registration.service_key)`.
   - If still present (the common case): remove this `Registration`
     from `tracked.registrations`. If the list is now empty:
     - Pop the record from `state.service_records`.
     - Call `short_ids.deactivate_service(key)` — `lookup_active_
       service` will now return `None`, but the `s{N}` mapping
       persists for re-registration of the same key.
     - Set `tracked.shutdown_event`.
4. If `registration.source` is an `RPCConnectionRecord` (the common
   case): remove this `Registration` from
   `registration.source.registrations` (i.e. `record.registrations`).
   Workstation-internal `SystemSource` registrations skip this step
   — there's no per-connection list to update.

After step 3 sets `shutdown_event`, the worker wakes, exits its
`async with`, and `service_scope` drains. The bridge inside it
releases its lease on the source `call_scope`'s ref, freeing
`call_scope` to finish its own drain (if it was waiting on this
lease — see the cycle discussion below).

For consumers (`get_service`-derived refs), `service_scope`'s drain
is *also* blocked until each consumer's `call_scope` releases its
bridge. So in the bounceback / consumer case, `shutdown_event` set →
worker wakes → `service_scope.aclose()` → blocks until consumers
release → drains → workstation's outgoing leases on the source ref
finally drop.

### The auto-release cycle

When the *same* peer that registered the service also owns the scope
that holds the registered ref (e.g., `register_service(name,
echo_ref)` where `echo_ref` is mounted on `session.scope`, and the
chain-bridged `release_cb_ref` lands in `session.scope`):

- The peer can only drop `release_cb_ref` by draining `session.scope`.
- `session.scope` can't drain until the workstation releases its
  outgoing lease on `echo_ref` (the wire arena holds it on the
  workstation side, pinning the peer's local handle).
- The workstation releases that lease only when `service_scope`
  drains.
- `service_scope` drains only after `shutdown_event` is set.
- `shutdown_event` is set by the end-callback on `call_scope`.
- That end-callback fires only when `call_scope` drains.
- `call_scope` drains only after the peer drops `release_cb_ref`.

Cycle. The two structurally safe usage patterns documented on
`register_service` (explicit `release()`, or hold `release_cb_ref` in
a child of the scope that owns the registered ref) sidestep it.

**The recommended way to avoid this entirely is the
`expose_service` async context manager helper** (see
`protocols.py`). It does the explicit-release-on-exit dance plus an
unconditional `scope.forget_service(release_cb_ref)` backstop, so
the cycle can't form even if the in-band release stalls.

## States

### `TrackedServiceRecord` lifecycle states

```
                      (none)
                        │   register_service (first)
                        ▼
            ┌────────── PRE-BRIDGE ──────────┐
            │ • record in service_records    │
            │ • short_id active              │
            │ • bridge_ready not set         │
            │ • registrations == []          │
            │ • service_scope is None        │
            └────────────────┬───────────────┘
                worker bridges & sets bridge_ready
                             │
                             ▼
            ┌──────────── ACTIVE ────────────┐
            │ • record in service_records    │
            │ • short_id active              │
            │ • bridge_ready set             │
            │ • registrations != [] (one+)   │
            │ • service_scope alive          │
            └────────────────┬───────────────┘
                last registration released
                             │
                             ▼
            ┌─────────── DRAINING ───────────┐
            │ • record popped from           │
            │   service_records              │
            │ • short_id deactivated         │
            │ • shutdown_event set           │
            │ • worker exiting               │
            │ • service_scope draining       │
            │   (may block on consumer       │
            │    bridges)                    │
            └────────────────┬───────────────┘
                worker task fully exits
                             │
                             ▼
                          (gone)
```

Only ACTIVE is a directly observable, `get_service`-able state. Both
PRE-BRIDGE and DRAINING are transient and only briefly visible.

If `register_service` raises (bridge failed, cancelled before
appending its `Registration`, etc.), its exception handler rolls
back the record creation when it owns the cleanup — popping the
record, deactivating the short ID, and setting `shutdown_event` so
the worker drains. See `register_service`'s `except BaseException`
arm in `service_impl.py`.

### `Registration` states

Just `active` (in both lists, `released=False`) and `released`
(removed from both lists, `released=True`). The `released` flag is
the idempotency key.

### Short-ID states

- `unallocated`: not in any allocator dict.
- `active`: in `_service_short_ids` and `_active_service_keys`.
- `inactive` (remembered): in `_service_short_ids`, not in
  `_active_service_keys`. `lookup_active_service` returns `None`,
  but `get_or_assign_service` for the same key returns the same ID.

## Race windows

Concurrent invocations on the workstation share the event loop, so
"truly parallel" doesn't apply — but coroutines can interleave at
every `await`. The interesting interleavings:

### R1: `get_service` racing concurrent `register_service` on the same key

`register_service` inserts the record into `service_records` and
marks the key active *before* the worker has bridged (the await on
`bridge_ready` happens after the lock is dropped). A racing
`get_service` can acquire the lock, see the key as active, fetch
`tracked` with `service_scope` still `None`, then exit the lock and
hit `bridge_ready.wait()`. It blocks there until the worker either
bridges (success) or fails (raises the bridge error). Either is a
useful semantic for the consumer.

### R2: `get_service` racing concurrent release

- `get_service` enters, takes lock, looks up key (active), fetches
  `tracked`, reads `tracked.service_scope`, releases lock.
- Concurrently (between the lock-release and the
  `bridge_service_from_scope` call): a `release_registration` runs
  (e.g., from another peer's connection drop), pops `tracked` from
  `service_records`, deactivates the short ID, sets `shutdown_event`.
- The worker wakes and starts draining `service_scope`.
- `get_service` calls `bridge_service_from_scope(ref, service_scope)`
  on a draining scope.

`bridge_service_from_scope`'s `check_alive`/`_check_mountable` raises
`RuntimeError` if `self` is draining/closed. So the consumer gets a
spurious-looking error. **Probably fine** for stage 1: it's a real
race the consumer needs to handle (the registration genuinely went
away while they were looking). We could surface a nicer error
specifically for this case.

### R3: Concurrent `release()` and end-callback

Both call `release_registration`; second one early-returns due to
`released=True`. **Idempotency works as designed.**

### R4: `release()` arrives while `register_service` is mid-flight

Can't happen — `register_service` only returns the
`release_cb_ref` to the peer after appending the `Registration`. The
peer can't call `release()` before holding the ref.

### R5: Connection drop during `register_service`

The peer's RPC connection drops between steps 4 and 6 of the setup
flow (worker spawned but `register_service` hasn't yet appended the
`Registration`). `register_service`'s coroutine is cancelled. Its
`except BaseException` arm runs: `registration is None`, `is_new_record
is True`, so under the lock it pops the record from `service_records`,
deactivates the short ID, and sets `shutdown_event`. The worker
wakes (or is already past `bridge_service_from_scope` if the bridge
succeeded) and drains `service_scope`.

### R6: Bridge fails

The worker catches a non-BaseException in `bridge_service_from_scope`,
sets `bridge_error`, sets `bridge_ready`, returns. `service_scope`
drains in the worker's CM exit. `register_service`'s `await
bridge_ready` returns; the `if tracked.bridge_error is not None: raise`
fires. Same exception-handler path as R5 cleans up the record.

## Decisions and remaining defensive checks

### Resolved (by simplifications applied to the code)

1. **`get_service` blocks on `bridge_ready`** instead of raising on
   `service_scope is None`. R1 becomes "consumer waits for the bridge",
   bridge errors surface to the consumer.
2. **`release_registration`'s `.remove()`s are unconditional.** The
   `released` short-circuit at the top guarantees each registration
   is removed exactly once.
3. **`register_service` rolls back on failure.** R5/R6 no longer leave
   stuck records: if no `Registration` was appended and we created
   the record, we pop / deactivate / set `shutdown_event` under the
   lock.

### Kept as belt-and-suspenders, with a warning log

4. **`add_rpc_connection.finally`'s registration sweep.** By the time
   we reach this `finally`, `open_rpc_scope` has already exited, so
   the connection is gone and every dispatch scope from this
   connection's calls should have its end-callback fire — meaning
   each `Registration` should already be released. We don't have
   independent confirmation of this, though, and we don't want to
   leak if it turns out a teardown path can miss the end-callback.
   So the sweep stays, but it logs a warning whenever it actually
   has work to do; if we ever see the warning fire we can dig into
   why and either prove the assumption or fix the missed path.

### Also kept (small, narrowly defensive)

5. **Worker's `finally` "set `bridge_ready` if not set" safety net.**
   Covers worker cancellation before the bridge attempt completes
   (e.g., during server shutdown). Without this, `register_service`
   would deadlock on `bridge_ready.wait()` before its rollback could
   run.
6. **`register_service`'s sync-section comment.** The construction
   + double-`append` happens with no `await` between, so it's atomic
   from any other coroutine's POV. The inline comment now calls this
   out explicitly so future edits don't introduce an `await` mid-way.

### Not touched in this pass

- Making the workstation lock optional / removing it. Cost: future
  multi-step state transitions become more constrained. Benefit:
  small. Skip until we have a concrete reason.

## Things this doc deliberately leaves out

- Entity lifecycle (not yet implemented).
- External-name resolution (next step).
- Cleanup-on-shutdown ordering at the `WorkstationServer` level.
  That belongs in a sibling doc once the connection-level model
  here is locked down.
