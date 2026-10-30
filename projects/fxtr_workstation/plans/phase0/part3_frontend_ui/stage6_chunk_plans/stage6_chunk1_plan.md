# Stage 6, Chunk 1 — Types, codec, stubs, allocate scaffolding

Foundation chunk for stage 6. Adds the type split, the new
abstract stub surface (`CallbackInboxStub` extended,
`CallbackHandleStub` new), the wire codec extensions, the
concrete workstation stubs, and a working `allocateCallback`.

After this lands: `allocateCallback` returns a fresh inbox stub
and writes a `pendingSlotSpec` of kind `"callback"`, but no
`CreateCallbackOp` is emitted yet — the flush walk doesn't
traverse `CallbackInboxStub` leaves until chunk 2 wires it.

Read [`../stage6_callback_inboxes.md`](../stage6_callback_inboxes.md)
first for the locked design decisions.

## Scope

In:

- Type split in `@fxtr-ui/core`: rename `StructuredValue` →
  `StateValue`; add `ResultValue`, `InvokeArgValue`. Retighten
  `Method<>`, `Session.userState`,
  `FxtrSessionUpdateCtx.allocateInvoke`.
- Sweep `StructuredValue` callers across
  `connector-workstation`, `fxtr-ui-react`, `fxtr-ui-demo`, and
  tests.
- Abstract surface in `@fxtr-ui/core/stubs.ts`:
  - `CallbackInboxStub<R>` grows `events`, `state`,
    `committed`, `handle`, `sliced(n)`, `cleared()`.
  - New `CallbackHandleStub<R>` (opaque; brand +
    `debugSummary`).
- Wire codec in `connector-workstation/escape.ts`:
  - New `TAG_INBOX = "inbox"` reserved tag.
  - `EscapeOptions`: add `encodeInbox?`,
    `encodeCallbackHandle?`.
  - `UnescapeOptions`: add `lookupInbox?`; retype
    `lookupCallback` return as `CallbackHandleStub<unknown>`.
  - `escapeWire` / `unescapeWire` route the new stub kinds
    through the new opts.
  - `escapeInvokeArgs.assertInvokeArgValueAllowed` allows
    `CallbackHandleStub`; still rejects `OpResultStub` and
    `CallbackInboxStub` at runtime.
  - `unescapeWire` overloaded so absence of
    `lookupOpResult`/`lookupInbox` in opts narrows the return
    to `ResultValue`.
- Concrete `WorkstationCallbackInboxStub<R>` and
  `WorkstationCallbackHandleStub<R>` in
  `connector-workstation/stubs.ts`. Both carry a session backref;
  per-class dead-sentinels at `slot=0`.
- New `WorkstationCallbackInboxStubSession` interface paralleling
  the op-result one.
- Session-level placeholder atoms initialized empty (consumed in
  chunks 2-3): `inboxLogsAtom`, `committedInboxSlotSetAtom`.
- `OpSpec` extension: `CallbackOpSpec = { kind: "callback" }`.
- `allocateCallback<R>()` impl: mint slot, construct
  `WorkstationCallbackInboxStub` at offset=0, register
  `pendingSlotSpec`, return stub.
- Wire encode/decode opt callbacks plumbed through
  `WorkstationSession`. `encodeCallbackHandle` does the
  cross-session check at encode time.
