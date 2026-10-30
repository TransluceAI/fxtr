# Stage 4 (was duplicate of 3) — Agent plugin frontend tweaks

## Goal

Refine the agent-MCP panel renderer so:

- Every entry block is **collapsible** with a triangle toggle.
- Collapsed entries show a **one-line summary** of what happened
  inline with their header.
- All blocks **start collapsed**, with one special case:
  `script_execution` entries with `present_to_user` ops split into
  two sibling blocks — the main script block (closed) and a
  dedicated "Presented to user" block that **defaults to open**.
- The **send box accepts ref chips**: the user can drag a
  `<RefLink>` into the input, or type `@[shortid]` literally, and
  see it rendered as a styled chip.
- When the user hits Send with at least one chip, the invoke
  payload becomes the dict `{message, attached}` instead of a bare
  string. Stubs in `attached` keep the refs server-side tracked
  for the agent's session.
- Received `workstation_inbox` messages with the new dict shape
  render their `message` text with `@[shortid]` tokens replaced by
  inline `<RefLink>`s pointing at the matching stubs from
  `attached`.

No backend (Python) changes are required — `send(payload)` is
already typed as opaque `FxtrServiceData`. The new dict shape rides
the standard callback-event machinery and lands in the chain as a
normal `CallbackNotificationLogEvent`.

## High-level design

### Layering recap

`@fxtr-ui/agent-plugin-renderer` sits below the shell and can be
loaded dynamically. It depends on `@fxtr-ui/workstation-dispatch`
(externalized for renderer authors) but **not** on
`@fxtr-ui/workstation` (the shell). All new shared affordances
(drag-payload constants, ref-stub resolver context) therefore live
in `@fxtr-ui/workstation-dispatch`; the shell registers concrete
implementations.

### New context: `RefStubResolver`

The agent panel needs two services that today only the connector
exposes:

- Given `(kind, shortId)`, mint the connector-interned
  `EntityRefStub` / `ServiceRefStub` (used by both drag and typed
  paths to assemble the `attached` list).
- Given a bare `shortId`, classify it as entity / service /
  unknown (used by the typed path, since drag carries kind
  explicitly).

We introduce a small context in `@fxtr-ui/workstation-dispatch`:

```ts
interface RefStubResolver {
  entityRef(shortId): EntityRefStub;
  serviceRef(shortId): ServiceRefStub;
  // null = not in the workstation's listEntities / listServices snapshot
  classifyShortId(shortId): "entity" | "service" | null;
}
const RefStubResolverContext = createContext<RefStubResolver | null>(null);
useRefStubResolver(): RefStubResolver | null;  // null outside a provider
```

Mirrors the existing `RefLinkImplContext` pattern: the dispatch
package owns the surface, the shell installs the concrete impl,
renderer modules consume it. Renderers see `null` outside a
shell (tests, headless rendering) and degrade gracefully.

The shell's concrete impl reads
`connector.listEntities()` / `listServices()` (the connector-level
reactive resource lists, already loaded by the shell) for
classification, and uses `connector.entityRef` / `serviceRef` for
interning. It's installed inside `<ShellRefLinkProvider>` (the
single shell-wiring mount near the top of `<WorkstationShell>`).

### Drag primitive migration

Today, `DRAG_MIME_FXTR_REF` and the `PanelViewDragData` shape live
in `@fxtr-ui/workstation/reflink/drag.ts` along with much
heavier shell-internal stuff (LinkDragPayload, breadcrumb trails,
HistoryRoot snapshots). We don't move the whole module — only the
minimum the renderer needs:

- `DRAG_MIME_FXTR_REF` constant.
- A minimal parser `parseRefDragData(dataTransfer): { kind, shortId } | null`
  that pulls the `target` out of either drag-payload variant.

These land in a new `@fxtr-ui/workstation-dispatch/dnd` submodule.
The shell's existing `drag.ts` re-exports `DRAG_MIME_FXTR_REF`
from there for back-compat (its `setDragData` writes to the same
MIME). Shell-internal types (LinkDragPayload, snapshot variant,
HistoryRoot drag data, breadcrumb drag data, trail helpers) stay
in the shell.

The renderer never produces drags — only consumes them — so it
doesn't need `setDragData`.

### Drop-region suppression (vs. dockview's drop overlay)

