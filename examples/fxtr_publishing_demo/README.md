# fxtr publishing demo

Build a small **report graph** with nix — provenance threaded through — and serve
it (with provenance exposed) over the stateless HTTP stack, viewed through the
shared `@fxtr-ui/published-app`. The publishing-flavored sibling of
`fxtr_workstation_demo`.

It demonstrates the whole build → serve → view arc working together: MDX reports
(`fxtr_report_builder`), custom entity renderers, figure dispatch to **both**, and
provenance on every derived entity.

## What gets built

```
seeded Notes (bare)  ──►  Stats (postprocess)  ──►  SubReport (MDX)  ──►  RootReport (MDX) = fxtr.entities.report
```

- **Notes** — seeded inputs, bare (the namespace / preload flow strips
  provenance). Rendered by the custom `renderers/note`.
- **Stats** — an aggregate over the notes (count / total words / longest title),
  built by the postprocess step, **provenanced**. Custom `renderers/stats`.
- **SubReport** / **RootReport** — MDX reports whose `RenderFigure` nodes embed
  the notes / stats / subreport. Rendered by the re-exported `@fxtr-ui/reports`
  handlers (`renderers/report`).

The served `RootReport` comes back **layered** (it carries the nix build as
provenance), and the same is true of the stats and subreport it embeds — open the
metadata for a figure in the viewer to see its `provenance_parent`.

## Prerequisites

- A configured nix backend (`fxtr_nix runtime setup`, or `FXTR_NIX_BACKEND=host`
  with `nix` on `PATH`) — the scaffold's relock + the serve build the flake.
- `uv` and `pnpm` + Node on `PATH`.

## Run it

```bash
cd examples/fxtr_publishing_demo

# 1. Scaffold a runnable workdir: copies the template into a fresh git repo,
#    seeds the Note bytes into its repo store, and relocks the preload FOD hashes.
uv run python scaffold_workdir.py /tmp/publishing-demo --force

# 2. Build the shared viewer once.
pnpm --filter @fxtr-ui/published-app build

# 3. Serve the built flake + the viewer on one origin.
uv run fxtr_nix serve /tmp/publishing-demo --port 8080 \
  --web-dist ../../projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-published-app/dist

# open http://127.0.0.1:8080
```

In the viewer: the **Roots** panel lists the served entities — open `report` to
render the `RootReport`. Its MDX body renders, each `RenderFigure` dispatches (the
nested subreport via the `@fxtr-ui/reports` renderers, the notes/stats via the
custom ones), and a figure's metadata shows its `provenance_parent`.

### Frontend dev loop (HMR)

To iterate on the viewer with hot reload, run the serve and vite separately:

```bash
uv run fxtr_nix serve /tmp/publishing-demo --port 8080          # terminal 1
VITE_PUBLISHED_BACKEND_URL=http://127.0.0.1:8080 \
  pnpm --filter @fxtr-ui/published-app dev                       # terminal 2
```

## What just happened

| Served thing            | Built by                                             |
|-------------------------|------------------------------------------------------|
| `report` (`RootReport`) | the `report` builder (MDX over notes + stats + subreport) |
| its `stats` figure      | the postprocess builder (aggregate over the notes)   |
| its `subreport` figure  | the subreport builder (MDX over stats + two notes)   |
| `demo:note_<k>` pins    | `seed_data.py` (committed in `root.fxtr.tsv`)         |
| the `dispatcher` service + renderers | `fxtr.uiPresets.demo` (built by `fxtr_nix serve`) |

Notes are **bare** (inputs); every derived entity is **provenanced** (carries the
nix build). See `CODE_OVERVIEW.md` for the structure and key conventions.

## Tests

```bash
# Headless provenance round-trip (nix_backend sweep; skips with no backend):
uv run pytest          # -k host to pick a single backend
```

> The hub story (pushing to a shared bare repo) is out of scope — the scaffolded
> repo is strictly local.
