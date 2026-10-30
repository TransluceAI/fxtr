# Stage 5 — UI presets + nix/git dispatcher

Stand up the nix+git-driven UI preset path: an `esbuildRenderer` helper
that produces ESM derivations (esbuild *inside a nix derivation*), a new
dispatcher impl (`NixConfigurableDispatchImpl`) that resolves presets by
git ref via a `set_preset` method, and the workstation-side nix wrapper
it uses.

> **Revised (2026-05-30):** presets are **plain nix attrsets** (not
> entities) referenced by git commit; the dispatcher builds renderer
> ESMs and wraps them in `RendererEntity`s at serve time. No
> `mkDispatcherPreset` / `mkRendererEntity` / `write_*` writers.
> `LiveConfigurableDispatcherProto` → `NixDispatcherProto`. See
> `design_notes/06_ui_presets.md`.

> **Revised (2026-06-01, pinned with the user):** see "Design decisions"
> below. Net effect on the original sketch: **`entityIdString` is
> dropped entirely** (the agent gets a type's id_string from Python in a
> `run_script`, so a nix-native helper adds little — references to it
> have been removed from the design notes + overview); the mtime impl
> (`BasicDynamicDispatchImpl`) is **not physically moved** to a
> `deprecated/` dir — only its docstrings are softened to "no longer
> suggested"; and the stage's agent-facing demo work (agent-side
> `NixDispatcherProto` dup, `run_demo_workstation` wiring, an
> end-to-end `run_script`-driven `set_preset`) is **deferred to
> Stage 6** — Stage 5 ships only a **direct integration test** that
> drives `NixConfigurableDispatchImpl` in-process.

## Design decisions (pinned 2026-06-01)

These resolve the open questions in the original sketch + `06_ui_presets.md`
for the v0 implementation. Deviations from the design notes are flagged
and collected under "Deviations" at the bottom.

