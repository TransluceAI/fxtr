# Stage 3 (was stage 5) — Service invokes

Adds the workstation-only invoke pipeline. `ServiceRefStub` is
already a trackable opaque value as of stage 1; this stage plumbs
`InvokeOp` so consumers can actually call methods on a service ref
and pull results back through the slot machinery.

The slot-lifecycle machinery built in stage 2 (allocator + spec
map + flush walk + fill events + preserve-set re-derive +
broken-stub handling) is shaped for additive op kinds. Stage 3 is
mostly a matter of adding an `"invoke"` arm to `OpSpec`, eagerly
encoding args at allocate time, broadening `handleFillEvent` and
the snapshot rebuild to include the invoke event kinds, and
extending the demo service with a method we can call.

## Scope

- Backend already supports kwargs (commit
  `330585ff4f0e fxtr workstation kwargs plumbing`):
  `InvokeOp.kwargs: dict[str, Any]` on the wire, the invoke worker
  calls `method(*args, **kwargs)`. Stage 3 brings the TS protocol
  type and `allocateInvoke` into line.
- `OpResultStub` and `CallbackInboxStub` in invoke args (positional
  or kwarg, at any depth) are rejected at `allocateInvoke` time
  with a clear error. (Op-results: user must read the value out
  first. CallbackInboxStub: stage 6 will wire send-side encoding;
  for now any callback ref in args is an error.)
- No auto-tracking of refs that appear only in args. The
  workstation requires arg refs to exist in its registries
  *somewhere*; it doesn't require the calling session to pin them.
  Auto-tracking would be surprising and complicate teardown. Users
  who want session pinning write the ref into their own state.
- `allocateInvoke` always mints a fresh slot — no dedup. Each
  invoke is a separate nondeterministic call.
- Args/kwargs are **encoded at allocate-time**, frozen into the
  `InvokeOpSpec`. This is simpler than capturing structured values
  and re-encoding at flush, and avoids re-walking on every flush.
  Cross-session-stub substitution doesn't apply to args (we'd reject
  `OpResultStub` anyway; entity/service refs are session-agnostic
  short-ids on the wire).
- Method-dot syntax (`svc.method("foo").call(...)` — OQ-β) and
  typed service-protocol declarations (OQ-α) are explicitly out of
  scope for stage 3. Stage 3's `allocateInvoke` takes `unknown[]`-ish
  args; consumers TS-check manually for now.

## What gets built

- **Protocol type:** `InvokeOp` in `protocol.ts` grows
  `kwargs: { readonly [key: string]: WireValue }`.
- **Codec helper:** `escapeInvokeArgs(args, kwargs, ctx)` in
  `escape.ts`, walking each arg / kwarg value through `escapeWire`
  and producing the wire form `(args: WireValue[], kwargs:
  Record<string, WireValue>)`. Throws if any leaf is an
  `OpResultStub` or `CallbackInboxStub`.
- **`OpSpec` extension:**
  ```ts
  export type OpSpec = LoadOpSpec | InvokeOpSpec;
  export interface InvokeOpSpec {
    readonly kind: "invoke";
    readonly serviceShortId: string;
    readonly methodName: string;
    readonly args: readonly WireValue[];
    readonly kwargs: { readonly [key: string]: WireValue };
  }
  ```
- **`Session.allocateInvoke(svc, method, args, kwargs?)`:**
  - `assertWritable`; `lifecycle === "connected"`.
  - Pre-encode args/kwargs via `escapeInvokeArgs` (rejects
    `OpResultStub` / `CallbackInboxStub`).
  - Mint slot via `++localSlotCounter`; intern fresh
    `WorkstationOpResultStub<R>` in `opResultStubs`; record
    `InvokeOpSpec` in `pendingSlotSpecs`.
  - Returns the stub. The user is expected to write it into state
    in the same tick (stage-2 contract).
- **Reducer (`computeOps`):** adds an `invoke` branch to the
  `new-with-spec` slot loop:
  ```ts
  if (spec.kind === "invoke") {
    ops.push({
      kind: "invoke",
      slot,
      service_short_id: spec.serviceShortId,
      method_name: spec.methodName,
      args: spec.args,
      kwargs: spec.kwargs,
    });
  }
  ```
  Everything else (Track/Untrack, preserve diff, broken-stub
  encoding) already works for invoke slots — the stub identity
  and slot mechanics are identical to loads.
- **Event handling:** `handleFillEvent` parameter type widens to
  `LoadResultEvent | LoadErrorEvent | InvokeResultEvent |
  InvokeErrorEvent`; `applyEvent` routes the two invoke kinds the
  same way as load kinds. Payload decode goes through the same
  `decodeLoadResult` path (rename to `decodeResultPayload` to drop
  the load-specific name).
