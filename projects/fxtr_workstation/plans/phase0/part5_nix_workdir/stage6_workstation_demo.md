# Stage 6 — `fxtr_workstation_demo` migration

Port the assembly-stage demo to the nix-driven code/entity-decoupled
path. Marks the end of the v0 vertical slice. Everything in earlier
stages is green via `basic_test_demo` + integration tests by the time
we get here; this stage touches the demo **once**.

> **Path note (2026-06-01):** the demo lives at
> **`examples/fxtr_workstation_demo/`**, not `projects/…` — earlier
> drafts of this file used the stale `projects/` path. All paths below
> are corrected.

> _NOTE 2026-06-02: the per-subtype `dispatchKeys` field in the snippets below
> was later renamed to `forDispatchKeys` (to parallel `forType`); the examples
> keep the original name as a record of this stage._

> **Revised (2026-05-30):** services are `fxtr.services` attrs spawned
> by git ref; the preset is a plain attrset pushed by git ref; the
> runtime workdir is its **own git repo**, scaffolded out of this
> monorepo. See `design_notes/05_sandbox_bundle.md`, `06_ui_presets.md`,
> `09_migration.md`.

## Prerequisites (split out)

Two pieces were carved off so this stage is "demo wiring only":

- **`stage6pre1_builtin_and_lib_entities.md`** — the `{ type =
  "builtin"; idString; }` entity shape (for `!basic_type`) +
  `lib.fxtr.entities.*` (ready-made `forType`s for the library types the
  `builtin/` renderer routes). **Must land before this stage.**
- **`stage5post3_run_script_set_preset_test.md`** — the agent-path
  `set_preset` E2E test on `basic_test_demo`. Independent; can land any
  time.

## Decisions locked in (2026-06-01)

- **Port everything**, iteratively allowed: old packages may stay
  un-wired across intermediate commits and get wired in later. Services
  are cheap (the only change is the `NixWasmServiceDefn` entry point).
- **The `builtin/` renderer stays demo-owned TSX** (at
  `agent_workdir/renderers/builtin/`), compiled by `esbuildRenderer`
  with an extended externals list, routed in the demo's `mkUiPreset`
  against `lib.fxtr.entities.*`. Lifting the whole builtin renderer +
  routing into `lib.fxtr` is a follow-up.
- **Only `delayed_echo`** (the *sandboxed* service) becomes a
  `fxtr.services.*`. `report_builder_service.py` + `docent_service.py`
  are **host** external services and are unchanged — only their
  *output-entity renderers* move into the preset.
- **The preset is named `default`** (`fxtr.workstation.uiPresets.default`).
- **One composed preset, auto-loaded on boot.** The launcher scaffolds
  the workdir, reads its HEAD, and configures the dispatcher with
  `initial_commit = HEAD` + `initial_attr =
  "fxtr.workstation.uiPresets.default"` so the agent panel renders
  before the agent does anything. `set_preset` is replace-not-merge, so
  `default` is a **superset** (builtin + every renderer); the agent
  re-pushes the same attr after editing the flake to add a renderer.
- **Scaffold is a script run inside the demo dir** taking a required
  dest path (not a global CLI). The runtime `fxtr` input resolves to the
  local repo via `git+file://`.
- **Orchestration inverts**: `run_demo_workstation.py` becomes the
  orchestrator and spawns Vite; the TS-side orchestrator
  (`frontend/src/server.ts`) goes away. The Python side already holds
  almost all the config (workdir, port, persistence, authorizers); the
  TS side only plumbed env vars.

## Workdir-as-its-own-repo + scaffold

The in-repo `agent_workdir/` is the **canonical template/source**
(version-controlled with the project; doubles as the note-09 example).
The *runtime* workdir is a copy-out into its own fresh git repo, via
`fxtr_workdir_harness.prepare_workdir_repo` (the same "copy + substitute
`fxtr` input + lock + `git init` + commit" the Stage 3/5 test fixtures
use). Rationale (unchanged): `current_commit()`'s clean-tree guard would
otherwise check the whole monorepo, and the agent would be committing
into the library/source repo.

