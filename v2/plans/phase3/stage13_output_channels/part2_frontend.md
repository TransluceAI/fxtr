# Part 2: Frontend — `@fxtr-ui/rd-types-channel`

New package
`projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-rd-types-channel/`
(`@fxtr-ui/rd-types-channel`), modeled on `fxtr-ui-rd-types-stream`
(package layout, key declaration, UUID-stability test). Joins the
existing pnpm workspace automatically via the workspace globs.

Dependencies: `@fxtr-ui/core`, `@fxtr-ui/react-dispatch`,
`@fxtr-ui/state-util`, `@fxtr-ui/workstation-dispatch` (for
`RefLink` / `JsonRefTree` / `RenderDispatch`), `react` peer.

## Wire types (`src/types.ts`)

Mirrors part 1's encoded event shapes as they arrive through a
workstation callback inbox (refs already decoded to stubs by
`unescapeWire`):

```ts
export type ChannelStatusWire = {
  readonly tag: EntityRefStub;
  readonly status: "open" | "complete" | "abort";
};

export type ChannelEventWire =
  | { kind: "snapshot_open"; tag: EntityRefStub; values: readonly ResultValue[] }
  | { kind: "snapshot_complete"; tag: EntityRefStub; result: ResultValue }
  | { kind: "snapshot_abort"; tag: EntityRefStub; reason: string }
  | { kind: "emit"; value: ResultValue }
  | { kind: "complete"; result: ResultValue }
  | { kind: "abort"; reason: string };
```

Plus a pure fold, exported for renderer reuse and unit testing:

```ts
export type ChannelViewState =
  | { phase: "connecting" }
  | { phase: "open"; tag: EntityRefStub; values: readonly ResultValue[] }
  | { phase: "complete"; tag: EntityRefStub; result: ResultValue }
  | { phase: "aborted"; tag: EntityRefStub | null; reason: string };

export function foldChannelEvents(events: readonly ChannelEventWire[]): ChannelViewState;
```

(`complete`/`abort` deltas need the tag carried over from the
snapshot; the fold handles that. Malformed/unknown kinds fold to
`aborted` with a diagnostic reason rather than throwing.)

## Dispatch keys (`src/keys.ts`)

Two new wire-stable UUIDs (uuidgen once, hardcode, snapshot-test like
`rd-types-stream` does):

```ts
export interface ChannelContentCardProps {
  /** The channel-storage service to subscribe to. */
  readonly channel: ServiceRefStub;
  /** Per-panel persistent storage carved out by the dispatching renderer. */
  readonly ownStorage: FxtrSessionAtom<unknown>;
}
export const ChannelContentCard: DispatchKey<ChannelContentCardProps, "entity"> =
  declareEntityDispatchKey<ChannelContentCardProps>({ ... });

export interface ChannelStatusPreviewProps {
  /** Pre-fetched status (the dispatching renderer already invoked status()). */
  readonly status: ChannelStatusWire;
  readonly channel: ServiceRefStub;
}
export const ChannelStatusPreview: DispatchKey<ChannelStatusPreviewProps, "entity"> =
  declareEntityDispatchKey<ChannelStatusPreviewProps>({ ... });
```

