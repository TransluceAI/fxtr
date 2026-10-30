# Stage 2 — Agent plugin renderer + demo

## Goal

Make the typed event log + user-message channel that
[`stage10_event_log_and_messages.md`](../part2_ws_mcp/stage10_event_log_and_messages.md)
landed *visible* — give operators a frontend they can point at a
running MCP session and watch the agent work in real time, with a
text box at the bottom to inject `workstation_inbox` messages.

Three deliverables:

1. **Backend tweak.** Change `AgentMcpRunningSessionProto.watch_state`
   so it returns the current chain head alongside subscribing.
   Removes the race between "fetch current state" and "subscribe to
   future updates" for any subscriber. Keep `get_state` as a one-shot
   probe but no in-tree subscriber uses it.

2. **Two new TS packages** under `projects/fxtr_workstation/frontend_lib/packages/`:
   - `@fxtr-ui/rd-types-stream` — a tiny package exporting one
     dispatch key, `StreamEntry`, plus its fallback bundle.
     Reusable across systems that render an ordered stream of typed
     log-like entries (the agent log is the first; another system
     will piggyback soon).
   - `@fxtr-ui/agent-plugin-renderer` — a `FullPanel` renderer for
     the `agent_mcp_running_session` service type plus four
     `StreamEntry` renderers (one per event payload type). The
     `FullPanel` drives a state machine via a jotai-effect atom:
     subscribe → maintain `(chain stub, chain load result)` list →
     render each entry's `event` ref via the `StreamEntry` dispatch
     key. Bottom of the panel: a text-box + send button that calls
     the service's `send`.

3. **New demo** at `projects/fxtr_workstation_demo/` — the
   workstation's main showcase from here on. A flat layout: the
   backend Python script (forked from
   `projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/`),
   the frontend Vite app (forked from
   `frontend_lib/packages/fxtr-ui-workstation-demo/`), a sample
   `agent_workdir/`, and a thin shell wrapper that starts both with
   fixed ports.

## High-level design

```
  Agent MCP plugin process
    ┌────────────────────────────────┐
    │ AgentMcpRunningSessionImpl     │   external_name:
    │   get_state                    │   ─────────────────►
    │   watch_state ──┐              │   agent_mcp:{session_id}
    │   send          │              │
    └─────────────────┼──────────────┘
                      │ new: returns
                      │ current head AS WELL AS
                      │ subscribing (race-free)
                      ▼

  Workstation /rpc + /client
    ┌────────────────────────────────────────────┐
    │ Frontend (separate /rpc peer)              │
    │                                            │
    │ <WorkstationShell>                         │
    │   StaticDispatcher                         │
    │   ┌──────────────────────────────────┐     │
    │   │ FullPanel ← agent-plugin-renderer│     │
    │   │ <AgentMcpFullPanel/>             │     │
    │   │   useMemo:                       │     │
    │   │     - atomEffect driving FSM     │     │
    │   │       init → subscribing →       │     │
    │   │       running                    │     │
    │   │     - maintain entries[] in      │     │
    │   │       user-state                 │     │
    │   │   render:                        │     │
    │   │     [Load more]?                 │     │
    │   │     <RenderDispatch              │     │
    │   │       as={StreamEntry}           │     │
    │   │       stub={entry.event}/>       │     │
    │   │     ...                          │     │
    │   │     <input> + Send               │     │
    │   └──────────────────────────────────┘     │
    └────────────────────────────────────────────┘
```

## Background

- **`watch_state` today** ([`agent_mcp_impl.py`](../../../../fxtr_workstation_agent_plugin/fxtr_workstation_mcp/fxtr_workstation_mcp/agent_mcp_impl.py))
  returns `None` and only fires `notify` on subsequent extensions.
  A subscriber that wants the current state must call `get_state`
  separately, racing the gap between the two calls.
- **The chain head wire shape.** `EntityRef[WsAgentLogEventChain]`
  wire-strips to a bare `FxtrID`; on the frontend it lifts to a
  connector-interned `EntityRefStub`. Once loaded, the chain head
  is `{prev: EntityRefStub | null, event: EntityRefStub,
  timestamp: float}`.
- **Event payload entities.** Four
  (`ScriptExecutionLogEvent` / `InvokeResultLogEvent` /
  `InvokeErrorLogEvent` / `CallbackNotificationLogEvent`) — typed in
  [`agent_log_types.py`](../../../../fxtr_workstation_agent_plugin/fxtr_workstation_mcp/fxtr_workstation_mcp/agent_log_types.py).
  Each frontend renderer narrows the loaded chain entry's `event`
  ref to one of these and renders the payload.
- **Persistence cap.** The MCP looks up its persistence cap by
  external name (`agent_mcp_store` by default). For the new demo we
  mount a `PersistenceNamespaceConfig` with `mode="preserve_provenance"`
  pointing at a sqlite-backed `FxtrNamespace`.

## Backend tweak — `watch_state` returns the head

### Protocol change

```text
# agent_mcp_proto.py
class AgentMcpRunningSessionProto(Protocol):
    async def get_state(self) -> EntityRef[WsAgentLogEventChain] | None: ...
    async def watch_state(
        self, listener: ServiceRef[EventListenerProto]
    ) -> EntityRef[WsAgentLogEventChain] | None: ...
    async def send(self, payload: FxtrServiceData) -> None: ...
```

### Impl change — ordering for race-freedom

The two-statement sequence has to be **register-then-snapshot,
with no `await` between them**:

```text
# inside watch_state, NO awaits between the two writes:
self._watchers.append(watcher)         # (1) start receiving updates
initial_head = self._chain_head        # (2) snapshot
await self._impl_tg.start(...)         # (3) start the worker
return initial_head
```

This gives us a tight guarantee: **the worker never sends a
notify for the same head we returned**. Proof, exhausting where a
concurrent `update_head(h1)` can land relative to (1)/(2)/(3):

- **Before (1).** `_chain_head` is already `h1` when we get to
  step (1); the watcher isn't in `_watchers` yet, so
  `update_head`'s walk doesn't see it — our `latest` stays `None`
  and our event isn't fired. (2) reads `h1`; we return `h1`. The
  worker blocks on `state.has_update.wait()` until the *next*
  `update_head`, which will carry a head ≠ `h1`. No duplicate.
- **Between (1) and (2).** Impossible — Python's event loop is
  cooperative and there's no `await` between these two
  statements; another task literally cannot run here.
- **After (1) but during (3).** Our watcher is in the list, so
  `update_head` sets `latest = h1` and fires the event. But (2)
  ran before (3), so we snapshotted the *previous* head `h0`.
  Return `h0`; worker eventually notifies with `h1`. Different
  heads.
- **After (3) returns.** Standard post-subscribe notify flow;
  return was `h0`, notify carries some later `hN`. Different.

So the subscriber never has to dedupe — the return value and any
subsequent `notify`s carry strictly distinct heads. The
no-`await` invariant between (1) and (2) is the load-bearing
piece; a code comment pins it in place.

(If we did snapshot-then-register instead, `update_head` between
the snapshot and the append would update `_chain_head` to a head
we don't observe AND set up no watcher entry to fire for it —
the subscriber would *miss* it. So the current order is the
correct one; we just need the no-`await` invariant nailed.)

The idempotent-duplicate-listener path (the `for existing in
self._watchers: ... return` early-out) also returns
`self._chain_head` now — same shape as the cold path, since the
caller still wants to know the current head even if their
listener was already registered. The same no-`await` invariant
holds trivially (the early-out is fully sync).

### Tests

- Update `tests/test_agent_mcp_impl.py` so any existing `watch_state`
  call sites assert against the new return type (currently `None`).
- New deterministic race test: prime the impl with head=h0; in one
  task call `await impl.watch_state(listener)`, in a sibling task
  call `impl.update_head(h1)`. Assert the subscriber sees h1 via
  either the return value or `listener.notify` (it can be
  duplicate but never missing).
- Update `tests/test_event_log_chain.py` integration tests that
  call `watch_state` and assert against the head return.

## New `@fxtr-ui/rd-types-stream`

A standalone package with one dispatch key. Lives in
`frontend_lib/packages/fxtr-ui-rd-types-stream/`.

### Module shape

```text
fxtr-ui-rd-types-stream/
├── package.json
├── tsconfig.json
└── src/
    ├── keys.ts            — StreamEntry DispatchKey + StreamEntryProps
    ├── fallbacks.tsx      — Loading / NoMatch / Error fallback components
    └── index.ts           — public re-exports
```

- `StreamEntry: DispatchKey<StreamEntryProps>` — `declareDispatchKey`,
  allows both entity and service stubs but practically the
  consumers (agent-plugin-renderer) only register entity entries.
- `StreamEntryProps` starts as `NoProps`. Renderers manage their
  own load via `useDispatchSharedAtom<OpResultStub<T> | null>(null)`
  + `makeLoadResultAtom` (same pattern as the report-link
  renderers). Keeping props empty leaves room for the second
  system to extend later without breaking us.
- Wire-stable UUID hardcoded.
- Fallback bundle: simple loading-pulse card, terse no-match card,
  red-bordered error card.

Dependencies: `@fxtr-ui/core`, `@fxtr-ui/react`,
`@fxtr-ui/react-dispatch`. Peer-dep `react`. Tiny.

## New `@fxtr-ui/agent-plugin-renderer`

Lives in `frontend_lib/packages/fxtr-ui-agent-plugin-renderer/`.

### Module shape

```text
fxtr-ui-agent-plugin-renderer/
├── package.json
├── tsconfig.json
└── src/
    ├── proto.ts                — AgentMcpRunningSessionProto + type id
    ├── data_shapes.ts          — loaded shapes for chain + events + ops
    ├── panel.tsx               — AgentMcpFullPanel
    ├── state_machine.ts        — atomEffect factory + state types
    ├── send_box.tsx            — text-area + send button
    ├── load_more.tsx           — "Load more" affordance
    ├── entries/
    │   ├── shared.tsx          — common card chrome
    │   ├── script_execution.tsx
    │   ├── invoke_result.tsx
    │   ├── invoke_error.tsx
    │   └── callback_notification.tsx
    ├── lookup_maps.ts          — pre-built lookup-map objects for
    │                             registry composition
    └── index.ts                — public re-exports
```

### TS protocol declaration (`proto.ts`)

Mirrors the Python protocol via the `Method<Args, Kwargs, Returns>`
type. Same hand-mirroring pattern as
[`_demo_service_proto.ts`](../../../frontend_lib/packages/fxtr-ui-demo/src/client/_demo_service_proto.ts) — a
contract test would be future work; not in scope here.

```text
export const AGENT_MCP_RUNNING_SESSION_TYPE_ID =
    "<id-string copied from Python>";

export interface AgentMcpRunningSessionProto extends ServiceProto {
    get_state: Method<[], {}, EntityRefStub | null>;
    watch_state: Method<
        [CallbackHandleStub<EntityRefStub>], {}, EntityRefStub | null
    >;
    send: Method<[InvokeArgValue], {}, null>;
    // Payload typed as `InvokeArgValue` — the TS analog of
    // Python's `FxtrServiceData` on the arg side. The session's
    // `escapeInvokeArgs` wire-encodes it on the way out; the
    // demo passes strings, but the protocol stays open for
    // structured payloads (dicts, lists, refs, callback handles).
}
```

