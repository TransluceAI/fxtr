# Renderer consolidation — one dispatch path + the repin/reload dev loop

**Status: complete** (landed 2026-07-18 as commits 1–5 below; the
follow-up — converting the other `_peer_rpc` copies — also landed
2026-07-18, see "Commit structure"). Scoped 2026-07-17; supersedes and replaces
`deferred_nix_wiring.md`, whose settled design and gathered context
are folded in below — the scoping round is logged in
`notes/discussion_log.md`. Loop economics as measured: a
renderer-only iteration is ~45–50s end to end (repin + relock +
preset rebuild + reload), dominated by the workdir-side venv/lock
rebuilds; `mkVenv` source narrowing is the deferred improvement if
that starts to hurt.

## Why (the current state)

Renderers reach the experiment demo's browser through three different
channels today:

1. **The nix preset** (the normal path, shared with
   `examples/fxtr_workstation_demo`): the workdir flake merges
   per-renderer slices into `mkUiPreset`, served by the generic
   `NixConfigurableDispatchImpl`. The demo-owned renderers (exp_note,
   the two drivers, behavior entities, the builtin shim) live here
   appropriately — but so does `renderers/config_entities/`, ~460
   lines of real renderer source for `fxtr_experiment`'s own
   `GridConfigEntity` / `SliceTemplateEntity` types, with the demo
   workdir template as its source of truth and a heavyweight
   `fxtr_experiment` dependency in the workdir's type-entities venv
   just to mint those two type entities.
2. **Static dev mounts**: `DevDashboardPanel.tsx` in the demo frontend
   discovers workflow-capability services by a hand-pasted type id and
   statically imports `@fxtr-experiment/dashboard` through the pnpm
   workspace. This is the *only* mount of the dashboard — opening
   `experiment-workflow` from the roots panel gives the generic
   service fallback, and no link pills render for the capability.
3. **Orphaned exports**: `frontend/src/index.tsx` exports the
   `frame_v0` renderers (phase 5) and the `experiment_workflow`
   subtype, but no dispatcher registers the module, so bare frame refs
   fall back to the generic entity panel.

The structural cause: there are two existing ways for repo-owned
renderer code to reach a workdir preset — as an import-map *external*
(the `builtin/` shim shape; right for substrate packages every shell
bundles, wrong for the dashboard) or as an *esbuild slice built from
repo source by reference* (the shape the repo nix lib was always meant
to expose; nothing uses it yet). This plan establishes the second
channel, moves everything `fxtr_experiment`-shaped onto it, deletes the
dev mount, and replaces the HMR special case with a general
repin-and-reload dev loop that works for every renderer.

## End state

Launching `examples/fxtr_experiment_demo` and opening
`experiment-workflow` from the shell's roots panel renders the
dashboard through the real dynamic dispatch — the same flow as the
workstation demo, nix all the way down. All `fxtr_experiment` renderer
source lives in `projects/fxtr_experiment/frontend/`; the workdir
carries only demo-owned renderers plus one merge line. Iterating on any
renderer (repo-lib or workdir) is `edit → reload_renderers.py → watch
the open panel reload in place`.

## The settled design

### 1. The repo-lib slice: `lib.fxtr.experimentRenderers`

New `nix/lib/experimentRenderers.nix`, exported from
`nix/lib/default.nix`: a function `{ keyIds }:` returning preset
entries in the same shape a workdir `fxtr_renderer.nix` slice returns,
so a preset flake merges it with one line. **One esbuild module**
(`lib.esbuildRenderer` over `projects/fxtr_experiment/frontend/src`)
serving four subtypes — the subtype keys MUST match `index.tsx`'s
default-export keys:

| subtype key           | forType (kind)                          | dispatch keys |
|-----------------------|-----------------------------------------|---------------|
| `experiment_workflow` | user capability (SERVICE type)          | fullPanel, blockLink, inlineLink, hoverPreview |
| `frame_v0`            | canonical frame entity                  | fullPanel, hoverPreview (links fall back on purpose) |
| `grid_config`         | stored grid config entity               | fullPanel, blockLink, inlineLink, hoverPreview |
| `slice_template`      | stored slice template entity            | fullPanel, blockLink, inlineLink, hoverPreview |

One module serving several subtypes is proven precedent (the demo's
`behavior_entities` slice). Anyone opening a config entity almost
certainly already loaded the dashboard module, so the shared bundle
costs nothing in practice; split into two modules later only if bundle
weight ever matters.

