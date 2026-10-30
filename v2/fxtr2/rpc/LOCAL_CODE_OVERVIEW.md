# RPC Local Code Overview

Detailed documentation for the Cap-IR RPC lowering system. For a high-level overview, see the
root `CODE_OVERVIEW.md`; for the lifetime primitives this layer sits on top of, see
`fxtr2/leases/LOCAL_CODE_OVERVIEW.md`.

## What this package provides

`fxtr2/rpc/` turns two Cap-IR endpoints (each a program holding some `IRHandle[...]`s, `IRFuture`s,
and lifetime state) into a pair of communicating processes over a bidirectional message stream. It
implements the "LeasedCapIR lowering protocol version 1", heavily inspired by [Cap'n'Web][] but
adapted for Cap-IR's lease-based ownership model and first-class `IRFuture`s.

The lowering layer is intentionally transport-agnostic: it operates on `RPCMessage` objects (a
closed sum type), not on bytes. A separate wire codec (planned) turns `RPCMessage`s into byte
frames suitable for a (web)socket. This split lets us exercise the protocol end-to-end using an
in-process `anyio.create_memory_object_stream` pair during testing.

[Cap'n'Web]: https://github.com/cloudflare/capnweb/blob/main/protocol.md

## File overview

| File | Purpose |
|---|---|
| `lowering_types.py` | Protocol definition: `RPCMessage` sum (push/pull/resolve/reject/watch/closed/break/discard/unpull/abort) and the `Expression[T]` sum that makes up the payload of those messages. |
| `lowering.py` | `LeasedCapRPCConnection` — the runtime object that owns the import/export tables, reader/writer task loops, `_LeaseOwner` impl for regranting, abort/drain lifecycle, and the `_subscribe_backing_lease_break` helper. Thin method wrappers on the class forward to the submodule functions below. |
| `lowering_state.py` | Typed containers for table entries (`TokenImportEntry` / `GrantImportEntry` / … / `FutureExportEntry`), reserved-ID constants, `ConstantExpr` (phase-1 substitution carrier), and `ConnectionLeaseCleanupInfo` (weakref-leak info for imported leases). |
| `lowering_remote_proxies.py` | `RemoteLifetimeToken` / `RemoteGrant` / `RemoteIRHandle` / `RemoteIRFuture` — stateless proxies keyed by `(conn, id)` that delegate to the connection. |
| `lowering_errors.py` | Exception types: `RPCConnectionError`, `HandshakeError`, `RPCConnectionAborted`. |
| `lowering_null_main.py` | `NullMainInterface` — the placeholder main handle used by `as_client`. |
| `lowering_devaluate.py` | Outbound devaluator: `IRCallArg → Expression[T]`. Allocates new export IDs, consults re-export dicts, handles bounce-back, rolls back atomically on error. |
| `lowering_evaluate.py` | Inbound phase-1 substitution and phase-2 evaluators (`eval_as_value` / `eval_as_call_arg` / `eval_forwardable`). |
| `lowering_message_handlers.py` | Per-message `_on_*` handlers. Reader loop routes via `dispatch_message`. |
| `json_serdes.py` | JSON wire codec: `RPCMessage` / `Expression` / `IRStatic` ↔ JSON-serializable trees, plus `wrap_json_stream` adapter. |
| `scope_bridge.py` | `open_rpc_scope` user-facing async CM: bootstraps a `ServiceRef` exchange over a connection and yields an `RPCSession(scope, remote_service)`. |

## Core concepts

### Connection, import table, export table

Each side of a live connection maintains two tables of long-lived, ID-addressable objects:

- **Export table**: values this side has sent (or will send) to the other side and needs to keep
    reachable.
- **Import table**: values this side has received from the other side, each represented by a
    remote-proxy object that routes operations back through the connection.

One side's exports correspond one-to-one with the other side's imports at the same ID. ID
allocation rules:

- IDs `0`, `1`, `2` are reserved for the main interface (`0` = main-interface token, `1` = main
    IRHandle, `2` = main-interface grant).
- When the *importing* side chooses the ID (e.g. `push` allocates three fresh imports for the
    result), it picks the next positive integer from 3 upward.
- When the *exporting* side chooses the ID (e.g. returning a new handle from a call), it picks the
    next negative integer from -1 downward.

IDs are **never reused**, and the identity of a value stored in a table is stable across message
round-trips: if you send a handle you already sent, you use its existing export ID, and the
receiver must intern to the existing import-proxy object. This "bounce-back identity" property is
what makes grant equality meaningful across the wire.

### Tables hold four kinds of value

The type of a table slot is implicit, inferred from the messages that reference it:

- **Tokens** — unique `LifetimeToken` objects, deduplicated by identity.
- **Grants** — objects granting access to a resource. Stored as a single lease on the exporting
    side; each remote lease is a `Lease` backed by a proxy grant on the receiver. Grants
    deduplicate: sending another lease that shares the same exporter-side grant reuses the
    existing ID.