### Loaded shapes (`data_shapes.ts`)

Plain TS interfaces for the wire-decoded forms of each entity. The
workstation's `unescapeWire` produces these on entity loads, with
nested ref leaves already lifted to `EntityRefStub` instances:

```text
interface ChainNodeValue {
    readonly prev: EntityRefStub | null;
    readonly event: EntityRefStub;
    readonly timestamp: number;
}

interface ScriptExecutionValue {
    readonly script_src: string;
    readonly preamble: EntityRefStub;      // EntityRef[ScriptPreamble]
    readonly stdout: string;
    readonly stderr: string;
    readonly error: string | null;
    readonly rejected: string | null;
    readonly ops: ReadonlyArray<ScriptOpRecord>;
    readonly committed_seq: number | null;
}

interface InvokeResultValue {
    readonly slot_key: string;
    readonly slot_n: number;
    readonly seq: number;
    readonly payload: ResultValue;         // decoded FxtrCbor
}

// ...InvokeErrorValue + CallbackNotificationValue similar...

// Discriminated subpart — fields are nullable except `kind`.
type ScriptOpRecord =
    | { kind: "export_entity"; slot_key: string; entity: EntityRefStub }
    | { kind: "export_service"; slot_key: string; service: EntityRefStub }
    | { kind: "forget"; slot_key: string }
    | { kind: "schedule_invoke"; slot_key: string; service_short_id: string;
        method_name: string; args: ResultValue; kwargs: ResultValue }
    | { kind: "create_callback"; slot_key: string }
    | { kind: "forget_callback_notification";
        slot_key: string; notif_index: number }
    | { kind: "present_to_user"; payload: ResultValue };
```

### Panel state shape

```text
type PanelPhase =
    | { kind: "init" }
    | {
        kind: "subscribing";
        inbox: CallbackInboxStub<EntityRefStub>;
        subscribe: OpResultStub<EntityRefStub | null>;
      }
    | {
        kind: "running";
        inbox: CallbackInboxStub<EntityRefStub>;
      }
    | { kind: "errored"; message: string };

interface PanelState {
    readonly phase: PanelPhase;
    readonly entries: ReadonlyArray<{
        readonly stub: EntityRefStub;
        readonly load: OpResultStub<ChainNodeValue>;
    }>;
    readonly draftMessage: string;
    readonly lastSend: OpResultStub<null> | null;
    readonly lastSendDraft: string | null;  // text of the last send
                                            // attempt, kept around so
                                            // an errored send can be
                                            // surfaced + offered for
                                            // retry/edit
}
```

`phase` rides the subscribe lifecycle; once `running`, the effect
just keeps draining the inbox and appending entries. `entries` is
the live list. `lastSend` anchors the most-recent invoke slot so
the session reducer doesn't GC it before its wire emission;
`lastSendDraft` carries the original text so the send-box can
render "sending: <text>…" / "send failed: <message>" feedback
(see `send_box.tsx` below).

### Effect-driven state machine (`state_machine.ts`)

A pure factory that returns one `atomEffect` driver and a derived
read atom. Same shape as the dynamic dispatcher's reload effect +
the static dispatcher's per-entry effect factory, just for our
panel.

```text
function makeAgentPanelDriver(
    panelStateAtom: FxtrSessionAtom<PanelState>,
    serviceStub: ServiceRefStub<AgentMcpRunningSessionProto>,
): atomEffect<...>:

  on each pass:
    state = get(panelStateAtom)

    if state.phase.kind === "init":
      set(panelStateAtom, (prev, ctx) => {
        inbox = ctx.allocateCallback<EntityRefStub>()
        subscribe = ctx.allocateInvoke(
            serviceStub, "watch_state", [inbox.handle]
        )
        return {...prev, phase: {kind: "subscribing", inbox, subscribe}}
      })
      return

    if state.phase.kind === "subscribing":
      r = get(state.phase.subscribe.result)
      if r.status === "pending": return
      if r.status === "errored":
        set(panelStateAtom, ...phase: errored(r.message))
        return
      # filled — r.value is EntityRefStub | null
      set(panelStateAtom, (prev, ctx) => {
        entries = []
        if r.value is not None:
          load = ctx.allocateLoad<ChainNodeValue>(r.value)
          entries.push({stub: r.value, load})
        return {
          ...prev,
          phase: {kind: "running", inbox: state.phase.inbox},
          entries,
        }
      })
      return

    if state.phase.kind === "running":
      events = get(state.phase.inbox.events)
      if events.length === 0: return
      # Per the backend invariant (see "Impl change — ordering"
      # in the backend tweak section), the worker never notifies
      # for the same head we returned at subscribe time, so we
      # can append every event unconditionally.
      set(panelStateAtom, (prev, ctx) => {
        new = [...prev.entries]
        for ref in events:
          load = ctx.allocateLoad<ChainNodeValue>(ref)
          new.push({stub: ref, load})
        return {
          ...prev,
          phase: {kind: "running", inbox: state.phase.inbox.sliced(events.length)},
          entries: new,
        }
      })
      return
```

`subscribe` and load slots are all anchored in `panelState`, so the
session reducer's "ref must be visible in userState at flush time"
contract is satisfied without extra plumbing.

