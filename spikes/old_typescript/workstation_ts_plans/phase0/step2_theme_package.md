# Step 2 — `@fxtr-workstation/theme`

Port the design-system prototype's tokens + panel chrome into a
CSS-only package that workstation-core (and the demo) import at their
entry points. No runtime JS/TS.

## Deliverables

```
projects/fxtr_workstation/ts/packages/fxtr-workstation-theme/
├── package.json           # subpath exports for each .css file + barrel
├── tsconfig.json          # unchanged from step 1
├── src/index.ts           # remains `export {};` — package is CSS-only
└── styles/
    ├── tokens.css             # design tokens in `:root`
    ├── panel-chrome.css       # `.fw-panel`, `.fw-panel__header`, `.fw-panel__body`, etc.
    ├── fw-dockview-theme.css  # `.fw-dockview-theme { --dv-* : ... }` mapping to our tokens
    ├── fonts.css              # @import Open Sans + JetBrains Mono from Google Fonts
    └── all.css                # convenience barrel that @imports the four above
```

Consumers import via the package exports:

```ts
// workstation-core entry
import "@fxtr-workstation/theme/all.css";

// or individually
import "@fxtr-workstation/theme/tokens.css";
import "@fxtr-workstation/theme/fonts.css";
```

## Decisions

- **Class prefix: `fw-`** — all selectors are `.fw-panel`,
  `.fw-panel__header`, etc. Class names are more collision-prone than
  CSS custom properties, and workstation will end up mounted inside
  host apps where bare `.panel` is easy to clobber.
- **Token names: unprefixed** — `--accent`, `--sp-3`, `--text-body`,
  etc., verbatim from the prototype. Renderers write
  `var(--accent)` naturally; the app owns its page so collision risk
  is low, and renaming adds noise.
- **Application scope: `:root`** — tokens apply to the whole document.
  Simpler than a `.fw-theme` scope class; if we later want to embed
  workstation inside a larger host page, we can add a scoped variant
  without breaking the existing surface.
- **Fonts: Google Fonts `@import`** — matches the prototype. Slower
  than `<link>` but easier to drop in from a single CSS file. Revisit
  (switch to `@fontsource/*` self-hosting) if flash-of-fallback
  becomes visible or we end up offline-targeted.
- **Dockview theme: author our own `.fw-dockview-theme` class from
  scratch (approach A).** Dockview exposes a set of ~30 `--dv-*` CSS
  variables read by its built-in themes (`.dockview-theme-light`,
  `.dockview-theme-dark`, ...). Rather than layering overrides on top
  of their light theme, we define all the vars inside our own
  `.fw-dockview-theme { ... }` selector and apply *that* class
  (instead of any `.dockview-theme-*`) to the dockview container.
  Fewer tokens to remember, and no coupling to their theme internals.

## `tokens.css` contents

Direct port of the `:root { … }` block from
`reference_info/design-system-prototype.html` (lines 12–88). Keep the
section comments (spacing, colors, typography, etc.) for navigability.
Omit nothing — even unused tokens are cheap and useful as a vocabulary
renderer authors can reach for.

## `panel-chrome.css` contents

The `.panel`, `.panel__header`, `.panel__title`, `.panel__controls`,
`.panel__control`, `.panel__body`, `.panel__body--flush`,
`.panel__body--compact` rules from the prototype (lines ~127–180),
with:

- Class names prefixed with `fw-` (`.fw-panel`, `.fw-panel__header`,
  etc.).
- `.fw-panel` sheds `overflow: hidden` — dockview's panel container
  already handles clipping.
- Scrollbar styles kept as-is.

These describe the **contents inside** a dockview panel — the outer
panel frame, tabs, and separators are dockview's concern (see
`dockview-overrides.css`).

## `fw-dockview-theme.css` contents

A single `.fw-dockview-theme { ... }` selector that defines every
`--dv-*` variable dockview reads. The mapping pulls from our tokens
wherever it makes sense; flat opinion for phase 0, to be tuned once
we see it rendered in step 3. Starting mapping (values are all
`var(--fw-token)` or direct literals):

