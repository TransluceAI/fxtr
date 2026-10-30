# Part 2: Frontend — `@fxtr-ui/rd-types-channel`

Rework the frontend channel package for the reader-based wire shapes. The redesign is
organized around one idea: **render any channel given only its reader ref**, with no
knowledge of the consumer's message schema. Depends on part 1's wire forms + the
**core 1e** `subscribe_debug_subchannels` feed; testable with vitest + mocks. The
`stream`→`channel` rename is applied to any remaining stream-named symbols.

Package: `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-rd-types-channel/`.

## The model (why the package looks the way it does)

In the new system a channel is fronted **only by a reader** (`CHANNEL_READER_TYPE_ID`).
Crucially, **the producer that spawns a subchannel does not know its tag** — the
*task* starts the channel (calls `start(tag, …)`), so a channel message can carry only
the subchannel **key** (a string), never the tag and never a service ref. Therefore:

- The **tag** — needed to pick the per-channel-type renderer — comes from exactly one
  place: `get_tag()` on the opened reader (which blocks server-side until `start`, and
  *raises* for abort-before-start).
- The package **never interprets message contents.** It hands the folded `values` to a
  card; recognizing "this field is a subchannel key" is an app convention living in the
  app's custom cards (parts 3–4), or — for the schema-blind generic fallback — comes
  from the structural `subscribe_debug_subchannels` feed (core 1e).

This collapses everything to a uniform recursion: *a channel is rendered from its
reader; a subchannel is just a reader you open by key and render the same way.*

## What stays the same

- **Two-level dispatch.** A fixed channel **reader service type** → "this is a channel"
  → re-dispatch the per-channel-type card on the **tag's entity type**; the generic
  no-match card is the fallback. The bridge between the two levels is now `get_tag()`.
- The `useChannelSubscription` inbox + callback-target-invoke mechanism (allocate an
  inbox, `subscribe_to(source, method)` on the inbox handle). The only change is the
  **method name** passed as the second `subscribe_to` arg — `"subscribe_full"` for the
  event stream, `"subscribe_state"` for the lifecycle stream, and
  `"subscribe_debug_subchannels"` for the debug feed. The workstation inbox + connectors
  already forward `args[1]` as the subscribe method (confirmed in `callback_impl.py`), so
  this needs **no** workstation change — only the frontend supplying the name.

## The generic surface

### 1. `makeInvokeResultAtom` (new, in `@fxtr-ui/state-util`)

The dual of `makeLoadResultAtom`: drive a one-shot invoke slot forward + observe its
result as a `LoadResult<T>` (`loading | filled | errored`). Same `atomEffect` shape,
`ctx.allocateInvoke(svc, method, args)` instead of `ctx.allocateLoad(stub)`.

```ts
function makeInvokeResultAtom<T>(
  slotAtom: FxtrSessionAtom<OpResultStub<T> | null>,
  svc: ServiceRefStub, method: string, args: InvokeArgValue[],
): Atom<LoadResult<T>>
```

Reused by `ChannelReaderView` (`get_tag`) and `useSubchannelReader`
(`open_subchannel_reader`). Lives in state-util because it is broadly useful and keeps
the channel package thin; `LoadResult<T>` already lives there.

### 2. `ChannelReaderView` — render a channel from its reader (the reusable unit)

A plain exported component (not a dispatch key — there is no standard "card" dispatch
key, and a plain component is the simplest reusable unit). Given `{reader, ownStorage}`:

- `makeInvokeResultAtom(getTagSlot, reader, "get_tag", [])` → the tag stub.
- while `loading` → "starting…" placeholder (channel spawned but not yet `start`ed);
  while `errored` → treat as aborted-before-start (render a terminal note).
- on `filled` → `<RenderDispatch stub={tag} as={ChannelContentCard} channel={reader}
  ownStorage={cardSlot}/>`.

This is the **only** place `get_tag` is needed for the full view. Both the service-type
`FullPanel` and `SubchannelView` render through it.

### 3. Service-type renderers (keyed on `CHANNEL_READER_TYPE_ID`)

Rename `storage_renderers.tsx` → `reader_renderers.tsx`. The service the panel/preview
receive is now a **reader** ref.

- **FullPanel** = panel chrome wrapping `ChannelReaderView` (carves a `subAtom` slot
  for the view out of its `ownStorage`).
- **Compact keys** (BlockLink / InlineLink / HoverPreview) → `ChannelStatusPreview`
  redispatch, now driven **live** (see §5): `get_tag` (best-effort, for the tag +
  reflink) + `useChannelStateSubscription` (primary lifecycle signal), both off the
  `useDispatchSharedAtom` slot.

### 4. `ChannelContentCard` + `useChannelSubscription` + `foldChannelUpdates`

`ChannelContentCard` (tag-entity-dispatched, role unchanged) receives `{stub: tag,
channel: reader, ownStorage}`. It does **not** call `get_tag` (already resolved upstream
and handed in as `stub`); it subscribes via `useChannelSubscription(reader, slot)` →
`subscribe_full`, folds, renders. NoMatch = `GenericChannelCard`.