### `scaffold_workdir.py`

New `examples/fxtr_workstation_demo/scaffold_workdir.py`: a thin wrapper
over `prepare_workdir_repo`.

```
uv run python scaffold_workdir.py <dest-path> [--force]
```

- Copies `agent_workdir/` → `<dest-path>`, substitutes the `fxtr` input
  to `git+file://<this-repo>?rev=<HEAD>`, locks, `git init` + commits,
  prints the path + HEAD.
- `--force`: `prepare_workdir_repo` refuses an existing dest; `--force`
  removes it first (you re-scaffold often during iteration).
- Reuses `resolve_lib_ref` defaults (local fxtr repo @ HEAD), so the
  scaffolded flake pins this checkout.

This concretizes the Q1.C/Q9 "scaffold a new workdir" story for real
users (the harness was previously test-only).

## Server-side wiring (`run_demo_workstation.py`)

- **New `--workdir <path>` (required)** — the scaffolded runtime repo.
  The launcher `git -C <path> rev-parse HEAD`s it to get the auto-load
  commit.
- **Dispatcher**: replace `mount_demo_dispatch(_RENDERERS_ROOT)` with

  ```python
  mount_nix_configurable_dispatch(
      repo=workdir,
      initial_commit=head,
      initial_attr="fxtr.workstation.uiPresets.default",
  )
  ```

  yielding `mounted.service_ref` + `mounted.scope`, plugged into the
  same `ExternalServiceConfig(external_name=RENDERER_DISPATCHER_NAME,
  …)` as today. The frontend is unchanged (same
  `WorkstationDispatcherProto` surface). Auto-load failures warn + come
  up empty (per `06_ui_presets.md`), so a cold/broken cache doesn't
  crash the demo.
  - **Cold-boot batch build:** the auto-load `set_preset` realizes — in one
    nix-parallel `build_drv_outputs` — every renderer ESM + every `typeEntity`
    layer, which on a cold cache also builds the aggregator venv + the
    `stage6pre1` library stub venv (offloaded to the builder). One-time and
    cacheable, but the first boot is slow — note it in the README.
- **Spawner**: the existing `sandbox-spawner` `ExternalServiceConfig`
  gains the `repo=workdir` configuration it needs to resolve
  `NixWasmServiceDefn` refs (mirror
  `_running_workstation_with_nix_spawner` in the MCP tests / the Stage 3
  spawner wiring).
- **Delete** the `mount_demo_dispatch` import + the `_RENDERERS_ROOT`
  constant.
- **Host services unchanged**: `mount_report_builder()` /
  `mount_docent_service()` + the seeded `demo-note` / `demo-transcript`
  entities stay (host-side; their types match the workdir copy because
  both come from the same package source — content-addressed type ids).

### Orchestration inversion (spawn Vite from Python)

- `run_demo_workstation.py` spawns Vite as a child process after the
  workstation is up, passing the env the frontend needs:
  `VITE_WORKSTATION_URL = ws.client_url`,
  `VITE_WORKSTATION_DEBUG_UI_URL = ws.debug_http_url`,
  `VITE_RENDERER_DISPATCHER_NAME = RENDERER_DISPATCHER_NAME`. It owns the
  child's lifecycle (terminate on SIGINT/SIGTERM, kill on timeout) the
  way `server.ts` owned the Python child before. A `--no-frontend` flag
  keeps the headless mode (tests / debug-UI-only).
- **Delete `frontend/src/server.ts`** and the stdout protocol
  (`READY` / `DEBUG_UI` / `RENDERER_DISPATCHER_NAME` lines) — no longer
  parsed by anyone. `frontend/package.json`'s `dev` script becomes plain
  `vite` (the script Python invokes, e.g. via `pnpm --filter
  @fxtr-workstation-demo/frontend exec vite`).
