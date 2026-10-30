# Phase 4 — the monitoring UI

Goal: the workflow **dashboard** — a workstation panel for understanding
the state of a workflow's grids at a glance and manipulating it (rerun,
clear, target, source, delete), per the standing design note
`notes/monitoring_ui.md`. The renderer drives the user capability
directly over the workstation invoke layer; the backend work is the
capability's introspection additions plus the change-event stream;
`examples/fxtr_experiment_demo` — a persistent experiment backend an
operator can build real state in and watch through the dashboard —
lands before the renderer and serves as its live harness.

## Chunks

Each chunk lands with tests, a CODE_OVERVIEW update (and, once chunk 4
creates it, `frontend/LOCAL_CODE_OVERVIEW.md` for anything touching the
frontend package — the top-level overview only points there), and
design-doc updates as decisions become current state; details and
deviations live in the chunk's own file (promote anything
design-relevant to `notes/discussion_log.md` and the canonical docs).

- [x] [chunk1_introspection.md](chunk1_introspection.md) — the
      capability's introspection additions + the missing mutation:
      `summarize`, `config_id` exposure, `inspect_generation`,
      `list_observers`, `list_bindings` + handle-cell boundness, and
      `delete_cell`.
- [x] [chunk2_change_events.md](chunk2_change_events.md) — the
      invalidation-event stream: session-side emission ("Events:"
      lines), the capability's `subscribe` (fxtr2 `EventSource`
      contract), coalescing.