`foldChannelEvents` → `foldChannelUpdates`:
- `update` appends a **batch** (`...values, ...ev.events`) and threads the current
  `status` into the view state.
- `replace_history: true` on an `update` **resets** accumulated `values` to `ev.events`
  (live history-condense), then continues; the generic card honors it (drops old log).
- carry `status` into `ChannelViewState`; keep the `connecting`/`open`/`complete`/
  `aborted` phases. **The tag is not in the fold** — the renderer holds it (from
  `get_tag`), so view state drops the `tag` field; `GenericChannelCard` already uses
  `props.stub` for its header reflink.
- snapshots seed `values`/`result`/`reason` + `status`.

### 5. `useChannelStateSubscription` + live previews

The cheap lifecycle-only view shared by the compact preview keys (and, later, part 5's
badge). Sibling of `useChannelSubscription`:

- allocate inbox + `subscribe_to(reader, "subscribe_state")`; the inbox carries
  `ChannelStateEventWire` (`ChannelState{status, kind}` | `ChannelAbortedBeforeStart`).
- fold = trivial latest-wins → the current `ChannelState`-ish view (`status`, lifecycle
  `kind`, or aborted-before-start).

The compact keys combine it with the best-effort `get_tag`: `subscribe_state` is the
**primary** signal (drives the pill even pre-start / for abort-before-start), and
`get_tag` only enables the richer per-type `ChannelStatusPreview` dispatch + the tag
reflink once it fills. No `peek_state` on the frontend at all.

### 6. `useSubchannelReader` + `SubchannelView` (the recursion)

```ts
function useSubchannelReader(
  parentReader: ServiceRefStub, key: string, slotAtom: FxtrSessionAtom<unknown>,
): LoadResult<ServiceRefStub>   // open_subchannel_reader(key) via makeInvokeResultAtom
```

The persisted invoke slot is what **keeps the child reader service alive** server-side
(verified: holding the `OpResultStub` in user-state drives the connector's
`PreserveEventsOp`/refcount machinery, pinning the returned service; dropping it
releases it; a parent finish/abort force-tears the child down regardless — so
`useSubchannelReader`'s `errored` state must render a graceful "channel closed" for the
late-open race).

`SubchannelView` is the headline consumer API bundling open + render: given
`{parentReader, channelKey, ownStorage}` → `useSubchannelReader` → on `filled`,
`<ChannelReaderView reader={child} ownStorage={…}/>`. Recursion falls out: card → sees
a key → `SubchannelView` → `ChannelReaderView` → `get_tag` → `ChannelContentCard` →
card → … `useSubchannelReader` stays exported as the lower-level hook.

Per-subchannel storage shape (carved by the consumer from `ownStorage`, **keyed by the
subchannel key string** — stable + per-parent-unique, replacing the old keying by a
channel stub's shortId we no longer have until we open it):
`Record<key, { readerInvoke: OpResultStub<ServiceRefStub> | null; view: unknown }>`,
where `view` is `ChannelReaderView`'s `ownStorage` (which itself holds the `get_tag`
slot + the card storage).

### 7. `GenericChannelCard` (NoMatch) + the debug subchannel list

The schema-blind fallback for an unregistered tag type. Header reflink uses `props.stub`
(the tag). Two sections:

- **(a) message log** — folded `values` (each through `JsonRefTree`, bare refs as
  `RefLink`), the final value on complete, the reason on abort. Honors `replace_history`.
  It does **not** auto-expand subchannels (it can't know which message fields are keys).
- **(b) subchannels** — driven by `useChannelStateSubscription`'s sibling for the debug
  feed: allocate inbox + `subscribe_to(reader, "subscribe_debug_subchannels")` →
  `SubchannelOpenedWire{key, name}[]`. For each entry, **lazily open** the child via
  `useSubchannelReader` and render a `<RefLink stub={childReader} as="block"/>`. Because
  the child is a `channelReader`-typed service, the RefLink routes through the compact
  preview keys (§5) — so each entry shows a **live state pill** (cheap `subscribe_state`,
  no heavy events) and is clickable to open the full `subscribe_full` view. "Somewhat
  lazy": each child is opened + state-subscribed; the heavy stream is pulled only on
  open. (Cost: a wide fan holds N child readers + N state subs — fine at demo scale.)

Custom cards ignore the debug feed and use their own key conventions.

## Wire types (`types.ts`)

Replace `ChannelEventWire` with `ChannelUpdateWire` matching part 1's dataclasses —
**no event carries the tag** (fetched via `get_tag`):
- `snapshot_open`: `{kind, status, values}`
- `snapshot_complete`: `{kind, status, result}`
- `snapshot_abort`: `{kind, status, reason}`
- `update`: `{kind, events: ResultValue[], status, replace_history: boolean}` (batched)
- `complete`: `{kind, result, status}`
- `abort`: `{kind, status, reason}`
- `aborted_before_start`: `{kind, reason}` (also on the state stream)

Plus `ChannelStateWire = {status, kind}` and the
`ChannelStateEventWire = ChannelStateWire | {kind: "aborted_before_start", reason}` union,
and `SubchannelOpenedWire = {key: string; name: string | null}`.

## Dispatch on the reader type id + registration

The builtin slice currently registers `channelStorageRenderers` against
`lib.entities.channelStorage` (`CHANNEL_STORAGE_TYPE_ID`). Switch to
`CHANNEL_READER_TYPE_ID`: rename the `lib.entities.channelStorage` entry to
`channelReader` (id string from part 1's `nix/fxtr_lib_type_ids` re-export of
`CHANNEL_READER_TYPE_ID`), update `nix/lib/entities.nix` + the slice registration + the
package's exported renderer set (`channelStorageRenderers` → `channelReaderRenderers`,
wrapped as `export default { channel_reader: channelReaderRenderers }`).

