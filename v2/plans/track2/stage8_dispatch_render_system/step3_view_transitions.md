# Step 3: View Transitions for Report Navigation

Goal: Add smooth animations between focused elements when navigating in the report shell. Compare two approaches side-by-side as separate commits.

## What should animate

Two key transitions:
1. **Card <-> Main view**: Clicking a SummaryCard navigates to MainView. Shared elements (e.g. title, thumbnail) should morph into position; the rest crossfades.
2. **Figure block <-> Main view**: A SubReport's FigureBlock or LinkedSVGFigure's FigureBlock navigates to their MainView. The figure content is the shared element.

Shared element matching uses a `view-transition-name` derived from the entity's FxtrID string (sanitized for CSS). Each handler that participates in a transition applies this name to its "hero" element.

## Approach A: React Canary `<ViewTransition>`

### Key changes

1. **Pin react@canary** in the workspace (isolated to this commit)
2. **Wrap navigation in `startTransition()`**: In `ReportShell.tsx`, the `navigateTo` and `navigateBack` callbacks that call `dispatch(...)` need to go through React's `startTransition()` so that React knows to activate ViewTransition boundaries.
3. **`<ViewTransition>` in ReportShell**: Wrap the main content area (`<Render as={MainView}>` and `<CardGrid>`) in a `<ViewTransition>` for the crossfade.
4. **`<ViewTransition name={...}>` in handlers**: Each handler component that participates in shared-element transitions wraps its "hero" DOM node in `<ViewTransition name={vtName(entityId)}>`. The same name appears in both the outgoing component (card/figure) and the incoming component (main view), so React matches them.
   - `RootReportSummaryCard` and `RootReportMainView` share a name for the title
   - `SubReportFigureBlock` and `SubReportMainView` share a name for the overview content
   - `LinkedSVGFigureBlock` and `LinkedSVGFigureMainView` share a name for the SVG element
5. **CSS**: Minimal — the default crossfade + morph is what we want. Add `prefers-reduced-motion` to disable.

### Tradeoffs
- Requires React Canary (unstable) — API may change
- Declarative, React manages the `startViewTransition` call and snapshot timing
- Shared element matching is automatic via `name` prop
- No manual `flushSync` needed

## Approach B: Vanilla `document.startViewTransition()` + `flushSync`

### Key changes

1. **Keep React 19 stable**
2. **Intercept navigation**: In `ReportShell.tsx`, wrap the `dispatch(...)` calls inside `document.startViewTransition(() => { flushSync(() => dispatch(...)) })`. This captures old snapshot, synchronously commits the React state update, then the browser animates old→new.
3. **`view-transition-name` CSS property on handlers**: Same hero elements as Approach A, but applied via inline `style={{ viewTransitionName: vtName(entityId) }}` instead of a React component.
4. **CSS**: Same as Approach A — default morph + crossfade, `prefers-reduced-motion`.
5. **Graceful fallback**: Check `document.startViewTransition` exists; if not, just dispatch normally.

### Tradeoffs
- Works on React 19 stable
- Imperative — we control when `startViewTransition` fires
- Must use `flushSync` (bypasses batching, forces synchronous render)
- Shared element matching is via CSS `view-transition-name` property
- More manual lifecycle management

## Shared utility

Both approaches need a helper to derive a CSS-safe `view-transition-name` from an FxtrID:

```ts
function vtName(entityId: FxtrID, suffix?: string): string {
  // FxtrID.idString() may contain characters invalid for CSS custom-ident
  // Replace non-alphanum with hyphens, prefix with "fxtr-"
  const base = "fxtr-" + entityId.idString().replace(/[^a-zA-Z0-9_-]/g, "-");
  return suffix ? `${base}--${suffix}` : base;
}
```

## Implementation notes

Both approaches were implemented as sibling jj commits off the plan doc commit. Both produce the same visual result: shared-element morphs for card↔main and figure↔main transitions, with the default browser crossfade for everything else.

### Issues encountered in both approaches