- Canonical invariants docstring added to the
  `WorkstationSession` class JSDoc (load + invoke + the inbox
  section's final wording).
- Codec unit tests for the `inbox` tag (encode/decode
  roundtrip), the retyped `callback` tag, invoke-arg encoding
  with handle stubs, and the `unescapeWire` overload's return
  narrowing.

Out (deferred):

- Flush walk traversal of inbox leaves;
  `CreateCallbackOp` / `CloseCallbackOp` emission. → Chunk 2.
- `CallbackEvent` reception and `inboxLogs` population;
  preserve / unpreserve for callback events; snapshot rebuild.
  → Chunk 3.
- Integration tests against live workstation. → Chunk 4.

## File touches

- `frontend_lib/packages/fxtr-ui-core/src/types.ts`: define
  `ResultValue`, `StateValue` (renamed), `InvokeArgValue`;
  retype `Method<>`.
- `frontend_lib/packages/fxtr-ui-core/src/stubs.ts`: extend
  `CallbackInboxStub`; add `CallbackHandleStub`; new
  `CALLBACK_HANDLE_BRAND` symbol.
- `frontend_lib/packages/fxtr-ui-core/src/session.ts`: retype
  `UserState`, `FxtrSessionAtom`, `FxtrSessionUpdateCtx`.
- `frontend_lib/packages/fxtr-ui-core/src/index.ts`: re-export
  the new types.
- `frontend_lib/packages/fxtr-ui-connector-workstation/src/escape.ts`:
  inbox tag; new opts; overloaded `unescapeWire`; relax
  `assertInvokeArgValueAllowed` for handles.
- `frontend_lib/packages/fxtr-ui-connector-workstation/src/stubs.ts`:
  add concrete inbox/handle stubs + dead sentinels +
  `WorkstationCallbackInboxStubSession`.
- `frontend_lib/packages/fxtr-ui-connector-workstation/src/session.ts`:
  inbox-related private + public atoms; implement
  `WorkstationCallbackInboxStubSession`; extend `OpSpec`;
  implement `allocateCallback`; plumb encode/decode opts; add
  invariants docstring.
- `frontend_lib/packages/fxtr-ui-connector-workstation/src/state.ts`:
  rename `StructuredValue` → `StateValue`. (Walk-collect /
  leafEqual updates for inbox slots wait for chunk 2.)
- `frontend_lib/packages/fxtr-ui-connector-workstation/src/index.ts`:
  re-export new stub types.
- `frontend_lib/packages/fxtr-ui-connector-workstation/tests/escape.test.ts`:
  new tests (~10) for inbox + handle + overload narrowing.
- Other consumer packages (`fxtr-ui-react`, `fxtr-ui-demo`,
  remaining connector tests): rename sweep only.

## Implementation steps

Order them so each step compiles and (where applicable) keeps
the existing test suite green before moving on.

1. [x] Add `ResultValue`, `StateValue` (renamed), `InvokeArgValue`
   to `@fxtr-ui/core/types.ts`. Retype `Method<>` Args / Kwargs
   / Returns constraints. Update fallback types
   (`MethodArgs` / `MethodKwargs` / `MethodReturns`).
2. [x] Update `@fxtr-ui/core/session.ts` and `stubs.ts` for the
   rename. Add the new public surface to `CallbackInboxStub<R>`
   (abstract methods/getters) and the new `CallbackHandleStub<R>`
   class.
3. [x] Update `@fxtr-ui/core/index.ts` re-exports.
4. [x] Rename sweep across `connector-workstation` (`state.ts`,
   `stubs.ts`, `escape.ts`, `session.ts`, tests).
5. [x] Rename sweep across `fxtr-ui-react`, `fxtr-ui-demo`.
6. [x] Wire codec extension in `escape.ts`: `TAG_INBOX`, new
   opts (`encodeInbox` / `encodeCallbackHandle` / `lookupInbox`),
   overloaded `unescapeWire` (`StateUnescapeOptions` vs
   `ResultUnescapeOptions`), relaxed
   `assertInvokeArgValueAllowed` (allows `CallbackHandleStub`).
   `escapeWire` and `unescapeWire` handle inbox + handle stubs.
7. [x] Concrete stubs + per-class dead sentinels in
   `connector-workstation/stubs.ts`, plus the new
   `WorkstationCallbackInboxStubSession` interface (with
   `currentInboxLog` for `cleared()`).
8. [x] Session-level atoms + `OpSpec` extension + `allocateCallback`
   impl + plumbing of encode/decode opt callbacks in
   `connector-workstation/session.ts`.
9. [x] Canonical invariants docstring on `WorkstationSession`.
10. [x] Tests in `tests/escape.test.ts` — inbox tag encode/decode,
    handle in invoke args, retyped `callback` decode (typed
    `TestCallbackHandleStub`), and the rejection-when-not-supplied
    paths.
11. [x] `pnpm check`, `pnpm test`, `pnpm lint` from `v2/ts`; all
    green (773 tests pass).
12. [x] Update `CODE_OVERVIEW.md` with the new types + inbox
    machinery + forward reference to chunk 2.

## Post-implementation cleanup

After the initial chunk-1 commit, a follow-up pass simplified a
few of the choices above:

- **One merged `WorkstationStubSession` interface.** The original
  plan had a per-stub-class session interface
  (`WorkstationOpResultStubSession`,
  `WorkstationCallbackInboxStubSession`); the cleanup folded
  these into a single `WorkstationStubSession` that exposes all
  stub-readable atoms. Less duplication; one interface to import.
- **`lookupInbox` / `currentInboxLog` removed from the session
  class.** Both were trivial helpers (one mints a fresh stub;
  the other read the inbox-log map). The codec call sites inline
  `new WorkstationCallbackInboxStub(slot, offset, this)` directly.
- **`lookupCallback` decode path removed entirely.** The
  `["callback", slot]` tag is send-only — the server never emits
  it to clients — so the decoder always throws on encountering
  it. No `lookupCallback` opt in `UnescapeOptions`.
- **`FillRecord.value` retyped to `ResultValue`.**
  `decodeResultPayload` uses the result-mode unescape (no
  `lookupOpResult` / `lookupInbox`), reflecting the truth that
  payloads from Python services can't contain client-only stubs.
- **`cleared()` removed entirely.** Was the only place in the
  codebase doing an imperative `store.get` on a session atom,
  and bypassed jotai's dependency tracking — would silently miss
  reactivity if called inside a derived atom's getter. Users now
  compose `inbox.sliced(events.length)` (with `events` from a
  reactive read) for the common case, or a custom jotai write
  atom for action-time snapshots. The `store` field on
  `WorkstationStubSession` was added solely for `cleared()` and
  was removed alongside it. The "snapshot point" (render time vs.
  action time) is now explicit in user code.

## What chunk 2 does with this

- `flushWalk` traverses `CallbackInboxStub` leaves, collects
  per-slot `min(offset)`, substitutes cross-session inboxes
  with the per-session dead-sentinel.
- `computeOps` emits `CreateCallbackOp` for `new-with-spec`
  callback slots, `CloseCallbackOp` for slots present in
  committed but absent in local.
- `state.ts`: `walkCollect` surfaces inbox slots + min-offsets;
  `leafEqual` compares inbox stubs by `(slot, offset)` and
  handles by `slot`; `isPlainContainer` recognizes
  `CallbackHandleStub`.