- **`start_demo.sh`** becomes a thin wrapper over the Python entry:
  `uv run python run_demo_workstation.py --workdir "$1" "${@:2}"` (it now
  takes the scaffolded workdir path). Update its header comment.

## The workdir flake (`agent_workdir/flake.nix`)

Modeled on `basic_test_demo`'s flake, but — since the big demo has many
renderers — the preset is **decomposed per renderer**: each renderer dir
carries a `fxtr_renderer.nix` next to its `index.tsx` that returns its
own slice of the `mkUiPreset` entries (the spiritual successor to the
deleted `fxtr_renderer.json`, keeping each renderer's routing co-located
with its code). The root flake imports each slice and merges them into
one `mkUiPreset` call. One `default` preset, the sandboxed service, and
(for parity / to demonstrate `retrieve_nix_entity`) one DATA entity.

### Per-renderer slices (`renderers/<name>/fxtr_renderer.nix`)

Each slice is a **function** of the deps it needs, returning a
name-keyed `mkUiPreset` entry (or entries). It compiles its own
renderer from `./.` (the file sits beside its `index.tsx`), so the
renderer's externals + dispatch routing live with the renderer.

`builtin/fxtr_renderer.nix` — needs only `lib` (its `forType`s all come
from `lib.entities.*`):

```nix
{ lib }:
{
  builtin = {
    renderer = lib.esbuildRenderer {
      src = ./.;
      # Builtin re-exports library frontend packages beyond the base set.
      # TODO(daniel): figure out better system for external workstation
      # modules (so renderers don't each carry a divergent externals list).
      externals = lib.workstationExternals ++ [
        "@fxtr-ui/agent-plugin-renderer" "@fxtr-ui/rd-types-stream"
        "@fxtr-ui/reports" "@fxtr-ui/workstation"
      ];
    };
    subtypes = {
      agent_mcp_running_session       = { forType = lib.entities.agentMcpRunningSession;       dispatchKeys = [ "21b2…FullPanel" ]; };
      log_script_execution            = { forType = lib.entities.logScriptExecution;           dispatchKeys = [ "4b2e…StreamEntry" ]; };
      log_invoke_result               = { forType = lib.entities.logInvokeResult;              dispatchKeys = [ "4b2e…StreamEntry" ]; };
      log_invoke_error                = { forType = lib.entities.logInvokeError;               dispatchKeys = [ "4b2e…StreamEntry" ]; };
      log_callback_notification       = { forType = lib.entities.logCallbackNotification;      dispatchKeys = [ "4b2e…StreamEntry" ]; };
      workstation_service_placeholder = { forType = lib.entities.workstationServicePlaceholder; dispatchKeys = [ "f1e2…InlineLink" ]; };
      basic_type                      = { forType = lib.entities.basicType;                    dispatchKeys = [ "b3c4…BlockLink" "c4d5…InlineLink" ]; };
      root_report                     = { forType = lib.entities.rootReport;                   dispatchKeys = [ "21b2…" "b3c4…" "c4d5…" "d5e6…" ]; };
      sub_report                      = { forType = lib.entities.subReport;                    dispatchKeys = [ "21b2…" "b3c4…" "c4d5…" "d5e6…" "f4a3…Figure" ]; };
    };
  };
}
```

`note/fxtr_renderer.nix` — needs `lib` + the agent-authored `forType`
(built from a workdir-package venv in the root, passed in):

```nix
{ lib, noteType }:
{
  note = {
    renderer = lib.esbuildRenderer { src = ./.; };          # base externals suffice
    subtypes."note" = { forType = noteType; dispatchKeys = [ "21b2…" "b3c4…" "c4d5…" "d5e6…" ]; };
  };
}
```

`transcript/` is the same shape (`{ lib, transcriptType }`).
`docent_fxtr_proxy/fxtr_renderer.nix` takes its three types
(`{ lib, dqlResultType, dqlSchemaType, metadataFieldsType }`) and
declares three `subtypes` under one compiled renderer.

