# Stage 11 — Special-node fixes for workstation dispatch

Three small, related fixes to how the workstation frontend handles
non-loadable / system-defined fxtr nodes:

1. **Treat `c`-tag (context provenance) shortIds as builtins** in the
   static entity dispatcher so we don't try to load them. They route
   to the same `resolved-builtin` terminal phase that `.` / `!` already
   use. `c`-nodes are a special built-in node type that users can't
   override directly — we lump them under "builtin" rather than
   introducing a new category.

2. **Give the `FullPanel` dispatch key a real `Builtin` fallback**
   that handles `c` / `!` / `.` stubs without a load — by issuing a
   metadata call and surfacing the populated metadata fields (most
   importantly `context_data` for c-nodes). `BlockLink` /
   `InlineLink` / `HoverPreview` already have load-free fallbacks and
   don't need a separate `Builtin`; their builtin path falls through
   to NoMatch as today.

3. **Add a `!basic_type` `BlockLink` + `InlineLink` renderer to both
   workstation demos** that displays the ref as a colored badge with
   text like `type {type_name}`. Mirrored across
   `frontend_lib/packages/fxtr-ui-workstation-demo` and the
   consumer-level `projects/fxtr_workstation_demo`.

## Background

- `staticEntityTypeDispatch` in
  `frontend_lib/packages/fxtr-ui-react-dispatch/src/static_entity_registry.ts`
  already short-circuits to a terminal `resolved-builtin` phase for
  stubs whose shortId starts with `.` or `!`. The phase renders the
  key's `fallbacks.Builtin ?? fallbacks.NoMatch`. The recognizer is
  `isBuiltinEntityShortId(shortId)`.
- The four workstation dispatch keys
  (`@fxtr-ui/workstation-dispatch/src/dispatch/keys.ts`) currently
  declare only `Loading` / `NoMatch` / `Error` — no `Builtin`. Today
  that means builtin stubs fall through to `NoMatch`, and for
  `FullPanel` specifically the `NoMatch` is `EntityFullPanelFallback`
  which unconditionally `allocateLoad`s the stub — wrong for c / ! /
  . nodes.
