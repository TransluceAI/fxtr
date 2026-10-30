# Stage 11 post-1 — Remove `useEntityLoad`, adopt `makeLoadResultAtom`

Project-wide cleanup: replace the `useEntityLoad` React hook (an
antipattern that monopolizes `useDispatchSharedAtom` and uses
`useEffect` for slot allocation) with the report-style
`makeLoadResultAtom(slotAtom, stub)` helper, which uses
`atomEffect` for slot lifecycle and lets the caller pick where
the slot lives.

The helper already exists in `@fxtr-ui/reports` (since stage 1 of
part 4); this stage just lifts it to a shared package so other
consumers can use it without taking a dep on `reports`.

## Background

- `frontend_lib/packages/fxtr-ui-reports/src/load_effect.ts` —
  the canonical implementation. `makeLoadResultAtom(slot, stub)`
  returns an atom whose read drives an `atomEffect` allocating
  the load on first subscribe, and whose value projects to
  `LoadResult<T> = loading | filled | errored`.
- See the "useEntityLoad was an antipattern" note in
  `plans/phase0/part4_assembly/stage1_report_views.md` for the
  prior rationale.

Current `useEntityLoad` callers (all in demo packages):

- `frontend_lib/packages/fxtr-ui-dispatch-demo/src/client/cards/NoteCard.tsx`
- `frontend_lib/packages/fxtr-ui-dispatch-demo/src/client/cards/PhotoCard.tsx`
- `frontend_lib/packages/fxtr-ui-workstation-demo/src/client/handlers.tsx`
  — 6 renderers (DemoNote × {FullPanel, BlockLink, HoverPreview},
  DemoListNode × {FullPanel, BlockLink, HoverPreview})

Plus the same inline antipattern in:

- `frontend_lib/packages/fxtr-ui-workstation/src/system_renderers/BasicTypeBadge.tsx`
  — added in stage 11. Two `DispatchedComponent`s
  (`BasicTypeBlockLink`, `BasicTypeInlineLink`) currently use
  `useDispatchSharedAtom<{load: …}>` + `useEffect` to drive the
  load.

Out of scope:

- `frontend_lib/packages/fxtr-ui-dispatch-demo/src/client/cards/EchoServiceCard.tsx`
  uses a related-but-different pattern (user-click-driven invoke,
  not mount-driven load). `makeLoadResultAtom` doesn't apply
  directly. Left alone for this stage.

## Changes

### Part A — Lift `makeLoadResultAtom` to `@fxtr-ui/state-util`

Move the helper to `@fxtr-ui/state-util` (`packages/fxtr-ui-state-util/`).
Conceptually it's a path/lifecycle helper over session-bound atoms
— same family as `subAtom` and `atomWithDefault`. Add `jotai-effect`
as a dep on `@fxtr-ui/state-util`.

Files:

- New: `packages/fxtr-ui-state-util/src/load_result_atom.ts` —
  identical implementation (copied from
  `fxtr-ui-reports/src/load_effect.ts`). Exports `LoadResult<T>`
  + `makeLoadResultAtom`.
- `packages/fxtr-ui-state-util/src/index.ts` — re-export.
- `packages/fxtr-ui-state-util/package.json` — add
  `"jotai-effect": "^2.0.0"` to dependencies.
- `packages/fxtr-ui-reports/src/load_effect.ts` — **delete**.
- `packages/fxtr-ui-reports/src/index.ts` — re-export
  `LoadResult` + `makeLoadResultAtom` from `@fxtr-ui/state-util`
  for back-compat (one-line re-export so existing import paths
  keep working).
- `packages/fxtr-ui-reports/src/handlers/RootReport.tsx` /
  `SubReport.tsx` — update their internal imports from
  `../load_effect.js` to `@fxtr-ui/state-util` (no functional
  change).

Tests: there are no existing tests targeting `makeLoadResultAtom`
directly (the reports tests treat it as an internal detail). No
tests added or removed in Part A — just relocation.

### Part B — Convert `useEntityLoad` callers

For each consumer, replace the `useEntityLoad<T>(stub)` call with
the report-style pattern:

```tsx
const sharedAtom = useDispatchSharedAtom<OpResultStub<T> | null>(null);
const resultAtom = useMemo(
  () => makeLoadResultAtom<T>(sharedAtom, stub),
  [sharedAtom, stub],
);
const result = useAtomValue(resultAtom);
```

And update the branch tree at the call site:

- `result.kind === "filled"` → unchanged (`.value`).
- `result.kind === "errored"` → unchanged (`.message`).
- `result.kind === "loading"` → covers both the old
  `"allocating"` and `"pending"` arms. UI strings folded into one
  case (e.g. `"loading…"` instead of branching on which arm).

This change touches:

- `dispatch-demo/src/client/cards/NoteCard.tsx`
- `dispatch-demo/src/client/cards/PhotoCard.tsx`
- `workstation-demo/src/client/handlers.tsx` — 6 renderers. The
  shared `renderStatusFallback` helper in that file branches on
  `EntityLoadStatus`; it shrinks to a three-arm
  `renderLoadResult<T>(result, whenFilled)`. Inline-able if
  preferred; keeping the helper means 6 fewer copies of the same
  switch.