By default, the shell's `DockviewLayout` accepts any
`DRAG_MIME_FXTR_REF` drag via `api.onUnhandledDragOverEvent` and
shows dockview's "open a panel" target overlay. When the user is
dragging onto the agent panel's send-box, that overlay is wrong —
we want the send-box to highlight and accept the drop instead.

We mirror the [thingwatcher][tw] mechanism almost verbatim:

[tw]: ../../../../thingwatcher/thingwatcher/frontend/lib/thingDropRegion.tsx

- A `RefDropRegionContext` exposes `{ isActive: boolean, increment(),
  decrement() }`. Lives in `@fxtr-ui/workstation-dispatch/dnd`.
- A `<RefDropRegionProvider>` holds a counter and computes
  `isActive = counter > 0`. The shell mounts it near the top of
  the `<WorkstationShell>` tree (alongside `<ShellRefLinkProvider>`).
- A consumer-facing helper — `useRefDropRegion()` returning
  `{ regionProps: { onDragEnter, onDragLeave, onDrop } }` (or a
  `<RefDropRegion>` wrapper component) — wires the bump/drop
  callbacks. The agent panel's send-box editor spreads
  `regionProps` onto its outer wrapper div (or installs the
  handlers as additional CodeMirror dom-event handlers).
- The shell's `DockviewLayout` reads `isActive` via context and
  short-circuits its `onUnhandledDragOverEvent` handler: when
  `isActive`, it does **not** call `e.accept()`, so dockview
  doesn't show its drop overlay. (Read via a ref so we don't have
  to re-subscribe on every counter bump.)

Counter-based tracking matters because nested DOM elements fire
`dragenter`/`dragleave` for every child traversal — a simple
boolean flips on/off rapidly and looks "inactive" mid-drag. The
counter approach (each dragenter +1, each dragleave −1, drop
clears to 0) matches the React-pattern used elsewhere for nested
drag-tracking. We also need an unmount cleanup: if the region
component unmounts while a drag is mid-enter, we must decrement
to avoid leaking an "active" state into the global context.

### Collapsible block primitive

A new `CollapsibleCard` in `entries/shared.tsx` wraps the
`StreamCardHeader` + body in a `<details>`/`<summary>` with:

- A small triangle indicator (CSS-rotated `▶` / `▼`).
- The existing tag + detail row in `<summary>`.
- An **inline one-line summary** that appears only while
  collapsed.
- `defaultOpen` prop (we'll pass `false` for all entries except
  the present_to_user companion block).

The card's body slot holds whatever sections the renderer used to
emit unconditionally (script source, stdout, stderr, ops, JSON
trees…). Open state lives in component-local React state — it
doesn't need to round-trip through session userState.

We use `<details>` rather than a hand-rolled toggle so keyboard
nav and screen-reader semantics come for free; the triangle is a
`<span>` inside `<summary>`, with `list-style: none` to hide the
browser default.

### Per-event one-line summaries

Each renderer computes its summary string. Rough rules:

| event                                          | summary                                                                                                  |
|------------------------------------------------|----------------------------------------------------------------------------------------------------------|
| `script_execution` success                     | `ran (N ops)` — `N = ops.length` excluding any `present_to_user`                                          |
| `script_execution` errored                     | `errored: <first 60 chars of v.error>`                                                                    |
| `script_execution` rejected                    | `rejected: <first 60 chars of v.rejected>`                                                                |
| `invoke_result`                                | `<slot_key>:<slot_n> → <short payload repr>` — primitives by value, refs by shortId, containers by shape |
| `invoke_error`                                 | `<slot_key>:<slot_n>: <first 60 chars of v.message>`                                                      |
| `callback_notification` (`workstation_inbox`)  | first 60 chars of `message` (or stringified payload if non-dict)                                          |
| `callback_notification` (other)                | `<slot_key>:<slot_n>: <short payload repr>`                                                               |

The short-payload-repr helper is a single shared function that
takes a `ResultValue` and returns a short text describing it
("string(42 chars)" / "list[3]" / "<entity e-abc>" / `"42"` etc.).
Implemented in `entries/summarize.ts`.

### `present_to_user` split

The script renderer's outermost element is already a column
(`cardStyle` is flex-column). We change it from emitting one
`<CollapsibleCard>` to emitting **two siblings inside the same
column**:

```
<column>
  <CollapsibleCard defaultOpen={false} title="script ran" summary="ran (3 ops)">
    {script_src, stdout, stderr, error?, rejected?, non-present ops}
  </CollapsibleCard>
  {presentedOps.length > 0
    ? <CollapsibleCard defaultOpen={true} title="presented to user" summary="<N value(s)>" emphasis>
        {presentedOps.map(op => <JsonRefTree value={op.payload}/>)}
      </CollapsibleCard>
    : null}
</column>
```

The row's left-side timestamp is still rendered once by
`StreamEntryRow` (panel-level); the right-side body just holds
the column with one or two blocks. No changes to row layout.

The `emphasis` prop on `CollapsibleCard` adds the existing accent
border / accent-color header treatment that `presentedBlockStyle`
applies today.

### Ref-chip editor (CodeMirror 6)

We replace the `<textarea>` in `send_box.tsx` with a CodeMirror 6
editor following clydra's pattern but with our own conventions.

**Source-of-truth text.** The editor's document is plain text,
with literal `@[shortid]` tokens marking refs. Copy / paste / undo
all operate on the text directly. There's no hidden state — what
you see is what gets sent.

**Decorations.** A `ViewPlugin` scans the document for the regex
`/@\[([A-Za-z0-9._:-]+)\]/g` and emits a `Decoration.replace` over
each match, replacing the literal text with a `RefChipWidget`.
The widget builds a placeholder `<span>` and registers it in a
module-local widget registry keyed by a generated id; the
SendBox component subscribes to the registry and renders a
React `<RefLink>` into each container via `createPortal`. Same
trick clydra used to keep React context (so chips inherit shell
context like `RefLinkImplContext`).

**Drop handling.** CodeMirror's `EditorView.domEventHandlers({
drop: ... })` lets us intercept native drops on the editor DOM.
On `dragover` we check for `DRAG_MIME_FXTR_REF` and call
`preventDefault()` so the browser shows the copy cursor. On
`drop`, we:
1. Call `parseRefDragData(e.dataTransfer)` → `{kind, shortId}`.
2. Use the drop coordinates to compute the document position
   (CodeMirror's `posAtCoords`).
3. Insert the literal text `@[shortid]` at that position via a
   transaction.

The widget plugin picks the new text up automatically and
renders a chip. We do not directly insert a widget — we insert
text and let the ViewPlugin re-scan, keeping the source-of-truth
invariant.

**Typed input.** No special handling needed. The user typing
`@[shortid]` produces the same text the drop produces, and the
ViewPlugin chips it identically.

**Keymap.** A `Prec.high(keymap.of([...]))` extension for Enter
(send when not in autocomplete) — currently nothing else uses
autocompletion in the agent panel, but the precedence pattern
matches clydra and keeps the door open. Shift+Enter inserts a
newline (default behavior).

**Send handler.** Reads the editor doc string, extracts every
`@[shortid]` match (preserving order), and builds the invoke
payload:

```
matches = parseEditorDoc(docText)  // list[{shortId, kind?}]
attached = matches.map(m => resolver.classifyShortId(m.shortId) ?
                            resolver.{entity|service}Ref(m.shortId) : drop_unknown)
// optional: drop unknown shortIds with a warning
if (attached.length > 0):
  payload = {message: docText, attached: attached}
else:
  payload = docText  // bare string, backwards compatible