### 2. Type entities: the stub-mirror route

Extend `nix/fxtr_lib_type_ids` with a `WORKFLOWS3_NS` mirror block
(mirror of `fxtr_experiment._ns.WORKFLOWS3_NS` =
`6843e93c-d2d8-4b1b-bfbe-592e4ca6defc`) and four one-line
`define_type` re-declarations, each with its canonical-source comment:

| stub constant | canonical source | label | idString (hand-pasted, eager-validated) |
|---|---|---|---|
| `EXPERIMENT_USER_CAPABILITY_TYPE_ID` | `fxtr_experiment.user_service:USER_CAPABILITY_TYPE_ID` | `user_capability` | `dpk4lldwrt6vws64rxlxqrscmnr6zwu2jkzjsyfshmuxpb4o6radq` |
| `EXPERIMENT_FRAME_TYPE_ID` | `fxtr_experiment.frame_encoding:FRAME_TYPE_ID` | `frame_v0` | `dgryl6ovr2364hrc4dgnbimcmexxkg7iniwydcdzqhj6n5365ik7a` |
| `EXPERIMENT_GRID_CONFIG_TYPE_ID` | `fxtr_experiment.encoding:GRID_CONFIG_TYPE_ID` | `grid_config_v0` | `d4ogp2tmcknjyf7jgjxqnfcaw3bcwsun3dmmce6xlw45efji3xhbq` |
| `EXPERIMENT_SLICE_TEMPLATE_TYPE_ID` | `fxtr_experiment.encoding:SLICE_TEMPLATE_TYPE_ID` | `slice_template_v0` | `dzxosrmzceiu2i3bnxivfy5equooxbmnynrcgjjo2socixzhxzscq` |

FxtrIDs are content-addressed, so the stub entities are byte-identical
to the real ones; the only cost is the keep-in-sync comments, and the
dispatcher's eager `set_preset` validation catches any drift loudly.
The four `forType` values join `nix/lib/entities.nix` (which already
owns the stub venv + the `forType` helper), grouped under an
"fxtr_experiment types" comment; `experimentRenderers.nix` consumes
them from there. The stub package's docstring widens its scope
statement from "workstation / report type-ids" accordingly.

(The venv route — `mkVenv` over `projects/fxtr_experiment` — stays
rejected: it drags the package's full closure (polars etc.) into the
preset build for identical output.)

### 3. elkjs: bundled from a pinned npm tarball

The dashboard's one non-`@fxtr-ui` bare import is `elkjs` (the plate
view's layout engine, imported as `elkjs/lib/elk.bundled.js`). It must
be **bundled into** the compiled `.mjs`, not externalized — it's a
real npm dep, not a workstation-provided one. Mechanism:

- `esbuildRenderer` gains an optional `nodeModules` arg (a derivation
  containing a `node_modules/` tree). When set, the build copies `src`
  into the sandbox work dir and symlinks the `node_modules` beside it,
  so esbuild's upward resolution finds non-external bare imports. The
  no-`node_modules` guard is preserved for every renderer that doesn't
  pass the arg.
- `experimentRenderers.nix` builds the tree from the npm registry
  tarball — elkjs has **zero runtime dependencies**, so this is one
  pinned `fetchurl` + untar into `node_modules/elkjs`, no pnpm
  machinery needed. Keep the pinned version consistent with the
  frontend `package.json` range (a keep-in-sync comment on both ends).

`plate_layout.ts` constructs `new ELK()` with no `workerUrl`, which in
`elk.bundled.js` runs layout on the main thread — no Blob worker is
expected in the served bundle, but the verification pass smokes the
Plates tab explicitly.

### 4. Source narrowing (rebuild hygiene)

Flake sources are single store paths, so any repo change invalidates
every derivation reading any subpath. `esbuildRenderer` re-adds its
`src` through `builtins.path` (content-addressed, fixed `name`), so an
unchanged renderer dir keeps its store path and the slice does not
rebuild when unrelated files change — this is what makes a
dashboard-only iteration rebuild exactly one esbuild slice. Narrowing
`mkVenv` the same way is **deferred, measure-first**: uv2nix reads
relative path deps outside the workspace root, so it's more involved,
and the venvs may prove cheap enough to rebuild (deps are cached FODs).

### 5. The config renderer moves in-repo