Both dispatch on the **tag entity stub** (that's the whole point —
per-stream-type customization keys off the tag's type); the storage
service ref rides in as a prop. Entity-only keys: the dispatched stub
is always the tag.

`ownStorage` on the card props: the channel-storage FullPanel renderer
(below) owns the panel's `ownStorage` and passes a `subAtom`-carved
slot down, so a custom card renderer can persist its subscription
slots the same way any panel renderer does.

### Fallbacks

- `ChannelContentCard.NoMatch` — **the generic stream card** (the
  sketch's default renderer), not an error state:
  - header: `<RefLink stub={tag} as="block" />`
  - subscribes to `channel` (pattern below), folds with
    `foldChannelEvents`
  - `open`: scrolling list of message rows, each value rendered via
    `JsonRefTree` (refs become reflinks)
  - `complete`: drop the rows, show `<RefLink stub={resultRef}>` when
    the result is/contains a ref, else `JsonRefTree` of the value
  - `aborted`: reason banner
- `ChannelContentCard.Loading` — lightweight placeholder card.
- `ChannelStatusPreview.NoMatch` — status pill: tag inline reflink +
  open/complete/abort badge.
- `Error` — `castErrorFallback()` both.

The generic card is exported as a named component too
(`GenericChannelCard`) so custom renderers can compose it.

## Subscription pattern (inside the generic card / custom cards)

Exactly the `simple_scheduler` renderer's pattern
(`agent_workdir/renderers/simple_scheduler/index.tsx`), against the
storage service:

```ts
// versioned ownStorage shape
{ kind: "channel-card/v1", inbox: CallbackInboxStub<ChannelEventWire> | null,
  subInvoke: OpResultStub<unknown> | null }

// on mount, once:
const inbox = ctx.allocateCallback<ChannelEventWire>();
const subInvoke = ctx.allocateInvoke(inbox.handle, "subscribe_to", [channel]);
```

`subscribe_to` resolves the storage as an `EventSource` and calls its
wire-compatible `subscribe` — no workstation backend changes needed.
First inbox event is the snapshot (seeded), then deltas. Closing the
panel closes the inbox which unsubscribes server-side.

Provide this as a small hook so cards don't copy-paste it:
`useChannelSubscription(channel, storageSlot): ChannelViewState`.

## Channel-storage service renderers (`src/storage_renderers.tsx`)

Renderers registered (in part 3's preset) for the **fixed**
channel-storage service type id, covering the standard workstation
keys:

- **FullPanel** — state machine in `ownStorage` (versioned shape
  `{kind: "channel-storage-panel/v1", statusInvoke, cardStorage}`):
  1. `ctx.allocateInvoke(stub, "status", [])` **once**, when the
     storage slot is fresh (null / failed validity check). The
     filled invoke result persists in `ownStorage` and is *read*
     on later mounts — no per-mount re-invoke. The tag is the only
     thing the panel needs from it, and a channel's tag never
     changes; live open/complete state comes from the card's own
     subscription, so a stale `status` here is harmless. (Getting
     *notified* of status changes is a real gap for the preview
     keys — tracked in [followups.md](followups.md).)
  2. on fill: render
     `<RenderDispatch stub={status.tag} as={ChannelContentCard}
     channel={stub} ownStorage={carvedSlot} />` where `carvedSlot`
     is `subAtom`-carved out of the panel's own storage shape
     (`cardStorage`).
  This is the tag-type double dispatch (overview decision 5).
- **BlockLink / InlineLink / HoverPreview** — invoke `status()` via
  the dispatch-entry shared state (link keys receive it even though
  they're display-style), same once-per-shared-state-lifetime
  semantics as the panel, then
  `<RenderDispatch stub={status.tag} as={ChannelStatusPreview}
  status={...} channel={stub} />`. While the status invoke is
  pending, render a neutral "channel…" pill. One shared
  fetch-then-redispatch wrapper serves all three keys; the
  dispatched preview gets no hint about which display style it is
  standing in for (easy to add later — note in followups.md).

These live in this package (framework code); the demo's existing
`renderers/builtin/` slice re-exports them (below) — same split as
its `@fxtr-ui/agent-plugin-renderer` re-export.

## Module exports

`src/index.ts`: keys, props types, wire types, `foldChannelEvents`,
`GenericChannelCard`, `useChannelSubscription`, and the storage
renderers as a named record keyed by dispatch-key uuid —
`channelStorageRenderers: {[FullPanel.uuid]: ..., [BlockLink.uuid]:
..., [InlineLink.uuid]: ..., [HoverPreview.uuid]: ...}`. No
`default` export from the package itself: the subtype key is the
consuming renderer slice's choice; it wraps the record as
`export default {channel_storage: channelStorageRenderers}` (the
`fxtr_renderer.nix` slice contract:
`{subtype_key: {dispatch_key_uuid: Component}}`).

## Demo builtin-slice registration

Extend the demo's existing builtin renderer slice
(`examples/fxtr_workstation_demo/agent_workdir/renderers/builtin/`)
so the storage renderers are registered for the fixed
channel-storage service type. Actually *producing* a channel in the
demo is part 3; this just gives the renderers a home and proves the
routing end-to-end-compilable.

- `nix/fxtr_lib_type_ids/src/fxtr_lib_type_ids/__init__.py` —
  re-export `CHANNEL_STORAGE_TYPE_ID` from `fxtr2.common.streams`.
  Unlike the existing constants this is a direct import, not a
  mirrored `define_type` re-declaration: the canonical definition
  already lives in fxtr2, the stub package's only dependency (an
  editable path-dep on `../../v2`), so there is nothing to drift.
- `nix/lib/entities.nix` — add
  `channelStorage = forType
  "dor36ohjfhtt4ggskqtwqvy4mowpax4nnsihr4xvrohpdbqkwwxja"
  "CHANNEL_STORAGE_TYPE_ID" "channel_storage";` (idString literal
  verified against the canonical define_type; the dispatcher's
  eager validation re-asserts it at build time).
- `renderers/builtin/index.tsx` — import
  `channelStorageRenderers` from `@fxtr-ui/rd-types-channel`, add
  `channel_storage: channelStorageRenderers` to the default export
  map.
- `renderers/builtin/fxtr_renderer.nix` — add
  `"@fxtr-ui/rd-types-channel"` to the slice's extended
  `externals` list, and a `channel_storage` subtype entry with
  `forType = lib.entities.channelStorage; forDispatchKeys =
  shellKeys;`.

## Externals wiring

Renderer modules loaded by the dynamic dispatcher import this package
as an external, so every host app that may load a channel renderer
adds it to `installFxtrExternals`. Wired in part 2 (the builtin
slice now imports the package, so the demo host needs the mapping
immediately):

- `examples/fxtr_workstation_demo/frontend/src/client/main.tsx`
- The `fxtr-ui-workstation-demo` / `fxtr-ui-dynamic-dispatch-demo`
  dev apps: skipped unless they turn out useful for iteration.

## Tests (vitest, `tests/`)

1. **UUID stability snapshot** for both keys (rd-types-stream
   pattern).
2. **`foldChannelEvents`** — snapshot-then-deltas, snapshot_complete
   only, abort mid-stream, complete after emits drops values,
   unknown-kind tolerance.
3. **Generic card render smoke** — feed a fake inbox atom with a
   scripted event sequence, assert the three phases render (values
   list → result reflink → abort banner). Mock at the
   `FxtrSessionAtom` level like `sub_atom.test.ts` does rather than a
   live workstation. Render-layer specifics:
   - `@vitest-environment jsdom` + `@testing-library/react` devDeps,
     like `fxtr-ui-react-tests`. (Colocating jsdom `.tsx` tests in
     the package's own `tests/` works now — the old sibling-package
     workaround documented in `fxtr-ui-react-tests/README.md` was a
     pre-reorg path issue; those packages run fine from
     `frontend_lib/packages/` today, verified.)
   - The card renders `RefLink` / `JsonRefTree`, whose default impl
     goes through `RenderDispatch` → `useDispatcher()` (throws
     outside a provider) — wrap test renders in a
     `RefLinkImplContext.Provider` with a trivial impl that prints
     the stub's shortId.
   - Local `_mocks.ts` (a scriptable `CallbackInboxStub` subclass +
     mock session atom dispensing it, modeled on
     `react-dispatch/tests/_mocks.ts` — test files aren't importable
     across packages).

Run from repo root: `pnpm exec vitest run
projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-rd-types-channel`.

## Followups doc

Create `followups.md` in this stage directory (tracking items that
came out of part 2 design review, not part of this change):

- **Status subscription.** Previews/panels currently fetch `status()`
  once and never learn about later transitions. We may want a way to
  subscribe to status changes — could be a separate subscribe-style
  method on the storage protocol, but the workstation's
  `subscribe_to` callback-target invoke would then need a way to
  parameterize its target method. Out of scope for stage 13.
- **Display-style hint for `ChannelStatusPreview`.** All three link
  keys re-dispatch the same preview with no hint about which display
  style invoked it; an optional prop can be added compatibly later.

## Implementation steps

- [x] Package scaffold (`package.json`, `tsconfig.json`) + `pnpm
      install` to link it (also added to the root `tsconfig.json`
      references so `pnpm check` covers it — and added the missing
      `fxtr-ui-rd-types-stream` reference as a drive-by)
- [x] `src/types.ts` (wire types + `foldChannelEvents`) + fold unit
      tests
- [x] `src/keys.ts` (two keys, fresh uuidgen'd UUIDs) +
      `src/fallbacks.tsx` (generic stream card as
      `ChannelContentCard.NoMatch`, status pill as
      `ChannelStatusPreview.NoMatch`) + `useChannelSubscription`
      (in `src/subscription.ts`) + UUID stability tests
- [x] `src/storage_renderers.tsx` + `channelStorageRenderers` record
      + `src/index.ts`
- [x] Generic card render smoke test
- [x] Demo builtin-slice registration (type-ids stub re-export,
      `entities.nix`, builtin `index.tsx` + `fxtr_renderer.nix`)
- [x] Externals: demo `main.tsx` `installFxtrExternals` entry +
      `@fxtr-ui/rd-types-channel` dep in the demo frontend's
      `package.json` (also fixed main.tsx's stale pointer to the
      removed Python-side `DEMO_EXTERNALS` list — compile-side
      externals live in nix now)
- [x] `followups.md`
- [x] Update `frontend_lib/CODE_OVERVIEW.md` (new package section +
      package-layout listing; also fix the stale
      `v2/ts/packages/*-tests` pointers — the sibling test packages
      live under `frontend_lib/packages/` since the reorg) + the two
      demo README "builtin covers …" summaries
