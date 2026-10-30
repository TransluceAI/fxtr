# Stage 8: Lease-based capability system

## Goal

Implement the lease-based lifetime + capability-passing RPC redesign whose scaffolding already lives in `fxtr2/leases/` and `fxtr2/rpc/`. The endgame is a single coherent system with three layers:

1. **Leases / Grants / Tokens** — primitives for communicating resource lifetimes safely, both locally and across a connection.
2. **LeasedCapIR (Cap-IR)** — an abstract intermediate representation for capabilities over JSON-like data. Local and remote resources look identical at this layer.
3. **Lowering** — a protocol that turns two LeasedCapIR endpoints into a bidirectional message stream (plus, later, a simple codec that turns messages into bytes for a (web)socket).

See also `log.md` for earlier design history, and the linked Google doc for the most recent sketch.

## Status going in

The module layout is in place:

- `fxtr2/leases/`
  - `lease_types.py` — `LifetimeToken`, `Grant`, `Lease`, `FreshLifetime[…]` phantom type machinery, exception types. Mostly complete ABCs + docstrings.
  - `local_lease.py` — `LocalLifetimeToken`, `LocalLifetimeLease`, `UnconditionalLocalLifetime`, `LocalAttachedTokenLease`, `AttachedLocalLifetime`. All methods `raise NotImplementedError`.
  - `leased_cap_ir.py` — `IRValue`, `IRTagged`, `IRException`, `IRHandle`, `IRFuture`, `UnwrapForPipelining`, request types (`Call`, `GetItem`, `WaitAndIgnoreFutureThen`). Mostly complete ABCs + docstrings.
  - `leased_cap_common.py` — `IRHandleLocalImplBase`, `LocalPendingResultBox`, `IRFutureLocalImpl`. Scaffolded, mostly `TODO`.
  - `lease_util.py` — `lease_cancel_scope` helper. Complete.
- `fxtr2/rpc/`
  - `lowering_types.py` — full protocol spec: `RPCMessage` variants and `Expression[T]` variants. Complete.
  - `lowering.py` — `LeasedCapRPCConnection` shell. All `TODO`.

`LOCAL_CODE_OVERVIEW.md` in both `leases/` and `rpc/` are stubs.

## Design clarifications decided during planning

These should be propagated into docstrings and `LOCAL_CODE_OVERVIEW.md` during Stage A.