- `EntityMetadata.context_data` (defined in
  `@fxtr-ui/core/src/metadata.ts`) carries the ref of the c-node's
  internal context-data d-node when the targeted entity has tag `c`.
  The workstation server populates this field on the wire (see
  `fxtr_workstation/state.py`'s metadata route). `allocateGetEntityMetadata`
  does not require the entity to be loadable.
- `FxtrBasicType` (defined in `v2/fxtr2/data/system_entities.py`)
  carries `{namespace_uuid: UUID, type_name: str}`. Its type-id
  string is `!basic_type` (the builtin name `basic_type`). User
  entity types like `_DemoNote`, `_Note`, etc. are minted via
  `define_type(namespace_uuid, "demo_note")` which stores a
  `FxtrBasicType` and returns its EntityRef; that ref is the value of
  `$fxtr.type` on each instance.

## Changes

### Part A — Route c-nodes to the builtin path

File: `frontend_lib/packages/fxtr-ui-react-dispatch/src/static_entity_registry.ts`

- Extend `isBuiltinEntityShortId` to also return `true` for
  `shortId.startsWith("c")`. Update the docstring above it to note
  that `c` is included because c-tag nodes are a system-defined
  context-provenance form that users can't replace with their own
  renderer — they share the unloadable-builtin treatment.
- No other state-machine changes — the existing `init` →
  `resolved-builtin` arm handles `c`-nodes the same way as `.` / `!`.

Tests (sibling tests file
`frontend_lib/packages/fxtr-ui-react-dispatch/tests/static_entity_registry.test.ts`):

- Add a case: stub with shortId starting with `c…` is short-circuited
  to `resolved-builtin` without invoking `allocateLoad`. Existing
  cases for `.` and `!` already cover the rendering arm.

### Part B — `Builtin` fallback for the `FullPanel` key

New file:
`frontend_lib/packages/fxtr-ui-workstation-dispatch/src/fallbacks/EntityFullPanelBuiltinFallback.tsx`

A `DispatchedComponent<EntityRefStub, FullPanelProps>` that:

- Owns a `FxtrSessionAtom<{metadata: OpResultStub<EntityMetadata> | null}>`
  lifted from `ownStorage` via `atomWithDefault` (mirroring the load
  flow in `EntityFullPanelFallback`, but `metadata` slot instead of
  `load`).
- On first mount, calls `setState((prev, ctx) => ({metadata: prev.metadata ?? ctx.allocateGetEntityMetadata(stub)}))`.
- Renders:
  - a header section with the `kind` (derived from `stub.shortId[0]`:
    `c` → "context provenance node", `!` → "builtin", `.` → "anonymous
    node") and the full shortId as monospace text.
  - a "Metadata" section that, once the result fills, renders the
    populated fields. Reuse the existing `MetadataFields` /
    `ResultGate` helpers from `EntityMetadataView.tsx` (extract them
    into a small `metadata/metadata_fields.tsx` so both views can
    share). Ref-typed fields render as `<RefLink as="inline">`;
    `id_string` and the `*_id_string` siblings render as monospace
    spans. For c-nodes the user will see the `context_data` row as
    a clickable inline link — that's the main user-visible payoff.
  - An "Errored" arm for the metadata stub (rare; some node IDs may
    have no server-side record).

Pseudocode shape:

```tsx
const stored = useAtomValue(typedStorage);
const set = useSetAtom(typedStorage);
useEffect(() => {
  set((prev, ctx) => prev.metadata ? prev : {metadata: ctx.allocateGetEntityMetadata(stub)});
}, [stub, set]);
return (
  <div className="fw-panel__body">
    <header>{kindLabel(stub.shortId)} · <code>{stub.shortId}</code></header>
    <Section title="Metadata">
      {stored.metadata
        ? <MetadataFromStub stub={stored.metadata}/>
        : <Muted>allocating…</Muted>}
    </Section>
  </div>
);
```

File: `frontend_lib/packages/fxtr-ui-workstation-dispatch/src/dispatch/combined_fallbacks.tsx`

- Add `FullPanelBuiltin(props)` that branches on `stub instanceof
  EntityRefStub` and forwards to `EntityFullPanelBuiltinFallback`. The
  `ServiceRefStub` branch is unreachable (services have no
  unloadable-tag convention; see `static_service_registry.ts` —
  there's no builtin arm), so it can render `null` with a debug
  warning, or simply throw — pick whichever matches the package's
  existing "shouldn't-happen" convention.

File: `frontend_lib/packages/fxtr-ui-workstation-dispatch/src/dispatch/keys.ts`

- Add `Builtin: FullPanelBuiltin` to the `FullPanel` key's fallbacks
  bundle. Leave `BlockLink` / `InlineLink` / `HoverPreview` alone —
  their existing `NoMatch` fallbacks already render just `shortId`
  without loading, which is the right behavior for c / ! / . stubs.

File: `frontend_lib/packages/fxtr-ui-workstation-dispatch/src/metadata/EntityMetadataView.tsx`

- Refactor the inline `MetadataFields` component into a separate
  module (`metadata/metadata_fields.tsx`) so it can be reused by the
  new builtin fallback. No behavior change; only extraction.

Tests:

- `frontend_lib/packages/fxtr-ui-workstation-dispatch/tests/keys.test.ts`
  — update the UUID/shape snapshot if the test asserts the fallback
  bundle shape; the `FullPanel` key now has a `Builtin` field.
- Render-layer tests live in
  `v2/ts/packages/fxtr-ui-workstation-dispatch-tests/`. Add a smoke
  test that renders `EntityFullPanelBuiltinFallback` against a mock
  session ctx (existing test mocks have `allocateGetEntityMetadata`
  queues), asserts no load is allocated, and that the rendered tree
  surfaces a `<RefLink>` for `context_data` when the queued metadata
  result includes one.

### Part C — `!basic_type` BlockLink + InlineLink renderer in `@fxtr-ui/workstation`

The `!basic_type` id_string is the type-id-string of the
`FxtrBasicType` builtin. To render a ref to a `FxtrBasicType` entity
as a badge, we register an entity-side `BlockLink` + `InlineLink`
handler keyed by that id_string. When the static dispatcher's state
machine resolves the entity's `$fxtr.type` metadata, it'll route to
our component.

**Where it lives.** Put the components in the shell package
`@fxtr-ui/workstation` (`frontend_lib/packages/fxtr-ui-workstation/`)
rather than duplicating across demos. A `FxtrBasicType` ref is not
demo-specific — it's the universal type-of-entity ref — so the badge
belongs alongside the shell's other workstation-wide primitives.
Proposed path:

```
frontend_lib/packages/fxtr-ui-workstation/src/system_renderers/
├── BasicTypeBadge.tsx       # both components + shared badge styling
└── index.ts                 # re-exports the two components + the pre-baked
                             # entity-link lookup map entries
```

A `system_renderers/` subdirectory leaves room for future renderers
of other system-defined entity types (e.g. `FallbackContextMarker`)
without polluting the existing `reflink/` namespace, which is about
the link *infrastructure* (drag, hover, click) rather than per-type
renderers.

Component shape — a shared internal `BasicTypeBadge({stub, variant:
"block" | "inline", linkText?})` plus two thin
`DispatchedComponent<EntityRefStub, LinkProps>` exports
(`BasicTypeBlockLink`, `BasicTypeInlineLink`) that delegate with the
right variant:

- `useDispatchSharedAtom<{load: OpResultStub<FxtrBasicTypeValue> | null}>({load: null})`
  on mount; sets `load = ctx.allocateLoad(stub)` on first effect run
  (mirrors the `useEntityLoad` pattern used by the demo's existing
  entity cards).
- Reads `useResult(load)`; renders:
  - pending → base styling with `linkText ?? "type …"` placeholder.
  - errored → base styling with `linkText ?? "type ?"` and the
    error message as a `title` attribute.
  - filled → `type {value.type_name}` (or `linkText` if set).
- Styled as a colored badge (accent background, pill-shaped,
  monospace `type_name`); `inline` variant uses smaller padding to
  fit inside prose / JsonTree without disturbing line height,
  `block` variant matches the chrome of `EntityBlockLinkFallback`.

Public surface (added to
`frontend_lib/packages/fxtr-ui-workstation/src/index.ts`):

- `BasicTypeBlockLink`, `BasicTypeInlineLink` — the two
  `DispatchedComponent`s, ready to drop into a lookup map.
- `BASIC_TYPE_ID_STRING` (= `"!basic_type"`) — the id_string the
  static dispatcher will look up, exported as a named constant so
  demos don't hardcode the literal.

Loaded value shape (TS mirror of `FxtrBasicType`):

```ts
interface FxtrBasicTypeValue {
  readonly namespace_uuid: string;  // mashumaro encodes UUIDs as strings
  readonly type_name: string;
}
```

(`namespace_uuid` isn't displayed; we just need to know the field
exists so the TS interface matches the wire shape — useful if a
future variant wants to color the badge per namespace.)

Registration in demos:

- `frontend_lib/packages/fxtr-ui-workstation-demo/src/client/dispatch_registry.tsx`
  — add `[BASIC_TYPE_ID_STRING, BasicTypeBlockLink]` to
  `entityBlockLink` and `[BASIC_TYPE_ID_STRING, BasicTypeInlineLink]`
  to `entityInlineLink`. Imports come from `@fxtr-ui/workstation`.
- `projects/fxtr_workstation_demo/frontend/src/client/dispatch_registry.tsx`
  — drop the `EMPTY_ENTITY_BLOCK_LINK_MAP` /
  `EMPTY_ENTITY_INLINE_LINK_MAP` literals in favor of real maps
  containing the same two `BASIC_TYPE_ID_STRING` entries.

## Implementation steps

- [x] **Part A** — extend `isBuiltinEntityShortId` to include `c`;
      update docstring. Add unit test case for `c…` shortId. Run
      `pnpm -F @fxtr-ui/react-dispatch test`.
- [x] **Part B (1/4)** — extract `MetadataFields` from
      `EntityMetadataView.tsx` into `metadata/metadata_fields.tsx`;
      re-import in `EntityMetadataView.tsx`. Confirm
      `pnpm -F @fxtr-ui/workstation-dispatch check` clean.
- [x] **Part B (2/4)** — write
      `fallbacks/EntityFullPanelBuiltinFallback.tsx` using the
      extracted `MetadataFields`.
- [x] **Part B (3/4)** — add `FullPanelBuiltin` wrapper in
      `dispatch/combined_fallbacks.tsx`; wire `Builtin:
      FullPanelBuiltin` onto the `FullPanel` key in
      `dispatch/keys.ts`.
- [x] Update `CODE_OVERVIEW.md` to reflect (1) the extended c-node
      handling in the dispatch's builtin path, and (2) the new
      `Builtin` fallback on `FullPanel`. Update
      `frontend_lib/packages/fxtr-ui-workstation-dispatch/LOCAL_CODE_OVERVIEW.md`
      if it lists fallback files.
- [x] **Part B (4/4)** — render-layer test for
      `EntityFullPanelBuiltinFallback` in
      `v2/ts/packages/fxtr-ui-workstation-dispatch-tests/`.
- [x] **Part C (1/4)** — add
      `frontend_lib/packages/fxtr-ui-workstation/src/system_renderers/BasicTypeBadge.tsx`
      with the shared `BasicTypeBadge` internal + `BasicTypeBlockLink`
      / `BasicTypeInlineLink` `DispatchedComponent` exports, plus
      `system_renderers/index.ts` re-exporting them and
      `BASIC_TYPE_ID_STRING`.
- [x] **Part C (2/4)** — export `BasicTypeBlockLink`,
      `BasicTypeInlineLink`, `BASIC_TYPE_ID_STRING` from
      `frontend_lib/packages/fxtr-ui-workstation/src/index.ts`.
- [x] **Part C (3/4) — lib demo wiring** — in
      `frontend_lib/packages/fxtr-ui-workstation-demo/src/client/dispatch_registry.tsx`,
      import `BasicTypeBlockLink`, `BasicTypeInlineLink`,
      `BASIC_TYPE_ID_STRING` from `@fxtr-ui/workstation`; add entries
      to `entityBlockLink` and `entityInlineLink`. Launch via
      `pnpm dev` and visually confirm that the `$fxtr.type` ref on
      a demo note renders as a "type demo_note" badge inline / in
      the roots panel.
- [x] **Part C (4/4) — consumer demo wiring** — in
      `projects/fxtr_workstation_demo/frontend/src/client/dispatch_registry.tsx`,
      replace `EMPTY_ENTITY_BLOCK_LINK_MAP` /
      `EMPTY_ENTITY_INLINE_LINK_MAP` with real maps containing the
      `BASIC_TYPE_ID_STRING` entries. Smoke-test by following the
      demo's local README.
- [x] Update `CODE_OVERVIEW.md` for `@fxtr-ui/workstation` (mention
      the new `system_renderers/` directory) and add a short note
      to both demo packages' overviews where the basic_type renderer
      is registered. If
      `frontend_lib/packages/fxtr-ui-workstation/LOCAL_CODE_OVERVIEW.md`
      lists subdirectories, add `system_renderers/` there too.

## Open questions / follow-ups

- **`Builtin` fallbacks on the other three keys** — we leave them
  unset (falling through to NoMatch) because the existing NoMatch
  fallbacks for `BlockLink` / `InlineLink` / `HoverPreview` are all
  load-free shortId displays. If a future renderer author wires up a
  loading NoMatch for these keys, we'll need to revisit and add an
  explicit `Builtin` that preserves the shortId-only behavior.
- **`u` / `p` nodes** — these can carry payloads but their handling
  is currently fine (they load through the normal path). Out of scope
  for this stage; flag for revisit if we hit lookup failures.