`examples/fxtr_experiment_demo/agent_workdir/renderers/config_entities/index.tsx`
relocates to `projects/fxtr_experiment/frontend/src/config_entities.tsx`
(adapted to the package's conventions — root tsc/biome, relative
imports + curated externals only), with its components registered in
`index.tsx`'s default export under the `grid_config` /
`slice_template` subtype keys. The workdir dir is deleted; the workdir
flake drops its `configEntityTypes` block; the workdir's
`type_entities` aggregator drops its `fxtr_experiment` dependency
(pyproject + the source redeclarations), lightening the venv.

### 6. Demo preset wiring

The merge seam is already waiting in the demo workdir flake
(`rendererEntries` carries the comment "The dashboard slice from the
repo lib joins here"):

```nix
// (lib.experimentRenderers { inherit keyIds; })
```

The `keyIds` UUIDs there are the same dispatch keys `index.tsx`
exports (`FullPanel.uuid` etc.), already proven by the driver
renderer. Wiring the slice into the demo preset IS the build coverage
— no standalone nix check target. (Debug tip, from demo experience: an
opaque `IRException` from a preset build is best debugged via a direct
`nix build .#<attr>` of the failing slice from the workdir.)

### 7. Delete the dev mount

`DevDashboardPanel.tsx` and its `App.tsx` wiring
(`useDevDashboardRegistrations`) are deleted outright — no flag-gated
variant. The roots-panel dispatch path becomes the only mount. (Remove
the demo frontend's `@fxtr-experiment/dashboard` workspace dep if
nothing else imports it.) What HMR bought us is mostly preserved
anyway: the dashboard keeps all its state in `ownStorage` (workstation
session state), so a dispatch-level reload remounts the panel with its
watch mirrors and navigation intact.

### 8. The repin/reload dev loop

The hot-reload machinery already exists at both ends: the dispatcher's
`set_preset(git_commit, flake_attr)` re-evaluates the preset,
nix-builds what changed, and broadcasts a
`ReloadEverythingNotification`; `createDynamicDispatcher` subscribes,
wipes its module caches, and re-resolves every mounted entry (open
panels grey via the staleness wrapper and re-render). What's missing is
the repin step and the one-command driver:

- **`fxtr_workdir_harness` gains an in-place repin**:
  `repin_workdir(workdir, *, lib_repo=None, lib_rev=None, uv=False)`.
  It reuses `resolve_lib_ref` (jj working-copy `@` → incremental mirror
  push — no manual commit needed; jj snapshots the working copy),
  rewrites the `# fxtr-lib-sub:` marked lines **in place** (the marker
  comments persist in workdir copies; unlike scaffold-time
  substitution, the tokens are gone, so the rewrite replaces the
  already-substituted URL/rev on marked lines), re-runs
  `nix flake lock` through the configured `fxtr_nix_runner` backend
  (the workdir commits a `flake.lock`; the dispatcher builds
  `--no-update-lock-file`), commits the workdir, and returns the new
  HEAD. `uv=True` additionally repins the uv git sources + reruns
  `uv lock` — off by default because renderer iteration doesn't need
  it and skipping avoids venv rebuilds. Exposed as a small CLI
  (`python -m fxtr_workdir_harness.repin <workdir> [--uv]`) since the
  mechanics are fully general.
- **`fxtr_workstation` gains the client-side RPC helper**
  (`fxtr_workstation/peer_rpc.py`): `open_workstation_rpc(rpc_url, *,
  name)`, an async CM yielding fxtr2's
  `RPCSession[WorkstationServiceProto]` (`.remote_service` +
  `.scope`) — the public client counterpart of the private
  server-side `_ws_transport.py`, same `JSONObjectStream` EOF
  discipline: httpx_ws websocket → send/receive callables
  (`HTTPXWSException` → `BrokenResourceError` / `EndOfStream`) →
  `JSONObjectStream` → `wrap_json_stream` →
  `open_rpc_scope(our_main_service=None,
  remote_service_type=WorkstationServiceProto)`. This is today's
  `_peer_rpc` block, copy-pasted across both demo orchestrators +
  three frontend_lib demo workstations + the agent-plugin example;
  this plan converts only `run_experiment_demo.py`, in the helper's
  own isolated commit. The other sites are the planned **immediate
  follow-up** after the consolidation lands (see "Commit structure")
  — mechanical, but each conversion needs its demo re-smoked, so they
  don't ride the consolidation commits.
  `httpx-ws` becomes a declared dependency via a **`client` optional
  extra** (`fxtr_workstation[client]`), not a core dep — discovered at
  implementation time: `tests/_websocket_util.py` documents a
  deliberate stance that the core workstation library not require
  `httpx-ws` at runtime (it's why the MCP and the tests each carry
  their own copy of the httpx-ws quirk workaround). The extra
  preserves that stance while fixing the existing smell that the
  demos import `httpx_ws` without declaring it anywhere (it arrives
  transitively via fxtr v1's deps). `peer_rpc` is import-on-demand
  and not re-exported from the package `__init__`, so importing
  `fxtr_workstation` never requires the extra. It also folds in the
  httpx-ws self-referential-exception-context workaround the demo
  copies lacked.
- **The demo gains `reload_renderers.py`**: repin (via the harness),
  then over one `open_workstation_rpc` session:
  `get_service_by_external_name("renderer-dispatcher")` →
  `replace_service_type(NixDispatcherProto)` (the documented retype
  contract for external-name lookups) → resolve into the peer scope →
  `set_preset(new_head, "fxtr.uiPresets.default")`. The script must
  document its behavior thoroughly in its docstring: what it rewrites,
  that it commits the workdir, that it requires the demo to be running
  against a durable `--workdir` (not `--auto-workdir`, which wipes on
  relaunch), and its failure modes (demo not running → connect
  refused; dispatcher name not registered; a failing preset build
  surfacing as an `IRException` — point at the direct `nix build`
  debug tip). Workdir-local renderer edits need no repin — the script
  detects nothing changed in the pin and still commits + re-pushes the
  preset, so it's the one command for both cases.

**Loop economics**: after a repin, nix rebuilds every derivation whose
source store path changed. With `esbuildRenderer` narrowing (item 4), a
renderer-only edit rebuilds one esbuild slice (seconds); the venvs
rebuild only when a workdir commit actually changes their source trees
— except that today a workdir commit changes the whole workdir flake
source path, so **measure the loop first** and narrow `mkVenv` if the
venvs dominate. First build after a fresh scaffold batch-builds
everything (slow; cached after) — unchanged.

## Implementation steps / commit structure

Each numbered group below is one logically-distinct commit (docs and
CODE_OVERVIEW updates for a group ride its commit, per the standing
convention); the sequence is ordered so every intermediate state keeps
the demo working (the dev mount survives until the dispatch path is
verified).

**Commit 1 — `fxtr_workstation.peer_rpc`** (isolated on purpose;
depends on nothing else here):

- [x] The `open_workstation_rpc` helper + the `httpx-ws` dependency
      declaration (landed as the `client` extra — see the design
      bullet); convert `run_experiment_demo.py`'s `_peer_rpc` /
      `_ws_stream` to it (one converted call site proves the helper).
      *(Landed with a real-websocket round-trip test,
      `tests/test_peer_rpc.py`; downstream uv.locks refreshed.)*

**Commit 2 — nix groundwork** (repo-lib only, no demo behavior
change):

- [x] **Stub type ids**: the `WORKFLOWS3_NS` block in
      `nix/fxtr_lib_type_ids` (four constants + keep-in-sync comments;
      widen the module docstring's scope statement); the four
      `forType` entries in `nix/lib/entities.nix`. *(All four stub id
      strings verified byte-identical to the canonical package's;
      `lib.fxtr.entities.experiment*` attrs verified by pure eval of
      the mirror-pinned flake.)*
- [x] **`esbuildRenderer` changes**: `builtins.path` source narrowing;
      the optional `nodeModules` arg (copy-src + symlink layout).
      *(Verified by building `renderer-esm-check` through the podman
      backend: ESM output with externals kept bare.)*

**Commit 3 — the slice + the config-renderer move** (the dashboard
becomes dispatch-served; the dev mount still exists as fallback):

- [x] **Config renderer move**:
      `frontend/src/config_entities.tsx` (adapt to package
      conventions), register both subtypes in `index.tsx`, delete the
      workdir renderer dir, drop `configEntityTypes` from the workdir
      flake, drop `fxtr_experiment` from
      `agent_workdir/type_entities/pyproject.toml`. Root
      `pnpm exec tsc --build` / biome / vitest stay green.
- [x] **`nix/lib/experimentRenderers.nix`** (+ the elkjs
      `node_modules` derivation inside it — elkjs pinned at the
      pnpm-lock-resolved 0.11.1) exported from `default.nix`; one
      merge line in the demo workdir flake. *(Headless-verified: an
      `--auto-workdir` boot scaffolds and eagerly builds + validates
      the full preset through podman with no warnings — the slice,
      the elkjs bundling, and the stub type entities all build.)*
- [x] Verification items 1–4 (the dispatch path, with the dev mount
      still available for comparison) — the human browser pass.
      *(Passed 2026-07-18; item 2's frame material came from the
      extra2 chunk-5 dataframe migration, which landed in between.
      The pass also surfaced and fixed a latent wire-codec bug: the
      base64 validation regex overflowed Chrome's stack on the
      multi-MB module — see the codec commit.)*

**Commit 4 — delete the dev mount** (only after commit 3 verified):

- [x] `DevDashboardPanel.tsx`, its `App.tsx` wiring, and the demo
      frontend's dashboard workspace dep (removed). *(Headless-checked:
      the shell boots with only Roots/Debug panels, dashboards open
      through dispatch, no console errors.)*

**Commit 5 — the reload loop**:

- [x] **Harness repin**: `repin_workdir` + the `__main__` CLI in
      `nix/fxtr_workdir_harness`; extend the harness's mechanics tests
      (the `relock=False` family) with an in-place repin round trip.
      *(One design point beyond the sketch: the repin commit sweeps
      the whole workdir tree on purpose, so pending workdir-local
      renderer edits ride the same commit — one command for both
      iteration cases.)*
- [x] **`reload_renderers.py`** in the demo, with the full behavioral
      docstring.
- [x] Verification item 5. *(Three live iterations: edit a repo-lib
      renderer label → reload → the change renders in open panels with
      state intact, no browser refresh; revert → reload → gone. Timing:
      ~45–50s per iteration, dominated by the workdir-side venv/lock
      rebuilds rather than the esbuild slice — `mkVenv` source
      narrowing stays the known deferred improvement, below.)*
- [x] Remaining docs-on-landing sweep (below).

**Immediate follow-up (landed 2026-07-18)**: the remaining `_peer_rpc`
copies are converted to `open_workstation_rpc` — one commit per site;
the inventory, recipe, and per-question resolutions are
`projects/fxtr_workstation/plans/peer_rpc_migration.md`.

## Verification (live pass)

Relaunch the demo against a durable `--workdir` and drive it from the
browser. Items 1–4 run at commit 3 — the dev mount still exists then,
but these items exercise only the dispatch path (opened from the roots
panel, not the dev sidebar buttons), with the mount available for
comparison; commit 4 then re-smokes that the shell is clean without
it, and item 5 runs at commit 5:

1. Open `experiment-workflow` from the shell's roots panel — the
   dashboard loads through the real dynamic dispatch (first exercise
   of the esbuild-bundled, non-HMR build: tree, drawer, mutations,
   watch-driven updates). Link pills render for the capability in the
   roots list.
2. Open a dataframe cell's value ref — the `FrameFullPanel` table must
   render through real dispatch (the drawer's *inline* table is a
   static import and proves nothing about the slice); hover a frame
   ref for the static card.
3. Open a grid-config ref from the drawer (and a slice-template ref if
   one is reachable) — the moved config renderers through real
   dispatch.
4. Switch to the Plates tab — the elkjs layout must run in the served
   bundle (main-thread `new ELK()`; this is the explicit smoke for the
   bundled-worker concern).
5. Run the reload loop end to end: edit something visible in the
   dashboard source, `reload_renderers.py`, watch the open panel grey
   and re-render with the edit and its `ownStorage` state intact.
   Time the loop; decide whether `mkVenv` narrowing is needed.

## Docs on landing

- `frontend/LOCAL_CODE_OVERVIEW.md`: fill the packaging section (its
  TODO points here); replace the dev-mount/HMR section with the
  repin/reload dev loop.
- `notes/monitoring_ui.md` § "Renderer packaging": drop the TODO;
  state the landed shape (including that the stored-config renderers
  are part of the repo package, not demo-added, and that there is no
  dev mount).
- The demo README's dashboard note + the workdir README (renderer list,
  type_entities scope).
- `nix/CODE_OVERVIEW.md` (new lib module + stub scope +
  harness repin), `projects/fxtr_experiment/CODE_OVERVIEW.md` (the
  phase-4 TODO), and the phase overview's status.