ctx.allocateInvoke(serviceStub, "send", [payload])
```

The drag path stamps the kind at drag time, so we want to thread
it through alongside the shortId. Two ways to do that:

- (a) Maintain a separate side-map of "shortId → kind" inside the
  editor that drag fills and is consulted at send time. Pros:
  authoritative for dragged chips even if the classifyShortId
  list is stale. Cons: drifts if the doc is edited (chip deleted
  then retyped: we only have classifyShortId then).
- (b) Always go through `resolver.classifyShortId` at send time.
  Pros: single source of truth. Cons: depends on the resource
  lists being live.

We'll do **(b)** for simplicity. The shell's connector keeps
both lists loaded, and a dragged ref is guaranteed to be in the
relevant list (it's how it got dragged in the first place).

**Resolver unavailable.** If `useRefStubResolver()` returns
`null` (renderer used outside a shell), the editor renders text
without chip decoration and the send path always falls back to
bare-string mode — refs in the doc are sent verbatim as literal
`@[...]` characters in the message.

### Receive path: rendering `@[shortid]` in `workstation_inbox` payloads

`CallbackNotificationStreamEntry`'s workstation_inbox branch
needs to handle three payload shapes:

1. **Plain string** (current sender + legacy peers): render as
   text. Existing behavior.
2. **`{message: string, attached: Array<EntityRefStub | ServiceRefStub>}`**:
   tokenize `message` on the `@[shortid]` regex; for each
   matched token, find the matching stub in `attached` by
   `shortId` and render `<RefLink as="inline" stub={...}>`;
   render literal text segments between tokens with
   `whiteSpace: pre-wrap`.
3. **Anything else** (older or third-party callers): fall back
   to `<JsonRefTree>`. Existing behavior.

Branch detection: a small `isStructuredUserMessage(v: ResultValue)`
predicate checks the dict shape; everything else flows through
existing branches.

Unmatched `@[shortid]` tokens (the message references a shortId
that isn't in `attached`) render as literal `@[shortid]` text —
the sender's referrer dropped on the floor but no UI crash.

### Dependencies

- `@codemirror/state`
- `@codemirror/view`
- `@codemirror/commands`
- `codemirror`  (umbrella for the basic setup; we'll use a
  trimmed extension list, not `basicSetup`, to keep the bundle
  small)
- `@uiw/react-codemirror` — *optional*: we may import directly
  from `@codemirror/view` and wire our own React wrapper, since
  the agent panel needs only a tiny subset. Decide during
  implementation; if implementing the React wrapper is more than
  ~50 lines, we add the dep.

Versions: ask `npm view` at install time (per repo convention —
don't guess). All packages added to
`@fxtr-ui/agent-plugin-renderer/package.json` only.

CodeMirror is **not** externalized — the agent panel renderer is
not currently shipped as a dynamic renderer module, so it can
ship the editor in its own bundle.

## File layout

```
packages/fxtr-ui-workstation-dispatch/src/
  dnd/                        (new)
    constants.ts              # DRAG_MIME_FXTR_REF
    parse.ts                  # parseRefDragData
    drop_region.tsx           # RefDropRegionContext + Provider + useRefDropRegion
    index.ts                  # re-exports
  reflink/
    ref_stub_resolver.ts      (new) # RefStubResolver type + Context + hook
    (existing files)
  index.ts                    # add new re-exports

packages/fxtr-ui-workstation/src/
  reflink/
    drag.ts                   # import DRAG_MIME_FXTR_REF from dispatch package; re-export for back-compat
    ShellRefStubResolver.tsx  (new) # shell-side concrete impl
    ShellRefLinkImpl.tsx      # install RefStubResolverContext alongside RefLinkImplContext
  shell/
    DockviewLayout.tsx        # consult RefDropRegionContext; skip e.accept() while active
    WorkstationShell.tsx      # mount <RefDropRegionProvider> near the top of the tree

packages/fxtr-ui-agent-plugin-renderer/src/
  entries/
    shared.tsx                # add CollapsibleCard + emphasis variant
    summarize.ts              (new) # one-liner helpers per event type
    script_execution.tsx      # use CollapsibleCard; emit two siblings when present_to_user
    invoke_result.tsx         # use CollapsibleCard with summary
    invoke_error.tsx          # use CollapsibleCard with summary
    callback_notification.tsx # use CollapsibleCard + new structured-message branch
  send_box/                   (new dir, replaces send_box.tsx)
    index.tsx                 # SendBox component (was send_box.tsx)
    ref_chip_editor.tsx       # CodeMirror wrapper + chip decoration + drop handler
    chip_widget.ts            # WidgetType + widget registry + portal manager
    parse_refs.ts             # tokenize doc / collect shortIds
  panel.tsx                   # import path tweak only
  package.json                # add CodeMirror deps
