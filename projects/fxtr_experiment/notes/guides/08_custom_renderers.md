# Rendering your data in the dashboard

Your experiment's grids render in the workstation dashboard out of the
box — statuses, currency, generations, frames as tables. But the
*values* in your cells are your own entity types, and they render as
generic entity views until you teach the UI about them. This guide
covers writing custom renderers, assuming your workflow runner is
hosted in a fxtr workstation and viewed through its panel shell (the
`examples/fxtr_experiment_demo` setup). It is abstracted from the
workstation demo's agent instructions
(`examples/fxtr_workstation_demo/agent_workdir/README.md`, "Writing a
custom visualizer") — go there for the fully-worked walkthrough.

## The dispatch model

fxtr separates presentation from content. The frontend never hardcodes
what an entity looks like; for every entity (or service) it needs to
show, it asks the backend's **renderer dispatcher**: "got a renderer
for type X under dispatch key Y?" — where X is the entity's **type id**
(the `define_type(NS, "name")` id on your dataclass) and Y is the
**rendering context**:

- `FullPanel` — the type opened as its own panel;
- `BlockLink` / `InlineLink` — how a reference to it renders as a
  block-level / in-sentence link (typically a labeled pill);
- `HoverPreview` — the tooltip preview;
- `Figure` — embedded block inside a report (optional; has a fallback).

Renderer modules are TSX files compiled to ESModules **via nix** and
delivered to the browser dynamically. Any renderer can itself dispatch
other entities (`<RenderDispatch .../>`-style), which is what makes
vibe-coded renderers compose: your `JudgeResult` panel can render its
`transcript` ref as a link that opens the transcript's own renderer.

Because the dashboard renders cell values through this same dispatch,
**writing a renderer for your entity type is all it takes** for your
results to show up properly inside the workflow dashboard's cell
drawer, in reports, and anywhere else a ref appears. The behavior demo
registers renderers for its types (`behavior_entities`: transcripts as
chat bubbles, configs as cards, judge verdicts as score badges, the
analysis as a table) — crib from those.
**NEEDS CODE REFERENCE**: point at the exact directory
(`examples/fxtr_experiment_demo/agent_workdir/renderers/...`) once the
naming settles post renderer-consolidation.

## The workflow, step by step

1. **Have a type** — a `DataclassEntity` in an importable package, and
   its printed id string (`MyType.fxtr_type.id_string()`).
2. **Write the module** — `renderers/<name>/index.tsx` in your workdir,
   default-exporting `{ <subtype-key>: { [FullPanel.uuid]: Component,
   ... } }`. Components receive a **stub** (a ref), not loaded data:
   each component allocates a load itself and renders by status — the
   `makeLoadResultAtom` helper from `@fxtr-ui/state-util` collapses
   this to one call. See the workstation demo README's `Recipe` example
   for a complete minimal module, and its import-allowance list
   (renderer TSX has no `node_modules`; only the curated
   `react`/`jotai`/`@fxtr-ui/*` externals resolve — keep modules
   self-contained).
3. **Declare the slice** — `renderers/<name>/fxtr_renderer.nix`
   declares which `(type, dispatch key)` pairs the module covers, and
   the workdir `flake.nix` merges the slices into the UI preset
   (`fxtr.uiPresets.default`), building each type's `typeEntity` layer
   from the workdir's type-entities venv.
4. **Push the preset** — renderers are CODE, pinned by git commit:
   commit, then tell the dispatcher to load the preset at that commit.
   In the experiment demo the dev loop is packaged as one script:

   ```
   uv run python reload_renderers.py --workdir <your workdir>
   ```

   which re-pins the workdir to your working copy, re-pushes the
   preset, and reloads open panels in place. On a build/validation
   failure the previous renderer table stays in effect — fix, commit,
   re-push. (Driving it manually — `set_preset(current_commit(),
   "fxtr.uiPresets.default")` on the `renderer-dispatcher` service —
   is the workstation demo's flow.)

Services render the same way (keyed by the service type id, receiving
a `ServiceRefStub`, driving invokes instead of loads) — that's how the
demo's driver panels (the button UIs) are built.

## Design advice for experiment renderers

- **Lead with the refs.** Your entities carry provenance refs
  (guide 2); render them as links/figures so every number is
  click-through-able to its evidence. The dispatch system means you get
  the target's renderer for free.
- **Cover the link keys, not just FullPanel.** Most encounters with
  your type are as a ref inside something else (a cell drawer, a
  report, another entity's panel); a good `InlineLink` pill +
  `HoverPreview` does more for browsability than a fancy panel.
- **Frames need no renderer** — dataframe cells and frame outputs
  render through the built-in frame-table renderer (the frame entity is
  self-describing). Write renderers for your *entity* types.
- **Keep renderer state versioned** if you persist panel state
  (`ownStorage`) — see the workstation README's `validityCheck`
  pattern; stale persisted shapes from older renderer builds otherwise
  crash newer ones.

## TODOs for this guide

- **TODO**: a copy-paste quickstart specific to the *experiment* demo
  workdir (which flake lines to touch, where `behavior_entities`'
  slices live) — write after the renderer-consolidation plan
  (`plans/phase4_monitoring_ui/renderer_consolidation.md`) fully lands,
  since it moves renderer sources between the workdir and the repo lib.
- **NEEDS CONFIRMATION**: for a *new user's own project* (not the
  demos), is the recommended starting point "scaffold a workdir with
  `scaffold_workdir.py` and add renderers there", or "copy the
  agent_workdir template into your own repo"?
- **OPEN QUESTION**: should custom *dashboard cell* renderings (beyond
  the entity drawer — e.g. a custom summary chip per cell in the grid
  table) be documented, or is that surface deliberately not
  user-extensible yet?