- **IRHandles** — dispatchable remote objects, deduplicated by identity.
- **IRFutures** — result placeholders, deduplicated by identity.

New grants and futures can also be introduced dynamically by `PushMessage`s, which implicitly
allocate three new imports.

### Pipelined outer lease

`IRHandle.dispatch` / `IRHandle.request` accept either a concrete `Lease[B]` or an
`UnwrapForPipelining[Lease[B], Any]` in the outer-lease slot. The wire form re-uses
the existing `AwaitFutureExpr` inside the `HandleRequestExpr.lease` slot (no schema
change, no codec bump):

- **Outbound** (`RemoteIRHandle.dispatch`). The devaluator's
    `_devaluate_walk(UnwrapForPipelining)` branch already encodes the pipelined
    form as `AwaitFutureExpr(future=…, lease=…)`. `devaluate_handle_request`
    accepts the wider type for `handle_lease`. Outbound resolve-obligation
    accounting (`_bump_await_future_obligation`) is symmetric with the existing
    `pipeline_dispatch` `inner_handle_lease` path.
- **Inbound** (`_dispatch_handle_request`). The new `_eval_lease_or_pipelined`
    helper mirrors `eval_as_call_arg`'s `AwaitFutureExpr` / `HandleRequestExpr`
    branches: it produces an `UnwrapForPipelining[Lease, Any]` instead of
    eagerly awaiting, and `handle.dispatch` resolves it inside the spawned
    runner (per the widened `IRHandle.dispatch` contract). This means an
    inbound push whose lease slot pipelines through an upstream future doesn't
    block phase-2 evaluation on the lease's resolution — the dispatch fires
    immediately and the local runner pipelines the lease wait alongside any
    request-level pipelining.

If the lease future rejects, the rejection propagates through the dispatched
future via the standard outcome-routing path (`box.set_exception`); no special
return-packet shape.

### Push / pull / resolve

An RPC call follows this sequence:

1. The caller sends a `PushMessage` containing an expression (typically a `HandleRequestExpr`).
    The push implicitly allocates the next three positive import IDs for the caller:
    - a fresh **token** for the result lifetime,
    - an **IRFuture** representing the result, and
    - a **grant** giving access to the result.
2. The callee evaluates the expression, delivers the call, and registers these three IDs in its
    own export table.
3. If the caller wants the actual value delivered back, it sends a `PullMessage` referencing the
    future import ID. (Pure pipelining chains can skip the pull.)
4. When the callee's computation resolves, it sends a `ResolveMessage` with the resolved
    expression, or a `RejectMessage` with an `IRException` expression. These are the same
    messages used for any exported future, not just push-created ones.
5. The caller processes `resolve`, then sends a `DiscardMessage` for the grant once it no longer
    needs the result — at which point the callee is free to drop its copy.

Supporting messages:

- `WatchMessage` / `ClosedMessage` observe when an imported token ends.
- `BreakMessage` revokes an exported grant and breaks any outstanding leases on the importer.
- `DiscardMessage` (with refcount) releases an import reference.
- `UnpullMessage` (with refcount) cancels a previously-sent pull request.
- `AbortMessage` terminates the session: both sides revoke all connection-imported grants and
    close all connection-imported tokens.

### Expressions

`Expression[T]` is a recursively-composable mini-language that describes how to reconstruct or
reference an IRValue on the receiving side:

| Expression | Evaluates to | Notes |
|---|---|---|
| `StaticExpr[T]` | A static `IRStatic` value | Primitives / nested static structures |
| `ListExpr[T]` / `DictExpr[T]` | A list / dict of sub-expressions | |
| `IRTaggedExpr[T, V]` | An `IRTagged` | Tag is static; value is an expression |
| `ImportedLeaseExpr` / `ExportedLeaseExpr` | A `Lease` | Cloned from the exporting side's grant; `ExportedLeaseExpr` also carries the subject `token_export_id` to pin grant→token identity on first introduction |
| `ImportedIRHandleExpr` / `ExportedIRHandleExpr` | An `IRHandle` | |
| `ImportedIRFutureExpr` / `ExportedIRFutureExpr` | An `IRFuture` | |
| `AwaitFutureExpr[T]` | The resolved value of a future | Used to represent `UnwrapForPipelining` |
| `WalkFutureExpr[T]` | A sub-future via an `attr_path` | Same lifetime as the parent future |
| `HandleRequestExpr` | The result of making a request on a handle | Core of `push` payloads; can be nested inside other expressions for chained pipelining |

### Regranting

The connection acts as a **regrantor** for every lease that crosses it. For any lease it sends,
it retains one lease on the original grant and uses that to service dispatches from the other
side. For any lease it receives, it issues a new local grant bounded by the connection's
session — so when the connection closes, every incoming re-grant breaks synchronously.

The concrete implementation lives in `lowering.py`, as remote-proxy subclasses of
`LifetimeToken` / `Grant` / `Lease` / `IRHandle` / `IRFuture`. User code never constructs these
directly; they arrive via the main interface or via values delivered inside other expressions.