### Panel component (`panel.tsx`)

```text
AgentMcpFullPanel(stub: ServiceRefStub, ownStorage: FxtrSessionAtom<unknown>):

  panelStateAtom, driverAtom = useMemo(...) over [ownStorage, stub]:
    panelStateAtom = atomWithDefault(
        ownStorage as FxtrSessionAtom<PanelState | null>,
        INITIAL_PANEL_STATE,
    )
    driverAtom = makeAgentPanelDriver(panelStateAtom, stub as ServiceRefStub<...>)
    return {panelStateAtom, driverAtom}

  useAtomValue(driverAtom)  # mounts the effect

  state = useAtomValue(panelStateAtom)

  render:
    if state.phase.kind === "errored": ErrorCard
    if state.phase.kind === "init" or "subscribing": SubscribingCard
    if state.phase.kind === "running":
      <Stream entries={state.entries} panelStateAtom={panelStateAtom}/>
      <SendBox panelStateAtom={panelStateAtom} serviceStub={stub}/>
```

### Send box (`send_box.tsx`)

Controlled text-area bound to `state.draftMessage`; Send button
fires an updater that allocates the invoke + records the draft
text:

```text
onSend():
  set(panelStateAtom, (prev, ctx) => {
    if prev.draftMessage.length === 0: return prev
    invoke = ctx.allocateInvoke(serviceStub, "send", [prev.draftMessage])
    return {
      ...prev,
      draftMessage: "",
      lastSend: invoke,
      lastSendDraft: prev.draftMessage,
    }
  })
```

Status line above the text-area reads from `state.lastSend?.result`:

- `null` (no prior send) → no status line.
- `pending` → "sending: <lastSendDraft>…" (muted text).
- `filled` → no status line (the message will appear in the chain
  as a `workstation_inbox` callback notification anyway; success
  is visible there). Clears `lastSend` / `lastSendDraft` on the
  next user keystroke or after a brief timeout so stale "sent ✓"
  notes don't linger.
- `errored` → "send failed: <message>" in error styling, with a
  small "retry" button that puts `lastSendDraft` back into
  `draftMessage` for the user to re-send/edit.

The `lastSend` slot anchors the invoke for the session reducer; on
each new send we overwrite (the prior slot's wire footprint drops
in the same flush — standard reducer behaviour).

### "Load more" affordance (`load_more.tsx`)

For each entry `i ≥ 1`, once `entries[i].load` is filled we have
`entries[i].load.value.prev` available. If `prev !== null` AND
`prev.shortId !== entries[i-1].stub.shortId`, render a `Load more`
button above entry `i`. Click handler:

```text
onClick():
  set(panelStateAtom, (prev, ctx) => {
    prior = prev.entries[i-1]
    current = prev.entries[i]
    if not (current.load is filled): no-op
    gapHead = current.load.value.prev
    newLoad = ctx.allocateLoad<ChainNodeValue>(gapHead)
    return {
      ...prev,
      entries: [
        ...prev.entries.slice(0, i),
        {stub: gapHead, load: newLoad},
        ...prev.entries.slice(i),
      ],
    }
  })
```

The button disables itself while the new entry's `load` is pending;
on fill, the new entry's `prev` may *itself* differ from the prior
entry's stub — another "Load more" appears. Iterating produces the
full back-walk.

Normal case (no gap): the inbox delivers every extension, so
`entries[i].load.value.prev === entries[i-1].stub` and no button
appears. Gaps happen on watcher skip-ahead (the impl explicitly
allows missing intermediate heads) or on reattach to a panel whose
userState carried entries from an earlier session that has since
extended further.

### Per-event-type `StreamEntry` renderers (`entries/*.tsx`)

Same shape as the report-link renderers — each gets the loaded
event value via `useDispatchSharedAtom<OpResultStub<T> | null>(null)`
+ `makeLoadResultAtom`. Render as a styled card:

- **`ScriptExecutionStreamEntry`** — header strip
  ("script ran" / "script errored" / "script rejected" + timestamp,
  pulled from the chain entry's timestamp via the parent — see
  caveat below); body shows `stdout`, `stderr`, then a collapsed
  `ops` summary. If `ops` contains any `kind === "present_to_user"`:
  a dedicated bottom block, visually emphasized (larger text,
  border, "Presented" header), rendering each `present_to_user`
  op's `payload` through `<JsonRefTree>` from
  `@fxtr-ui/workstation-dispatch` — so ref leaves in the payload
  surface as clickable `<RefLink>` instances (the operator can
  click through to open the referenced entity / service in
  another panel).
- **`InvokeResultStreamEntry`** — "invoke result @ slot_key (slot_n,
  seq)" header, payload through `<JsonRefTree>` below (same
  rationale — invoke result payloads commonly carry refs).
- **`InvokeErrorStreamEntry`** — "invoke error @ slot_key" header,
  message in error styling.
- **`CallbackNotificationStreamEntry`** — branch on `slot_key`:
  - `slot_key === "workstation_inbox"` → "User said" header with
    the payload rendered as text (if it's a string); the inbox is
    where `send()` payloads land.
  - Otherwise → generic "callback notification @ slot_key"
    presentation, payload through `<JsonRefTree>`.

`<JsonRefTree>` is the right primitive here — it already walks
structured values and routes ref leaves through `<RefLink>`, which
picks up the shell's `RefLinkImpl` (the click/drag/hover-preview
wrappers). No reinvention.