> **Externals — confirmed (2026-06-02).** esbuild **never resolves**
> externalized imports (`--external:<pkg>` leaves the bare import untouched
> and skips disk resolution), so out-of-flake `@fxtr-ui/*` deps are a
> non-issue even though `esbuildRenderer` runs in a hermetic derivation with
> no `node_modules` — no rewriting needed. Confirmed by grepping the actual
> renderers:
> - All renderer imports are **package-root** (zero subpath imports like
>   `@fxtr-ui/reports/handlers/…`), so each `--external:<pkg>` matches cleanly.
> - The full import union is the base set + four extras
>   (`@fxtr-ui/{agent-plugin-renderer,rd-types-stream,reports,workstation}`);
>   **only `builtin/` uses the extras**, so it passes the extended list while
>   `note` / `transcript` / `docent_fxtr_proxy` compile with the default
>   `lib.workstationExternals`. (Type-only imports like
>   `import type … from "@fxtr-ui/core"` are stripped by esbuild — not even
>   externals.)
> - The frontend's `installFxtrExternals({…})` (`frontend/src/client/main.tsx`)
>   already provides every one of those specifiers, and the current demo
>   renders these exact renderers — so the runtime side is proven and Stage 6
>   doesn't touch the frontend.
> - `pkgs.esbuild` (nix) vs the old host `npx esbuild`: same flags, and Stage 5's
>   `test_esbuild_renderer` proved the nix path emits correct ESM with externals
>   preserved. The richer renderers are just more (externalized) imports.
>
> The remaining care item is the **externals ↔ shim manual coupling** (the
> `TODO(daniel)` above): a *future* renderer importing a new `@fxtr-ui/*`
> package must be added to **both** the esbuild externals list **and**
> `installFxtrExternals`. Fixed/known set for Stage 6.

### Root flake (assembly)

The root builds the one aggregator venv + the agent-authored `forType`s
(the single `typeEntitiesVenv` backs every type's typeEntity *and* the
`demo_note` DATA entity), imports each renderer slice, and merges:

```nix
let
  lib = fxtr.lib.fxtr;

  # ONE aggregator venv over all the agent-owned type packages (see
  # "Type-entity venv" below). `mkEntityFromPython` imports each type's
  # module from this single venv to materialize its typeEntity layer.
  typeEntitiesVenv = lib.mkVenv { workspaceRoot = ./type_entities; };

  forType = idString: pyConst: { inherit idString; entity = lib.mkEntityFromPython {
    venv = typeEntitiesVenv; fromPython = pyConst; layerKey = "type"; }; };
  noteType       = forType "<paste>" "presentables:NOTE_TYPE_ID";
  transcriptType = forType "<paste>" "transcripts:TRANSCRIPT_TYPE_ID";
  dqlResultType  = forType "<paste>" "docent_fxtr_proxy:DQL_RESULT_TYPE_ID";
  # … dqlSchemaType, metadataFieldsType …

  # Merge the co-located per-renderer slices. Entry names are unique, so
  # `//` is the right merge; mkUiPreset still throws on a duplicate
  # (type-id, uuid) across slices.
  rendererEntries =
       (import ./renderers/builtin/fxtr_renderer.nix          { inherit lib; })
    // (import ./renderers/note/fxtr_renderer.nix             { inherit lib noteType; })
    // (import ./renderers/transcript/fxtr_renderer.nix       { inherit lib transcriptType; })
    // (import ./renderers/docent_fxtr_proxy/fxtr_renderer.nix { inherit lib dqlResultType dqlSchemaType metadataFieldsType; });