- **Snapshot rebuild:** `rebuildSlotFillsFromSnapshot` includes
  `invoke_result` / `invoke_error` in its event-kind filter. The
  fill-slot-counter tripwire applies the same way.
- **Demo service:** `_demo_workstation.py`'s `_TestServiceProto`
  grows an `echo(value)` method (and exercises kwargs via a small
  variant or via the integration test passing kwargs through). The
  Python method just returns its argument so the integration test
  can verify arbitrary structured args round-trip.

## What doesn't need changing

- Wire codec (other than the new helper) — `escapeWire` /
  `unescapeWire` already handle every value type that can appear
  in args. The codec doesn't know about args specifically; the
  helper just calls it.
- `state.ts` walks — invoke args aren't in `userState`, so
  `collectStubsInState` doesn't see them. The slot stub written
  into state participates in the existing slot-walk just like a
  load slot.
- Preserve-events machinery — invoke fills land in `slotFills`
  with their own `seq`, and the existing
  `committedPreservedSeqs` re-derive picks them up.
- Stub class (`WorkstationOpResultStub`) — already general over
  "load or invoke or future kinds." The `result` atom doesn't
  care which spec produced the slot.

## Implementation chunks

Each chunk is one reviewable PR. The order follows review flow:
codec/types first, then allocator + reducer wiring, then event
handling, then live integration test.

### Chunk 1 — Protocol type + invoke-arg codec helper ✅

- [x] Extend `InvokeOp` in `protocol.ts`:
  add `kwargs: { readonly [key: string]: WireValue }`. Doc comment
  notes the backend already supports this (commit
  `330585ff4f0e fxtr workstation kwargs plumbing`).
- [x] Add `escapeInvokeArgs(args, kwargs, ctx)` to `escape.ts`:
  - Signature: `(args: readonly StructuredValue[], kwargs:
    { readonly [k: string]: StructuredValue } | undefined,
    opts: EscapeOptions) => { args: WireValue[]; kwargs: {
    [k: string]: WireValue } }`.
  - Pre-walks the input via `assertInvokeArgValueAllowed` (a
    private helper) which throws `WireEscapeError` on any
    `OpResultStub` / `CallbackInboxStub` it encounters, with a
    path string (`args[i].deep.key[j]`) pointing at the
    offending leaf for ergonomic errors.
  - Then runs each value through `escapeWire(v, opts)`. Reuses
    the same opts (encoder context) for every value.
  - Empty / `undefined` kwargs → empty object on the wire
    (matches Python `InvokeOp.kwargs` default).
- [x] 16 new tests in `escape.test.ts` covering: empty / primitive
  / container args, refs nested through dicts/lists, bytes, kwargs
  alongside args, omitted kwargs → empty dict, rejection of
  `OpResultStub` at top-level / nested in list / nested in dict /
  in kwargs / nested in kwargs, rejection of `CallbackInboxStub`
  in same positions, and that the error message carries the path.
- [x] `pnpm check` / `pnpm lint` / `pnpm test` all green (660
  tests total; 56 in `escape.test.ts`, 16 new).
- [x] Update `frontend_lib/CODE_OVERVIEW.md` with the new helper
  and the kwargs field on `InvokeOp`.

### Chunk 2 — `OpSpec` extension + `allocateInvoke` + reducer emit ✅

- [x] Extend `OpSpec` in `session.ts` with `InvokeOpSpec`
  (`{ kind: "invoke"; serviceShortId; methodName; args:
  readonly WireValue[]; kwargs: { [k]: WireValue } }`).
- [x] Abstract `Session.allocateInvoke` in `@fxtr-ui/core` now
  declares the kwargs param.
