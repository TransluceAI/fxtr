# Part 1 — Dynamic-dispatcher reload-on-break + manual reload

A self-contained change to the **dynamic dispatcher**
(`@fxtr-ui/react-dispatch`'s `createDynamicDispatcher`) — *not* the
generic `Dispatcher` surface. Connector-agnostic (it reads the
abstract `CallbackInboxStub.connectionState`), so it improves the
workstation too and needs no HTTP backend to develop/test. Lands first
so a WS dispatch service hot-reloads "just work" later (Part 5).

Design rationale: [`design/07_dynamic_dispatch.md`](design/07_dynamic_dispatch.md)
§"Part 1". Background: [`overview.md`](overview.md) §"Precursor work —
done" (the `connectionState` signal, part6 stage4post1).

## What's already there (read before changing)

| What | Where |
|---|---|
| Reload effect (subscribe + reception) | `fxtr-ui-react-dispatch/src/dynamic_dispatcher.ts:486-586` |
| Reception-path wipe — (a) flip every `staleAtom`, (b) userState batch wiping the 4 type caches + resetting entries to `init` | `dynamic_dispatcher.ts:545-581` |
| (Re-)subscribe branch: `inbox === null \|\| inboxState === "closed"` → `allocateCallback` + `allocateInvoke(handle, "subscribe_to", [dispatchServiceRef])` | `dynamic_dispatcher.ts:517-538` |
| The 4 session-backed caches (atoms) | `dynamic_dispatcher.ts:401-405` (shape `:151-205`) |
| `debugView()` builds `DynamicDispatcherDebugView` | `dynamic_dispatcher.ts:719-741` |
| The dynamic debug view (read-only today) | `fxtr-ui-react-dispatch/src/dynamic_debug_view.tsx` |
| `Dispatcher.debugView?()` (optional, per-dispatcher) | `dispatcher.ts:124-139` |
| Abstract inbox `connectionState`/`subscriptionBreakReason` | `fxtr-ui-core/src/stubs.ts` (part6 stage4post1) |

Key fact this hinges on: on a ws drop the inbox goes
`connectionState: "broken"` but `inbox.state` stays `"open"`, so the
existing reload effect (which only re-subscribes on `state ===
"closed"` and only wipes on delivered events) does **nothing** — events
just stop. That's the gap.

## Non-goals (explicit)

- **No new method on the generic `Dispatcher` interface.** Reload is a
  dynamic-dispatcher capability; the action stays an internal closure
  in `createDynamicDispatcher`.
- **No change to the workstation's `DispatcherDebugPanel`** — it just
  renders `dispatcher.debugView()`; the button lives inside the dynamic
  dispatcher's own debug view, so it appears automatically.

## Steps

### A. Factor out the reload action ✅
- [x] Extracted the reception-path wipe into a pure
  `wipeRootCaches(prev)` (empties the four type caches + resets every
  entry's `dispatcher_state` to `{phase:"init"}`; leaves `reload_inbox`
  and the in-memory compiled-module cache untouched) + a
  `flipAllStale(setter)` helper. Plus a closure-scoped
  `lastReloadConnState` edge tracker.
- [x] Reception path now calls `flipAllStale(set)` + `set(rootAtom,
  prev => ({ ...wipeRootCaches(prev), reload_inbox: sliced }))` —
  behavior unchanged.

### B. Reload-on-break (the new trigger) ✅
- [x] `reloadEffect` now also reads `inbox.connectionState`. On the
  edge into `"broken"`: `flipAllStale` + `set(rootAtom, (prev, ctx) =>
  ({ ...wipeRootCaches(prev), reload_subscribe_result:
  ctx.allocateInvoke(prev.reload_inbox.handle, "subscribe_to",
  [dispatchServiceRef]) }))` — reuses the existing handle (the
  `null`/`"closed"` allocate branch is untouched).
- [x] Once-per-broken-edge guard via `lastReloadConnState` (set to
  `"broken"` on fire; reset to the observed state otherwise); the
  `null`/`"closed"` branch resets it to `"idle"`. No spin on an
  unreachable source.

### C. Manual reload button (in the dynamic debug view) ✅
- [x] Added `onReloadEverything: () => void` to
  `DynamicDispatcherDebugViewProps` + a "Reload everything" button
  (`data-testid="reload-everything"`) in the header.
- [x] `debugView()` passes `onReloadEverything: manualReloadEverything`
  — an internal closure that runs `flipAllStale` + `wipeRootCaches` via
  `session.store.set`, and resubscribes on the existing handle only if
  the inbox is currently `broken`. Not on the `Dispatcher` interface.

- [x] Updated `frontend_lib/CODE_OVERVIEW.md` (react-dispatch
  dynamic-dispatcher section: `wipeRootCaches`/`flipAllStale` helpers,
  the `connectionState`-broken reload trigger, the debug-view "Reload
  everything" button, the new test coverage; noted no
  `Dispatcher`-surface change).

## Tests ✅
- [x] **Reload-on-break** (`dynamic_dispatcher.test.ts`): flipping the
  reload inbox's `connectionState` to `"broken"` wipes the caches and
  re-issues `subscribe_to` on the same handle (`reload_inbox` unchanged,
  `subscribeToCalls` grows 1→2). Needed two `_mocks.ts` additions —
  a controllable/stable `connectionState` on `TestCallbackInboxStub`
  (shared across `sliced()`), and `subscribeToCalls` recording in
  `makeMockSession`.
- [x] **Once-per-edge guard** (`dynamic_dispatcher.test.ts`): staying
  `broken` doesn't grow the count; `broken → connected → broken` fires
  again (1→2, …, →3).
- [x] **Manual button** (`debug_view.test.tsx`, RTL): seeding a
  non-empty `type_renderer_cache` then clicking the "Reload everything"
  button empties it (count `(1)` → `(0)`).
- [x] **Regression**: full react-dispatch unit suite green (78 tests),
  including the existing reception-path harness (`dynamic_reload.test.ts`)
  and the closed-inbox reattach test.
- [ ] (Optional, end-to-end against the existing workstation backend —
  not done; unit coverage above is sufficient.)

## Notes / watch-fors
- The effect reads `inbox.connectionState` from `session.store`;
  confirm the existing `reloadEffectUnsub` teardown (`:586`, `:710`)
  covers the added subscription (same effect, so it does).
- On a **cold** reload the inbox starts `idle` (not `broken`), so
  reload-on-break won't fire on first load — the existing first-connect
  subscribe branch (+ the server's on-subscribe reload, Part 5) covers
  that. Intended (design/07).
- Keep the `null`/`"closed"` (re-)allocate branch as-is; the new branch
  is specifically for `state === "open"` + `connectionState ===
  "broken"`, which is the ws-drop / grant-break case.

- [x] CODE_OVERVIEW.md updated (see step above) — Part 1 complete.
