# Part 7a — open questions

**Status: all round-2 + mechanical questions resolved (2026-06-13).**
Resolutions are folded into [`overview.md`](overview.md) and the
[`design/`](design/) docs; this file is now the decision record. Any
genuinely new questions go under "Newly opened" at the bottom.

## Resolved — round 2

A. **ws connect-on-demand, no background reconnect loop** →
   **on-demand** (design/04). Connections open lazily when an invoke
   needs them; invokes during a failed attempt error; nothing
   reconnects until next use; no reconnect timers.

B. **One live session instance per sessionId per connector** →
   **intern** (design/04). Documented as an explicit deviation from
   the workstation connector (which returns a fresh independent
   instance per `attachSession` call, because the server arbitrates).

C. **Record shapes / portable connector interface** → **dissolved by
   the PanelViewer refactor** (Part 4). Roots + debug panels become
   connector-specific injected registrations, each using its own
   connector's lists + record types; the portable interface drops
   `listEntities` / `listServices`, so `UiEntityRecord` /
   `UiServiceRecord` are no longer needed (design/06).

D. **Debug panels** → **injected panel registrations** (option b),
   generalized into the full PanelViewer decomposition (design/06,
   Part 4).

E. **Best-effort `unsubscribe` on inbox close** → **yes, mirror the
   workstation parker's teardown** (design/05). The session remembers
   handle↔inbox associations and sends a best-effort `unsubscribe`
   on a still-live transient handle when the inbox closes.

F. **Inbox-handle invokes other than `subscribe_to`** → **implement
   `notify` locally (append to own log), error on anything else**
   (design/05). `subscribe_to` now also carries `method=` (see N2).

G. **Persist-failure handling** → **loud `console.error`, in-memory
   state stays authoritative, keep going** (design/04).

H. **Service-metadata type id** → **mirror the workstation: bundle
   `type_id_string` wherever a service is introduced (discovery +
   returned descriptors); `allocateGetServiceMetadata` resolves it
   locally; no "unknown" sentinel** (design/01, design/03). The
   `type_id_string` is a non-identity annotation — excluded from
   descriptor canonicalization for shortId hashing. Dynamic dispatch
   is unchanged — it still uses the `$fxtr.type` invoke (a *loadable*
   type-entity ref, distinct from the string).

I. **Dispatcher caches across backend redeploys** → **make the
   dispatch service a WS service that emits reload-everything; the
   dispatcher wipes caches + resubscribes on connection loss.** Split
   the dispatcher-side change out as an earlier setup part (Part 1) so
   the WS reload "just works" once the backend lands; Part 1 also adds
   a manual reload action to the dispatcher debug panel (design/07,
   Part 1).

## Resolved — mechanical

J. **Descriptor canonicalization** → **absolute URL + sorted-key
   JSON**; the `type_id_string` annotation is excluded (design/01).
K. **`error_type` field** → **drop it. Mirror the workstation, which
   carries no `error_type`** — invoke/load/metadata errors are
   `message`-string-only (`repr(exc)`). Its only structured error
   classification is `MutationRejectReason`, which has no stateless
   analogue (there are no server-side mutations) (design/01, design/02).
L. **Transient-arg generation check timing** → **check at allocate
   (fail fast) and at send (errored fill)** (design/01).
M. **Node ws library** → **`@hono/node-ws` (+ `@hono/node-server`)**,
   but **deferred** with the TS backend (Q-R) — only relevant once the
   TS backend lands (design/02).
N. **Package promotion timing** → **do NOT rename/promote
   `fxtr2.experimental.http` in this change.** Left as a follow-up to
   avoid conflicting with parallel work (design/02).
O. **Ping/keepalive** → **skip until proxies force it** (design/01).
R. **TypeScript backend** → **deferred to a follow-up; build the
   Python backend only.** The TS port is already partial and some TS
   logic it would have used has had a change of plans. Per-language
   design notes are kept but marked `[deferred]`. Consequences: the
   frontend integration tests spawn the **Python** tester backend over
   the wire (the `_demo_workstation.py` + `_integration_harness.ts`
   pattern), the demo backend is Python, and the cross-backend smoke
   test + the entity-decode parity test's TS half return with the TS
   backend (overview.md, design/02, design/03).

## Folded in from the completed precursor (part6 stage4 / stage4post1)

N1. **Inbox connection-state parity.** The abstract `CallbackInboxStub`
    now carries `connectionState` / `subscriptionBreakReason`. The
    stateless connector synthesizes these locally: `connected` on a
    successful ws subscribe, `broken` when that connection's
    generation dies (design/05). This is what lets the Part 1
    dispatcher reload-on-break work uniformly across connectors.
N2. **`subscribe_to(method="subscribe")`.** `subscribe_to` gained a
    `method` param; design/05's desugaring becomes `<method>(handle)`,
    default `"subscribe"` (design/05).

## Tracked separately

P. **Broken-grant detection precursor** → **done** in part6
   stage4 / stage4post1 (subscription-break notifications).

## Resolved (round 1) — where each landed

1. Discovery content is the server author's business; library is
   unopinionated; demo mirrors pins → overview.md, design/02.
2. ws RPC kept simple: no timeouts, server-side ordering, optional
   ping; drops are invoke errors, never mutation rejections →
   overview.md (governing principle), design/01.
3. `subscribe` returns a **connection-scoped transient service**
   (breaks with the connection; same-connection-only as an arg) →
   design/01.
4. No subscription replay; uniform breakage detection is the
   precursor (P) → overview.md, design/05.
5. Simple-service errors: non-2xx + JSON body → design/01.
6. GET size limits: documented constraint → design/01.
7. `Cache-Control: immutable` on `/data` → design/03.
8. ShortId table persisted, same storage backend as sessions *and*
   shell layout; no durable/non-durable split → design/04.
9. In-flight invokes at reload rehydrate errored → design/05.
10. Multi-tab: last-writer-wins; sessionStorage implemented first →
    design/04.
11. Code sharing: fork-and-simplify, extract later → design/05.
12. Session lifecycle: local; never `stale`; ws health is
    connector-level → design/05.
13. Connector interface: scoped in design/06 (live remainder: C, D).
14. Debug panels: options sketched → D.
15. Backend home: extend in place; promotion deferred → N.
16. Demo content: scope during the demo part's planning (unchanged).

## Newly opened

(none yet)