- [x] [chunk3_experiment_demo.md](chunk3_experiment_demo.md) —
      `examples/fxtr_experiment_demo`: the persistent runner mount, the
      demo-driver scenarios, the workdir preset, operator docs.
      (Swapped ahead of the renderer, 2026-07-10: the demo lands
      dashboardless and serves as the renderer chunk's live harness.)
- [x] [chunk4_dashboard_renderer.md](chunk4_dashboard_renderer.md) —
      the in-repo renderer package (`projects/fxtr_experiment/frontend/`):
      the wire-dialect layer + live round-trip harness, the tree / grid
      detail / cell drawer panel, mutations, subscription-driven
      refetch. (The nix preset wiring was deferred out of the chunk at
      close — next bullet.)
- [x] [chunk5_watch_backend.md](chunk5_watch_backend.md) — watch
      subscriptions, backend + wire (design:
      `notes/event_system.md`): the recompute-and-diff watch engine
      over the internal hint stream, the `watch_*` capability methods
      + event schemas (`watch_api.py`), the step-log buffer, the
      workstation `subscribe_to` args extension; deletes the
      wire-level `subscribe` and `attach_path`. (Supersedes chunk 2's
      wire surface; the hint emission layer stays.)
- [x] [chunk6_watch_frontend.md](chunk6_watch_frontend.md) — the
      renderer rewrite onto folded watch mirrors: generic watch
      driver, per-view inboxes (drop = unsubscribe), mutations off
      watch-payload grants; deletes the refetch/attach-cache/hint-plan
      machinery. (Landed 2026-07-14; the live pass surfaced the
      `subscribe_to` `**kwargs` ergonomics rework and a known-gone
      `recomputing` fix — see the chunk file.)
- [ ] [chunk6post1.md](chunk6post1.md) — post-verification
      refinements noticed in the live pass: the multi-watch update
      ripple and generation-report (checkpoints/observations)
      streaming granularity. Both deferred, neither blocks the phase.
- [x] [renderer_consolidation.md](renderer_consolidation.md) — the
      renderer consolidation (supersedes the deferred nix-wiring
      handoff, rescoped 2026-07-17): the repo-lib slice serving the
      dashboard + frame + stored-config renderers, the config-renderer
      move in-repo, deleting the demo's dev mount, and the
      repin/reload dev loop that replaces the HMR special case.
      (Landed 2026-07-18; live pass also surfaced + fixed the wire
      codec's base64-validation stack overflow on multi-MB modules.)
- [x] [chunk7_part1_task_logging.md](chunk7_part1_task_logging.md) —
      task-level `log()`: `TaskContext.log`/`open_channel` (the latter
      a stub, like `StepContext`), the outer-level log plumbing through
      the run capability into the run-log stream. Pulled forward from
      the chunk-7 sketch (landed 2026-07-14). Step-level log lines
      landed with chunks 5–6.
- [ ] [chunk7_part2_channels.md](chunk7_part2_channels.md) —
      **optional, deferred (needs scoping)**: the rich output-channel
      system over fxtr2 channels (`open_channel()` becomes real), with
      the open integration/rendering questions (reader plumbing,
      task/step subchannel nesting) called out. May be dropped or moved
      to a later phase without affecting the rest.
- [x] [chunk8_plate_view.md](chunk8_plate_view.md) — the **plate view**:
      a second dashboard tab drawing the grids as a plate-notation graph
      (node per grid, plates for replication axes via containment +
      grant-matched cross-tree merges, typed grid-level edges). A minimal
      backend overlay (`grid_graph` + a `watch_graph` family) plus the
      frontend elkjs tab. Design: `notes/plate_view.md`.

## Cross-cutting decisions (settled in the 2026-07-09 scoping round)

- **No UI gateway service.** The renderer drives
  `ServiceRef[UserCapabilityProto]` directly: typed service dispatch
  decodes wire-form JSON into dataclass args impl-side and encodes
  returns symmetrically, so the browser speaks the same wire contract
  as any RPC client. New queries land on the capability itself (the
  one-capability doctrine); the `Workflow` wrapper gains matching
  passthroughs.
- **Introspection stays presence-class and absolute.** Every new query
  records nothing, nothing downstream can depend on it, and locations
  in outputs are key-path text + flattened coords (root-frame
  transparency). Presence queries stay **value-free**; content arrives
  only via `inspect_cell` (value with verdict, one snapshot).
- **Boundary discipline applies to every new wire method**: returns
  carrying ids re-attach them (`WorkflowSession.ensure_loadable`), with
  the membership-rationale comments `notes/boundary_discipline.md`
  requires.
- **Currency rollups force-evaluate.** `summarize` derives-and-stamps
  any unstamped cell it counts; the stamps make repeat summaries
  O(changes). First-summary cost on a large cold namespace is a
  measure-before-optimizing watch-item.
- **Change events are coalesced invalidation hints** — at-least-once,
  no payload beyond addressing (`grids` / `cells`+path / `intent` /
  `bindings`); subscribers reconcile by refetching, so missed or
  duplicated events are safe by construction. Emission rides the
  session (one point covers user- and task-driven mutations); the
  service-facing hub lives with the capability impl. *(Revised in the
  2026-07-12 round: the hints and their emission remain exactly this,
  but as an internal layer only — the wire serves watch subscriptions
  computed from them; `notes/event_system.md`, chunks 5–6.)*
- **Grants are a re-derivable cache** renderer-side: panels persist
  grants in `ownStorage` (versioned), and re-root on token-invalid or a
  fresh service stub. Nothing token-shaped is durable.
- **The demand affordance never auto-targets silently**: "Target + run"
  (explicit `add_target`, `adopt` prompted) vs "Run" on targeted cells;
  "Retry" (re-demand) vs "Clear + rerun" (`clear_cells` + demand) on
  errored cells.
- **The renderer is in-repo, first-class** (not demo-workdir code):
  esbuild-bundled against the standard externals, exposed through the
  repo nix lib so preset flakes include its slice by reference (the
  builtin-slice wiring shape); in the pnpm workspace for typecheck +
  vitest.
- **The demo hosts a persistent runner** (session + executor +
  capability, sqlite under the demo workdir) host-mounted in the
  orchestrator via the mount-lifespan pattern; the demo driver builds
  state through the same capability; sandboxed task fns are flag-gated
  and agent-driven, exactly as in the workstation demo.
- **External names**: `experiment-workflow` (the capability),
  `experiment-demo-helper` (the driver).

## Out of scope (this phase)

- Writing data / creating grids / binding handles from the UI.
- Server-side paging/filtering for `list_cells`; bulk value previews
  (the sanctioned future shape is a batched `inspect_cells`).
- Sub-panel decomposition of the dashboard.
- Queue/waiting visibility (needs a scheduling notion of waiting
  first); richer scheduling generally.
- Event-stream durability or replay (hints are ephemeral by design).

## Followups (noticed during the phase, not scoped into it)

- **`revoke_cell_sourcing` refuses `dataframe` cells** (kinds
  `("data", "handle")` only) while `write_cell_frame` creates dataframe
  sourcing records and `delete_cell` accepts all three leaf kinds —
  align the revoke when dataframe cells get real use.
- **Report-builder CLI local installs need `pnpm install
  --ignore-workspace`** (from the cli dir; documented in
  pnpm-workspace.yaml). A plain install silently targets the repo
  workspace, leaving the cli's node_modules missing/stale — which
  breaks the root `pnpm check` (its tsconfig references the cli).
  Structural fix candidates, deferred because both touch the nix
  build's lockfile FOD: a one-member pnpm-workspace.yaml in the cli
  dir (stops pnpm's upward walk, but rewrites the lockfile shape →
  `fetchPnpmDeps` re-pin), or moving the cli's typecheck out of the
  root tsconfig references. (`.npmrc ignore-workspace=true` does NOT
  work — flag-only in pnpm 10, verified.)

## Status

In progress. Chunks 1–6 complete: the watch subscriptions
(backend chunk 5, 2026-07-12; renderer chunk 6, 2026-07-14, verified
by a human-operated browser pass) are the live read path, replacing
the hint-refetch flow; chunk6post1 (generation-report streaming + view
simplification) and chunk 7 part 1 (task-level `log()`) landed
2026-07-14. The renderer consolidation
([renderer_consolidation.md](renderer_consolidation.md) — the nix
slice + the repin/reload dev loop) landed 2026-07-18.
Remaining: chunk6post1's
deferred item 1 (the update ripple), and the optional, needs-scoping
output-channel system ([chunk7_part2_channels.md](chunk7_part2_channels.md)).
Design: `notes/monitoring_ui.md` + `notes/event_system.md` (linked
from the design overview's decision map); scoping rounds logged in
`notes/discussion_log.md`.