```

## Implementation steps

### Phase A — Drag primitives + resolver context (no UI changes yet)

- [x] **A1.** Add `packages/fxtr-ui-workstation-dispatch/src/dnd/`
      with `DRAG_MIME_FXTR_REF` constant and
      `parseRefDragData(dataTransfer): {kind, shortId} | null`.
      The parser reads the `application/x-fxtr-ref` MIME, attempts
      `JSON.parse`, returns `{kind, shortId}` from `parsed.target`
      if it matches the expected `{kind: "entity"|"service",
      shortId: string}` shape. Falls back to `null` on any
      mismatch.
- [x] **A2.** Add `dnd/drop_region.tsx` to the same package:
      `RefDropRegionContextValue = {isActive, increment, decrement}`,
      `RefDropRegionContext` (default null),
      `<RefDropRegionProvider>` (counter-based state, computes
      `isActive = counter > 0`), and `useRefDropRegion()` returning
      `{regionProps: {onDragEnter, onDragLeave, onDrop}}` that
      bumps the shared counter via a per-region ref. Each region
      tracks its own local nested-enter counter (per thingwatcher's
      pattern) so we only bump the shared counter on true
      enter/leave transitions, not on every child-traversal. The
      hook also installs an unmount-cleanup `useEffect` that
      decrements the shared counter if a drag was mid-enter when
      the region unmounted.
- [x] **A3.** Add `packages/fxtr-ui-workstation-dispatch/src/reflink/ref_stub_resolver.ts`:
      `RefStubResolver` interface, `RefStubResolverContext` (default
      null), `useRefStubResolver()` hook (returns nullable).
- [x] **A4.** Re-export `DRAG_MIME_FXTR_REF`, `parseRefDragData`,
      `RefDropRegionContext`, `RefDropRegionProvider`,
      `useRefDropRegion`, `RefStubResolver`,
      `RefStubResolverContext`, `useRefStubResolver` from
      `@fxtr-ui/workstation-dispatch/index.ts`.
- [x] **A5.** In `@fxtr-ui/workstation`'s `reflink/drag.ts`,
      remove the local `DRAG_MIME_FXTR_REF` declaration and import
      it from `@fxtr-ui/workstation-dispatch`. Re-export it so the
      shell's existing public surface stays unchanged.
- [x] **A6.** Add `packages/fxtr-ui-workstation/src/reflink/ShellRefStubResolver.tsx`:
      `<ShellRefStubResolverProvider>` reads the connector via
      `useWorkstation()`; concrete impl uses `connector.entityRef`
      / `serviceRef` and reads `connector.listEntities().records`
      / `listServices().records` for classification (returns "entity"
      / "service" / null). The lookup walks the snapshot once per
      call — no memoization needed; shortId is unique across the
      two lists by workstation invariant.
- [x] **A7.** In `ShellRefLinkImpl.tsx`'s `<ShellRefLinkProvider>`,
      wrap the existing `<RefLinkImplContext.Provider>` with
      `<RefStubResolverContext.Provider>` so both contexts mount
      together near the top of the shell tree.
- [x] **A8.** Mount `<RefDropRegionProvider>` near the top of the
      shell tree (in `<WorkstationShell>`, above
      `<DockviewLayout>`). Picking the right level matters: the
      provider must wrap both the dockview-grid surface (where
      `DockviewLayout` reads `isActive`) and all panel content
      (where renderers install regions). The existing
      `<ShellRefLinkProvider>` mount point already satisfies both
      and is a natural sibling.
- [x] **A9.** Update `DockviewLayout.tsx`'s
      `onUnhandledDragOverEvent` handler to consult
      `RefDropRegionContext` (via a ref-mirrored value so the
      handler doesn't re-subscribe per render). When
      `isActive === true`, skip `e.accept()` so dockview doesn't
      show its drop overlay. Otherwise, existing
      `DRAG_MIME_FXTR_REF` accept logic stands. (No change to
      `onDidDrop` itself: when the drop-region eats the drop,
      dockview's `onDidDrop` won't fire at all because we never
      accepted.)
- [x] **A10.** Update `frontend_lib/CODE_OVERVIEW.md`:
      - `@fxtr-ui/workstation-dispatch`: list the new `dnd/`
        submodule (including drop-region context) and the new
        `ref_stub_resolver.ts`.
      - `@fxtr-ui/workstation`: note that `DRAG_MIME_FXTR_REF` now
        lives in the dispatch package; the shell installs
        `RefStubResolverContext` and `RefDropRegionProvider`
        alongside `RefLinkImplContext`; `DockviewLayout` defers to
        active drop regions.

### Phase B — Collapsible-block primitive + summaries

- [x] **B1.** Add `entries/summarize.ts` with:
      - `truncate(s, n)` helper.
      - `summarizeValue(v: ResultValue): string` short-form repr
        (primitives by value, refs as `<entity e-abc>` /
        `<service s-xx>`, lists as `list[N]`, dicts as `{K keys}`).
      - Four per-event summary functions: `summarizeScript(v)`,
        `summarizeInvokeResult(v)`, `summarizeInvokeError(v)`,
        `summarizeCallbackNotif(v)`.
- [x] **B2.** Add `CollapsibleCard` to `entries/shared.tsx`. Props:
      `{tag, detail?, summary, defaultOpen?, emphasis?, children}`.
      Renders a `<details>` with custom triangle + summary line
      that shows only while closed. `emphasis` styles the border
      and tag color per the existing `presentedBlockStyle` /
      `presentedHeaderStyle`.
- [x] **B3.** Refactor `invoke_result.tsx` to wrap its body in
      `CollapsibleCard` (default closed) with
      `summary={summarizeInvokeResult(v)}`. Pending / errored
      states render an always-open card (no summary needed) — keep
      them simple.
- [x] **B4.** Same as B3 for `invoke_error.tsx`.
- [x] **B5.** Same as B3 for `callback_notification.tsx` (both
      `workstation_inbox` and generic branches). The
      structured-message branch is still TBD until phase D, so for
      now keep the existing string-vs-JsonRefTree dichotomy.
- [x] **B6.** Refactor `script_execution.tsx`:
      - Compute `presentedOps` / `otherOps` (already exists).
      - Outer element: a flex-column wrapper.
      - First child: `<CollapsibleCard defaultOpen={false}
        tag={scriptTag} summary={summarizeScript(v)}>` containing
        everything that's not `present_to_user` (script_src,
        stdout, stderr, error, rejected, non-presented ops).
      - Second child (only if `presentedOps.length > 0`):
        `<CollapsibleCard defaultOpen emphasis tag="presented to
        user" summary={\`${presentedOps.length} value(s)\`}>`
        containing the `JsonRefTree` entries.