Removed types:

- `EntityLoadStatus<T>` — replaced by `LoadResult<T>` from
  `@fxtr-ui/state-util`. Import sites swap the type name and the
  `kind: "allocating" | "pending"` arms collapse to `"loading"`.

### Part C — Refactor `BasicTypeBadge.tsx`

Inside `BasicTypeBadge` (the internal component), drop the local
`SharedSlot` interface + `useEffect` + manual result-projection
in favor of the same pattern:

```tsx
const sharedAtom = useDispatchSharedAtom<OpResultStub<FxtrBasicTypeValue> | null>(null);
const resultAtom = useMemo(
  () => makeLoadResultAtom<FxtrBasicTypeValue>(sharedAtom, stub),
  [sharedAtom, stub],
);
const result = useAtomValue(resultAtom);
const label = linkText ?? labelFor(result);
```

`labelFor` becomes:

```tsx
function labelFor(result: LoadResult<FxtrBasicTypeValue>): string {
  if (result.kind === "loading") return "type …";
  if (result.kind === "errored") return "type ?";
  return `type ${result.value.type_name}`;
}
```

Imports: drop the local `atom` / `useEffect` / `useSetAtom` /
`OpResultStub` / `ResultStatus` imports; bring in
`makeLoadResultAtom` + `LoadResult` from `@fxtr-ui/state-util`.

### Part D — Delete `useEntityLoad` files

Remove:

- `frontend_lib/packages/fxtr-ui-dispatch-demo/src/client/use_entity_load.ts`
- `frontend_lib/packages/fxtr-ui-workstation-demo/src/client/use_entity_load.ts`

Confirm via `grep -r useEntityLoad` that no stragglers remain.

## Implementation steps

- [x] **Part A** — copy `load_effect.ts` → `state-util/src/load_result_atom.ts`;
      add `jotai-effect` dep on `@fxtr-ui/state-util`; re-export
      from state-util's `index.ts`; update reports'
      `RootReport.tsx` / `SubReport.tsx` imports to come from
      `@fxtr-ui/state-util`; delete `reports/src/load_effect.ts`;
      add a re-export in `reports/src/index.ts` for back-compat;
      run `pnpm install` to pick up the new dep; confirm
      `pnpm check` + `pnpm exec vitest run packages/fxtr-ui-reports/`
      green.
- [x] **Part B (1/2) — dispatch-demo** — convert `NoteCard.tsx`
      + `PhotoCard.tsx`. Delete
      `dispatch-demo/src/client/use_entity_load.ts`. Run
      `pnpm check` and visually launch `pnpm dev` to confirm
      cards still render (manual; see "out-of-scope" note below).
- [x] **Part B (2/2) — workstation-demo** — convert all 6
      renderers in `handlers.tsx`; rewrite the local
      `renderStatusFallback` helper to operate on `LoadResult<T>`
      (or inline at each call site if preferred). Delete
      `workstation-demo/src/client/use_entity_load.ts`. Run
      `pnpm check`; visually confirm via `pnpm dev`.
- [x] **Part C** — refactor `BasicTypeBadge.tsx` to use
      `makeLoadResultAtom`. Verify the badge test suite still
      passes: `pnpm exec vitest run packages/fxtr-ui-workstation-dispatch-tests/`.
- [x] **Part D** — sanity-check: `grep -r useEntityLoad
      projects/fxtr_workstation v2/ts` returns no matches outside
      historical plan docs. Update
      `frontend_lib/CODE_OVERVIEW.md` to remove the
      `use_entity_load.ts` mentions and note the canonical
      `makeLoadResultAtom` pattern.
- [x] Final: `pnpm check`, `pnpm lint`, `pnpm exec vitest run`
      all green.
- [x] Update `CODE_OVERVIEW.md`:
      - `@fxtr-ui/state-util` entry — list the new
        `load_result_atom.ts` module + `makeLoadResultAtom` /
        `LoadResult<T>` exports.
      - `@fxtr-ui/reports` entry — note that `load_effect` moved
        to state-util (still re-exported here for back-compat).
      - Demo entries — drop `use_entity_load.ts` mentions.

## Open questions / follow-ups

- **`EchoServiceCard` projection** — out of scope per discussion.
  The "slot or sentinel-null atom" projection it inlines could
  become a sibling helper (`makeResultProjectionAtom(slot)` that
  skips the auto-allocate effect) if another caller needs it.
  Flag for later.
- **`make<X>` vs `use<X>`** — the helper is `make*` because it
  returns an atom (composable inside `useMemo`), not a React
  hook. Consistent with `makeFigureStorage` in `@fxtr-ui/reports`.
- **Loading-state UI consolidation** — collapsing
  `"allocating"`/`"pending"` into `"loading"` loses the
  before-and-after-effect-fires distinction. The renderers
  previously branched on this only with placeholder strings
  ("allocating load…" vs "loading body…"); no behavioural
  change. If we later want to surface "request hasn't been sent
  yet" UI, that's a `LoadResult` shape change, not a recovery of
  `useEntityLoad`.