Timestamp caveat: the timestamp lives on the *chain entry*, not on
the event payload. The simplest path is for the parent `<Stream>`
component to render the timestamp above each `<RenderDispatch>`
call (i.e. as a row prefix), so the StreamEntry renderers don't
need to know about it. The per-event-type renderers focus on the
event payload itself.

### Lookup maps (`lookup_maps.ts`)

Pre-built lookup maps so consumers (the new demo) can compose a
`createDispatchRegistry(...)` call concisely. The package exports:

```text
export const AGENT_PLUGIN_TYPE_IDS = {
    agent_mcp_running_session: AGENT_MCP_RUNNING_SESSION_TYPE_ID,
    script_execution_log:      "...",
    invoke_result_log:         "...",
    invoke_error_log:          "...",
    callback_notification_log: "...",
};

export const agentPluginFullPanelServiceMap =
    new Map([[AGENT_MCP_RUNNING_SESSION_TYPE_ID, AgentMcpFullPanel]]);

export const agentPluginStreamEntryEntityMap = new Map([
    [TYPE_IDS.script_execution_log, ScriptExecutionStreamEntry],
    [TYPE_IDS.invoke_result_log,    InvokeResultStreamEntry],
    [TYPE_IDS.invoke_error_log,     InvokeErrorStreamEntry],
    [TYPE_IDS.callback_notification_log, CallbackNotificationStreamEntry],
]);
```

Type-id strings are hand-mirrored from the Python `FxtrID.id_string()`
values for the relevant `system_entities.define_type(WS_AGENT_NS,
...)` calls. A small runtime sanity check in the demo's dispatch
registry construction will assert the strings match an env-injected
`TYPE_IDS` blob — same defensive pattern the other demos use.

### Tests

In `frontend_lib/packages/fxtr-ui-agent-plugin-renderer/tests/`:

- `state_machine.test.ts` — drive the effect-atom directly against
  a mock session (mirrors the dispatch-package's `_mocks.ts`
  pattern). Cases: init → subscribing → running with non-null head,
  with null head, with errored subscribe, append-on-event, dedupe
  of head-equal-to-subscribe-return, "Load more" gap fill.
- `load_more.test.ts` — given a synthetic entries array with a
  gap, click handler splices in the prev-chain stub with a fresh
  load.

React-render-layer tests for the panel + per-entry components live
in `v2/ts/packages/fxtr-ui-agent-plugin-renderer-tests/` if needed
(same workaround the other React-render-test packages use); skip
unless we find the renderers need explicit React-tree coverage.

## Demo at `projects/fxtr_workstation_demo/`

Flat layout — outside the pnpm workspace's existing roots; the
workspace yaml gains one new entry so the Vite app can use
`workspace:*` deps.

### File layout

```
projects/fxtr_workstation_demo/
├── README.md                          (operator setup)
├── run_demo_workstation.py            (backend with persistence cap)
├── start_demo.sh                      (thin wrapper — runs `pnpm dev`)
├── frontend/                          (Vite app — fork of fxtr-ui-workstation-demo)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── server.ts                  (orchestrator: spawn backend, start Vite)
│       └── client/
│           ├── main.tsx
│           ├── App.tsx
│           └── dispatch_registry.tsx
├── .data/                             (.gitignored — sqlite store created at runtime)
└── agent_workdir/                     (sample workdir for the MCP plugin)
    ├── README.md                      (agent-facing)
    ├── .fxtr_workstation.toml.example
    └── preamble.py
```

### `run_demo_workstation.py` changes

Fork from
[`basic_test_demo/run_demo_workstation.py`](../../../../fxtr_workstation_agent_plugin/examples/basic_test_demo/run_demo_workstation.py).
Two diffs:

1. Mount an `agent_mcp_store` persistence cap.

   ```text
   _AGENT_MCP_STORE_DB = _HERE / ".data" / "agent_mcp_store.sqlite"

   async with create_sandbox_environment(cache_dir=...) as sandbox_env:
       async with SQLiteFxtrNamespace(
           str(_AGENT_MCP_STORE_DB),
           "agent_mcp_store",
       ) as ns:
           agent_mcp_store_cfg = PersistenceNamespaceConfig(
               external_name="agent_mcp_store",
               namespace=ns,
               mode="preserve_provenance",
           )
           async with WorkstationServer(
               port=8642,
               external_services=...,
               persistence_namespaces=[agent_mcp_store_cfg],
               debug_ui_dist_path=...,
           ) as ws:
               ... (rest unchanged)
   ```

2. Also emit `TYPE_IDS <json>`, `READY <ws-url>`, and `DEBUG_UI
   <http-url>` lines so the frontend orchestrator can pick them up
   the same way the existing Vite demos do. `TYPE_IDS` carries the
   four event-payload-entity type-id strings plus the
   agent-mcp-running-session type id — the dispatch registry's
   sanity check verifies them against the hand-mirrored constants
   in `proto.ts` / `lookup_maps.ts`.

`.data/` is gitignored. The existing print-the-URL-and-park
behaviour stays.

### Frontend (`frontend/`)

Fork from
[`fxtr-ui-workstation-demo`](../../../frontend_lib/packages/fxtr-ui-workstation-demo/).
Three substantive changes from the source:

- `package.json` — rename to `@fxtr-workstation-demo/frontend` (not
  `@fxtr-ui/*` since this isn't part of the fxtr-ui library); add
  workspace dep `@fxtr-ui/agent-plugin-renderer`.
- `vite.config.ts` — fixed port (`3470` chosen so it doesn't
  collide with the existing 3461 / 3462 / 3463 demos).
- `src/client/dispatch_registry.tsx` — register
  `agentPluginFullPanelServiceMap` on the `FullPanel` key's
  service entry, plus the `StreamEntry` key registered with
  `agentPluginStreamEntryEntityMap`. Other types (Note, etc.) are
  intentionally not registered — operators see "no renderer" cards
  for those, which is fine for the demo's purpose (the agent's
  service view is what's meant to shine).
- `src/server.ts` — point at our `run_demo_workstation.py`. Same
  spawn-Python-then-Vite shape; only the script path changes.

The frontend uses the existing `<WorkstationShell>` (so all the
panel-viewer chrome comes for free — sidebar, breadcrumbs,
metadata-mode toggle, etc.). The user opens the
`agent_mcp:{session_id}` service entry → the shell's per-panel
session attaches → `FullPanel` dispatches to `AgentMcpFullPanel` →
the state machine takes over.

### `start_demo.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
pnpm --filter @fxtr-workstation-demo/frontend dev
```

The `pnpm dev` script in `frontend/package.json` runs
`tsx src/server.ts`, which spawns the Python backend and starts
Vite. Two fixed ports: workstation on `8642` (set in
`run_demo_workstation.py`), Vite on `3470` (set in
`vite.config.ts`). Ctrl-C tears both down (existing pattern).

### `agent_workdir/`

Starts pretty empty: a `README.md` mirroring
[`basic_test_demo/agent_workdir/README.md`](../../../../fxtr_workstation_agent_plugin/examples/basic_test_demo/agent_workdir/README.md)
in shape but pointing operators at the new frontend (and the new
demo's setup steps); a `.fxtr_workstation.toml.example` carrying
`workstation_url = "ws://127.0.0.1:8642"` and `agent_mcp_store =
"agent_mcp_store"`; a minimal `preamble.py`. The
`packages/` subfolder is omitted for now — scenarios get populated
later.

### Operator README

A top-level `README.md` documenting the demo setup steps: install
the MCP plugin (pointing at the same paths the basic_test_demo
does), run `./start_demo.sh`, open `http://localhost:3470`, point
the agent at `agent_workdir/`. Mirrors the basic_test_demo's
README shape.

### Workspace integration

One added line in
[`pnpm-workspace.yaml`](../../../../../pnpm-workspace.yaml):

```yaml
packages:
  - "packages/*"
  - "../../projects/fxtr_workstation/ts/packages/*"
  - "../../projects/fxtr_workstation/frontend_lib/packages/*"
  - "../../projects/fxtr_report_builder/ts/packages/*"
  - "../../projects/fxtr_workstation_demo/frontend"   # NEW
```

One `pnpm install` from `v2/ts/` picks it up.

## Tests

End-to-end coverage of the new demo is "operator runs `./start_demo.sh`
and exercises the flow" — same model the existing Vite demos use,
not subject to automated regression. Per-package unit tests as
described inline above.

## Implementation chunks (for pair-programming review)

The plan splits into four review chunks. Each chunk is independently
verifiable; we pause between for review.

- **Chunk A — Backend `watch_state` returns head.** Steps 1–3. ✅ **done**
- **Chunk B — `@fxtr-ui/rd-types-stream` package.** Step 4. ✅ **done**
- **Chunk C — `@fxtr-ui/agent-plugin-renderer` package.** Steps 5–10. ✅ **done**
- **Chunk D — Demo at `projects/fxtr_workstation_demo/`.** Steps 11–15. ✅ **done**

## Implementation steps

### Chunk A — Backend `watch_state` returns head

1. ✅ **Protocol + impl change.** Updated
   `AgentMcpRunningSessionProto.watch_state` return type; reordered
   `watch_state`'s body to register-then-snapshot, returning the
   captured head. The no-`await` invariant is pinned with a code
   comment and the docstring carries the case-by-case proof. The
   idempotent early-out also returns the current head.
2. ✅ **Update existing tests.** `tests/test_agent_mcp_impl.py` —
   updated the `_call_watch_state` helper to return the head;
   threaded the return value through `test_duplicate_watch_state_is_noop`
   (asserts both first and idempotent second calls return the
   current head — `None` here since no prior extension). Added
   four new tests: `test_watch_state_returns_none_for_fresh_session`,
   `test_watch_state_returns_initial_head`,
   `test_watch_state_no_duplicate_notify_for_pre_subscribe_head`
   (covers "update before append" — return carries the head, no
   spurious notify), and
   `test_watch_state_no_dup_no_miss_under_concurrent_update`
   (covers the "during start-await" + "strictly after" cases via
   `tg.start_soon` race; asserts the union of return-value +
   notify payloads contains the new head exactly once). The
   "between (1) and (2)" case has no test — structurally excluded
   by the no-`await` invariant, called out in the impl's
   docstring + a code comment.
3. ✅ **Adjust `test_event_log_chain.py`.** Verified — that file
   doesn't call `watch_state` (only `send`), so no adjustments
   needed.

### Chunk B — `@fxtr-ui/rd-types-stream`

4. ✅ **New package.**
   - Scaffolded `package.json` (workspace-deps on `@fxtr-ui/core`
     + `@fxtr-ui/react-dispatch`, react peer-dep) + `tsconfig.json`.
   - `src/keys.ts` declares `StreamEntry: DispatchKey<NoProps>`
     via `declareDispatchKey` with hardcoded wire-stable UUID
     `4b2e1a3c-7d68-4e91-a25f-9c3d8b0e1f7a`; admits both entity
     and service stubs. Exports `StreamEntryProps = NoProps`
     alias.
   - `src/fallbacks.tsx` carries `StreamEntryLoadingFallback`,
     `StreamEntryNoMatchFallback`, `StreamEntryErrorFallback` —
     all using the same outer card chrome via the workstation
     theme's CSS custom properties.
   - `src/index.ts` re-exports the key + fallback components.
   - `tests/keys.test.ts` snapshots the UUID + asserts the
     fallback bundle shape (Loading + NoMatch + Error present,
     Builtin absent). 4/4 vitest passes.
   - Workspace `pnpm install` picks up the new package; full
     workspace `pnpm check` (tsc --build) passes.
   - Updated `frontend_lib/CODE_OVERVIEW.md` — added the
     package to the layout listing between
     `fxtr-ui-workstation-dispatch` and `fxtr-ui-workstation`,
     plus a new dedicated section.

### Chunk C — `@fxtr-ui/agent-plugin-renderer`

5. ✅ **Skeleton package.** `package.json` (workspace-deps on
   `@fxtr-ui/core` + `@fxtr-ui/rd-types-stream` +
   `@fxtr-ui/react` + `@fxtr-ui/react-dispatch` +
   `@fxtr-ui/state-util` + `@fxtr-ui/workstation-dispatch`;
   `jotai` + `jotai-effect`; react peer-dep), `tsconfig.json`.
6. ✅ **TS protocol + data shapes.** `src/proto.ts` declares
   `AgentMcpRunningSessionProto extends ServiceProto` with
   `Method<...>` descriptors for `get_state` / `watch_state` /
   `send` (payload typed `InvokeArgValue`). `src/data_shapes.ts`
   carries TS interfaces for the wire-decoded forms of the
   chain node + each event type; `ScriptOpRecord` is a
   discriminated union over the seven `OP_KIND_*` values.
   Type-id strings extracted by running a Python one-liner that
   prints `id_string()` for each `system_entities.define_type`
   call.
7. ✅ **State machine factory.** `src/state_machine.ts` —
   `PanelState` / `PanelPhase` types, `INITIAL_PANEL_STATE`,
   `makeAgentPanelDriver(panelStateAtom, serviceStub)` returning
   an `atomEffect`-backed driver. Unit-test mock built in
   `tests/_mock_session.ts` (mirrors the dispatch package's
   `_mocks.ts` shape, extended with `allocateCallback`). 7/7
   tests pass: init→subscribing allocates inbox + invokes
   watch_state; subscribing→running with non-null head seeds
   one entry; subscribing→running with null head leaves entries
   empty; errored subscribe transitions to errored; running
   appends per delivered event + slices inbox; idle on no
   events; errored is terminal.
8. ✅ **Panel component + Send box + Load more.** `src/panel.tsx`
   (`AgentMcpFullPanel` mounting the driver atom + branching
   on phase; `EntriesStream` / `EntryRowWithGap` /
   `StreamEntryRow` subcomponents for the row layout + gap
   affordance), `src/send_box.tsx` (text-area + Send button +
   `SendStatusLine` for `lastSend` feedback), `src/load_more.tsx`
   (the "Load missing entries" button + splice updater).
9. ✅ **Per-event-type renderers.** `src/entries/shared.tsx`
   (common card chrome + timestamp formatter) +
   `entries/script_execution.tsx` (with the `present_to_user`
   emphasized footer using `<JsonRefTree>`), `invoke_result.tsx`,
   `invoke_error.tsx`, `callback_notification.tsx`
   (special-cases `workstation_inbox` as a "User said" card).
   `src/load_effect.ts` carries an inline copy of
   `makeLoadResultAtom` to avoid depending on `@fxtr-ui/reports`.
10. ✅ **Lookup maps + final wiring.** `src/lookup_maps.ts`
    exports `AGENT_PLUGIN_TYPE_IDS` (the five hand-mirrored
    Python type-id strings), `agentPluginFullPanelServiceMap`,
    `agentPluginStreamEntryEntityMap`. `src/index.ts`
    re-exports the public surface. Updated
    `frontend_lib/CODE_OVERVIEW.md` — added the package to the
    layout listing (grouped with the renderer family next to
    `@fxtr-ui/reports`) and a new dedicated section. Workspace
    `pnpm check` (tsc --build) passes cleanly.

### Chunk D — Demo at `projects/fxtr_workstation_demo/`

11. ✅ **Workspace yaml + scaffold.** Added
    `../../projects/fxtr_workstation_demo/frontend` to
    `pnpm-workspace.yaml`. Scaffolded the demo directory:
    `frontend/`, `agent_workdir/`, plus a top-level `.gitignore`
    for `.data/` + `.cache/` + the operator's edited toml.
12. ✅ **Backend.** New `run_demo_workstation.py` — opens a
    `SQLiteFxtrNamespace` at `.data/agent_mcp_store.sqlite` and
    a `PersistenceNamespaceConfig` with
    `mode="preserve_provenance"` under external name
    `agent_mcp_store`. Workstation boots on port 8642 with the
    sandbox spawner + HTTPS passthrough + optional LLM
    authorizers (via `--allow-llm-apis`) as external services.
    Emits `TYPE_IDS <json>` (the six type-id strings the
    frontend's dispatch registry needs, computed at import time
    from the same Python `define_type` calls the renderer
    library hand-mirrors), then `READY <ws-url>`, then
    `DEBUG_UI <http-url>`. Demo's own `pyproject.toml` declares
    deps (`fxtr2`, `fxtr_workstation`, `fxtr_workstation_mcp`,
    `fxtr_sandboxing`) so `uv run python
    run_demo_workstation.py` resolves cleanly from the demo
    directory. Smoke-tested: backend boots, emits all three
    protocol lines, parks until SIGINT. Drops the demo-pkg
    services from `basic_test_demo` for now — agent_workdir
    starts empty per the brief; we can re-add later.
13. ✅ **Frontend Vite app.** `frontend/` fork from
    `fxtr-ui-workstation-demo`: renamed package to
    `@fxtr-workstation-demo/frontend`, port set to 3470 in
    `vite.config.ts`, `src/server.ts` retargeted at
    `../../run_demo_workstation.py` with a 90s READY timeout
    (cold sandbox boot can take 30–60s). New
    `dispatch_registry.tsx` registers the agent-plugin-renderer's
    pre-built lookup maps on the `FullPanel` (service-side) and
    `StreamEntry` (entity-side) keys; other workstation keys
    fall through to defaults. Module-load assertion checks
    `VITE_DEMO_TYPE_IDS` matches `AGENT_PLUGIN_TYPE_IDS`
    field-by-field — drift between Python labels and the
    hand-mirrored TS constants surfaces immediately with a
    pointed error rather than as silent "no renderer matched"
    fallbacks. Workspace `pnpm install` + `pnpm check` (tsc
    --build) + `vite build` all clean.
14. ✅ **Sample workdir + operator README + start_demo.sh.**
    `agent_workdir/` carries a README (agent-facing setup,
    `present_to_user` starting handhold), a
    `.fxtr_workstation.toml.example` with the right URL +
    `agent_mcp_store` external name, and an empty
    `preamble.py`. Top-level `README.md` documents the operator
    flow: install plugin, `uv sync`, `pnpm install`,
    `./start_demo.sh`, point Claude Code at `agent_workdir/`,
    open `agent_mcp:<session_id>` in the frontend.
    `start_demo.sh` is a thin shell wrapper around `pnpm
    --filter @fxtr-workstation-demo/frontend dev`.
15. ✅ **CODE_OVERVIEW updates + smoke verify.** Added a
    "Consumer-level demos" section to
    `frontend_lib/CODE_OVERVIEW.md` pointing at the new
    `projects/fxtr_workstation_demo/`. Added a complementary
    pointer in
    `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md`
    framing the new demo as the operator-facing one and
    `basic_test_demo/` as the smaller plugin-only fixture.
    Workspace-level `pnpm check` clean; new packages' tests
    pass (4 rd-types-stream + 7 agent-plugin-renderer).

## Sharp edges / open questions

1. **No-duplicate guarantee at subscribe time.** Worth restating:
   the register-then-snapshot order with no `await` between the
   two statements guarantees the worker never notifies with the
   same head we returned. The frontend appends every inbox event
   unconditionally; no dedupe needed. See "Impl change —
   ordering for race-freedom" above for the proof.
2. **Watcher skip-ahead → gaps.** The watcher's contract
   explicitly allows missing intermediate chain heads. Gaps in
   the entries list are normal; "Load more" exists exactly to
   walk them. We don't aggressively pre-fetch gaps — the operator
   pulls them in on demand. Future work could auto-pull when a
   gap is small (e.g. 1–2 entries).
3. **Reattach to a stored panel session.** If the operator's
   browser closed and a workstation-side session for the panel
   survives, reattach hands back the prior `entries` from
   userState. The prior `phase` is also restored — if it was
   `running`, the inbox is preserved and we keep consuming new
   events. If it was `subscribing`, the subscribe slot is in
   userState too (its OpResult survives via the
   preserve-events machinery). The state machine just picks up
   where it left off. No special-case code expected; if reattach
   surfaces a bug we'll find it on `pnpm dev` and address then.
4. **Persistence db lifetime.** `.data/agent_mcp_store.sqlite`
   lives across demo runs (intentional — the log survives
   restarts). Wiping requires `rm -rf .data/` between runs;
   the README documents this. The cap mode is
   `preserve_provenance` so the agent's exported entities round-
   trip cleanly across reconnects.
5. **`send` payload shape on the wire.** The Python protocol
   types `send`'s payload as `FxtrServiceData` — anything the
   wire codec accepts. We pass strings (the user's typed text)
   from the demo; the TS protocol type stays generous. A future
   "rich messages" iteration could send a structured payload
   (e.g. `{kind: "text", body: "..."}`) without protocol churn.
6. **Operator-vs-agent ordering.** `start_demo.sh` brings up the
   workstation + frontend; the operator separately runs Claude
   Code with the MCP plugin pointed at `agent_workdir/`. The
   agent's `connect_workstation` tool creates the
   `agent_mcp:{session_id}` external_name. The operator
   refreshes the workstation shell's sidebar to see the new
   service and opens it. Documented in the operator README.
7. **MCP-supervisor-cycle goes silent.** The backend's watcher
   list resets every MCP supervisor cycle (per
   `agent_mcp_impl.py`'s docstring). After a cycle, the
   workstation may also assign the MCP a fresh service short_id
   under a new `agent_mcp:{session_id}` registration — so the
   already-open panel is pointing at a stale service ref anyway.
   For v1 the behaviour is: the open panel goes silent, the
   operator force-closes and reopens against the new service
   entry. Documented in the operator README. A future revision
   could add session-lifecycle-driven re-subscribe or a
   supervisor-cycle id the panel watches; out of scope here.
8. **Type-id hand-mirroring is temporary.** The renderer
   library hardcodes the four event-payload type-id strings.
   This couples the library to the Python type ids at build
   time. A broader rework of how type ids reach dispatch
   registries is planned in parallel; until then the demo's
   `TYPE_IDS` env-injected check is the defense-in-depth that
   surfaces any drift loudly.