- [x] **B7.** Update `entries/script_execution.tsx`'s existing
      `presentedBlockStyle` / `presentedHeaderStyle` — they merge
      into the `CollapsibleCard`'s `emphasis` mode and can be
      deleted locally.
- [x] **B8.** Update `CODE_OVERVIEW.md`'s
      `@fxtr-ui/agent-plugin-renderer` section: note the
      collapsible-card primitive, the new `summarize.ts` helper,
      and the two-block `present_to_user` layout.

### Phase C — Ref-chip editor (CodeMirror 6)

- [x] **C1.** Add CodeMirror deps to the package's
      `package.json`. Use `npm view` to pick current versions.
- [x] **C2.** Add `send_box/parse_refs.ts`:
      - `REF_TOKEN_RE = /@\[([A-Za-z0-9._:-]+)\]/g` (anchored at
        the literal `@[` / `]`).
      - `tokenizeMessage(doc: string): Array<{kind: "text",
        value: string} | {kind: "ref", shortId: string}>` —
        used both by the send-time builder and by the receive-time
        renderer (D2).
      - `collectShortIds(doc: string): readonly string[]` — convenience
        wrapper that returns just the shortIds in order.
- [x] **C3.** Add `send_box/chip_widget.ts`:
      - `RefChipWidget extends WidgetType` — builds a `<span>`
        placeholder with a unique id; registers it in the
        module-local `widgetRegistry`; calls `widgetRegistry.unregister`
        in `destroy`.
      - The widget's `eq` compares on `shortId` only — same
        shortId = no remount.
      - `widgetRegistry` mirrors the clydra pattern (Map +
        listeners + subscribe). Keys are widget ids; values are
        `{element: HTMLElement, shortId: string}`.