in {
  fxtr.services.delayed_echo = lib.mkWasmService { src = ./packages/delayed_echo; name = "fxtr-delayed-echo"; };

  # DATA entity — also from the aggregator venv (presentables provides DEMO_NOTE).
  fxtr.entities.demo_note = lib.mkEntityFromPython {
    venv = typeEntitiesVenv; fromPython = "presentables:DEMO_NOTE"; layerKey = "demo_note"; };

  fxtr.workstation.uiPresets.default = lib.mkUiPreset rendererEntries;
}
```

(`lib.entities.*` referenced by the builtin slice is the `lib.fxtr.entities`
attrset from `stage6pre1` — `lib` here *is* `fxtr.lib.fxtr`.)

### Type-entity venv (one aggregator, packages untouched)

The agent-owned types (`note` / `transcript` / the three docent types) +
the `demo_note` DATA entity all need their owning package importable so
`mkEntityFromPython` can pin each type's `FxtrID` into a `typeEntity` layer
(which the dispatcher's eager `set_preset` validation builds + checks against
the preset key). Confirmed (2026-06-02): `presentables` / `transcripts` /
`docent_fxtr_proxy` all import **only `fxtr2.*`** at module top level (Docent
SDK is lazy-imported inside methods), so these are light fxtr2-only venvs — no
heavy closure, no stub needed (unlike the library types).

Rather than a `mkVenv` per package — which, because uv only honors the
**resolution-root's** `[tool.uv.sources]`, would force each package to carry
its own comment-flip `fxtr2`/`fxtr`/`mashumaro` sources + build-dep hints +
`uv.lock` — use **one aggregator**: `agent_workdir/type_entities/pyproject.toml`,
a *virtual* project (no `[build-system]`, no `src/`) that path-deps all three
packages and declares the lib sources + hints **once**:

```toml
[project]
name = "fxtr-demo-type-entities"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = ["presentables", "transcripts", "docent_fxtr_proxy"]

[tool.uv.sources]
presentables      = { path = "../packages/presentables", editable = true }
transcripts       = { path = "../packages/transcripts", editable = true }
docent_fxtr_proxy = { path = "../packages/docent_fxtr_proxy", editable = true }
fxtr2 = { path = "../../../v2", editable = true } # fxtr-lib-sub: active-in-template
# fxtr2 = { git = "@FXTR_LIB_URL@", rev = "@FXTR_LIB_REV@", subdirectory = "v2" } # fxtr-lib-sub: active-in-workdir
# fxtr + mashumaro: same comment-flip convention as demo_data
[tool.uv.extra-build-dependencies]
cbrrr = ["setuptools"]   # + sqlitedict / mashumaro / fxtr / fxtr2, per demo_data
```

- The three packages stay **untouched** (they already declare `fxtr2` as a
  dep; no source redeclaration on them). One `uv.lock`, one comment-flip site,
  one venv build/closure instead of three.
- It lives **in the workdir** (built from the committed repo at the preset's
  git rev), so its `fxtr2` source carries the `# fxtr-lib-sub:` markers that
  `prepare_workdir_repo` / the scaffold rewrite to the pinned git rev — same as
  `demo_data`.
- This is **not** a uv *workspace* (`[tool.uv.workspace] members`) — members
  must live under the root; the packages are a sibling `../packages/`, so plain
  path-dep sources are the robust form.
- This does **not** replace the `stage6pre1` library-types stub
  (`nix/fxtr_lib_type_ids`): that exists to avoid `fxtr_workstation_mcp`'s heavy
  closure; an aggregator depending on it would pull that closure back. So:
  stub for library/builtin types, aggregator for agent-owned types — two venvs
  total for the whole demo's typeEntities.
- **Verify at build time:** uv2nix is happy with a virtual root (no
  `[build-system]`) — `workspace.deps.default` should install the three path
  deps. If it's fussy, the fallback is a packaged aggregator with an empty
  `src/` module. Confirm with a `nix build` of one entity (as the stub was
  verified).

