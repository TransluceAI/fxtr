# Chunk 5 — surface polish (frame rendering + doc sweep)

**Status: complete (2026-07-17).** See Notes/log.

*(Rescoped at chunk start — the original sketch called for a frame
`CellReport` variant: row count + schema + truncated preview rows
pushed through `inspect_cell` and the cell watch. Struck on review:
it predated chunks 3–4. `CellReport` already carries the frame entity
id, which crosses the browser transport as an `EntityRefStub` and
renders as an openable `RefLink`; the workstation's generic entity
fallback already loads any entity via `ctx.allocateLoad` and shows it
as a `JsonRefTree` — so frame content is already reachable in the
dashboard with zero new wire surface, and the "must not error"
minimum was met in chunk 3. The whole remaining gap is
presentational.)*

## Scope

**Zero Python / wire changes.** The frame value stays exactly the
entity we already have; truncation is a display choice, not a wire
policy.

- **`FrameTable`** (frontend): a component rendering a loaded
  `FrameEntity` value — a small pure decode of the self-describing
  form (tagged schema + row arrays; bytes arrive as `Uint8Array`
  via the escape codec, rendered as a length/hex gist; NaN arriving
  as `null` is a pre-existing platform-wide JSON quirk, not ours) —
  into a table: header row from the schema (name + kind, nullable
  marker), first N rows with a "show all" expander, row count.
- **Registered as the frame entity type's renderer** in the dynamic
  dispatch registry (the README's visualization doctrine: renderers
  key on the fxtr type id), so *every* frame ref — drawer, action
  log, hover previews, other panels — renders as a table rather than
  the generic CBOR tree. Panel + hover-preview contexts as the
  registry supports.
- **Inline in the cell drawer**: dataframe cells and `FrameOutput`
  results embed the same component (an `allocateLoad` on the stub the
  report already carries), so the common "look at the cell" gesture
  shows the table without a click.
- Accepted v0 trade: `allocateLoad` pulls the whole frame entity to
  the browser, on demand — fine at demo scale; the big-frame story
  (chunked cells, browser-side arrow) is already on the deferred
  list.
- **MH example touch-ups** (`notes/example_mh_pilot.md`): frames are
  built with `make_frame` / plain polars — no "factory TBD" remnants.
- **Final doc sweep**: retire the phase TODO blocks in
  `notes/dataframes.md` and `CODE_OVERVIEW.md` (design is current
  state now); frontend `LOCAL_CODE_OVERVIEW.md` gains the frame
  renderer; promote a phase-closing entry through
  `notes/discussion_log.md` per the standing policy.

## Tests

- Frontend: the pure frame-entity decode (schema forms incl.
  nullable, rows incl. null / bytes / unicode; malformed shapes fall
  back gracefully, never throw into the panel) + table-model
  truncation; root `tsc`/biome/vitest clean.
- No Python test changes (no backend changes); `uv run pytest` stays
  green.

## Notes/log

- **Landed as rescoped**, zero Python changes. `frame_table.tsx`
  carries the pure decode (`decodeFrameValue` / `formatFrameCell`),
  the table (`FrameTableView`, 20-row display truncation + show-all),
  the drawer embed (`DrawerFrameTable` over `PanelState.frameLoads` —
  keyed `allocateLoad` slots, capped at 8; `PANEL_KIND` bumped to
  v10 with the structural check grown in the same edit), and the
  dispatch renderers exported under the `frame_v0` subtype key.
- Drawer gating: dataframe cells embed under the value ref link;
  task results embed when the grant's output spec carries `schema`
  (a `FrameOutput`'s wire form; `EntityOutput` crosses empty).
- **HoverPreview carries no storage slot** (`HoverPreviewProps` is
  `NoProps`), so the hover renderer is a static card — no load; the
  FullPanel is where the table lives. Discovered at typecheck; the
  original sketch assumed hover could load.
- Deferred-nix note: the `frame_v0` dispatch entries ship in the
  module now but resolve only once the renderer dispatcher registers
  the module for the frame type id
  (`plans/phase4_monitoring_ui/renderer_consolidation.md` gains that
  association when it lands); until then the drawer embed is the
  live path and bare frame refs fall back to the generic entity
  panel (raw value still visible).
- MH example needed only a one-line join-arity touch-up (it was
  already `make_frame`/plain-polars). Doc sweep: the phase TODOs in
  `notes/dataframes.md` / `CODE_OVERVIEW.md` retired, the stale
  `CellReport`-variant sentence replaced with the entity-loading
  doctrine, the phase-closing entry promoted through
  `notes/discussion_log.md`.
- Live browser verification (the documented human pass for
  perceptual behavior) not yet run — flagged to do on the next demo
  session; the pure decode + dialect harness + tsc/biome/vitest are
  the automated coverage.