- [x] **C4.** Add `send_box/ref_chip_editor.tsx`:
      - Mount a CodeMirror 6 `EditorView` with a small extension
        list: `lineWrapping`, a basic theme matching the shell
        token vars (`--surface-sunken`, `--text-primary`, etc.),
        the chip ViewPlugin (`createRefChipDecorationPlugin()`),
        an Enter keymap (`Prec.high`) that calls a prop-supplied
        `onSubmit` when autocomplete is closed, and
        `EditorView.domEventHandlers({drop, dragover})`.
      - `dragover`: `if (e.dataTransfer.types.includes(DRAG_MIME_FXTR_REF))
        { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; }`
      - `drop`: parse via `parseRefDragData`, find the doc
        position via `posAtCoords(e)`, dispatch a transaction
        inserting `@[shortid]` at that position.
      - **Drop-region wrapper.** The editor's outer wrapper div
        spreads `useRefDropRegion()`'s `regionProps` so dockview
        defers to it (see Phase A). The wrapper also gets a
        visible "drag-over" highlight (border / background tint)
        driven by a local `isDragOver` state that we set on
        `dragenter` (when the drag carries `DRAG_MIME_FXTR_REF`)
        and clear on leave/drop. We track that state per-component
        rather than reading the shared `isActive` flag so multiple
        regions in the same shell don't all light up
        simultaneously.
      - Controlled component: `value` + `onChange` props, with
        external value diffed against `view.state.doc.toString()`
        and applied as a `replaceAll`-style transaction when
        they diverge (matches the `useState`-controlled pattern
        from the textarea).
      - Subscribe to `widgetRegistry`'s changes via `useSyncExternalStore`
        (or a `useState` + `useEffect` pair), and render
        `createPortal(<RefLink as="inline" stub={resolver.entityRef|serviceRef(shortId)} />, container)`
        for each registered widget.
      - The portal rendering does the `classifyShortId` lookup;
        if `null`, render an unstyled fallback span with the
        literal text (`@[shortid]`) so the user sees the
        unresolved ref.
- [x] **C5.** Refactor `send_box.tsx` → `send_box/index.tsx`:
      - Replace the `<textarea>` with `<RefChipEditor>`.
      - The send handler:
        1. Reads `draftMessage` from panel state.
        2. Calls `tokenizeMessage(draftMessage)` to extract refs.
        3. Calls `useRefStubResolver()` once at component scope; in the
           handler, classifies each shortId; mints stubs via
           `entityRef` / `serviceRef`. Unknown shortIds are
           dropped from `attached` (their literal text remains in
           `message` and renders unresolved on the receiver
           side). Optionally a `console.warn` for visibility.
        4. If `attached.length > 0`, passes `{message: draftMessage,
           attached}` as the invoke arg; otherwise the bare string.
- [x] **C6.** When the resolver context is `null` (no shell), the
      editor still mounts but the chip portal layer is inert
      (`<RefLink>` would also break without `RefLinkImplContext`).
      Add a tiny degradation note to the editor's render:
      decorations still apply (visual chips), but they render an
      empty placeholder span. Acceptable for tests / standalone
      rendering.
- [x] **C7.** Update `CODE_OVERVIEW.md`'s agent-plugin-renderer
      section: new `send_box/` subdir layout, CodeMirror
      dependency, drop-target wiring, chip portal pattern.

### Phase D — Receive-side rendering of structured `workstation_inbox` messages

- [x] **D1.** Add `isStructuredUserMessage(v: ResultValue): v is
      {message: string, attached: ReadonlyArray<EntityRefStub |
      ServiceRefStub>}` predicate in
      `entries/callback_notification.tsx` (or shared
      `data_shapes.ts`).
- [x] **D2.** Add a `StructuredMessageBody` subcomponent in
      `callback_notification.tsx`:
      - Tokenize `message` via `tokenizeMessage` (imported from
        `../send_box/parse_refs.ts` — small shared helper).
      - Build a `Map<shortId, stub>` from `attached`.
      - Render each segment: text segments as `<span style={{whiteSpace:
        "pre-wrap"}}>`, ref tokens as `<RefLink as="inline" stub={...}>`,
        unmatched ref tokens as literal `@[shortid]` text.
- [x] **D3.** Update the workstation_inbox branch in
      `CallbackNotificationStreamEntry`:
      - If `isStructuredUserMessage(v.payload)`: render
        `<StructuredMessageBody>`.
      - Else if `typeof v.payload === "string"`: existing plain-text
        branch.
      - Else: existing `<JsonRefTree>` fallback.