The dispatch-key UUIDs + per-type subtype keys + id-strings are lifted
verbatim from the existing `fxtr_renderer.json` files (`builtin/`,
`note/`, `transcript/`, `docent_fxtr_proxy/`) — the JSON's
`(subtype_key, for_type_id, dispatch_keys)` rows map 1:1 onto
`mkUiPreset`'s `subtypes` shape. `mkUiPreset` throws on any duplicate
`(type-id, uuid)`, so a transcription slip is caught at `set_preset`.

The `fxtr` input is the substitution placeholder + the
`# fxtr-lib-sub:` markers (copy from `basic_test_demo`'s flake), filled
by `scaffold_workdir.py` / the test harness.

## `agent_workdir` package + renderer changes

- **`packages/delayed_echo/`** — adopt the `mkWasmService` convention:
  `[project.entry-points."fxtr.service"]` entry point + the comment-flip
  `[tool.uv.sources]` (`# fxtr-lib-sub:` markers) like
  `basic_test_demo`'s `hello_service`; drop the committed `uv.lock` (the
  scaffold/harness regenerates it in the copy).
- **Type-id constants** — ensure `presentables`, `transcripts`,
  `docent_fxtr_proxy` expose a module-level type-id object
  `mkEntityFromPython`'s `fromPython` can name (a `*_TYPE_ID` constant or
  `Cls.fxtr_type`), plus a `DEMO_NOTE` object for `fxtr.entities.demo_note`.
  No `[tool.uv.sources]` / build-hint changes to these packages — the
  aggregator (below) owns the uv2nix concerns.
- **`type_entities/` aggregator** — add `agent_workdir/type_entities/`
  (`pyproject.toml` + generated `uv.lock`; no `src/`, no `[build-system]`)
  per "Type-entity venv" above: the single `mkVenv` root for every agent-owned
  typeEntity + the `demo_note` DATA entity.
- **Renderers** — keep the `index.tsx` files; **replace each
  `fxtr_renderer.json` with a `fxtr_renderer.nix`** carrying that
  renderer's `mkUiPreset` slice (the function shape above), and delete
  the JSON. `builtin/` stays "don't edit" (its `index.tsx` is
  unchanged; it just gains a `fxtr_renderer.nix`). `note/` /
  `transcript/` / `docent_fxtr_proxy/` keep their TSX as-is.
- **`.gitignore`** — add `agent_workdir/.gitignore` mirroring
  `basic_test_demo`'s (keep the runtime `.fxtr_workstation.toml`,
  `.tmp_outputs/`, `result*`, venvs out of the committed tree so
  `current_commit()`'s clean-tree guard + the untracked notice ignore
  them).

## Deletions

- **`demo_dispatch.py`** (the `BasicDynamicDispatchImpl` fork +
  `DemoDispatcherProto` + `mount_demo_dispatch`).
- **`packages/demo_dispatch/`** (the agent-side `recompile()` handle).
- **All `renderers/*/fxtr_renderer.json`**.
- **`frontend/src/server.ts`** + the stdout protocol.
- No reference to `prepare_package_bundle` anywhere in the demo
  materials. (`preamble.py` + `packages/` **stay** — host-built sandbox
  is unchanged; `packages/` is also where the `mkWasmService` source
  lives.)

## Documentation rewrites

- **`agent_workdir/README.md`** — currently marked OUT OF DATE. Rewrite
  the two loops:
  - **DATA loop (no commit):** edit a `fxtr.entities.*` source,
    `run_script` → `retrieve_nix_entity("demo_note")`, see it (tracked
    edits live; new files need `git add`).
  - **CODE loop (git):** edit a renderer / the preset / a service,
    `git commit`, then reference by `current_commit()` —
    `set_preset(current_commit(), "fxtr.workstation.uiPresets.default")`
    for renderers; `NixWasmServiceDefn(current_commit(),
    "fxtr.services.delayed_echo")` → `spawner.spawn(defn)` for the
    service.
  - Replace the **renderer-iteration** section (`demo_dispatch.recompile()`)
    with the commit→`set_preset` loop. Replace the **spawn** section
    (`prepare_package_bundle`) with the `NixWasmServiceDefn` flow (crib
    from `basic_test_demo/agent_workdir/README.md` + `05_sandbox_bundle.md`).
  - Update the "writing a custom visualizer" section: drop the
    `fxtr_renderer.json` step; the new step is "print the type's
    id_string, add a `mkUiPreset` entry to the flake, commit,
    `set_preset(current_commit(), …)`."