- **Outer `<ViewTransition>` / root crossfade causes zoom**: Wrapping the entire main content area in a single view transition boundary (React `<ViewTransition>` or the `::view-transition-group(root)`) causes the whole content block to animate size/position between old and new states. This looks like a zoom when switching between a card grid and a single main view. Fix: only use named view transitions on individual shared elements; let the rest snap.
- **SVG sizing mismatch**: The SVG figure's wrapper div defaults to `display: block` (full container width), but the SVG's intrinsic size is smaller. The old and new snapshots have different widths, causing a jarring resize during the morph. Fix: `display: inline-block` on the wrapper to shrink-wrap to the SVG's size.
- **Breadcrumb double-fire**: Clicking a breadcrumb entry triggered two view transitions — one from the breadcrumb's own `onNavigateBack` handler, and another from the inner `<Render as={ReferenceLink}>` component's `onClick` (which calls `navigateTo`). The second transition cancels the first, and by then the DOM has already been updated, so old and new snapshots are identical → no animation. Fix: use `onClickCapture` + `stopPropagation` on the breadcrumb span so it intercepts the click before the inner ReferenceLink fires. Root cause: `ReferenceLink` is used in breadcrumbs only for its display text, but it brings click-to-navigate behavior baked in.
- **Duplicate React keys in breadcrumbs**: The breadcrumb list used `entityId.key()` as the React key, but the same entity can appear multiple times in the breadcrumb stack. Fix: prefix with the index (`${i}-${entry.entityId.key()}`).

### React Canary observations

- Pinning to canary (`19.3.0-canary-3cb2c420-20260324`) via `pnpm.overrides` works cleanly; only the root `package.json` needs the override.
- Types come from `@types/react/canary.d.ts` (already shipped in `@types/react@^19`), enabled via `"types": ["react/canary"]` in tsconfig.
- `<ViewTransition>` is an exotic component (symbol-typed), imported directly from `"react"`.
- `startTransition()` is the trigger — React only activates ViewTransition boundaries for state updates inside `startTransition`.
- An outer `<ViewTransition>` with no `name` gets an auto-generated unique name, which creates a transition group for the whole subtree. This is what caused the zoom issue.
- The React API has nice affordances for more complex scenarios: `addTransitionType` for directional animations, per-boundary `enter`/`exit`/`share`/`update` props, type-based CSS class maps.

### Vanilla observations

- The core implementation is ~10 lines: `document.startViewTransition(() => { flushSync(update) })` with a feature-detection guard.
- `flushSync` forces React to commit the DOM synchronously inside the transition callback. This is necessary so the browser captures the correct "new" snapshot.
- `viewTransitionName` works as a plain inline style in React 19 stable — no special type augmentation needed.
- Shared element matching works the same way as the React approach: matching `view-transition-name` values in old and new DOM snapshots.
- `flushSync` does NOT force Suspense fallbacks to show if the underlying data resolves synchronously (via the Eager path). So previously-cached entities render fully within the transition callback.

## Recommendation: vanilla approach

The vanilla `document.startViewTransition` + `flushSync` approach is preferred for this codebase:

1. **No unstable dependency**: Stays on React 19 stable. The canary API could change before release, and pinning canary affects the entire workspace.
2. **Minimal abstraction**: The `withViewTransition` helper is ~10 lines. The hard parts (choosing which elements get `viewTransitionName`, sizing them correctly) are identical in both approaches.
3. **Same visual result**: Both produce the same default morph + crossfade. The React API's extra features (directional animations, per-boundary animation classes) aren't needed for the current use case.
4. **`viewTransitionName` as inline style is fine**: No wrapper component needed — just a style property on the hero element's existing div.

The React Canary approach would become more compelling if we needed per-component animation customization (different easing per dispatch key, directional slide based on nav direction via `addTransitionType`, etc.). Worth revisiting when `<ViewTransition>` reaches React stable.

## Future improvements

- Remove debug logging from `withViewTransition`
- Consider a `DisplayName` dispatch key for breadcrumbs (text-only, no click behavior) to avoid the capture-phase workaround
- Directional animations (slide-left for forward, slide-right for back) — could be done in vanilla via CSS classes toggled before `startViewTransition`
- View transitions for card list ↔ main view (currently snaps; could crossfade with a root-level transition if the zoom issue is managed)
- `prefers-reduced-motion` is handled via CSS; could also skip the `startViewTransition` call entirely

## Implementation steps



### Approach A (React Canary)
- [x] Pin react/react-dom to canary versions in workspace
- [x] Add `vtName` utility to `@fxtr/reports-react`
- [x] Wrap navigation dispatch in `startTransition()` in ReportShell
- [x] Add `<ViewTransition>` around main content area in ReportShell
- [x] Add `<ViewTransition name={...}>` to handler hero elements (SummaryCard, FigureBlock, MainView for RootReport, SubReport, LinkedSVGFigure)
- [x] Add transition CSS (minimal + prefers-reduced-motion)
- [ ] Test in demo app
- [ ] Commit as separate jj change

### Approach B (Vanilla)
- [x] Revert react to stable
- [x] Wrap navigation dispatch in `document.startViewTransition` + `flushSync` in ReportShell
- [x] Add inline `viewTransitionName` styles to handler hero elements
- [x] Add transition CSS (carried over from Approach A)
- [ ] Test in demo app
- [ ] Commit as separate jj change

- [ ] Update CODE_OVERVIEW.md