- [x] **D4.** Confirm the dispatch through `useDispatchSharedAtom`
      / `makeLoadResultAtom` decodes `attached` correctly: the
      load result's `payload` is a `ResultValue`, and refs nested
      inside list-typed fields decode to real `EntityRefStub` /
      `ServiceRefStub` instances via `unescapeWire`. No change
      needed — this works today by virtue of the structured
      decode pipeline.
- [x] **D5.** Update `CODE_OVERVIEW.md`: note the structured
      `workstation_inbox` payload branch.

### Phase E — Tests + verification

- [x] **E1.** `entries/summarize.test.ts` — pure-function tests
      for each summary helper across the three terminal shapes of
      `script_execution`, success / error variants of the other
      event types, dict-shape vs plain-string for
      callback_notification.
- [x] **E2.** `send_box/parse_refs.test.ts` — `tokenizeMessage`
      round-trips: plain text, single ref, multiple refs, refs at
      edges, unmatched-looking text (`@[]`, `@[abc def]`,
      `@[abc] extra @[`), ensures the regex doesn't over-match.
- [x] **E3.** Mock-resolver test in `tests/` that wires a fake
      `RefStubResolver` (handing out test stubs) into the SendBox
      via a context provider, drives a synthetic send through the
      existing `makeMockSessionAtom`, and asserts the resulting
      `InvokeOp.args` carry the expected dict shape when refs are
      present and bare string when not.
- [ ] **E4.** Live-fire smoke test: hand-run the
      `fxtr-ui-workstation-demo` (or the agent-plugin demo if
      it's wired in by then), drag an entity ref from a panel
      into the send box, confirm the chip appears, hit Send,
      confirm the chain extends with a `workstation_inbox` entry
      that round-trips through `StructuredMessageBody`. Document
      whatever was observed; if non-trivial issues come up, add
      them to the bottom of this plan before fixing.
      **(Deferred — requires manual drag-drop in a browser.)**
      Build-level verification: ran `vite build` against
      `projects/fxtr_workstation_demo/frontend` — 502 modules
      transformed cleanly, no warnings related to the new code
      (one chunk-size warning about the overall 952 kB bundle,
      pre-existing and not related to these changes). Awaiting
      user verification of the actual drag/drop UX.
- [x] **E5.** Final pass on `CODE_OVERVIEW.md` to make sure both
      the dispatch-package and renderer-package sections are
      complete.

### Phase F — Update `CODE_OVERVIEW.md` (final sweep)

- [x] **F1.** Sanity sweep of `frontend_lib/CODE_OVERVIEW.md`
      after all phases: new file paths listed, new exports listed,
      module-boundary text reflects reality. Cross-check against
      `git status` for stray files.

## Edge cases / open questions

- **Unknown shortIds in `@[...]`.** Classification miss means the
  ref isn't in the receiver's resource lists. Send path drops
  them from `attached`; receive path renders the literal token.
  This is a deliberate-degrade behavior; if it surfaces in
  practice we'll revisit and surface a UI warning on the chip.

- **CodeMirror cursor on chip boundaries.** Inserting a chip mid-
  word (e.g. user typed `hi@[abc]bye`) is allowed; CodeMirror's
  cursor navigation will hop over the chip widget as if it's a
  single character. We don't try to constrain insertion to
  whitespace-bounded positions in this stage — text is the
  source of truth, and the user can edit it however they like.

- **Sequential `@[a]@[b]` chips.** Rendered as two adjacent chips
  with no whitespace between. Probably want a tiny left/right
  margin on chip styling. Decide during implementation; tweak
  CSS only.

- **Resource-list staleness window.** Between dragging a ref and
  hitting Send, the connector's `listEntities` could in theory
  evict (e.g. if the workstation prunes inactive entities).
  Today's workstation doesn't do that, and even if it did the
  `EntityRefStub` instance is interned by the connector
  independently of the list, so the send would still work — the
  classification step might return null but we already mint the
  stub via `entityRef(shortId)` which doesn't depend on the
  list. This is a "decide during implementation" thing — if
  classify says null but the shortId was definitely in the list
  at drag time, we may want to remember kind from the drag
  payload after all. We can fold that in if it bites.

- **Update `CODE_OVERVIEW.md`** — last step of every phase, and
  one final sweep in Phase F.