- **`examples/fxtr_workstation_demo/README.md`** — new operator flow:
  (1) `scaffold_workdir.py <path>`; (2) `start_demo.sh <path>` (or
  `run_demo_workstation.py --workdir <path>`); (3) point Claude at
  `<path>`. Note the workdir is now its own git repo, the `fxtr` input
  pins this checkout, and the agent commits there. Note **first boot is
  slow** (cold-boot batch build above). Update the Layout block (remove
  `demo_dispatch.py`, the `fxtr_renderer.json` mention, the
  `packages/demo_dispatch/`; add `scaffold_workdir.py`, `flake.nix`,
  `type_entities/`, and the per-renderer `fxtr_renderer.nix`).

## Sequencing note

Deliberately last — migrating earlier would churn the demo across each
stage. `basic_test_demo` was updated as we went (it doubles as a test
fixture), so the helpers were all exercised before we touch the big
demo once here.

## Relevant existing code

- `projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/`
  — the migrated reference: `agent_workdir/flake.nix`,
  `run_demo_workstation.py`, the renderer-as-`esbuildRenderer` +
  `mkUiPreset` shape.
- `nix/fxtr_workdir_harness/` — `prepare_workdir_repo` + `resolve_lib_ref`.
- `examples/fxtr_workstation_demo/{run_demo_workstation,demo_dispatch}.py`,
  `frontend/src/server.ts`, `start_demo.sh` — the files being
  rewired/deleted.
- `…/dynamic_dispatch/nix_configurable_dispatch/` —
  `mount_nix_configurable_dispatch`.

## Relevant design notes

- `design_notes/09_migration.md` — hard-cut layout + template/scaffold
  responsibilities (the migrated demo workdir is the canonical in-repo
  example).
- `05_sandbox_bundle.md`, `06_ui_presets.md` — the git-ref spawn + preset
  flows the demo now uses.

## End-of-stage check

- Operator runs `scaffold_workdir.py <path>` then `start_demo.sh <path>`;
  the workstation + Vite both come up (Python owns both), the agent
  panel renders on boot via the auto-loaded `default` preset.
- Agent connects, renders log events / Note / Transcript / docent
  results / reports through `NixConfigurableDispatchImpl`-served
  renderers (pushed/auto-loaded by git ref), and spawns `delayed_echo`
  via a `NixWasmServiceDefn`.
- `demo_dispatch.py`, `packages/demo_dispatch/`, every
  `fxtr_renderer.json`, and `frontend/src/server.ts` are gone.
- Workdir docs walk a new user through the DATA loop (no commit) and the
  CODE loop (git).
- No reference to `prepare_package_bundle` in the demo materials;
  `preamble.py` + `packages/` stay.