| Variable | Value | Rationale |
| --- | --- | --- |
| `--dv-group-view-background-color` | `var(--surface-panel)` | Panel body sits on white. |
| `--dv-tabs-and-actions-container-background-color` | `var(--surface-sunken)` | Tab strip subtly distinct from body. |
| `--dv-activegroup-visiblepanel-tab-background-color` | `var(--surface-panel)` | Selected tab merges with body. |
| `--dv-activegroup-hiddenpanel-tab-background-color` | `var(--surface-sunken)` | Other tabs in focused group. |
| `--dv-inactivegroup-visiblepanel-tab-background-color` | `var(--surface-sunken)` | Selected tab in unfocused group. |
| `--dv-inactivegroup-hiddenpanel-tab-background-color` | `var(--surface-inset)` | Unfocused + hidden. |
| `--dv-activegroup-visiblepanel-tab-color` | `var(--text-primary)` | Focused tab text. |
| `--dv-activegroup-hiddenpanel-tab-color` | `var(--text-muted)` | Other tabs in focused group. |
| `--dv-inactivegroup-visiblepanel-tab-color` | `var(--text-secondary)` | Selected in unfocused. |
| `--dv-inactivegroup-hiddenpanel-tab-color` | `var(--text-muted)` | Unfocused + hidden. |
| `--dv-tab-divider-color` | `var(--border-subtle)` | Between sibling tabs. |
| `--dv-separator-border` | `var(--border)` | Between panel groups / sashes. |
| `--dv-paneview-header-border-color` | `var(--border-subtle)` | Tab strip bottom border. |
| `--dv-paneview-active-outline-color` | `var(--accent)` | Active panel outline. |
| `--dv-scrollbar-background-color` | `var(--border)` | Content scrollbar. |
| `--dv-tabs-container-scrollbar-color` | `var(--border)` | Tab overflow scrollbar. |
| `--dv-drag-over-background-color` | `rgba(63, 80, 194, 0.08)` | Subtle accent wash on drop target. |
| `--dv-drag-over-border-color` | `var(--accent)` | Drop target outline. |
| `--dv-icon-hover-background-color` | `var(--surface-inset)` | Close/overflow icon hover. |
| `--dv-sash-color` | `transparent` | Resize handle idle. |
| `--dv-active-sash-color` | `var(--accent-subtle)` | Resize handle active. |
| `--dv-floating-box-shadow` | `0 4px 12px rgba(0, 0, 0, 0.08)` | Floating group shadow (soft). |
| `--dv-tabs-and-actions-container-height` | `var(--panel-header-h)` | 32px, matches prototype. |
| `--dv-tab-font-size` | `var(--text-caption)` | 11px. |
| `--dv-tabs-and-actions-container-font-size` | `var(--text-caption)` | 11px. |
| `--dv-border-radius` | `var(--radius-none)` | Flat panels match prototype. |
| `--dv-tab-margin` | `0` | Flat, no gap. |
| `--dv-overlay-z-index` | `999` | Dockview default. |
| `--dv-active-sash-transition-duration` | `0.1s` | Dockview default. |
| `--dv-active-sash-transition-delay` | `0.5s` | Dockview default. |

workstation-core will apply `className="fw-dockview-theme"` on the
dockview container element (step 3). Expect to tune the exact color
mapping once we see it live.

## `fonts.css` contents

```css
@import url("https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap");
```

Matches the prototype's font loading verbatim.

## `all.css`

```css
@import "./fonts.css";
@import "./tokens.css";
@import "./panel-chrome.css";
@import "./fw-dockview-theme.css";
```

Note: `all.css` does **not** import dockview's own stylesheet. That's
a separate concern — `workstation-core` will `import "dockview/dist/styles/dockview.css"` (or the equivalent) at its entry
alongside `@fxtr-workstation/theme/all.css`. This keeps the theme
package free of peer-dep coupling to dockview.

Consumers typically want "just wire it all up." Importing
`@fxtr-workstation/theme/all.css` once in the demo's entry point does
that in one line.

## `package.json` exports

```json
{
  "name": "@fxtr-workstation/theme",
  "exports": {
    "./tokens.css": "./styles/tokens.css",
    "./panel-chrome.css": "./styles/panel-chrome.css",
    "./fw-dockview-theme.css": "./styles/fw-dockview-theme.css",
    "./fonts.css": "./styles/fonts.css",
    "./all.css": "./styles/all.css",
    ".": { "import": "./src/index.ts", "types": "./src/index.ts" }
  }
}
```

The `"."` export stays because `tsc --build` expects a buildable
project; `src/index.ts` remains an empty stub.

## Implementation todos

- [x] Create `styles/` directory.
- [x] Port token block from prototype → `styles/tokens.css`.
- [x] Port panel-chrome rules (prefixed `fw-`) → `styles/panel-chrome.css`.
- [x] Write `styles/fw-dockview-theme.css` with the full `--dv-*`
      mapping.
- [x] Create `styles/fonts.css` with the Google Fonts @import.
- [x] Create `styles/all.css` that @imports the four siblings.
- [x] Update `package.json` `exports` to expose each CSS file.
- [x] Update `projects/fxtr_workstation/ts/CODE_OVERVIEW.md` with the
      package's file layout and the import pattern.
- [x] Verify `pnpm check` (tsc) still clean and `pnpm lint` still
      clean. → both clean; biome picked up the 5 new CSS/JSON files
      without complaint.

## Notes from implementation

- The `panel-chrome.css` port dropped the `.panel`'s `overflow:
  hidden`; dockview clips the panel container, and for hover cards
  we'd rather not clip in the parent class.
- `--dv-border-radius: var(--radius-none)` maps to `0px`. If tuning
  later wants subtly rounded tabs, swap to `var(--radius-card)` (4px).
- Didn't include `dockview-core/dist/styles/dockview.css` in `all.css`
  — keeping the theme package peer-dep-free. workstation-core imports
  the dockview stylesheet separately at its entry (step 3).

## Out of scope for step 2

- Any panel tab styling, resizable-sidebar styling, context-menu /
  preview-card styling — those land with their owning components in
  step 3+.
- Higher-level renderer styles (`.card`, `.data-table`, `.article`,
  `.transcript`, `.code-view`, etc. from the prototype). Renderers
  use tokens directly and author their own CSS modules / inline
  styles; we don't ship these as a blessed library in phase 0.
- Dark mode or theme switching. The design system is light-only for
  now.