1. **`entityIdString` is dropped.** No nix helper. An agent obtains a
   type's id_string the same way the existing demos already document —
   in a `run_script`, `print(MyType.fxtr_type.id_string())` (or
   `retrieve_nix_entity` the type then read `.id_string()`) — and pastes
   that string as the preset's type-key. The `typeEntity` introspection
   slot in the preset shape **stays** (it's what eager validation
   builds; see #5). Removes the Stage-1 `esbuildRendererEntity` /
   `mkRendererEntity` / `mkDispatcherPreset` stubs from
   `nix/lib/default.nix`; `entityIdString` is simply never added.

2. **Preset eval → renderer build uses `drvPath`.** `set_preset`
   `nix eval --json … --apply <fn>` where `<fn>` maps every renderer
   leaf to its **`.drvPath`** (and every `typeEntity.layer` to *its*
   `.drvPath` + `layerKey`). The eval JSON therefore carries the whole
   routing table (type id_strings + dispatch-key uuids + subtype keys +
   renderer/typeEntity **drv paths**) with **no realization**. The
   dispatcher then `nix build`s each distinct renderer drv (`<drv>^out`)
   and reads the `.mjs` bytes. Robust to attr-name quoting (id_strings /
   uuids never become nix attr-path fragments we have to re-quote), one
   eval per `set_preset`.

3. **`esbuildRenderer` runs esbuild *at nix-build time*.** Unlike
   today's `compile.py` (runtime `npx esbuild`), the helper is a
   derivation running `${pkgs.esbuild}/bin/esbuild …`; `$out` is the
   single `.mjs` **file** (dispatcher does `Path(out).read_bytes()`).
   Default externals come from a new `lib.fxtr.workstationExternals`
   (the canonical list; the Python `DEFAULT_EXTERNALS` in
   `basic_dynamic_dispatch/compile.py` must stay in sync — note this in
   both).

4. **Stage 5 ships the full agent-facing API + a direct integration
   test; the demo round-trip is Stage 6.** "Agent-facing API" =
   everything reusable an agent needs to drive a dispatcher, so Stage 6
   adds *no new library plumbing* — only demo/test assembly:
   - Host side: `NixDispatcherProto`, `NixConfigurableDispatchImpl`,
     `mount_nix_configurable_dispatch` (chunk 3).
   - **Agent side: a `NixDispatcherProto` dup re-exported from
     `_fxtr_mcp_builtin`** (chunk 3b), so a `run_script` can
     `replace_service_type(NixDispatcherProto)` + `set_preset(...)`.
     Mirrors the `NixWasmServiceDefn` dup/re-export from Stage 3.
   - A **direct** in-process integration test that constructs the impl
     with `repo=<prepared workdir>` and calls `set_preset` (the analogue
     of Stage 3's "direct spawner integration test") — chunk 4.

   **Deferred to Stage 6** (uses the above, adds nothing new): wiring
   `NixConfigurableDispatchImpl` into `run_demo_workstation.py`,
   migrating the assembly demo's renderers/preset, and an end-to-end
   `run_script`-driven `set_preset(current_commit(), …)` test. Tracked
   in the Stage-6 additions.

5. **Key↔`typeEntity` validation is eager, at `set_preset` — same phase
   as the renderer builds.** Renderer drvs and `typeEntity` drvs both
   build eagerly in `set_preset`: after evaling the table, for each
   entry the dispatcher builds the renderer ESM(s) **and** builds that
   entry's `typeEntity.layer` (drv captured in the same eval), reads the
   pinned entity's id_string via a `LayeredFileStoreReader` (the read
   `retrieve_nix_entity` already does in `McpHostImpl`), and asserts it
   equals the entry's key. A mismatch **raises from `set_preset`**
   (whole-preset reject, table not swapped — consistent with how
   eval/build failures already behave); the agent sees an `invoke_error`
   immediately. `resolve` is then a **pure dict lookup** (no build, no
   validation, no per-type cache).

   *Why eager, not request-time:* lazy resolve-time validation can't
   catch the failure mode it exists for. `resolve` only validates on a
   *hit*, and on a hit the key already equals `type_ref.id_string()`; a
   *stale/wrong* key (note 06's "silently keying a stale id") makes
   `resolve` **miss** → `NoMatchFallback`, so the mis-keyed entry is
   never validated. Iterating all entries at `set_preset` catches both
   the stale-key case and the wrong-`typeEntity` case, at the moment the
   agent can fix it. It's also simpler (no resolve-path build/cache) and
   consistent with eager renderer builds. (If renderer builds ever go
   lazy — a deferred optimization — revisit validation then.)

6. **`BasicDynamicDispatchImpl` stays put.** Not moved to a
   `deprecated/` dir (avoids churning the several live importers —
   `examples/fxtr_workstation_demo/demo_dispatch.py`, the `frontend_lib`
   dynamic-dispatch demos). Its module/class docstrings just gain a
   "no longer the suggested impl — prefer `NixConfigurableDispatchImpl`
   for nix/git-driven workdirs" line. The physical move (if ever) is a
   later cleanup.

## Preset attr shape (authored in the agent's flake; no fxtr writer)

Keyed by the type's **id_string** (the attribute name). Each entry
carries a `typeEntity` (introspection + #5 validation) and a
`byDispatchKey` map. See `06_ui_presets.md`.

```nix
fxtr.workstation.uiPresets.<name> = {
  "<type-id-string>" = {
    typeEntity = <mkEntityFromPython attr>;        # { type, layer, layerKey }
    byDispatchKey = {
      "<dispatch-key-uuid>" = {
        subtypeKey = "default";                    # module[subtypeKey][keyUuid]
        renderer   = esbuildRenderer { src = ./renderers/<name>; };  # ESM drv
      };
    };
  };
};
```

The `set_preset` `--apply` reshapes this to the JSON the dispatcher
decodes (drv paths, no realization):

```nix
# applied to fxtr.workstation.uiPresets.<name>
preset: builtins.mapAttrs (idStr: r: {
  typeEntityLayerDrv = r.typeEntity.layer.drvPath;
  typeEntityLayerKey = r.typeEntity.layerKey;
  byDispatchKey = builtins.mapAttrs (uuid: e: {
    inherit (e) subtypeKey;
    rendererDrv = e.renderer.drvPath;
  }) r.byDispatchKey;
}) preset
```

## Implementation plan (chunks)

Each chunk is independently reviewable; check off as completed. The new
unknowns to retire early are the **in-derivation esbuild build** (chunk
1) and the **serve-time `RendererEntity` mint + frontend load through the
unchanged path** (validated by chunk 4's integration test).

### Chunk 1 — nix: `esbuildRenderer` + `workstationExternals` ✅

- [x] **`nix/lib/esbuildRenderer.nix`** — `{ canonicalSystem, pkgsFor,
      workstationExternals }: { src, externals ? workstationExternals,
      name?, system? }`. A `runCommand` that runs
      `${pkgs.esbuild}/bin/esbuild ${src}/index.tsx --bundle --format=esm
      --platform=browser --jsx=automatic --outfile="$out"` plus a
      `--external:<x>` per `externals`. `$out` is the compiled `.mjs`
      **file** (not a dir). No venv arg, no fxtr2.
- [x] **`workstationExternals`** — added to `lib.fxtr` (a plain list)
      mirroring `compile.py`'s `DEFAULT_EXTERNALS`; cross-referenced in
      both files' comments.
- [x] **`nix/lib/default.nix`** — wired in `esbuildRenderer` +
      `workstationExternals`; **deleted** the `mkRendererEntity` /
      `esbuildRendererEntity` / `mkDispatcherPreset` `notYet` stubs
      (`mkDockerPackageBundle` stub retained). `entityIdString` not added.
- [x] **Validated** via the repo-root `packages.x86_64-linux.renderer-esm-check`
      target (inline TSX fixture): builds to a 294-byte `.mjs` — ESM
      output, automatic-JSX transform (`import { jsx } from
      "react/jsx-runtime"`), externals kept as bare imports (`from
      "react"`). The esbuild-in-a-derivation unknown is retired.
- [x] **Lifted into a `py_testing` regression test**
      (`nix/py_testing/tests/test_esbuild_renderer.py`) — builds the
      `renderer-esm-check` target via the `nix_build` fixture and asserts
      the ESM shape + bare externals, matching the
      `test_hello_world` / `test_entity_from_python` convention (focused
      helper-rot guard, independent of chunk 4's heavier integration
      test). Green (1.25s, cache hit).
- [x] **Updated `nix/CODE_OVERVIEW.md`** — new helper + `workstationExternals`,
      removed-stubs note, the new test.

### Chunk 2 — workstation-side nix wrapper ✅

- [x] **New `fxtr_workstation/dynamic_dispatch/nix_configurable_dispatch/nix.py`**,
      modeled closely on `fxtr_sandboxing/nix.py` (`build_flake_ref` +
      `anyio.run_process`, **step-typed** errors that do *not* parse
      stderr):
      - `eval_preset_table(repo, commit, attr, flake_path=None) -> dict`
        — `nix eval --json <ref>#<attr> --apply '<fn>' --no-update-lock-file`
        (the `_PRESET_APPLY` reshape from "Preset attr shape"). Returns
        the decoded `{id_string: {typeEntityLayerDrv, typeEntityLayerKey,
        byDispatchKey: {uuid: {subtypeKey, rendererDrv}}}}`.
      - `build_drv_outputs(drv_paths) -> dict[str, Path]` — **one** `nix
        build "<drv>^out" … --json --no-link` over a *batch* (renderer drvs
        → `.mjs` files + typeEntity layer drvs → layered-store dirs). One
        invocation lets nix schedule the builds in parallel (its own
        `max-jobs`); `--json` yields an unambiguous `{drvPath: out}` map (no
        output-order guessing, no `anyio` ExceptionGroup that N concurrent
        subprocesses would bring). Building realized `.drv`s needs no
        `--no-update-lock-file` (lock concern is at eval).
      - Errors: `NixPresetRefError` base + `NixPresetEvalError` /
        `NixPresetBuildError`, each carrying `context`/`stderr`/`returncode`
        (`set_preset` re-raises with stderr in the message →
        `invoke_error`, per `11_failure_and_ux.md`; no `.tmp_outputs` dump).
      - Package `__init__.py` re-exports the wrapper symbols (impl added
        in chunk 3).
- [x] **Tests** (`tests/test_nix_preset_ref.py`): `build_flake_ref` unit
      + live eval-error paths (bad rev / missing attr / non-preset attr
      tripping the `--apply` / bad `flake_path`) against the repo-root
      flake — analogous to `fxtr_sandboxing/tests/test_nix_git_ref.py`.
      Good-path eval/build covered by chunk 4. **9 passed (0.80s).**

### Chunk 3 — `NixDispatcherProto` + `NixConfigurableDispatchImpl` ✅

- [x] **`NixDispatcherProto`** — added to `dynamic_dispatch_types.py`.
      **Deviation:** it is a **standalone** `@service_protocol Protocol`
      (re-declaring `resolve` / `subscribe_to_changes` + adding
      `set_preset(git_commit, flake_attr, flake_path=None) -> None`), **not**
      `NixDispatcherProto(WorkstationDispatcherProto)` as sketched — the
      service-resolver registry rejects registering a resolver for a subtype
      of an already-registered Protocol ("conflicting handlers"). Same
      workaround as `demo_dispatch.DemoDispatcherProto`; tracked as a
      protocol-inheritance follow-up in `followups.md`. Base
      `WorkstationDispatcherProto` unchanged → no frontend work.
- [x] **`nix_configurable_dispatch/service_impl.py`** — the impl +
      `mount_nix_configurable_dispatch` async-CM (mirrors
      `mount_basic_dynamic_dispatch`). Constructor `(scope, repo,
      impl_tg, *, initial_commit=None, initial_attr=None,
      initial_flake_path=None)`. `repo` required; `scope` hosts the
      minted `RendererEntity`s + listener workers (needs `GLOBAL_SCOPE`
      reachable for the `RENDERER_ENTITY_TYPE_ID` descriptor, same as
      basic). Wire-emits `WORKSTATION_DISPATCHER_TYPE_ID` from
      `_service_type_id` (frontend's `replace_target_type` flow unchanged).
      - **State.** `_table: dict[str, dict[str, RendererResolution]]`
        (id_string → uuid → resolution); `_renderer_cache: dict[(commit,
        rendererDrv), EntityRef[RendererEntity]]`; `_current_ref:
        (commit, attr, flake_path) | None`; the listener machinery. (No
        validation cache — validation is eager in `set_preset`, see #5.)
      - **Listener machinery** — copy the `_ListenerState` +
        `_listener_worker` + `trigger_reload` pattern verbatim (the
        established approach; `DemoDispatchImpl` already copies it).
        *(Followup candidate: factor a shared listener helper across the
        three impls — note it, don't do it here.)*
      - **`set_preset(commit, attr, flake_path)`** — (1) `eval_preset_table`;
        (2) **one batched `build_drv_outputs`** over every typeEntity layer
        drv + every renderer drv not already in `_renderer_cache[(commit,
        drv)]` (nix-parallel); (3) **eager validation (#5)** — per entry,
        `LayeredFileStoreReader(outs[typeEntityLayerDrv]).retrieve(
        typeEntityLayerKey)` (into_scope=None → bare `FxtrID`, no transient
        scope) → assert `fid.id_string()` == the entry's key; mismatch →
        raise `IRException(category="RendererMisconfigured")` (whole-preset
        reject); (4) mint a `RendererEntity` per newly-built renderer
        (`.store(self._scope)` → cache); (5) build the new `_table` from the
        eval JSON + renderer cache; (6) set `_current_ref`, atomic-swap
        `_table`; (7) `trigger_reload()`. Any failure raises (stderr in
        message), leaving the previous table intact.
      - **`resolve(type_ref, dispatch_key_uuid)`** — a **pure lookup**:
        `_table.get(type_ref.entity_id.id_string(), {}).get(uuid)`; miss
        → `None`. No build, no validation (done eagerly in `set_preset`).
        (The `not _registered`-style guard from basic isn't needed — an
        empty `_table` is a valid state that returns `None`/fallback.)
      - **`subscribe_to_changes`** — reuse the basic listener-worker
        spawn-on-`impl_tg` pattern verbatim.
      - **Startup auto-load (`async initialize()`).** Called by the
        mount helper (and by direct constructors). If `initial_commit` +
        `initial_attr` set, run the `set_preset` body. **Failure policy:
        always warn, never crash** — *any* failure (missing/stale ref,
        eval/build error, malformed preset) logs a warning with the
        stderr and comes up with an empty `_table`; the agent recovers
        via `set_preset` and the empty-table debug surface makes the
        unloaded state visible. (Single behavior — no missing-vs-
        malformed hybrid; an agent-invoked `set_preset` is separate and
        always raises.) `_current_ref` is still recorded for the debug
        view.
      - **Empty-table visibility** (open-concerns #5). Expose
        `current_ref` (the `(commit, attr)` or `None`) so a debug
        surface can show "currently-loaded ref: none / `(commit, attr)`".
        v0: a read-only property/field; no new wire frame.
- [x] **Softened `BasicDynamicDispatchImpl` docstrings** (decision #6) —
      "no longer the suggested impl; prefer `NixConfigurableDispatchImpl`"
      on the module + class docstrings. No move, no import changes.
- [x] **Fast unit tests** (`tests/test_nix_configurable_dispatch.py`, **no
      builder**) — monkeypatch the `nix.py` wrapper + a real tiny layered
      store (`LayeredFileStoreIsolatedWriter`) for validation; the impl is
      **mounted, resolved to a `NixDispatcherProto` proxy, and driven
      through the proxy** (real dispatch path, per the user's request).
      Covers: `set_preset` builds `_table` + mints a loadable
      `RendererEntity` + validates + fires the reload to a recording
      listener; `resolve` hit / dispatch-key miss / type miss (pure lookup);
      renderer cache reused across repeat same-commit `set_preset`;
      validation mismatch → `set_preset` raises `RendererMisconfigured` +
      table not swapped; failed initial auto-load → warn + empty +
      `current_ref` recorded. **4 passed (0.31s); 30 dispatch tests green
      overall.**
- [x] **Updated `fxtr_workstation/CODE_OVERVIEW.md`** — two-impl intro,
      `NixDispatcherProto` (standalone-proto note), the
      `nix_configurable_dispatch/` subsection (nix.py + impl + mount helper +
      eager-validation rationale), `BasicDynamicDispatchImpl` "no longer
      suggested".

### Chunk 3b — agent-facing protocol export (`fxtr_workstation_agent_plugin`) ✅

Ships the reusable agent surface so Stage 6 is pure demo/test assembly
(decision #4). Mirrors the `NixWasmServiceDefn` dup/re-export pattern
from Stage 3 (chunk 5).

- [x] **Agent-side `NixDispatcherProto` dup** in the sandbox bridge
      `protocols.py` (alongside the `EventListenerProto` /
      `NixWasmServiceDefn` dups). A standalone `@service_protocol`
      declaring **only** `async set_preset(git_commit, flake_attr,
      flake_path=None) -> None` — the one method the agent invokes. The
      base `resolve` / `subscribe_to_changes` are omitted on purpose: they
      reference workstation-only `RendererResolution` / `RendererEntity`
      types not in the sandbox dep closure. Wire dispatch is by method
      name, so the agent uses `replace_service_type(NixDispatcherProto)` on
      the looked-up dispatcher ref regardless of the agent-side type-id.
- [x] **Re-export from `_fxtr_mcp_builtin`** via the bridge `builtins.py`
      (added to the returned mapping + module docstring), so a `run_script`
      does `from _fxtr_mcp_builtin import NixDispatcherProto`.
- [x] **No dedicated parity test** (decision, with the user): a method-
      signature-equality test is brittle and catches no real bug class for
      a `@service_protocol` (wire dispatch is by name; arg drift just fails
      at dispatch). Unlike `NixWasmServiceDefn` — where `define_type` keys
      only on namespace+label so field drift silently breaks decode (hence
      *its* parity test) — there's no analogous silent trap here. The dup
      is a one-method mirror, smoke-verified to import + carry the right
      `set_preset` signature + be wired into the builtins mapping; the real
      verification is Stage 6's e2e `run_script` `set_preset` test.
- [x] **Updated `fxtr_workstation_agent_plugin/CODE_OVERVIEW.md`** — the
      new dup + `_fxtr_mcp_builtin` export.

### Chunk 4 — demo preset in `basic_test_demo` + nix integration test ✅

- [x] **`renderers/demo_data/index.tsx`** — renderer for `DemoDataEntity`
      (title/body), modeled on the `fxtr_workstation_demo` `note` renderer,
      covering all four shell keys, exporting
      `default { demo_data: { [FullPanel.uuid]: …, … } }`. Imports restricted
      to `workstationExternals`. (No `fxtr_renderer.json` — routing is inline
      in the preset attr.)
- [x] **`agent_workdir/flake.nix`** — added
      `fxtr.workstation.uiPresets.demo_preset` keyed by `DemoDataEntity`'s
      id_string (hardcoded literal
      `dlt7gpfp765qazhxo2tkdx5evpkzgycifm657eocbl4ozqe5piuhq`, with a comment
      on how it was obtained); `typeEntity = mkEntityFromPython { venv =
      demoDataVenv (shared via a `let`); fromPython =
      "demo_data:DEMO_DATA_TYPE_ID"; layerKey = "demo_data_type"; }`;
      `byDispatchKey` routes the four shell-key uuids → `esbuildRenderer {
      src = ./renderers/demo_data; }`. Factored the demo-data venv into a
      `let` so the entity + the typeEntity share one venv build.
- [x] **Integration test** (`fxtr_workstation_mcp/tests/test_nix_dispatch_preset.py`,
      `@pytest.mark.slow` + `@pytest.mark.deadline(seconds=900)`, nix-gated):
      `prepare_workdir_repo(agent_workdir)` → committed repo + HEAD;
      construct `NixConfigurableDispatchImpl(scope, repo=<workdir>, impl_tg)`;
      mount + **resolve a `NixDispatcherProto` proxy**; subscribe a recording
      `EventListenerProto`; `set_preset(HEAD, "…demo_preset")` (real batched
      build + eager validation); assert reload fired + `current_ref` set;
      `resolve(type_ref, FullPanel)` → `RendererResolution(subtype_key=
      "demo_data")` whose `RendererEntity` loads to real ESM bytes
      (`b"export" in …`); wrong-type resolve → `None`. (Stores a
      `DemoDataEntity` instance first so the type-ref is wire-encodable;
      imports `demo_data` via a `sys.path` insert.) **Passed (29.48s,
      venv cached).** (Dropped the optional mismatch sub-case — the unit
      test already covers `RendererMisconfigured`.)
- [x] **Updated `examples/basic_test_demo/agent_workdir/README.md`** — added
      a "UI presets (renderer dispatch)" section (the `set_preset` loop, the
      id_string-from-Python key, no `entityIdString`) + directory listing for
      `renderers/demo_data/` + the `uiPresets` flake attr; noted the live
      `renderer-dispatcher` wiring is the stage-6 demo migration (for now the
      path is exercised by the integration test).

### Chunk 5 — final docs pass

- [x] **Stage-6 tracking** + **`entityIdString` removal** done at
      planning time (2026-06-01): `stage6_workstation_demo.md` gained a
      "Carried over from Stage 5" section (agent-side `NixDispatcherProto`
      dup, `run_demo_workstation` wiring, e2e `run_script` `set_preset`
      test); `entityIdString` references were removed from
      `06_ui_presets.md`, `10_nix_infrastructure.md`, `overview.md`, and
      the scratch docs.
- [x] **Final `CODE_OVERVIEW.md` pass** — `nix/` (esbuildRenderer +
      workstationExternals + test), `fxtr_workstation/` (two-impl intro,
      `NixDispatcherProto`, `nix_configurable_dispatch/` subsection with the
      batched-build + eager-validation rationale), and the agent plugin
      (bridge `NixDispatcherProto` dup + `_fxtr_mcp_builtin` export, the
      `renderers/demo_data/` + `uiPresets` workdir entries, and
      `test_nix_dispatch_preset.py`).

## Module layout (cosmetic, Q6.C)

```
fxtr_workstation/dynamic_dispatch/
  dynamic_dispatch_types.py        # + NixDispatcherProto
  basic_dynamic_dispatch/          # unchanged in place; docstrings softened
  nix_configurable_dispatch/       # NEW
    __init__.py
    nix.py                         # git-flake-ref eval/build wrapper (chunk 2)
    service_impl.py                # NixConfigurableDispatchImpl + mount helper
```

## Sequencing note

The agent-side flow (Stage 6) is just `schedule_invoke(…, dispatcher,
lambda d: d.set_preset(git_commit=current_commit(),
flake_attr="fxtr.workstation.uiPresets.my_preset"))` — `current_commit()`
is the Stage-4 builtin; no `retrieve_nix_entity` round-trip (presets
aren't entities). Stage 5 retires the two genuinely-new unknowns
in-process before any agent wiring: (1) esbuild-in-a-derivation
(`esbuildRenderer`, chunk 1); (2) the dispatcher minting a
`RendererEntity` in the workstation store at serve time and a load
through the unchanged frontend path (chunk 4's resolve→load assertion).

## Relevant design notes

- `design_notes/06_ui_presets.md` — primary reference: preset attr
  shape, type-id-string keying, protocol split, serve-time entity
  wrapping (reconciled to the decisions above 2026-06-01).
- `design_notes/01_flake_schema.md` — git-ref consumption + `flakePath`.
- `design_notes/10_nix_infrastructure.md` — helper inventory + the
  venv-vs-internal-fxtr2 convention.
- `design_notes/11_failure_and_ux.md` — `set_preset` git-ref failure
  surfaces (raise with stderr; no dump file).

## Relevant existing code

- `projects/fxtr_workstation/CODE_OVERVIEW.md` — `dynamic_dispatch/`
  surface, `BasicDynamicDispatchImpl`, the listener-worker pattern,
  `RendererResolution` / `RendererEntity` shapes.
- `fxtr_workstation/dynamic_dispatch/basic_dynamic_dispatch/service_impl.py`
  + `compile.py` — the listener machinery to copy + the
  `DEFAULT_EXTERNALS` list to mirror in `workstationExternals`.
- `examples/fxtr_workstation_demo/demo_dispatch.py` — the
  `type_id_string → {uuid: RendererResolution}` table + atomic-swap +
  copied listener pattern (closest existing analogue to the new impl).
- `projects/fxtr_sandboxing/fxtr_sandboxing/src/fxtr_sandboxing/nix.py`
  — the git-flake-ref wrapper to model `nix.py` on (step-typed errors,
  `--no-update-lock-file`, no stderr parsing).
- `nix/fxtr_workdir_harness/` — `prepare_workdir_repo` (the integration
  test's committed-workdir harness).
- `nix/lib/{mkWasmService,mkEntityFromPython,_mkGuestSitePackages}.nix`
  — the helper patterns `esbuildRenderer` sits beside.
- `projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/`
  — `agent_workdir/flake.nix` (gains the preset), `packages/demo_data/`
  (the type to key on), `run_demo_workstation.py` (the `sys.path`
  insert + external-service wiring pattern, used by Stage 6).
- The sandbox bridge `protocols.py` + `builtins.py` (the
  `EventListenerProto` / `NixWasmServiceDefn` dups + `_fxtr_mcp_builtin`
  re-exports) and `tests/test_nix_wasm_service_defn.py` (the wire-parity
  test) — the patterns chunk 3b's `NixDispatcherProto` dup mirrors. See
  `fxtr_workstation_agent_plugin/CODE_OVERVIEW.md`.
- `projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md` —
  `@fxtr-ui/react-dispatch/dynamic_dispatcher.ts` (protocol-level
  unchanged; no frontend work).

## Deviations from the design notes (for reviewers)

- **`entityIdString` dropped** — the agent uses the Python id_string
  print instead (decision #1). References removed from notes 06/10 +
  overview (no longer a deviation, just reconciled).
- **`BasicDynamicDispatchImpl` not moved** to `deprecated/` (Stage-5
  sketch said move it) — docstrings softened in place (decision #6).
- **Demo round-trip deferred to Stage 6, but the agent-facing API is
  not** — Stage 5 ships the full reusable surface (host impl + mount
  helper + the agent-side `NixDispatcherProto` export, chunk 3b) plus a
  direct in-process integration test; only `run_demo_workstation` wiring
  + the e2e `run_script` `set_preset` test (which *use* that API) move
  to Stage 6, so Stage 6 adds no new library plumbing (decision #4).
- **Startup auto-load: always warn, never crash** — the note's
  "missing→warn / malformed→fail" hybrid is replaced by a single
  behavior (any auto-load failure warns + comes up empty). Simpler, and
  avoids the wrapper having to parse nix stderr to classify failures.
  An agent-invoked `set_preset` still always raises.
- **Key↔`typeEntity` validation is eager, not request-time** (note 06
  argued request-time) — validated in `set_preset` alongside the eager
  renderer builds, so `resolve` is a pure lookup. Lazy resolve-time
  validation can't catch a stale key (the mis-keyed entry never
  resolves); eager catches it where the agent can fix it (decision #5).
  Note 06's validation section was reconciled to match.

## End-of-stage check ✅

- [x] A flake declaring `fxtr.workstation.uiPresets.<name>` (type-id-keyed,
  `renderer = esbuildRenderer` output) `nix eval`s to the expected
  routing table via the `--apply` reshape; each renderer ESM builds to a
  `.mjs` (`esbuildRenderer`). — `basic_test_demo` flake + the chunk-4
  integration test (real build); `test_esbuild_renderer.py`.
- [x] A `NixConfigurableDispatchImpl` configured with a repo (+ optional
  initial ref) auto-loads on startup; `set_preset(<HEAD>, <attr>)` swaps
  the table, mints `RendererEntity`s in the workstation store, and
  broadcasts the reload; the subscribed listener receives it. — chunk-4
  integration test (real) + unit tests (auto-load success/failure, reload).
- [x] `resolve` returns the right `RendererResolution` for a known type +
  `None` for an unknown one (pure lookup); the minted `RendererEntity`
  loads to non-empty ESM bytes; eager validation in `set_preset` passes
  for a correct `typeEntity` and raises a loud "renderer misconfigured"
  error (rejecting the swap) on a deliberate key/`typeEntity` mismatch. —
  integration test (load to real ESM) + unit tests (miss / mismatch).
- [x] A failing initial preset (missing/stale ref, eval/build error,
  malformed) warns and comes up with an empty table — never crashes the
  workstation. — `test_initialize_failed_autoload_warns_and_comes_up_empty`.
- [x] `BasicDynamicDispatchImpl`'s importers still work unchanged (no move).
  — 370 non-slow `fxtr_workstation` tests + 211 agent-plugin tests green.

## Implementation notes / things to carry forward

- **Nix-wrapper centralization → REVERSED to simplify-in-place** (done,
  `stage5post2_nix_wrapper_simplify.md`). The three near-duplicate wrappers were
  *not* extracted into a shared lib (premature: ~50 mechanical lines across
  three domains, and the typed-error apparatus was used by nobody but the
  tests). Instead: `build_flake_ref` → local-only + private, errors collapsed
  (sandboxing/dispatcher → `RuntimeError`; MCP keeps `NixError`), and the two
  repo-root-coupled wrapper tests (`test_nix_preset_ref.py` +
  `test_nix_git_ref.py`) deleted (so the fixture-flake sub-task dissolved too).
  See `followups.md` for the full rationale.
- **Protocol inheritance** — `NixDispatcherProto` had to be a standalone
  `@service_protocol` (not `WorkstationDispatcherProto` subclass) because the
  resolver registry rejects subtype registration. Tracked in `followups.md`
  (user wants to revisit); `service_impl.py` also carries a
  `TODO(daniel)` about expressing subclassing in type-ids.
- **Demo `set_preset` round-trip is Stage 6** — `basic_test_demo`'s
  `run_demo_workstation.py` doesn't register a `renderer-dispatcher` yet, so
  the agent-driven `set_preset` walkthrough + e2e `run_script` test are
  tracked in `stage6_workstation_demo.md`'s "Carried over from Stage 5".