- Update CODE_OVERVIEW.md (`fxtr_workstation_agent_plugin` if the demo
  inventory lives there; the demo's own README layout block) as the
  final step.

## Iteration order (suggested commits)

1. Scaffold script + `prepare_workdir_repo` reuse; `agent_workdir`
   `.gitignore`; `delayed_echo` → `mkWasmService` pyproject; the
   `type_entities/` aggregator (pyproject + `uv.lock`) + type-id constants.
   (Workdir is buildable + scaffoldable; verify the aggregator venv builds
   one entity — the virtual-root check.)
2. `flake.nix` `default` preset (per-renderer `fxtr_renderer.nix` slices +
   merge) + `fxtr.services.delayed_echo` + `fxtr.entities.demo_note`; delete
   `fxtr_renderer.json`s.
3. `run_demo_workstation.py` rewire (dispatcher + spawner repo +
   `--workdir`) + delete `demo_dispatch.py` / `packages/demo_dispatch/`.
4. Orchestration inversion (Python spawns Vite; delete `server.ts`;
   `start_demo.sh` + `frontend/package.json` updates).
5. Doc rewrites + CODE_OVERVIEW updates.

Each commit can keep prior behavior partially working (e.g. step 2's
flake builds before step 3 wires it in), per the "iteratively allowed"
decision.

## Implementation notes (2026-06-02)

Implemented in three jj commits (`fxtr ws 5.6 impl 1/2/3`) + a docs commit.
What landed and how it deviated from the plan:

**Verified.** Against a scaffolded copy on the `x86_64-linux` builder: the
scaffold (`scaffold_workdir.py` → `prepare_workdir_repo`) runs end-to-end;
the `default` preset evals to 14 type keys (no duplicate-claim throw); the
**full dispatcher build path** (`eval_preset_table` + `build_drv_outputs`,
i.e. exactly what the auto-load `set_preset` does) builds all 17 drvs —
including the **aggregator venv**, confirming the virtual-root
`[tool.uv] package = false` works under uv2nix; the `delayed_echo` wasm
`sitePackages` + the `demo_note` layer build; a sample renderer is real ESM.

**Deviations from the plan (all minor):**
- **Chunks 3 + 4 were done as one commit** (`impl 3`) — they're coupled by
  the `--workdir` threading + the Vite-spawn inversion; splitting them left
  an awkward broken intermediate (a required `--workdir` with no caller).
- **`fromPython` uses `Cls.fxtr_type` / existing constants** rather than
  adding new `*_TYPE_ID` constants: `docent_fxtr_proxy` already exported
  them; `Note`/`Transcript` use `Cls.fxtr_type`. Only **`DEMO_NOTE`** was
  added (to `presentables`, for the DATA entity).
- **Dropped the stray `allow_llm_apis = True` debug override** in
  `run_demo_workstation.py` so the `--allow-llm-apis` flag is honored. (It
  was unconditionally forcing LLM authorizers on.)

**Not verified / to revisit:**
- **The full live demo was not run in this environment.** It needs a heavy
  `uv sync` of the demo's closure + a cold sandbox-wheel build, and then
  parks. So: the flake build path is verified (above), and the
  spawner/dispatcher/auto-load wiring *mirrors* the green MCP test fixtures
  (`test_in_memory_e2e_nix_spawn` / `_running_workstation_with_nix_dispatcher`)
  — but the **Vite subprocess spawn/teardown and the end-to-end browser
  render are unverified**. Worth a manual `./start_demo.sh <workdir>` pass.
- **Vite teardown is best-effort:** `_maybe_run_vite` does `terminate()` then
  `kill()` after 5s, but `pnpm`-spawned `vite` grandchildren may be orphaned
  on a hard kill (no process-group handling). Fine for a dev launcher; revisit
  if it leaks.
- **`frontend/src/client/App.tsx`** still hints "Run via the orchestrator
  (`pnpm dev` or `./start_demo.sh`)" — but `pnpm dev` is now plain `vite`
  (no `VITE_*` injection), so only `./start_demo.sh` works. Stale hint, harmless.
- **`tsx` is now an unused devDependency** in `frontend/package.json`
  (`server.ts` deleted). Left to avoid a pnpm-lock churn; drop on next touch.
- **`fxtr.entities.demo_note` overlaps the host-seeded `demo-note`** (two
  different mechanisms for a similar Note). Intentional — `demo_note`
  demonstrates `retrieve_nix_entity`; the host-seeded one demonstrates the
  renderer without driving the agent. Could dedup later.
- **externals ↔ shim coupling** (the `TODO(daniel)` in
  `renderers/builtin/fxtr_renderer.nix`): the builtin renderer's extended
  externals must stay in sync with the frontend's `installFxtrExternals`.