- [x] Implement `allocateInvoke<R>(svc, method, args, kwargs?)`
  in `WorkstationSession`:
  - `assertWritable("allocateInvoke")` + `lifecycle ===
    "connected"`.
  - Encoder opts: `encodeEntity/Service = s => s.shortId`,
    `encodeOpResult` throws (defensive — pre-walk rejects
    `OpResultStub` first).
  - Run `escapeInvokeArgs(args, kwargs, opts)` (rejects bad
    stub kinds; throws *before* the counter advances so a bad
    call doesn't burn a slot).
  - `++localSlotCounter`; mint + intern the stub; record the
    spec in `pendingSlotSpecs`.
  - Returns the stub. No-dedup by design.
- [x] Extend `computeOps`'s `new-with-spec` loop with an
  `invoke` branch emitting `InvokeOp` from the captured spec.
- [x] 13 new tests in `invoke_reducer.test.ts` (new file —
  the slot_reducer file is already large):
  - allocate + write → `InvokeOp` with expected
    slot/service/method/args/kwargs.
  - Omitted kwargs → `{}` on the wire.
  - Allocate-without-write → no op shipped.
  - Two `allocateInvoke` calls mint distinct slots (no dedup).
  - Entity/service refs in args encoded as tagged forms.
  - Dicts/lists in args go through list-escape.
  - kwargs values walk through `escapeWire`.
  - `OpResultStub` rejected at top-level / nested in dict /
    nested in list / in kwargs (allocate time; no slot
    burned).
  - `CallbackInboxStub` rejected in args + kwargs.
  - Rejection doesn't burn a slot (next allocate uses
    counter=1).
  - `lifecycle !== "connected"` refuses allocate.
  - `detaching` / `closed` refuse allocate.
  - Track ops fire for refs in *state* alongside the result
    stub but NOT for refs that appear only in args.
- [x] Updated the pre-existing session.test.ts test that asserted
  `allocateInvoke` throws `NotImplementedError` — that suite
  now only covers `allocateCallback`.
- [x] `pnpm check` / `pnpm lint` / `pnpm test` all green
  (674 tests total; 13 new).
- [x] Update `CODE_OVERVIEW.md`.

### Chunk 3 — Invoke fill event handling + snapshot rebuild ✅

- [x] Widened `handleFillEvent`'s parameter type to
  `LoadResultEvent | LoadErrorEvent | InvokeResultEvent |
  InvokeErrorEvent`.
- [x] Renamed `decodeLoadResult` → `decodeResultPayload`;
  body unchanged (result events for both op kinds share the
  same shape).
- [x] Routed `invoke_result` / `invoke_error` through
  `handleFillEvent` in `applyEvent`.
- [x] Extended `rebuildSlotFillsFromSnapshot`'s event-kind
  filter to include the two invoke kinds.
- [x] 6 new tests in `invoke_reducer.test.ts`:
  - `InvokeResultEvent` flips `stub.result` to filled with
    the decoded value (and `done` / `committed` flip).
  - `InvokeErrorEvent` flips `stub.result` to errored with
    the message.
  - Result payloads with embedded entity refs decode
    through `unescapeWire` and intern correctly.
  - Malformed result payload synthesizes an errored fill;
    decoder doesn't throw; `console.error` fires.
  - Slot-counter tripwire fires on out-of-range invoke fill
    slot (caught + logged by connection layer).
  - Snapshot with preserved `invoke_result` rebuilds
    `slotFills` so the attached stub reports filled with
    the preserved payload.
  - Snapshot with preserved `invoke_error` same path for
    errored.
- [x] `pnpm check` / `pnpm lint` / `pnpm test` all green
  (681 tests total; 6 new).
- [x] Update `CODE_OVERVIEW.md`.

### Chunk 4 — Demo service `echo` + live integration test ✅

- [x] Extended `_demo_workstation.py`'s `_TestServiceProto`
  with three methods:
  - `echo(value)` — returns its argument unchanged.
  - `combine(value, *, name="default")` — positional `value`
    plus keyword-only `name`; returns `{"value": ..., "name":
    ...}` so the JS test can introspect both.
  - `fail(message)` — raises `RuntimeError(message)` to drive
    the `invoke_error` path.
- [x] Added `resolveTestServiceShortId` helper to
  `integration.test.ts` analogous to the existing
  `resolveTestEntityShortId`.
- [x] 5 new live integration scenarios:
  - **Happy invoke** (`echo(42)`): stub transitions pending →
    filled with `42`; `committed` and `done` flip.
  - **Refs in args** (`echo({note: entityRef, items:
    [serviceRef]})`): refs round-trip through the
    workstation and intern back to the same instances we
    started with.
  - **kwargs** (`combine("v", name="alice")`): Python side
    receives both; result reflects them.
  - **Default kwargs** (`combine("v")`): Python-side default
    `"default"` fires.
  - **Error** (`fail("deliberate test failure")`): stub
    transitions to errored; message contains the passed-in
    string; slot still `committed` (the `InvokeOp` itself
    succeeded — only its delivered result is errored).
- [x] Fixed a pre-existing bug in `_integration_harness.ts`'s
  SIGKILL fallback: it gated on `child.killed`, which Node
  sets to `true` immediately after any signal is sent
  (including SIGTERM), so the fallback was a no-op. Replaced
  with a closure-tracked `exited` flag. The new invoke tests
  surfaced this because the workstation server doesn't
  always exit cleanly on SIGTERM after invokes have run
  (see "Things to flag" below).