- **Grants, 1:1 with "authorities".** One `Grant` per `UnconditionalLocalLifetime`. Each `AttachedLocalLifetime` has one grant for its `key_token`. The grants returned by `exchange_forward` are dataclass grants that identify `(AttachedLocalLifetime, input_grant)`, so output-lease equality mirrors input-lease equality.
- **Synchronous revoke.** `revoke()` synchronously ends the token and fires broken callbacks for all extant leases. Registering a broken callback on an already-broken lease fires immediately. (Over RPC, the other side re-grants synchronously at the moment it learns of the upstream revoke.)
- **Auto-close on last lease.** For `UnconditionalLocalLifetime`, discarding the last outstanding lease closes the token itself (not just marks it reclaimable); end-callback subscribers use this as the reclaim signal.
- **Key token is structurally gated.** The only way to reach the output lifetime is `exchange_forward(key_lease, req_lease)`. Nothing else should be attached to the key token by convention.
- **IRFuture shape.** Local `IRFuture` = `(LocalPendingResultBox, attr_path)`. `walk` extends the path; `wait` awaits the box, descends the path, and deep-copies the result; `pipeline_dispatch` queues a continuation on the box.
- **Fresh lifetime on dispatch.** `dispatch` returns a `(future, lease)` pair under a fresh `UnconditionalLocalLifetime` owned by the dispatcher. That lifetime is safe to close as soon as no leases remain (even if the underlying call hasn't completed — the resolution is then ignored).
- **Dispatch lease discipline.** All `request`/`dispatch` implementations should `lease.take()` on entry and discard their internal copy when done. Callers should assume leases passed as args are consumed.
- **Task group hosting.** `IRHandleLocalImplBase._task_group` must outlive the handle's token. The typical setup is "one task group per token, multiple handles per (token, task group) pair." We will want a helper context manager that creates a token + task group together and plumbs errors-in-task-group → revoke-token.
- **Concrete RPC subclasses.** `rpc/lowering.py` will house the concrete remote-proxy subclasses of `LifetimeToken`/`Lease`/`Grant`/`IRHandle`/`IRFuture` that route back into the connection manager. These are implementation details; application code only sees the ABCs.

## Open issues noted for later

- `Lease.take` was accidentally `@abstractmethod` with a default body. Already fixed by daniel during planning.
- Exact timing for closing a dispatch-result lifetime (before vs. after the underlying call completes) is deferred until we implement the result box.

## Stages

Each stage should:
- End with a passing test suite.
- Check off progress in this file as it goes.
- Update `CODE_OVERVIEW.md` + the relevant `LOCAL_CODE_OVERVIEW.md` before tests are written.
- Surface weird issues at the bottom of this doc.

### Stage A — Documentation pass

Goal: before writing any logic, lock in the shared mental model so subsequent stages have an explicit spec to check their implementation against.

- [x] Fill out `fxtr2/leases/LOCAL_CODE_OVERVIEW.md`:
  - File-by-file overview
  - Summary of the grant/token/lease triad and lease transfer convention
  - UnconditionalLocalLifetime vs AttachedLocalLifetime: when to use each, what the key token buys you, and the concrete flow of `exchange_forward` / `exchange_reverse`
  - Phantom lifetime branding: how to annotate callers/callees, when to introduce `FreshLifetime[Literal["…"]]`
  - `lease_cancel_scope` and recommended patterns
- [x] Fill out `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md`:
  - File-by-file overview
  - Summary of IRValue tree and ownership rules (copy on dispatch, copy on return, `wait` returns a copy)
  - How requests, futures, and pipelining compose; the role of `UnwrapForPipelining` / `WaitAndIgnoreFutureThen`
  - Lowering layer sketch: import/export tables, ID conventions, push/pull/resolve flow
  - Pointer to `leases/LOCAL_CODE_OVERVIEW.md` for lifetime primitives
- [x] Propagate clarifications above into docstrings:
  - `Grant` (describe the "identity" grant dataclass in `AttachedLocalLifetime.exchange_forward`)
  - `UnconditionalLocalLifetime.revoke` / `.root_lease` (synchronous break, auto-close on last lease)
  - `AttachedLocalLifetime.exchange_forward` / `.key_token` (structural gating)
  - `IRHandle.request` / `.dispatch` (lease-`take` discipline)
  - `IRFuture` / `IRFutureLocalImpl` / `LocalPendingResultBox` (path walk + shared box semantics)
  - `IRHandleLocalImplBase._task_group` (must outlive `_token`; one task group per token convention)
  - Also: added "callback fires immediately if already-closed/broken" notes on `LifetimeToken.register_end_callback` and `Lease.register_broken_callback`.
- [x] Update root `CODE_OVERVIEW.md` `### rpc/` section pointer and add a new `### leases/` section that points at `leases/LOCAL_CODE_OVERVIEW.md`.

### Stage B — `local_lease.py` core

Implement the local lifetime primitives end-to-end and test thoroughly. This is the foundation everything else rests on.

- Implementation sketch:
  - `LocalLifetimeToken`: stores `_ended`, `_end_callbacks`. `register_end_callback` fires immediately if already ended; returns deregister. `_mark_ended` (private) drains + fires callbacks exactly once.
  - `LocalGrant` (new concrete class): holds subject token + reason channel for broken-notifications. Registers leases; `revoke(reason)` synchronously walks leases, fires broken callbacks, marks token ended if it was the last thing gating it.
  - `LocalLifetimeLease`: holds `(grant, active_flag)`. `clone` asks grant for another lease (increment refcount). `take` returns clone+discard. `discard` is idempotent; drops refcount; when grant refcount hits zero on an unconditional grant, token closes. `ensure_active` raises `LeaseBrokenError` / `LeaseDiscardedError` appropriately.
  - `UnconditionalLocalLifetime`: owns one `LocalGrant`. Exposes `token`, `root_lease` (one-shot; subsequent accesses error). `revoke(reason)` breaks all outstanding leases synchronously and ends token.
  - `AttachedLocalLifetime`:
    - Internal state: `_output_token`, `_key_token`, `_key_grant`, `_req_token` (from arg), pluggable `_output_grants_by_input_grant` dict for dedup.
    - Subscribes to end-callback on `_req_token` (closes output) and on `_key_token` (closes output).
    - `exchange_forward(key_lease, lease)`: validates both leases active, dedups a grant per `(self, lease.grant)`, returns a fresh lease on the resulting grant. Takes ownership of both input leases per convention.
    - `exchange_reverse(out_lease)`: looks up the original-input grant via the output grant's identity, clones a lease on it.
    - `revoke(reason)`: synchronous break on both key and output.
  - Helper context manager `unconditional_lifetime_task_group(name)` (or similar) that opens a task group and an `UnconditionalLocalLifetime`, and on task-group exception revokes the lifetime.
- Tests (`tests/test_leases/test_local_lease.py`) — **85 tests passing**:
  - [x] Basic clone/discard/take behavior, including idempotency of `discard`.
  - [x] `revoke` breaks live leases synchronously; broken callbacks fire; subsequent `ensure_active` raises.
  - [x] Auto-close: token closes when all leases discarded; end-callbacks fire exactly once.
  - [x] `AttachedLocalLifetime`: exchange-forward requires both active; exchange-reverse returns an equivalent lease; grant equality mirrors input grant equality.
  - [x] Cascade correctness: requirement-end breaks output & closes output token; key-end does NOT break output leases (arms auto-close); revoke breaks both.
  - [x] `_via` lifecycle on `LocalAttachedTokenLease`: output leases keep requirement alive; clone/discard/break all handle `_via` correctly.
  - [x] Dead-on-arrival: already-ended requirement at construction closes output immediately.
  - [x] Structural checks: foreign leases rejected with clear `RuntimeError` messages.
  - [x] `lease_cancel_scope` integration: task cancels when lease breaks mid-context; both lease-break exception and any downstream exception are reported. *(Tests in `tests/test_leases/test_lease_util.py`.)*
  - [x] Task-group helper: exception inside task group revokes the lifetime and propagates cleanly. *(Implemented as `open_lifetime_task_group` in `lease_util.py` during Stage C; tests in `tests/test_leases/test_lease_util.py`.)*
- Doc updates:
  - [x] Flesh out `leases/LOCAL_CODE_OVERVIEW.md` with the concrete class list + cascade rules + internal-implementation notes.
- [x] Update `CODE_OVERVIEW.md`. *(Entries match current state; no structural refresh warranted.)*

**Design decisions that emerged during Stage B:**
- `LocalGrant` is a pure-identity object (subject reference + `__hash__ = object.__hash__`). No `issue_lease`/`_revoke` methods — those would imply authority.
- The owner protocol (`_LeaseOwner`) has just `_register_lease` + `_deregister_lease`. Leases self-register in `__init__`. Clone constructs a new lease directly (which self-registers). This gives `LocalAttachedTokenLease` a clean path for its `_via` without polluting the protocol with subclass-specific args.
- `UnconditionalLocalLifetime.root_lease` is a **stable reference** (same object on every access), not a one-shot factory. Auto-close when `_live_leases` becomes empty.
- `AttachedLocalLifetime` manages both its key-side and output-side break groups directly (no contained `UnconditionalLocalLifetime`). Disambiguates via `isinstance(lease.grant, _AttachedOutputGrant)` in `_classify_lease`.
- `_AttachedOutputGrant` is a `@dataclass(frozen=True)` with fields `(_attached, _input_grant)` — value equality gives us "same input grant → equal output grants" for free, no dedup dict needed.
- `LocalAttachedTokenLease` holds a `_via: Lease[I]` field on each output lease. Clone clones `_via`; discard/break discard `_via`. No central storage on `AttachedLocalLifetime`.
- Key-end does NOT break output leases — it only arms auto-close once output drains. This is the key subtlety of the cascade design.
- `revoke()` is idempotent (no RuntimeError on double call).
- Owner methods include structural checks (grant identity/classification) so foreign leases produce clear `RuntimeError`s.

### Stage B.5 — Pre-Stage-C cleanup pass

While reorienting at the top of Stage C, we folded a handful of small improvements into the lease system before starting on `leased_cap_common.py`. Landed as part of the same iteration:

- **`UnwrapForPipelining` shape fix.** Changed from a `@dataclass(frozen=True, init=False)` wrapping a `pair: tuple[...]` (un-constructable without `object.__setattr__`) to a regular frozen dataclass with two fields `future` and `lease`. `.pair` is retained as a `@property` returning `(self.future, self.lease)` for callers that want the tuple. Natural construction: `UnwrapForPipelining(future, lease)` or `UnwrapForPipelining(*handle.dispatch(...))`.
- **Owner-side weakref-based leak detection.** Added `weakref.finalize` on every registered lease; if a lease is garbage-collected without being explicitly discarded, the finalizer logs a warning (with allocation traceback when `LEASE_LEAK_TRACK_ALLOCATIONS` is True) and runs the same cleanup path as `_deregister_lease` would (releases any `_via` claim on the requirement side, closes the token if this was the last holder). The finalizer's strong reference to `_LeaseCleanupInfo` keeps `via` alive until the cleanup runs.
- **`_via` bookkeeping moved to the owner.** `LocalAttachedTokenLease` no longer overrides `discard`/`_mark_broken` — all `_via` discard logic lives on `AttachedLocalLifetime._clean_up_lease_data`, called from both the normal discard path and the leak finalizer.
- **Unified cleanup path.** `_clean_up_lease_data(lease_id, info)` is now the single implementation used by both `_deregister_lease` (after detaching the finalizer) and `_on_lease_leaked` (after the finalizer fires). O(1) lookup thanks to `lease_id` in the finalizer args.
- **Scoped-usage context managers on both lifetime classes.**
  - Sync `with lt.revoke_after_block() as lt:` — revokes on exit. For self-contained scopes.
  - Async `async with Lifetime(...) as lt:` via `anyio.AsyncContextManagerMixin` — discards the signalling lease on normal exit (root for unconditional, key for attached) and waits for the drain token to end; revokes on any `BaseException` (including cancellation during drain). For scopes that hand out leases and want holders to drain first.
  - Both async CMs share a `_drain_async_scope(drain_token, signal_lease, revoke)` module-level helper so the shape is written once.
- **Test hygiene.** Every test in `test_local_lease.py` that creates a lifetime now wraps it in `revoke_after_block()` (typically a multi-`with` for tests that also create an attached lifetime). Removed the `_new_requirement()` helper in favour of inline construction — easier to read test-by-test. Added `TestLeakWarnings` with two tests (unconditional + attached) that explicitly exercise the finalizer path via `caplog` + `gc.collect()`. 87 tests passing; full repo suite (2003 tests) still green.
- **Docstring notes** on `LifetimeToken.register_end_callback` and `Lease.register_broken_callback` explaining that callbacks fire immediately on already-ended/already-broken subjects (was already decided in Stage A, added here for symmetry with the new behaviour).

### Stage C — Local Cap-IR (`leased_cap_common.py`)

Implement `LocalPendingResultBox`, `IRFutureLocalImpl`, and a concrete local `IRHandle` flavor built on `IRHandleLocalImplBase`. This gives us a working local "loopback" capability system.

- Implementation sketch:
  - `LocalPendingResultBox[T, B]`:
    - State: `_token` (lifetime of the future), `_result: IRValue | _Pending | IRException`, `_waiters` list, `_pipelined_continuations` list.
    - `_set_result(v)` / `_set_exception(e)`: idempotent, drains waiters + continuations.
    - `await_resolution(path)` → deep-copied sub-value with leases cloned, or re-raise.
    - Owns all leases inside `_result`; on token close, discards them all.
  - `IRFutureLocalImpl`:
    - Pair of `(box, attr_path)`. Implements `token`, `wait`, `walk`, `pipeline_dispatch`.
    - `pipeline_dispatch`: takes the inner handle lease, allocates a fresh `UnconditionalLocalLifetime`, schedules a continuation on the box that runs `target_handle.request(cloned_inner_lease, request)` once resolved, routing the result through a new box.
  - `IRHandleLocalImplBase`:
    - Final implementation of `request`:
      1. `lease.take()`; ensure token active.
      2. Deep-copy the request (transfer leases; copy collections). Use a small `_copy_ir_value` helper.
      3. Call `_run_request_async(internal_lease, request)`.
      4. Deep-copy the result out (transfer leases); discard the internal copies of argument leases.
      5. Discard our owned copy of `lease`.
    - `dispatch`: as above, but schedules the whole thing onto `_task_group` and returns a `(IRFutureLocalImpl, Lease)` pair backed by a fresh `UnconditionalLocalLifetime`.
  - Small concrete `IRHandleFromCallable` (or similar) for tests: wraps a sync/async Python function as an IRHandle.
    - Figure out the concrete task-group pattern and possibly write helper at this point if not already written
- Tests (`tests/test_leases/test_leased_cap_local.py`):
  - Plain `request` with primitive args/results.
  - `dispatch` → `wait` round-trips; `wait` returns a deep copy (mutating the copy doesn't affect further walks).
  - `walk` on an unresolved future resolves when the box resolves.
  - `pipeline_dispatch` on a future that resolves to a handle.
  - Ownership: leases inside the argument get discarded after the call completes; leases in the result stay alive under the caller's returned lease and are freed on lease discard.
  - Exception propagation: `IRException` raised from the body surfaces on `request` / `wait`.
  - Token closed while a dispatch is pending: pending future cancels/discards cleanly.
- [x] Update `CODE_OVERVIEW.md` and both `LOCAL_CODE_OVERVIEW.md`.

### Stage D — Lowering protocol state machine

> **Status: complete.** The detailed sub-stage plan and per-landing notes live in
> `phase_d_plan.md`. The prose below is the original pre-implementation sketch —
> useful as historical context but predates several renames (e.g.
> `Imported…` → `Remote…` proxies) and design decisions (option B regranting +
> deferred token-discard pin-chain). Consult `phase_d_plan.md` +
> `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` for the as-built shape.

Implement `LeasedCapRPCConnection` and the concrete remote-proxy subclasses (tokens, grants, leases, handles, futures that live in the import table). At this stage the transport is a `MemoryObjectStream` pair in tests, not real sockets.

- Concrete subclasses (in `rpc/lowering.py`):
  - `ImportedLifetimeToken`, `ImportedGrant`, `ImportedLease`, `ImportedIRHandle`, `ImportedIRFuture`.
  - Each proxy holds a weakref back to the `LeasedCapRPCConnection` and a stable import/export ID.
  - Method calls on proxies enqueue the appropriate messages and register waiters against the connection's pending-result map.
- `LeasedCapRPCConnection` internals:
  - Tables: `exports: dict[int, ExportEntry]`, `imports: dict[int, ImportEntry]`; separate allocators for positive/negative IDs.
  - Background reader task: pull messages off the stream, dispatch to handlers (`_on_push`, `_on_pull`, `_on_resolve`, `_on_reject`, `_on_watch`, `_on_closed`, `_on_break`, `_on_discard`, `_on_unpull`, `_on_abort`).
  - Writer helper: serialize an IRValue → `Expression` tree, allocating export IDs for newly-seen objects, reusing existing IDs for previously-seen ones (bounce-back identity). May need to also handle call expressions / UnwrapForPipelining, or that could be a different helper. Can call this a "devaluator" to match Cap'n'Web naming
  - Reader-side evaluator: `Expression` → IRValue, mirroring. Handles `AwaitFutureExpr`/`WalkFutureExpr`/`HandleRequestExpr`.
  - Push pipeline: when an outbound call is made on an imported handle, we allocate three sequential positive IDs, construct a `PushMessage` containing a `HandleRequestExpr`, and return a future/lease pair whose proxies point at those import IDs.
  - Re-grant management: (option A) imported leases are backed by a one-shot `AttachedLocalLifetime` attached to the connection's session token, so connection close breaks everything imported. (option B, preferred but details TBD): `LeasedCapRPCConnection` directly makes and manages its own grants and leases
  - `as_client` / `as_server` convenience constructors.
  - Possible extra method on `LeasedCapRPCConnection` to directly `push` an IRValue to the remote side and receive an IRFuture
- Tests (`tests/test_rpc/test_lowering.py`):
  - Two in-process `LeasedCapRPCConnection` halves wired with an anyio memory object stream.
  - Main-interface round-trip call with primitive args/results.
  - Dispatch + pull + resolve.
  - Sending a lease across, using it on the other side, discarding: refcount + discard messages work correctly.
  - Pipelining: push a call on the result of an earlier push without awaiting.
  - `Break` on an exported grant propagates as a broken lease on the other side.
  - Connection close (abort) revokes all imported grants and closes all imported tokens.
  - Bounce-back identity: a handle/grant sent there-and-back compares identical to the original.
- [x] Update `CODE_OVERVIEW.md` and `rpc/LOCAL_CODE_OVERVIEW.md`. *(Stage D §2k doc pass.)*

### Stage E — JSON wire codec

Small module that turns `RPCMessage` objects into JSON-serializable values (and back), suitable for a (web)socket transport once a JSON framer is layered on top. Implemented in `fxtr2/rpc/json_serdes.py`. The on-the-wire shape is heavily inspired by [Cap'n'Web's JSON protocol][capnweb-protocol]: lists-as-escape, string first element as the tag, primitives literal, dicts literal.

[capnweb-protocol]: https://github.com/cloudflare/capnweb/blob/main/protocol.md

#### Design

- **`JSONTypes` type alias.** `type JSONTypes = None | bool | int | float | str | Sequence["JSONTypes"] | Mapping[str, "JSONTypes"]`. Output produces `list` / `dict` concretely; input accepts any Sequence / Mapping. Defined at the top of `json_serdes.py`.
- **Inline `StaticExpr` values.** `StaticExpr` values are serialized inline using the same escape mechanism as Cap'n'Web — no outer `["static", ...]` wrapper. This keeps primitive-heavy payloads cheap. Ambiguity is avoided by context: the three entry-points (`encode_message` / `encode_expression` / `encode_static`) and their matching decoders know what shape they're operating on.
- **Strict decoder.** Every decode entry-point validates structure (tag arity, field types, recursive children) and raises a well-typed error on any malformed input. Rejection and abort payloads additionally enforce the "static-only" constraint at decode time (defense-in-depth against malformed peers).
- **No version evolution.** The codec is pinned to version 1. The only forward-compat hook is the `HelloMessage` serialized form: `["fxtr_rpc_hello", 1]`. A decoder that sees any other payload under that tag raises `ProtocolVersionError`.

#### Wire forms

**Top-level messages** (each is a JSON list with a string tag as element 0):

| Message | Wire form |
|---|---|
| `HelloMessage` | `["fxtr_rpc_hello", 1]` |
| `PushMessage` | `["push", <expr>]` |
| `PullMessage` | `["pull", future_import_id]` if `grant_import_id == future_import_id + 1`, else `["pull", future_import_id, grant_import_id]` |
| `ResolveMessage` | `["resolve", future_export_id, <expr>]` |
| `RejectMessage` | `["reject", future_export_id, <static_expr>]` |
| `WatchMessage` | `["watch", token_import_id]` |
| `ClosedMessage` | `["closed", token_export_id]` |
| `BreakMessage` | `["break", grant_export_id, reason]` |
| `DiscardMessage` | `["discard", import_id, refcount]` |
| `UnpullMessage` | `["unpull", future_import_id, refcount]` |
| `AbortMessage` | `["abort", <static_expr>]` |

**Expressions.** Primitives (`None` / `bool` / `int` / `float` / `str`) decode to `StaticExpr(value)` and encode inline. Plain JSON `{...}` objects encode/decode as `DictExpr`. All other forms are list-tagged:

| Expression | Wire form |
|---|---|
| `StaticExpr(prim)` | `prim` (inline) |
| `StaticExpr(bytes)` | `["bytes", "<base64>"]` |
| `StaticExpr(list)` / `ListExpr` | `[[<elem>, ...]]` (array escape; children recursively encoded) |
| `StaticExpr(dict)` / `DictExpr` | `{key: <value>, ...}` (literal JSON object) |
| `StaticExpr(IRTagged)` / `IRTaggedExpr` | `["tag", <tag_inline>, <value>]` |
| `StaticExpr(IRException)` | `["error", <message>, <category_or_null>, <data_or_null>]` (always 4-element) |
| `ImportedLifetimeTokenExpr` | `["import_token", id]` |
| `ExportedLifetimeTokenExpr` | `["export_token", token_export_id]` |
| `ImportedLeaseExpr` | `["import_lease", grant_import_id]` |
| `ExportedLeaseExpr` | `["export_lease", grant_export_id, token_export_id]` |
| `ImportedIRHandleExpr` | `["import_handle", id]` |
| `ExportedIRHandleExpr` | `["export_handle", handle_export_id, token_export_id]` |
| `ImportedIRFutureExpr` | `["import_future", id]` |
| `ExportedIRFutureExpr` | `["export_future", future_export_id, token_export_id]` |
| `AwaitFutureExpr` | `["await", <future_expr>, <lease_expr>]` |
| `WalkFutureExpr` | `["walk", <future_expr>, [<path_step>, ...]]` |
| `HandleRequestExpr` | `["request", <handle>, <lease>, <request_form>, [<await_expr>, ...]]` |
| `HandleDispatchExpr` | `["dispatch", <handle>, <lease>, <request_form>, [<await_expr>, ...]]` |

Where `<request_form>` is either `["call", <method>, <arg_expr>]` or `["get", <attr>]` (with `method` / `attr` being a string, a number, or a JSON array of mixed strings/numbers for multi-step paths).

Notes:
- `wait_for` is always emitted as an array (possibly empty). No elision.
- `StaticExpr(IRException).data` — `None` serializes as JSON `null` in the 4th slot. Arbitrary non-null data is recursively encoded as an inline static (same scheme as the top-level `StaticExpr`).
- Symmetric decode: encountering e.g. `["tag", ...]` in expression context produces `IRTaggedExpr`; in static context it produces `IRTagged`.

#### Decode mode

`decode(value, *, mode)` where `mode ∈ {"message", "expression", "static"}`. Each mode has a strict parser. Errors raised as a single `JsonCodecError` hierarchy (e.g. `MalformedMessageError`, `ProtocolVersionError`, `NonStaticInStaticContextError`).

For expression mode, the decoder produces the "obvious" structural shape: primitives / `["bytes", ...]` / `["error", ...]` decode to `StaticExpr` (their wire form is inherently static), but container forms always decode to their structural expression (`ListExpr` / `DictExpr` / `IRTaggedExpr`) — i.e. `StaticExpr` is pushed in toward the leaves rather than pulled out to the root. The codec deliberately avoids making a "is this tree fully static?" decision; callers that want that answer can walk the tree themselves. Consequence: `StaticExpr([1, 2])` encodes to the same wire form as `ListExpr((StaticExpr(1), StaticExpr(2)))` and both decode to the latter. Still semantically equivalent under the Cap-IR evaluator.

#### Stream helper

```python
def wrap_json_stream(
    transport: anyio.abc.ObjectStream[JSONTypes],
) -> anyio.abc.ObjectStream[RPCMessage]: ...
```

Returns a wrapper whose `send` encodes to JSON and writes through, and whose `receive` reads and decodes as `mode="message"`. Delegates `send_eof` / `aclose` / `__aenter__` / `__aexit__` to the underlying transport. A framing-to-bytes layer is a separate concern that sits underneath this helper.

#### Module layout (`fxtr2/rpc/json_serdes.py`)

- `JSONTypes` type alias
- Exception hierarchy: `JsonCodecError` + subclasses
- `encode_message(msg: RPCMessage) -> JSONTypes` / `decode_message(value: JSONTypes) -> RPCMessage`
- `encode_expression(expr: Expression[Any]) -> JSONTypes` / `decode_expression(value: JSONTypes) -> Expression[Any]`
- `encode_static(value: IRStatic) -> JSONTypes` / `decode_static(value: JSONTypes) -> IRStatic`
- Unified `decode(value, *, mode)` / `encode(value)` entry-points that dispatch on mode
- `wrap_json_stream(transport) -> ObjectStream[RPCMessage]`

#### Implementation steps

- [x] **E.1** Draft module skeleton in `fxtr2/rpc/json_serdes.py` — `JSONTypes`, error types, function signatures.
- [x] **E.2** Implement `encode_static` / `decode_static` (bytes, IRTagged, IRException, recursive containers).
- [x] **E.3** Implement `encode_expression` / `decode_expression` (all expression variants + `Call` / `GetItem` sub-form).
- [x] **E.4** Implement `encode_message` / `decode_message` (all 11 message variants, with `PullMessage` grant-id elision).
- [x] **E.5** Implement `wrap_json_stream`.
- [x] **E.6** Unit tests in `tests/test_rpc/test_json_serdes.py` — 104 tests covering roundtrip (static / expression / message) and malformed-input coverage across all three decode modes.
- [x] **E.7** One integration test in `tests/test_rpc/test_json_lowering.py` — bidirectional main-interface round-trip through `wrap_json_stream` on each side.
- [x] **E.8** Update `CODE_OVERVIEW.md` and `rpc/LOCAL_CODE_OVERVIEW.md` — added `json_serdes.py` to the file table, described the inline-static convention, the mode-kwarg decode, and the stream helper.

#### Open questions noted for later

- Whether to add a tiny framer (newline-delimited / length-prefixed) alongside `wrap_json_stream` as a second helper `wrap_byte_stream_newline`. Deferred; the application can slot any JSON framer underneath.

## Implementation steps checklist

- [x] Stage A: Docs pass (LOCAL_CODE_OVERVIEW files + docstring clarifications + top-level CODE_OVERVIEW entry)
- [x] Stage B: `local_lease.py` implementation + tests
- [x] Stage C: Full local Cap-IR layer — `leased_cap_common.py` + `lease_util.open_lifetime_task_group`. `leases/LOCAL_CODE_OVERVIEW.md` refreshed. All 216 tests passing. See the detailed breakdown below.
- [x] Stage D: `rpc/lowering.py` connection + remote proxies + tests *(all 2a–2j.7 substages; see `phase_d_plan.md` for per-substage breakdown)*
- [x] Stage D: Update CODE_OVERVIEW.md *(§2k doc pass)*
- [x] Stage E: JSON wire codec — `fxtr2/rpc/json_serdes.py` + `test_json_serdes.py` + `test_json_lowering.py` (see Stage E §above)

### Stage C — final contents

Everything from Stage C lives in `fxtr2/leases/leased_cap_common.py` except the task-group helper (in `lease_util.py`):

- **`copy_ir_value(value, *, take)`** — deep-copies an `IRValue` / `IRCallArg`, cloning or taking each lease. Canonicalizes containers to plain `list`/`dict`.
- **`discard_leases_in(value)`** — walks the same shape (including `Request` variants) and discards every lease. Public; also used from the §2d phase-2 evaluator cleanup paths.
- **`_copy_request(req, *, take)`** (private) — the `Request` analog of `copy_ir_value`.
- **`LocalPendingResultBox[T, B]`** — state container for a pending result. Five states: `pending | resolved | rejected | forwarded | discarded`. Supports `set_result` / `set_exception` / **`resolve_from(future, lease)`** (promise-pipelining delegation) / `await_resolution(attr_path, lease)` (takes the lease, validates grant by equality, waits cancellably, descends + deep-copies the result OR delegates to a forwarded future with its own cancel scope). Token-end cleanup discards stored leases and zeroes the fields.
- **`IRFutureLocalImpl[T, B]`** — `(box, attr_path, task_group)` view. `token` / `wait` / `walk` (single-step or sequence) / `pipeline_dispatch` (uses `handle.dispatch` + `UnwrapForPipelining` to forward-resolve through the result box — no eager re-box).
- **`spawn_ir_future_in_task_group(*, name, work, task_group)`** — the canonical dispatcher helper. Creates a fresh `UnconditionalLocalLifetime` + box, schedules work in the task group, returns `(IRFutureLocalImpl, root_lease)`. Routes outcomes: `IRValue` → `set_result`; `UnwrapForPipelining` → `resolve_from`; `IRException` → `set_exception`; other `Exception` → wrapped; non-`Exception` `BaseException` → revoke + reraise.
- **`IRHandleLocalImplBase[B]`** — concrete default `token` / `request` / `dispatch`. Subclasses implement only `_run_request_async(lease, request: Call[UnwrappedCallArg] | GetItem) -> IRValue | UnwrapForPipelining`. `request = dispatch + wait + discard-result-lease` so abandoning the wait doesn't cancel the underlying work. `dispatch` handles `_unwrap_request_futures` (resolving any `UnwrapForPipelining` in `Call.arg` and awaiting+discarding `WaitAndIgnoreFutureThen` chains) before delegating to `_run_request_async`. Standard transfer convention: subclass consumes / forwards / embeds each input lease, exactly one.
- **`open_lifetime_task_group(name)`** (in `lease_util.py`) — async context manager yielding `(UnconditionalLocalLifetime, anyio.TaskGroup)`. Task group is the outer CM so it outlives the lifetime. Normal exit drains the lifetime; any exception revokes it.

### Resolved design open issue from earlier

> Exact policy for closing a dispatch-result lifetime when no leases remain but the call hasn't completed.

Landed: when the result lifetime's token ends (last lease discarded or revoke), the `LocalPendingResultBox` transitions to `"discarded"`, zeroes out stored value/exception/forward-lease, and wakes any waiters. The underlying task keeps running — its eventual `set_result` / `resolve_from` is a graceful no-op that discards whatever it was going to deliver. This matches the "fire-and-forget if you don't want the result" design.

## Notes / things to revisit

(none currently open; the dispatch-result-lifetime question was resolved in Stage C — see above.)