## Rename sweep

`src/storage_renderers.tsx` → `reader_renderers.tsx`; any `Stream*`/`stream` and
`*Storage*` channel identifiers → `Channel*`/`channel`/`*Reader*`; `keys.ts`,
`index.ts`, `fallbacks.tsx`, `subscription.ts`, `types.ts` updated; mocks + tests
updated.

## Tests (vitest)

- `makeInvokeResultAtom` (in state-util's tests): allocate-on-first-read, filled /
  errored projection, no double-allocate.
- `foldChannelUpdates`: batched `update` appends all `events`; `replace_history` resets
  the log; status threads through; snapshots seed; malformed deltas degrade to
  `aborted` (keep the graceful-degrade behavior).
- `ChannelReaderView`: pending `get_tag` → "starting…"; errored → aborted-before-start
  note; filled → FullPanel/card re-dispatch on the tag entity type; no-match → generic
  card.
- state subscription + live previews: `subscribe_state` fold (coalesce/latest-wins,
  abort-before-start); a preview pill driven by state with `get_tag` pending vs filled.
- `useSubchannelReader` / `SubchannelView`: a key → `open_subchannel_reader` invoke →
  child reader stub dispatched through `ChannelReaderView`; errored (parent finished) →
  graceful note.
- debug fallback: `subscribe_debug_subchannels` events → list of `RefLink`s to lazily
  opened child readers (state-pill compact preview); generic card renders log + the
  subchannel list; honors `replace_history` on the log.
- keys/registration: renderers register against `channelReader` type; FullPanel
  re-dispatches `ChannelContentCard` on the tag entity type; no-match fallback renders.

## Implementation steps

- [x] `@fxtr-ui/state-util`: `makeInvokeResultAtom` (new `invoke_result_atom.ts`) + its
      test (4 cases). ✓
- [x] `types.ts`: `ChannelUpdateWire` (batched `update`, `replace_history`, `status`,
      `aborted_before_start`), `ChannelStateWire` / `ChannelStateEventWire`,
      `SubchannelOpenedWire`; dropped `ChannelEventWire` / `ChannelStatusWire`. ✓
- [x] `foldChannelUpdates` + `latestChannelState`; view-state status; honor
      `replace_history`; dropped `tag`. ✓
- [x] `subscription.ts`: three hooks (`useChannelSubscription` → `"subscribe_full"`,
      `useChannelStateSubscription` → `"subscribe_state"`, `useDebugSubchannels` →
      `"subscribe_debug_subchannels"`) over a shared inbox helper. ✓
- [x] `ChannelReaderView` (plain component, `reader_view.tsx`): `get_tag` via
      `makeInvokeResultAtom`, pending/errored placeholders, tag re-dispatch. ✓
- [x] `useSubchannelReader` + `SubchannelView` (`reader_view.tsx`). ✓
- [x] `reader_renderers.tsx` (replaces `storage_renderers.tsx`): reader-type FullPanel
      (chrome + ChannelReaderView) + compact keys (live state previews). ✓
- [x] `keys.ts` / `fallbacks.tsx`: `ChannelContentCard`/`ChannelStatusPreview` props
      (`channel` is a reader; `ChannelStatusPreview` takes the live state);
      `GenericChannelCard` log + debug subchannel list (opened-child `RefLink`s). ✓
- [x] builtin-slice registration → `channel_reader` (`channelReaderRenderers`), update
      `nix/lib/entities.nix` (`channelReader`, id from the `CHANNEL_READER_TYPE_ID`
      re-export) + the demo `builtin/fxtr_renderer.nix` slice. ✓
- [x] rename sweep + mocks/tests; vitest green (88 tests), tsc + biome clean. ✓
- [x] Update CODE_OVERVIEW (`frontend_lib/CODE_OVERVIEW.md` — package one-liner +
      prose section + state-util `makeInvokeResultAtom`). No package README; the
      `v2/CODE_OVERVIEW.md` storage→reader naming is python-side (untouched here). ✓

## Chunking (tentative — revisit when we get here)

Natural checkpoints: **(1)** state-util `makeInvokeResultAtom` + wire types + folds (+
their vitest) — the pure core; **(2)** `ChannelReaderView` + service-type renderers +
subscription hooks; **(3)** `SubchannelView` + the debug-feed generic fallback; **(4)**
registration/nix rename sweep. Likely one `jj pre`/commit per checkpoint; collapse if
small. Only formally pause if the fold or the recursion surprises us.