## `LeasedCapRPCConnection`

The top-level connection object.

- Construction: `LeasedCapRPCConnection(transport, our_main_interface, our_main_lease)`. The
    transport is an `anyio.abc.ObjectStream[RPCMessage]`. The connection transfers
    `our_main_lease` to the remote side and exposes `our_main_interface`, `our_main_grant`,
    `their_main_interface`, and `their_main_lease` as properties.
- `as_client(transport)`: convenience — create a connection with a null main interface on our
    side (we don't expect to be called).
- `as_server(transport, our_main_interface, our_main_lease)`: convenience — discard the
    remote's main lease immediately, since we don't plan to call back.

The connection internally runs a background reader task that pulls `RPCMessage`s off the
transport and dispatches them to per-message handlers. Outbound calls made on imported handles
(or imported futures, for pipelining) are translated into `PushMessage`s and routed back through
the same transport.

## JSON wire codec

`json_serdes.py` turns the transport-agnostic `RPCMessage` objects into JSON-serializable
trees suitable for a (web)socket transport once a JSON framer is layered on top. The wire
shape is modeled on [Cap'n'Web's JSON protocol][capnweb-protocol]:

- Primitives (`None` / `bool` / `int` / `float` / `str`) are emitted literally.
- `bytes` uses the `["bytes", "<base64>"]` escape.
- Plain mappings are literal JSON objects.
- Plain sequences use the `[[inner]]` array-escape.
- Everything else is a `["<tag>", ...]` form: `["fxtr_rpc_hello", 1]`, `["push", <expr>]`,
    `["import_handle", id]`, `["await", ...]`, `["error", msg, cat, data, traceback_info]`,
    etc.

The `["error", ...]` form is exactly 5 elements; the 5th is a JSON list of formatted-traceback
strings (`IRException.traceback_info`). On every encode of an `IRException`, `encode_static`
appends a fresh entry to that list capturing the local Python `__traceback__` / `__notes__`
state, so the original Python stack survives across each wire hop.

`StaticExpr` values are **inlined** into their equivalent static representation (no outer
wrapper) — a clean match for Cap'n'Web's shape at the cost of requiring the decoder to be
told whether it's decoding a message, an expression, or a static.

The decoder is deliberately structural: primitives / `["bytes", ...]` / `["error", ...]`
produce `StaticExpr` (their wire form is inherently static), but container forms
(`[[...]]`, `{...}`, `["tag", ...]`) always produce their structural expression types
(`ListExpr` / `DictExpr` / `IRTaggedExpr`), even when every child happens to be a
`StaticExpr`. `StaticExpr` is pushed in toward the leaves rather than pulled out to the
root. Consequence: `StaticExpr([1, 2])` and `ListExpr((StaticExpr(1), StaticExpr(2)))`
encode to the same wire form and both decode to the latter — still semantically equivalent
under the Cap-IR evaluator.

The public surface:

- `encode_message` / `decode_message`
- `encode_expression` / `decode_expression`
- `encode_static` / `decode_static`
- `decode(value, *, mode)` — unified entry-point keyed by mode.
- `wrap_json_stream(transport: ObjectStream[JSONTypes]) -> ObjectStream[RPCMessage]` —
    adapter that pipes through the message codec on each send/receive.

The decoder is strict. Every structural anomaly raises a typed subclass of `JsonCodecError`:
`MalformedMessageError`, `MalformedExpressionError`, `MalformedStaticError`,
`NonStaticInStaticContextError`, or `ProtocolVersionError`. Reject and Abort payloads
additionally enforce the "static-only" rule at the codec layer — a peer that tries to
smuggle an import reference through a `RejectMessage` is rejected before the connection
ever sees it.

The codec is pinned to protocol version 1. The only forward-compat hook is the serialized
form of `HelloMessage` (`["fxtr_rpc_hello", 1]`); a non-1 version raises
`ProtocolVersionError`.

[capnweb-protocol]: https://github.com/cloudflare/capnweb/blob/main/protocol.md

## What the connection supports

The lowering layer covers:

- Connection bootstrap via `as_client` / `as_server` plus a mandatory
    `HelloMessage` handshake, with graceful abort on protocol violations.
- Outbound devaluator (IRCallArg → `Expression`) and inbound two-phase evaluator
    (phase-1 substitution, phase-2 evaluation) for all `Expression` variants.
- Full push / pull / resolve / reject round-trip on main-interface and nested
    handles, with pipelining (including forward-delegation via
    `ResolveMessage(AwaitFutureExpr(...))`).
- Leases-across-the-wire: regrant, break, discard, watch / closed, drain-based
    shutdown. The connection is the regrantor for every lease it passes in either
    direction; connection close breaks everything imported.
- **Resolve-obligation accounting** that keeps drain robust to no-lease waits and
    cancelled-wait `UnpullMessage`s.
- Handle bounce-back identity, `as_client`-style rejection paths
    (`NullMainInterface` discards leases inside inbound request args), and
    best-effort `DiscardMessage` emission for handle/future/token imports when
    their proxies are garbage-collected.
- Proxy singleton caching to collapse per-access churn, and a pin-chain
    invariant that defers a token's accumulated introduction-refcount to a
    single `DiscardMessage` emitted when the token proxy itself GCs — see the
    "Proxy caches" and "Proxy-GC tracking" subsections below.

See `plans/phase3/stage8_lease_system/overview.md` + `phase_d_plan.md` for the
design history behind these features.

Not yet implemented: a byte-level framer that sits underneath `wrap_json_stream`
(e.g. newline-delimited JSON for HTTP-ish transports) — applications currently
have to bring their own framer.

### Drain and shutdown

On normal CM exit the connection:

1. Discards `their_main_lease`, which — via the `_LeaseOwner._clean_up_lease_data` path —
    emits `DiscardMessage(import_id=2, refcount=1)` to the peer.
2. Awaits `_drain_event`. Drain is satisfied when **all four** of these conditions hold:
    - every `GrantImportEntry.live_leases` is empty or broken (no imported leases we hold);
    - every `GrantExportEntry.outstanding_count == 0` (peer has discarded every grant
      we exported);
    - every `FutureImportEntry.resolve_obligation_refcount == 0` (peer owes us no pending
      resolve, either from our own `PullMessage` or from inbound `AwaitFutureExpr`);
    - every `FutureExportEntry.resolve_obligation_count == 0` (we owe the peer no
      pending resolve).
   Setting the event is done by `_check_drain`, called after every state transition
   that might flip any of the four (outbound `DiscardMessage` / `UnpullMessage`
   emission, outbound `ResolveMessage` / `RejectMessage`, inbound `DiscardMessage` /
   `BreakMessage` / `ResolveMessage` / `RejectMessage` / `UnpullMessage`, local
   grant-backing-lease break, and `_abort`).
3. Closes the outbox; the writer loop drains remaining messages, `send_eof`s, and exits.
    Reader unwinds on the peer's own EOF.

If the peer never releases our exported grants, the drain blocks indefinitely — that's
the intended "I'm done, waiting for the remote to agree" pattern. Callers wanting a
bounded wait should wrap the `async with` in their own cancel scope.

### Resolve-obligation accounting (§2h)

The protocol's `UnpullMessage` rule: the refcount on an unpull equals the number of
pending obligations the exporter has to deliver a resolution, from the importer's
perspective. Two events contribute +1 per occurrence (per `UnpullMessage` docstring):

1. The importer sent a `PullMessage` that has not yet been answered.
2. The importer *received* an `AwaitFutureExpr` referencing this future from the
    exporter — evaluating such an expression requires pulling the future, so each
    inbound occurrence implicitly preemptively obligates the exporter to resolve.

Outbound `AwaitFutureExpr`s (that we write out, referencing one of our future
exports) contribute symmetrically on our **export** side: the exporter's
`resolve_obligation_count` is bumped every time the devaluator emits an
`AwaitFutureExpr` that resolves to an `ExportedIRFutureExpr`. A single outbound
`ResolveMessage` / `RejectMessage` discharges every outstanding obligation on
both sides — once emitted (`resolved_sent = True`), late inbound `PullMessage` /
`AwaitFutureExpr` references are silently no-op'd.

**Where tracking lives:**

- `FutureImportEntry.resolve_obligation_refcount` — mutated by `RemoteIRFuture.wait`
    (bumps on the 0→1 transition that sends a `PullMessage`) and by phase-1
    substitution (bumps for each inbound `AwaitFutureExpr` whose `future` slot
    resolves to one of our imports; returned to the message handler as
    `phase1_obligations` for release in the phase-2 spawn's `finally` block).
    Zeroed on inbound `ResolveMessage` / `RejectMessage` via
    `_zero_future_import_obligation` and on outbound `UnpullMessage` via
    `_decrement_local_consumer`.
- `FutureImportEntry.local_consumer_count` — mutated by `RemoteIRFuture.wait` (on
    entry / `finally`) and by phase-1 substitution (per-spawn). When this drops
    to zero while `resolve_obligation_refcount > 0`, `_decrement_local_consumer`
    emits `UnpullMessage(refcount=resolve_obligation_refcount)` and zeroes the
    refcount. This is how cancelled waits release their obligations without
    aborting the peer's resolver if another local consumer still wants the result.
- `FutureExportEntry.resolve_obligation_count` — mutated exclusively by two
    primitives on the connection,
    `_increment_resolve_obligation_count(entry, *, count=1)` and
    `_decrement_resolve_obligation_count(entry, *, count=1)`. These fold in the
    resolver-spawn / resolver-cancel transitions so the load-bearing invariant

      `resolve_obligation_count > 0 ∧ ¬resolved_sent
          ⟺ resolve_task_cancel_scope is not None`

    holds at every quiescent point. The increment primitive spawns a resolver on
    the 0 → positive crossing (via `_ensure_resolver_running_for` →
    `spawn_resolver_for_future_export`); the decrement primitive cancels the
    resolver on the positive → 0 crossing (and clamps over-decrement via
    `phase1_protocol_violation`). All three legitimate call sites route through
    them: `_on_pull` (inbound `PullMessage`), the devaluator's
    `_bump_await_future_obligation` (outbound `AwaitFutureExpr`, with the
    devaluator's `ctx.undo_log` calling `_decrement_…` on rollback), and
    `_on_unpull` (inbound `UnpullMessage`, applying `msg.refcount` in one shot).
    The sole exception is `_zero_future_export_obligation`, which is invoked by
    the resolver itself at the moment of `ResolveMessage` / `RejectMessage`
    emission: it zeroes the count and sets `resolved_sent = True` directly,
    bypassing the cancel path because the caller *is* the resolver.

    This is the load-bearing property the importer side relies on when
    suppressing redundant `PullMessage` emission: an inbound `AwaitFutureExpr`
    pre-pays the exporter's obligation via the importer's
    `resolve_obligation_refcount`, and the importer trusts that a resolver is
    actually mid-delivery on the exporter side whenever the obligation is
    non-zero. Before the fix in `plans/extras/extra6_devaluator_obligation_bug.md`
    the devaluator-bump path violated this — it incremented the count without
    spawning — so importer-side waits could block forever; the unified
    primitives close the gap.

    `FutureExportEntry.grant_export_id` is the canonical source of the backing
    lease the resolver clones (one `wait_lease` consumed by `wait_or_forward`,
    one `survival_lease` held in the resolver's `finally`). It is populated by
    `_on_push` (which registers the future + grant pair together) and by the
    devaluator's `UnwrapForPipelining` walk via
    `_pair_grant_with_future_export` (which pairs a freshly-devaluated lease
    with the future it accompanies, before `_bump_await_future_obligation`
    triggers any spawn).
- `GrantImportEntry.wait_break_callbacks` — break-notification subscriptions for
    in-flight no-lease waits. `RemoteIRFuture.wait` registers one after
    discarding the user-visible lease (per the §2h design: the wait holds no
    `live_leases` slot but still needs a prompt `LeaseBrokenError` if the grant
    breaks). Fired alongside `live_leases` broken-callbacks by
    `_mark_grant_import_broken`. Does not contribute to `introduction_refcount`
    or `DiscardMessage` accounting.

**No-lease wait flow** (`RemoteIRFuture.wait`):

1. Validate the user's lease, `take()` it.
2. Look up `FutureImportEntry` and `GrantImportEntry`, materialize the result box
    via `LeasedCapRPCConnection._materialize_future_box` if absent (which also
    installs the entry's token-end subscription — see "Future-import box
    discard policy" below).
3. Send `PullMessage` on the 0→1 transition of `resolve_obligation_refcount`.
4. Bump `local_consumer_count`.
5. Discard the user-taken lease — if this was the last `live_leases` entry, the
    owner-cleanup path emits `DiscardMessage` for the full
    `introduction_refcount`.
6. Register a wait-break callback on the `GrantImportEntry` (in place of the
    lease's own `register_broken_callback`).
7. `await box.await_resolution(path, register_break=<the grant-backed registrar>)`.
8. In `finally`: decrement `local_consumer_count`. On the 1→0 transition with
    `resolve_obligation_refcount > 0`, emit `UnpullMessage` with the full refcount
    and zero it. `_decrement_local_consumer` also re-checks the box-discard
    gate, which lets a deferred discard (token ended while a waiter was draining)
    finally fire.

**Future-import box discard policy.** Unlike `LocalPendingResultBox`, which auto-
discards via a token-end subscription, a `FutureImportEntry`'s box is a plain
`PendingResultBox` whose lifecycle is owned by the entry itself. The peer's token
can end while we're still locally producing a result (async `eval_forwardable`
inside `_on_resolve`'s `deliver` task), so coupling box-discard directly to
remote-token-end would surface `LeaseBrokenError` to a parked waiter that should
have received the value (see `plans/extras/extra5_future_box_race_fix.md`).
Instead, `FutureImportEntry` carries `delivery_in_flight: bool` and
`token_ended: bool`, and `LeasedCapRPCConnection._maybe_discard_future_box`
gates the discard on:

- The token has ended.
- No delivery is currently in flight.
- If the box has already resolved/rejected/forwarded, no consumer is still draining.

`_on_resolve` flips `delivery_in_flight = True` before spawning `deliver`, the
finally clears it, and the trigger sites — token-end callback,
`_on_resolve`'s deliver-finally, `_on_reject`'s tail, and
`_decrement_local_consumer` — each call `_maybe_discard_future_box`. Removal of
the entry through `_maybe_remove_future_import` also discards the box and
deregisters the token subscription, so stored leases don't leak when the entry
is fully drained.

**Resolver survival lease (A-side `_on_pull`).** When a resolver follows a
forwarded box (`box.state == "forwarded"` triggered by
`work → UnwrapForPipelining → resolve_from`), `wait_or_forward` returns
immediately and releases its held clone of `backing_lease`. Combined with the
importer's prompt `DiscardMessage`, this could tear the push lifetime down while
the resolver is still mid-chain — which would end the token, send `ClosedMessage`,
and spuriously break the importer's pending wait. To prevent that, `_on_pull`
clones `backing_lease` **twice**: one `wait_lease` consumed by the chain
traversal, and one `survival_lease` held in the resolver task's `finally` block.
The survival lease is released only when the resolver has emitted a
`Resolve`/`Reject` or been cancelled by `_on_unpull`.

### BreakMessage propagation

When we register a grant export we subscribe to the backing lease's broken-callback via
`_subscribe_backing_lease_break`. If the lease breaks locally (e.g. the user revoked the
subject lifetime), the callback:

- Records a break tombstone (`_broken_grant_export_tombstones[export_id] = (grant,
    reason)`).
- Emits `BreakMessage(grant_export_id, reason)`.
- Tears the export entry down locally (removes from `_exports` /
    `_grant_export_id_by_grant`, discards the now-broken backing lease).
- Re-checks drain.

Local teardown is important because the peer is **not** obligated to respond with a
`DiscardMessage` for a broken grant — keeping the entry alive waiting for one would
block drain. A racing `DiscardMessage` from the peer is tolerated: `_on_discard` sees
no entry, drops the tombstone (the discard is the peer's acknowledgment it will never
reference the id again), and is otherwise a no-op.

The tombstone covers the **push-path** side of the same race: a peer request whose
`ImportedLeaseExpr` crossed our `BreakMessage` on the wire still references the
torn-down export. Phase 1 (`_phase1_walk`'s `ImportedLeaseExpr` branch) consults the
tombstones on a missing entry and substitutes an already-broken lease on the original
grant (`make_broken_lease` in `leases/local_lease.py`), so the request degrades with a
request-scoped `LeaseBrokenError` (raised by `dispatch`'s `take()`, rejected through
the normal per-push failure path) instead of aborting the connection. Export ids are
never reused, so tombstone-absence still cleanly means "never had" → protocol
violation. Without a peer discard, tombstones are retained for the connection's
lifetime (small, consistent with import entries never being removed).

Inbound `BreakMessage` (`_on_break`): marks the `GrantImportEntry.broken_reason`,
iterates `live_leases` via `finalizer.peek()`, calls `_mark_broken` on each (firing
user broken-callbacks), detaches finalizers, clears `live_leases`, fires (and then
clears) every `wait_break_callbacks` entry so in-flight no-lease waits receive a
prompt `LeaseBrokenError`, re-checks drain.

### Table invariants and "active" set bookkeeping

`LeasedCapRPCConnection` maintains six integer sets alongside `_imports` /
`_exports`, so drain checks can be O(1) and abort can walk only the entries that
still have state to tear down:

| Set | Element condition |
|---|---|
| `_active_grant_imports` | `GrantImportEntry.live_leases` non-empty AND `broken_reason is None` |
| `_active_grant_exports` | `GrantExportEntry.outstanding_count > 0` |
| `_active_token_imports` | `TokenImportEntry.ended is False` |
| `_watched_token_exports` | `TokenExportEntry.watch_deregister is not None` |
| `_active_resolve_obligation_imports` | `FutureImportEntry.resolve_obligation_refcount > 0` |
| `_active_resolve_obligation_exports` | `FutureExportEntry.resolve_obligation_count > 0` |

Together with the table invariants, these form the bookkeeping contract of the
connection:

- **Every** `GrantExportEntry` in `_exports` has a matching entry in
    `_grant_export_id_by_grant` (keyed by `backing_lease.grant`). Analogously for
    handle / future / token exports with their respective `_*_export_id_by_*`
    dicts. Registered uniformly by the `_register_*_export` helpers (main grant
    in `__init__`, `_on_push`, `_devaluate_*` — the last of which does the same
    mutation under a speculative-rollback context). Teardown in `_on_discard` /
    `on_break` removes the entry from both places with unconditional `del`.
- **Every** transition that could flip a membership condition above updates the
    corresponding set. The set mutation sites are:
    - `_register_lease` / `_clean_up_lease_data` (grant imports empty↔non-empty).
    - `_on_break` (grant import broken).
    - `_register_grant_export` (new grant export, count=1 → added).
    - `on_break` callback in `_subscribe_backing_lease_break` (local revoke
        tears down the export).
    - `_on_discard` at count=0 (remove grant export).
    - Devaluator rollback for a fresh grant export (undo the add).
    - `_register_token_import` (new token import).
    - `_on_closed` (peer-side end).
    - `_on_watch` (install watch; also registers its deregister in `on_end` so
        a local token end scrubs the set in place).
- **Abort uses the sets for teardown.** `_abort` iterates only `_active_*` for
    grants / tokens instead of scanning the full tables; each iteration includes
    a kind assertion as an invariant tripwire.

When adding a new code path that inserts into `_imports` / `_exports`, use the
`_register_*_import` / `_register_*_export` helpers — they own the full
transition (table insert + reverse-lookup + active set + `_subscribe_backing_lease_break`
for grant exports). The devaluator's speculative path doesn't use them (its
undo-log shape is a different abstraction) but mirrors the same invariants
site-by-site.

Teardown is symmetric. Use the `_tear_down_*_export` helpers for export removal
(`_on_discard` at count=0, the `on_break` callback, and `_abort`) and the
`_mark_*_import_*` helpers for import state transitions (`_on_break`,
`_on_closed`, `_abort`). Each teardown helper removes the entry from the
relevant active set (and the export variant removes from the reverse-lookup
dict + deregisters the broken callback / watch deregister where applicable),
so callers that iterate an active set during mass teardown should walk a
snapshot (`list(...)`) — the helper mutates the set from under the iterator.
Import helpers assume non-idempotent preconditions (`broken_reason is None` /
`not ended`); callers check first.

### Proxy caches (§2j)

Every `Remote…` proxy produced anywhere in the connection goes through
one of the connection's `_get_or_create_{token,handle,future}_proxy`
helpers, which consult a `weakref.WeakValueDictionary` keyed by import id
and return a cached singleton for that `(conn, id)` — or mint a fresh
proxy on a cache miss. The WeakValueDictionary means cached proxies
don't pin themselves: the moment external references drop, the weakref
evicts and the proxy's finalizer fires.

`RemoteIRFuture` with a non-empty `attr_path` is *not* cached — each walk
path is a distinct local view of the same future import, and the user may
hold several walked views independently. Those proxies bump/decrement
`live_proxy_count` directly via `__post_init__` without going through a
cache lookup.

The cache collapses what would otherwise be per-access churn (every
`.token` / `.subject` property access, every phase-1 re-emission) down to
one proxy object per id, so `live_proxy_count` on the entry tracks
"is anyone still referencing this import" rather than counting transient
reconstructions. See `lowering_remote_proxies.py` for the cache-hit /
cache-miss paths.

### Proxy-GC tracking (§2j / §2j.5)

`TokenImportEntry`, `HandleImportEntry`, and `FutureImportEntry` each
carry a `live_proxy_count: int`. Every `Remote…` proxy's `__post_init__`
bumps the entry's count and installs a `weakref.finalize` callback that
decrements on proxy GC. When the count drops to zero while
`introduction_refcount > 0`, the callback enqueues a single
`DiscardMessage` for the remaining refcount and zeroes the counter.
Asserts (not silent skips) guard against bump/decrement mispairings and
entry-kind drift — our bookkeeping either holds or blows up loudly.

**Deferred token discharge.** Introduction refcounts are a wire-clock,
not a liveness counter: their only purpose is to make discard-vs-
reintroduction races safe. Every `Exported…Expr` that carries a
`token_export_id` — i.e. every `ExportedIRHandleExpr`,
`ExportedIRFutureExpr`, and `ExportedLeaseExpr` — counts as one
introduction of the subject token on the peer's `token_export`, *and*
the importer bumps its own `token_entry.introduction_refcount` by the
same amount. That's all the rule says; it does **not** say when the
importer has to emit the matching discard.

So when a handle/future/grant dependent retires, the importer emits
only its own `DiscardMessage` — the subject token's share of
introductions stays accumulated in `token_entry.introduction_refcount`.
The cached `RemoteLifetimeToken` proxy's own GC finalizer later emits
one `DiscardMessage(token_import_id, <full accumulated refcount>)`
that retires every introduction in one shot. Between those two events,
the peer's `token_export.outstanding_count` legitimately exceeds the
sum of its live dependents' counts — the surplus is exactly the
token-introductions we still owe to retire. No underflow, no paired
emission, far fewer wire messages for a long-running session with
churny handles.

**Pin chain.** What makes the deferral safe is ordering: the token
proxy must not GC before its dependents have already emitted their
retirements. `RemoteGrant`, `RemoteIRHandle`, and `RemoteIRFuture`
each carry a required `_token: RemoteLifetimeToken[B]` field holding
a strong reference to the cached subject-token proxy (supplied at
construction by `_get_or_create_{handle,future}_proxy`, the main-
interface init block, and phase-1 substitution's `ExportedLeaseExpr`
path). So while any dependent proxy for a given subject token is
alive, the token proxy is pinned; its finalizer fires only after all
dependents have fired theirs.

`RemoteIRFuture.walk` forwards `_token` to child proxies, so a walked
sub-future correctly participates in the same pin chain.

**Pair-bump at registration.** Phase-1 substitution naturally pair-bumps
the subject token's `introduction_refcount` because
`_phase1_register_{handle,future,grant}_import` first calls
`_phase1_register_token_import`. The main-interface setup
(`__init__`) and the push-allocating sites (`RemoteIRHandle.dispatch`,
`RemoteIRFuture.pipeline_dispatch`, `_on_push`) register each entry
directly without going through phase-1, so they manually bump
`token_entry.introduction_refcount += 2` (one per dependent). That
keeps this side's running total in lock-step with the peer's
`token_export.outstanding_count`, so the single final token discard
retires the correct amount.

Reserved main-interface IDs (0 / 1) are kept at `live_proxy_count ≥ 1`
for the connection's lifetime via the cached
`_their_main_interface_proxy` and `_their_main_token_proxy` strong
references. No final token discard is emitted on clean shutdown for
these — the abort path short-circuits the finalizer — and the peer
tears their main-token export down via its own abort handling.

Holding onto a handle/future proxy never pins a cross-process resource
(leases do that, via grant-import refcounting in §2f), so this whole
layer is pure optimization on the peer's export-table footprint. A
slow-to-fire finalizer or a peer that ignores handle/future/token
discards causes no correctness issues.

### Error-handling policy for `_on_*` handlers

`_on_*` handlers distinguish two kinds of anomaly:

- **Protocol violation** (peer references state we've never had): abort via
    `phase1_protocol_violation` or an explicit `_abort(...)` + raise. Examples:
    `PullMessage` / `ResolveMessage` / `BreakMessage` for an ID never allocated;
    `DiscardMessage` whose refcount overshoots `outstanding_count`;
    kind-mismatched re-introduction of an ID.
- **Teardown race** (peer references state we no longer have): silent no-op.
    Examples: `DiscardMessage` for a grant we just locally revoked + tore down
    via `on_break`; duplicate `ClosedMessage` / `BreakMessage`.

The distinction is "peer references state we've never had" (abort) vs "peer
references state we no longer have" (no-op). Duplicate redundant messages count
as teardown races — the protocol leaves "re-send or not" to sender discretion.

## `open_rpc_scope` (`scope_bridge.py`)

User-facing async CM that wraps :class:`LeasedCapRPCConnection` with a fresh
:class:`fxtr2.data.entity_ref_scope.FxtrScope` containing a :class:`ServiceRef`
pointing at the peer's main service. Caller has already mounted/bridged
``our_main_service`` in the active scope; ``open_rpc_scope`` does *not* mount
anything itself.

```
async with open_rpc_scope(transport, our_main_service) as session:
    # session.scope is a fresh mountable FxtrScope (child of active)
    # session.remote_service is a ServiceRef[T2] pointing at the peer's main
    proxy = await session.remote_service.resolve(session.scope)
    ...
```

**Bootstrap protocol.** Both sides eagerly send their main-service info as the
call argument of a single ``Call("fxtr_export_main_service", packet)`` on the
peer's wrapper handle. The packet shape is ``IRTagged("fxtr_main_export",
{handle, type_id, handle_lease})`` — ``handle`` is the peer's main-service
handle, ``type_id`` is its wire-portable identity bytes (raw data-id, no
envelope), ``handle_lease`` is a fresh ``Lease[handle.token]`` from the peer's
host scope. No arena coverage for the type_id (it's wire-portable identity,
not arena-tracked content; consumers wanting the loadable form dispatch
``$fxtr.type``). Each side's wrapper handle parses the inbound packet, writes
it to an in-process ``MemoryObjectStream``, and returns ``None``. The body
reads the peer's packet and installs the peer's main service into the
freshly-opened ``user_scope`` via ``attach_bridged_lowlevel_cap`` as an
``IndependentLocalBridge`` (the peer's lifetime is independent of
``user_scope``).

**Lifetime nesting** (outer → inner): ``host_scope`` (caller's active scope)
→ wrapper ULL + task group (via ``open_lifetime_task_group``) → conn → user
scope. The wrapper sits *outside* the conn because the conn needs it as
``our_main_interface`` from construction; it sits *inside* host_scope because
``_build_export_packet`` reaches into host_scope's bridge graph to mint the
shipped lease.

**No explicit drain coordination needed.** Each side's user_scope holds bridges
that import the peer's tokens; symmetrically, the bootstrap packet ships
host_scope main-token leases, so the peer's user_scope holds imports of *our*
exports. On exit, each side's user_scope drain releases the peer's grant
exports (DiscardMessages flow), and the conn drain inside ``open_rpc_scope``
blocks until the peer also drains. The drain is the rendezvous: while a side
sits in conn drain, its outer ``host_scope`` (wrapping the ``open_rpc_scope``
CM) stays alive, so its services keep dispatching the peer's calls. Once both
sides have entered drain and torn down their bridges, both unblock together.

## See also

- `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` — the lifetime primitives used by the lowering layer.
- `lowering_types.py` module docstring — the full protocol specification, written as reference.
- `lowering.py` module docstring — implementation-level notes.