- [x] Bumped `afterAll` hook timeout to 30s for headroom on
  the SIGTERM grace period.
- [x] `pnpm check` / `pnpm lint` / `pnpm test` all green
  (686 tests total; 5 new live tests).
- [x] Update `frontend_lib/CODE_OVERVIEW.md`.

## Things to flag

- **SIGTERM-after-invoke wedge: diagnosed and fixed.** Original
  symptom: the demo workstation didn't exit on SIGTERM within 5s
  after the refs-in-args invoke test ran; the harness's SIGKILL
  fallback was firing. Diagnosis: the test used `detach()`
  (server-side session lives on) on a session whose state still
  held a preserved invoke-result event. The workstation's invoke
  result delivery auto-pins refs surfaced in the result onto the
  calling session; those refs only release when the preserved
  event is unpreserved (or the session is destroyed). With the
  session alive at SIGTERM, the workstation was correctly
  draining held resources during shutdown — not deadlocking.

- **Workstation `CloseOp` bundling rule relaxed in passing.**
  Initial fix: write `null` to `userState`, then `detach()`.
  Better fix landed during the investigation:
  - **Workstation** (`fxtr_workstation/state.py`): `CloseOp`
    used to require "must be the only op in the mutation".
    Relaxed to "must be the *last* op" — ops before it run
    normally (the docstring already said this; only the
    validation was stricter than intended). This lets a
    single mutation release session state and close in one
    atomic step.
  - Two existing tests adjusted
    (`test_close_must_be_only_op` → `test_close_must_be_last_op`
    in `test_sessions_3b2.py`;
    `test_close_must_be_only_op` →
    `test_load_before_close_commits_but_result_lands_on_gone_session`
    in `test_sessions_3c2b.py`), two new positive tests added
    (`test_close_with_cleanup_ops_before_it_commits` +
    `test_close_followed_by_other_op_rejects`).
  - Bundling work-launching ops (load / invoke / metadata /
    create_callback) with `CloseOp` is now permitted but
    pointless — the spawned workers' result deliveries
    short-circuit on session-gone. It's a footgun, not a
    correctness hole; documented in the inline comment.
  - **Frontend**: with the relaxed rule, `session.terminate()`
    Just Works for sessions holding active state. The
    refs-in-args integration test now uses `terminate()`;
    suite shuts down cleanly in <1s.

- **Pre-existing harness bug fixed in passing.**
  `_integration_harness.ts`'s SIGKILL fallback gated on
  `child.killed`, which Node sets to `true` after *any* signal
  (including SIGTERM), so the fallback was always a no-op.
  Replaced with a closure-tracked `exited` flag. Pre-stage-3
  tests happened to always exit on SIGTERM (simpler held
  state), hiding the bug. The fix stays in place — the fallback
  is genuinely meant to be a last resort.

## Design notes to consult

- `design_notes/03_stub_types.md` — `ServiceRefStub` /
  `OpResultStub` semantics and OQ-α / OQ-β notes.
- `design_notes/04_session_state_layout.md` — preserve-events flow
  applies the same way for invoke fills.
- `stage2_entity_loading.md` — slot lifecycle / broken-stub /
  reducer contract that we're additively extending.

## Open / deferred questions

- **Service protocol declarations (OQ-α) — partial.** Stage 3
  ships **Option L**: type-only protocol declarations via
  `Method<Args, Kwargs, Returns>` + protocol-typed
  `ServiceRefStub<P>`. `Session.allocateInvoke`'s signature
  uses `MethodNames` / `MethodArgs` / `MethodKwargs` /
  `MethodReturns` helpers from `@fxtr-ui/core` to infer the
  method/args/kwargs/return types when `P` carries `Method`
  declarations, and falls back to loose typing when `P =
  unknown`. No runtime metadata yet — that's deferred to a
  future `serviceProto({...})` builder (Option H) when the
  dispatch system (stage 7) needs it. The builder can emit
  values whose static type is `Method<…>`-shaped, so the
  consumer-facing `allocateInvoke` signature wouldn't need
  to change again.
- **Method-dot syntax (OQ-β).** Ergonomics layer to convert
  `svc.method("foo").call(...)` into an `allocateInvoke` — also
  deferred until the basic pipeline is in place.
- **Callback refs in invoke args.** `CallbackInboxStub` in args
  encodes to `["callback", slot]` on the wire (existing tag), but
  stage 3 rejects these — wiring them up is stage 6's job.
- **Service-protocol arg validation against an actual schema.**
  We don't validate at allocate time that args match the method's
  declared shape (since we don't have declarations). The
  workstation server validates that refs resolve; mismatched
  shapes surface as `invoke_error` from Python.
