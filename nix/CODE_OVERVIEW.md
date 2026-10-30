# fxtr nix infrastructure (`nix`)

The nix layer that lets a flake build fxtr2 artifacts — Python venvs and
fxtr2 *entities* (layered file stores) — reproducibly. The **repo-root
`flake.nix`** is the entry point: it sets `lib.fxtr = import ./nix/lib`
and exposes a few buildable example/test outputs under
`packages.x86_64-linux.*`. Workdir flakes elsewhere consume this flake as an
input and use `lib.fxtr.*` to assemble their own `fxtr` attrset.

Design lives in `projects/fxtr_workstation/plans/phase0/part5_nix_workdir/`
(`overview.md` + `design_notes/`); this doc is the current state of the code.

## Mental model

- **`lib.fxtr` is the helper namespace.** It carries `canonicalSystem` /
  `pkgs` / `pkgsFor` and a set of helper functions. The `fxtr.*` object
  namespace is deliberately **not** indexed by `${system}` (single
  canonical architecture, `x86_64-linux`; see `overview.md` "A principled
  single architecture"). Each buildable-producing helper takes an optional
  `system` arg defaulting to `canonicalSystem`.
- **Helpers produce buildable derivations + plain entity attrs.** An entity
  is the three-field attrset `{ type = "fxtr_layer_entity"; layer; layerKey; }`
  where `layer` is a derivation producing a layered-file-store directory and
  `layerKey` names the content in it (`design_notes/03`).
- **Writers run Python inside a nix derivation** to emit a layered file
  store at `$out`, using fxtr2's `layered_file_store.writer`. The Python
  comes from a venv built by `mkVenv`.
- **fxtr2 enters venvs as a relative path-dep** (`fxtr2 = { path = "../../../v2" }`)
  that resolves inside the nix store copy because the flake is at the repo
  root. Downstream user flakes (outside the repo) instead git-dep fxtr2
  through the same `mkVenv`. Rationale + the path-walk history:
  `design_notes/10_nix_infrastructure.md`.
- **Build via a git flake reference, not `path:`** — see
  `design_notes/10` Q10.E + its "Implementation environment note" for why
  and from which checkout.

## File structure

```
nix/
├── CODE_OVERVIEW.md
├── lib/                              # the lib.fxtr helper namespace
│   ├── default.nix                   # assembles lib.fxtr: canonicalSystem/pkgs/pkgsFor + helpers (real + stubbed); wires the internal guest-venv helpers
│   ├── mkVenv.nix                    # uv2nix workspace -> Python venv derivation (host-runnable, with /bin/python)
│   ├── runFxtrWriter.nix             # shared scaffold: run a fxtr_nix_writer entry point in a derivation -> layered store (or, with outputHash set, a fetch FOD)
│   ├── mkEntityFromPython.nix        # import a named Python entity object -> entity attr + layer derivation
│   ├── tsvNamespaceExport.nix        # publish a committed tsv namespace as a PURE JSON manifest -> { type="fxtr_namespace_export"; export=<drv> } (backs fxtr.namespaces.<name>)
│   ├── entityFromTsvPin.nix          # one tsv-namespace pin as fxtr.entities.<name>: a preload FOD fetching the pinned entity's bytes via the rehost -> { type="fxtr_layer_entity"; layer; layerKey }. Narrows its source (builtins.path: marker + *.fxtr.tsv only) — precise inputs; layer is independent of flake.nix/fod_lock.toml
│   ├── fodLockReader.nix             # standalone reader: { lockFile } -> (key -> SRI hash) read from a fod_lock.toml; dropped in wherever lib.fakeHash would go (decoupled from the FOD helpers). Lazy read + missing-key throw; managed by `fxtr_nix update-hashes`
│   ├── _runFxtrBuilder.nix           # INTERNAL scaffold beneath the two builder helpers: runCommand (__structuredAttrs `builderSpec`={parents;spec;provenanced;entry?;provenanceInfo?}) running a user `runCmd` -> layered store at $out; pure, or FOD by `outputHash` (recursive sha256, `impureEnvVars`, cacert). `provenanced ? false` opens the $out writer in provenanced mode (pins round-trip provenance — see fxtr2 nix_builder / LayeredFileStoreIsolatedWriter) and mints a default build-step provenance_context. `provenanceInfo ? null` (JSON; key emitted only when set so existing drvs are byte-unchanged) is the author-declared default-provenance supplement; eval-time `throw`s if set without `provenanced`, or if a provenanced FOD (outputHash set) omits it (no safe $out anchor). Generic `extraBuildInputs` (PATH) + `env` knobs; `env` vars are `export`ed at the top of the build command (under __structuredAttrs scalar attrs aren't exported to child processes — the python builder reads them via os.environ; a store path in `env` is still a build dep via string context). Not exposed in lib.fxtr.*
│   ├── mkFxtrFnBuilder.nix           # run an importable `module:fn` async entrypoint as a layer builder (over a caller mkVenv venv); runs `${venv}/bin/python -m fxtr2.data.layered_file_store.nix_builder` with `entry` in the builderSpec. `provenanced ? false` round-trips provenance. Returns the layer drv (user wraps as fxtr.entities.<name>)
│   ├── mkFxtrScriptBuilder.nix       # run a loose `.py` script (owns `main`, calls nix_builder.main(fn)) as a layer builder over a caller venv: `${venv}/bin/python ${script}`. `provenanced ? false` round-trips provenance. One venv backs many scripts. Sibling-file caveat: ${script} copies a single file (shared local code must be a venv workspace package)
│   ├── reportBuilderCli.nix          # lib.fxtr.reportBuilderCli: the fxtr_report_builder MDX→FxtrCbor CLI built pure pnpm-in-nix (fetchPnpmDeps lockfile FOD + pnpmConfigHook + the package's esbuild.config.mjs) wrapped via makeWrapper as a self-contained `node` executable ($out/bin/fxtr-report-builder). A pinned derivation attr (not a function); consumers wire its path via a builder's generic `env`. The cli lives outside the repo pnpm workspace (its own lock)
│   ├── mk_entity_from_python_writer.py  # standalone writer mkEntityFromPython runs in the CALLER's venv
│   ├── builtinEntity.nix             # { type="builtin"; idString; } for a PRIMITIVE FxtrID (no layer to build); rejects non-`!`/non-`.` ids (content ids must go through mkEntityFromPython). Consumed by the dispatcher preset path + retrieve_nix_entity
│   ├── entities.nix                  # lib.fxtr.entities.*: ready-made `forType` ({idString; entity}) values for library type ids (workstation/agent + report + fxtr_experiment) — typeEntity layers built via mkEntityFromPython over the fxtr_lib_type_ids stub venv (+ basicType via builtinEntity)
│   ├── _readServiceEntryPoint.nix    # shared reader: the single [project.entry-points."fxtr.service"] entry from a guest pyproject (used by mkWasmService + mkContainerService)
│   ├── mkWasmService.nix             # build a wasm-kind fxtr.services.<name> attr { kind = "wasm"; sitePackages = <drv>; entryPoint } from a guest package (sitePackages via _mkLockedSitePackages with binaryPlatformTags=[])
│   ├── mkPyodideService.nix          # build a pyodide-kind fxtr.services.<name> attr { kind = "pyodide"; sitePackages; runtimeBundle; entryPoint } — pyodide CPython-wasm in a permission-locked deno subprocess on the host; sitePackages may include PEP 783 pyemscripten binary wheels. Takes a `pyodidePin` override (default: lib.fxtr.pyodideDefaultPin — version/tarballHash/abiTag/pythonMinor move together)
│   ├── mkContainerService.nix        # build a container-kind fxtr.services.<name> attr { kind = "container"; image = <nix2container JSON drv> } — full-Linux mkVenv + baked Cmd (base python -m fxtr_service_runner <ep>, PYTHONPATH=venv site-packages; venv activation doesn't survive binfmt emulation)
│   ├── esbuildRenderer.nix           # compile a renderer's index.tsx -> ESM .mjs derivation, esbuild run INSIDE a nix derivation (no venv); $out is the .mjs file; consumed by the dispatcher (uiPresets renderer slots). Narrows src via builtins.path (unrelated flake-tree changes don't rebuild the slice); optional nodeModules arg symlinks a node_modules tree beside the src copy for bare imports that must be BUNDLED (e.g. elkjs)
│   ├── experimentRenderers.nix       # lib.fxtr.experimentRenderers: the fxtr_experiment renderer slice ({ keyIds } -> mergeable preset entries) — dashboard + frame + stored-config renderers compiled from projects/fxtr_experiment/frontend/src as ONE esbuild module, elkjs bundled in from a pinned npm tarball (KEEP version in sync with pnpm-lock.yaml), forTypes = lib.fxtr.entities.experiment*
│   ├── mkUiPreset.nix                # pure transpose: per-renderer declarations (name-keyed attrset) -> the type-id-keyed uiPresets routing table the dispatcher consumes; throws on a duplicate (type, dispatch-key) claim. Builds nothing (the _build_routing_table analog)
│   ├── _mkLockedSitePackages.nix     # INTERNAL helper: uv.lock -> flat guest site-packages, bypassing mkVirtualEnv so PEP 783 pyemscripten binary wheels can be selected (pyproject-nix's selectWheels can't match them). Registry wheels: binary-tag-preferring pick + pure fetchurl by lock hash + unzip; local/git packages: built via the pyproject-nix pythonSet under the guest environ with propagation stripped, installed site-packages subtree copied. Consumers: mkPyodideService (pyemscripten tags) + mkWasmService (binaryPlatformTags=[] = pure-only)
│   └── _mkPyodideRuntimeBundle.nix   # INTERNAL helper: the pyodide runtime assets as one drv — unpacked pyodide npm tarball (fetchurl by the pin's hash; the host deno driver file-path-imports pyodide.mjs from it, no npm at spawn time) + guest_bootstrap.py copied from this fxtr rev. Consumer: mkPyodideService
├── fxtr_nix_writer/                  # lib-internal writer package (its venv = the build-time venv)
│   ├── pyproject.toml                # uv workspace; redeclares fxtr2's non-PyPI sources (fxtr v1 path, mashumaro git)
│   ├── uv.lock
│   └── src/fxtr_nix_writer/
│       ├── __init__.py
│       ├── cli.py                    # `fxtr-nix-writer` console script: reads writerSpec from structuredAttrs, dispatches on kind (hello_world, namespace_export, entity_from_tsv_pin)
│       ├── writers.py                # write_hello_world; write_namespace_export (pure TSV->manifest); write_entity_from_tsv_pin + write_preload_layer (FOD: fetch a pin's bytes via the rehost)
│       └── entities.py               # HelloWorldEntity
├── fxtr_lib_type_ids/                # stub package: re-declares the workstation/report/fxtr_experiment type-ids (define_type(SAME_NS, label)) backing lib.fxtr.entities. fxtr2-only (light venv), content-identical to the canonical types. KEEP IN SYNC if a canonical type's NS/label changes
│   ├── pyproject.toml                # deps fxtr2 only; redeclares fxtr2's non-PyPI sources + build hints (same convention as fxtr_nix_writer)
│   ├── uv.lock
│   └── src/fxtr_lib_type_ids/__init__.py  # *_TYPE_ID = define_type(NS, label) constants, each commented with its canonical source module
├── sandbox_smoke/                    # minimal fxtr.services workdir; built via prepare_workdir_repo in test_sandbox_import (the git-ref path), not by the repo-root flake
│   ├── flake.nix                     # fxtr.services.smoke = mkWasmService { src = ./.; }; the fxtr input is a substitution placeholder (filled by prepare_workdir_repo)
│   ├── pyproject.toml                # fxtr-sandbox-rpc-adapter[fxtr2-portable] + a no-op fxtr.service entry point; [tool.uv.sources] uses the comment-flip convention — relative paths active in-place (lock against local code), pinned git source activated in the workdir copy (gives uv2nix the cross-package context fxtr2-portable's force-include needs)
│   ├── uv.lock                       # locked from the relative (template) source form
│   └── src/fxtr_sandbox_smoke/__init__.py  # no-op `run` service entry point (build-only fixture); satisfies uv_build's src layout
├── fxtr_workdir_harness/            # stdlib-only leaf package: prepare_workdir_repo + repin_workdir
│   ├── pyproject.toml                # no runtime deps (dev-group: pytest); dev-dep'd by py_testing + fxtr_sandboxing + the agent plugin
│   ├── src/fxtr_workdir_harness/__init__.py  # prepare_workdir_repo: copy an in-repo template -> fresh tempdir git repo, substitute the fxtr lib ref (flake-input placeholder + uv-source comment-flip markers), uv lock / nix flake lock, commit, capture HEAD. The shared test/scaffold helper for the git-ref code path; see the stage3 plan's "Test & scaffold workdir setup". In jj workspaces (".jj" wins over ".git", incl. decoy .git dirs) the lib ref auto-resolves to the workspace's @ commit, pushed into a bare cache mirror so uv/nix can fetch it without bookmark+export. repin_workdir is the in-place counterpart (the renderer dev loop's repin half): rewrite the marked lines' substituted values in an EXISTING workdir, relock, commit the whole tree (pending workdir edits ride along); CLI `python -m fxtr_workdir_harness.repin <workdir> [--uv]`
│   └── tests/                        # test_workdir_harness.py (relock=False mechanics + jj-workspace mode against throwaway jj repos) + _workdir_templates/minimal/ fixture
└── py_testing/                       # Python side of the nix-integration tests + throwaway entities
    ├── pyproject.toml                # uv workspace; deps fxtr2 + fxtr_nix_writer + fxtr_report_builder (the report-step fixture); dev-group adds pyeryx + fxtr-workdir-harness for the sandbox-import test
    ├── uv.lock
    ├── sample_build_script.py        # script-form builder for the mkFxtrScriptBuilder check: imports `build` from py_testing.builder_fixture + drives it via nix_builder.main (a loose file run as __main__)
    ├── src/py_testing/
    │   ├── __init__.py
    │   ├── sample_type.py            # sample_type_id = define_type(...) — the mkEntityFromPython test target
    │   ├── builder_fixture.py        # InputNote/WrappedNote types + `input_note` instance (mkEntityFromPython input layer) + the `build` entrypoint (reads the parent's InputNote, pins a derived WrappedNote) shared by the fn + script builder checks
    │   └── report_fixture.py         # report-step entrypoint (`build`): reads two parent-layer inputs, embeds them as RenderFigure refs via build_root_report (shells out to the env-wired reportBuilderCli), pins the RootReport — the report step as a plain part-3 builder
    └── tests/
        ├── conftest.py               # `nix_build` / `flake_ref` fixtures (resolve the flake ref via fxtr_workdir_harness.resolve_lib_ref — colocated/non-colocated jj + CI git)
        ├── test_hello_world.py       # write_hello_world round-trip (read the message back via a fxtr2 reader)
        ├── test_entity_from_python.py # mkEntityFromPython round-trip
        ├── test_namespace_export.py  # namespace_export writer unit tests (happy path, full-key prefix, validation failures) + a nix round-trip building namespace-export-check
        ├── test_entity_from_tsv_pin.py # write_preload_layer core (in-process, direct RepoEntityStore remote): pin recorded + bytes local + pure-reader round-trip + byte-stable + missing-id raises
        ├── test_esbuild_renderer.py  # builds the repo-root renderer-esm-check target; asserts the compiled .mjs is ESM with externals (react / react/jsx-runtime) kept as bare imports
        ├── test_mk_ui_preset.py      # builds ui-preset-transpose-check (reads back toJSON of the transposed table — asserts inversion + sharding + passthrough) and asserts ui-preset-conflict-check fails the build with the duplicate-claim throw
        ├── test_lib_entities.py      # builds two lib.fxtr.entities.*.entity.layer (built from the fxtr_lib_type_ids stub venv) + asserts the pinned id_string equals the baked literal (the stub re-declaration is content-identical to the canonical type)
        ├── test_report_builder_cli.py # builds report-builder-cli-smoke (runs lib.fxtr.reportBuilderCli against the README envelope IN the derivation, asserts {ok:true}) — covers the pnpm-in-nix bundle + makeWrapper
        ├── test_report_step.py       # builds report-step-check (a part-3 builder with fxtr_report_builder + the env-wired CLI; reads two parent inputs → build_root_report → pins RootReport): asserts the report references both inputs + a determinism gate (report-step-check-alt, an independent build, pins the same content-addressed RootReport id)
        ├── test_nix_builder.py       # builds fxtr-fn-builder-check + fxtr-script-builder-check (a builder reading the builderInputLayer parent's InputNote and pinning a derived WrappedNote) and reads each back — asserts the wrapped note + its inner ref resolves through the parent layer (pure, both builder faces). Plus fxtr-provenanced-fn-builder-check: reads "wrapped" back as a layered id, hops the n-node → build c-node, asserts the NixInputAddressedBuildProvenance anchors on $out + carries the declared provenanceInfo; and the two eval-time throw targets (provenanceInfo without provenanced; provenanced FOD with no info) fail the build with the expected messages
        └── test_sandbox_import.py    # forcing-function via the git-ref path: builds sandbox_smoke's fxtr.services.smoke.sitePackages through prepare_workdir_repo + a --no-update-lock-file git-ref build, bind-mounts the flat site-packages into eryx, runs `import fxtr2`; also retires the note-12 git-ref checklist. Module-scoped fixture auto-repairs the $TMPDIR/eryx-embedded canary if macOS pruned its contents
```

## Module details

### `lib/` — the `lib.fxtr` namespace

`default.nix` takes the flake `inputs` and returns the `lib.fxtr` attrset.
Live members:

- `canonicalSystem` (`"x86_64-linux"`), `pkgs` (canonical `legacyPackages`
  over the flake's pinned nixpkgs), `pkgsFor system` — so downstream flakes
  need no nixpkgs input of their own.
- `mkVenv { workspaceRoot, system?, sourcePreference?, name?, python?, environ? }`
  (`mkVenv.nix`) — runs uv2nix (`loadWorkspace → mkVirtualEnv`) over a uv
  workspace and returns the venv derivation. `${venv}/bin/python` has the
  workspace's default dependency closure importable. `environ` is a PEP 508
  marker-override dict (passed through to `mkPyprojectOverlay`) for
  retargeting marker evaluation when the lock's supported environments
  differ from the build host — used by `_mkGuestSitePackages` to satisfy
  uv2nix's supported-markers check against a wasi-restricted lock.
- `runFxtrWriter { name, spec, system?, outputHash?, impureEnvVars? }`
  (`runFxtrWriter.nix`) — the shared writer scaffold. Builds the lib-internal
  venv (`mkVenv` over `fxtr_nix_writer`) and runs the `fxtr-nix-writer` console
  script, handing the writer `spec` over via `__structuredAttrs` (`writerSpec`);
  the script reads `$out` for the output dir. When `outputHash` is set the
  result is a **fixed-output derivation** (recursive sha256, network-allowed)
  with `impureEnvVars` passed through — for writers that fetch from the
  build-time rehost; pure writers leave both unset.
- `tsvNamespaceExport { namespaceRoot, name, system? }`
  (`tsvNamespaceExport.nix`) — wraps `runFxtrWriter` with the `namespace_export`
  kind: a **pure** derivation parsing/validating the committed
  `fxtr_namespace.toml` + `*.fxtr.tsv` into `$out/fxtr_namespace_export.json`.
  Returns `{ type = "fxtr_namespace_export"; export = <drv>; }` (backs
  `fxtr.namespaces.<name>`; the runner-manager's namespace read view consumes
  the manifest). No FOD, no network, no layer.
- `entityFromTsvPin { namespaceRoot, key, outputHash, system?, name? }`
  (`entityFromTsvPin.nix`) — wraps `runFxtrWriter` with the
  `entity_from_tsv_pin` kind as a **FOD**: resolves one pin key → id from the
  committed TSVs (pure) and fetches that id's bytes via the rehost
  (`FXTR_REMOTE_*` impure env) into a one-pin layer. Returns the
  `{ type = "fxtr_layer_entity"; layer; layerKey }` shape (the
  `fxtr.entities.<name>`-from-a-TSV-pin form). Narrows its source with
  `builtins.path` to the marker + `*.fxtr.tsv` (excluding `fod_lock.toml`,
  `flake.nix`, VCS files) — source hygiene: precise inputs, and the layer never
  depends on the lock file it's pinned by. Set `outputHash` via
  `lib.fodLockReader` + `fxtr_nix update-hashes` (or `lib.fakeHash` + manual
  re-pin as the low-level escape hatch).
- `fodLockReader { lockFile }` (`fodLockReader.nix`) — a standalone, helper-
  agnostic reader returning `key -> <SRI hash>` read from a `fod_lock.toml`
  (`builtins.fromTOML`). Drop the result in wherever a FOD's `outputHash` goes;
  it is *not* coupled to any FOD helper. Lazy (applying to `{ lockFile }` does
  not read the file, so `builtins.attrNames` over a FOD attrset never forces a
  hash — which lets the tool discover slots before the lock exists); a missing
  slot throws, naming the key + `fxtr_nix update-hashes`. The lock is written by
  that tool (in `fxtr_nix_runner`); slots are declared as `fxtr.preloads.<key>`.
- `mkFxtrFnBuilder { venv, entry, parents?, spec?, outputHash?, impureEnvVars?, provenanced?, provenanceInfo?, extraBuildInputs?, env?, name?, system? }`
  (`mkFxtrFnBuilder.nix`) — the builder primitive in **fn form**: run an
  importable `entry = "module:fn"` (an `async def fn(ctx)`) over a
  caller-supplied `venv`, writing a layered store at `$out`. Runs
  `${venv}/bin/python -m fxtr2.data.layered_file_store.nix_builder`; the launcher
  resolves `entry` and hands the function an open writer + pre-opened parent
  readers (see that Python module). `parents` is a name→layer-derivation map
  (pre-opened as `ctx.readers["<name>"]`); `spec` is arbitrary JSON passthrough.
  Returns the **layer derivation** (the user flake wraps it as
  `fxtr.entities.<name> = { type = "fxtr_layer_entity"; layer; layerKey; }`).
  **Purity is `outputHash`**: omit → pure (reads only parents); set +
  `impureEnvVars = [ FXTR_REMOTE_* ]` → a FOD fetching via the build-time rehost.
  **`provenanced`** (default false) opens the `$out` writer in provenanced mode
  *and* mints a default build-step `provenance_context` on the builder scope, so
  every entity the entrypoint stores carries the build as provenance (see
  `nix_builder` / `nix_build_provenance` in fxtr2). **`provenanceInfo`** is an
  author-declared JSON supplement (recommended `{ desc = "..."; }`): only legal
  when `provenanced`, **required** for a fixed-output (FOD) build (which has no
  safe `$out` anchor), optional for an input-addressed build (which anchors on
  `$out`). Both are static config errors otherwise (eval `throw`).
- `mkFxtrScriptBuilder { venv, script, parents?, spec?, outputHash?, provenanced?, provenanceInfo?, … }`
  (`mkFxtrScriptBuilder.nix`) — the builder primitive in **script form**: run a
  loose `.py` `script` file that owns `main` and calls
  `nix_builder.main(entrypoint)` itself (runs as `__main__`, imports what it
  needs *from* the venv). Same return + purity + `provenanced` / `provenanceInfo`
  model as `mkFxtrFnBuilder`. One `venv` backs many scripts (cheap new steps, no
  per-step dir). Sibling-file caveat: `${script}` copies a *single* file into the
  store, so shared local code must be a path-member package in the venv's
  workspace, not a loose sibling.
- `_runFxtrBuilder` (`_runFxtrBuilder.nix`, **internal** — not in `lib.fxtr.*`)
  — the scaffold both builders wrap: `runCommand name` with `__structuredAttrs`
  carrying the `builderSpec` attr (`{ parents; spec; provenanced; entry?;
  provenanceInfo?; }`, parents serialized to a name→out-path map) that runs the
  helper's `runCmd`. Generalizes `runFxtrWriter`'s FOD branch (pure when
  `outputHash` is null; recursive-sha256 FOD with `impureEnvVars` + `cacert` when
  set). `provenanceInfo` is emitted into `builderSpec` only when non-null (so
  existing drvs are byte-unchanged) and is guarded by two eval-time `throw`s
  (illegal without `provenanced`; required for a provenanced FOD). Generic
  `extraBuildInputs` (→ `nativeBuildInputs`, for bare-name tools on PATH) and
  `env` (merged extra derivation env).
- `reportBuilderCli` (`reportBuilderCli.nix`, a **derivation attr** — not a
  function) — the `fxtr_report_builder` MDX→FxtrCbor CLI
  (`projects/fxtr_report_builder/ts/packages/cli`) built **pure pnpm-in-nix**:
  `fetchPnpmDeps` (a lockfile FOD over the cli's own `pnpm-lock.yaml` — the only
  networked step; `fetcherVersion = 3`, v0 re-pin via `lib.fakeHash` → mismatch)
  + `pnpmConfigHook` (needs `pkgs.pnpm` in `nativeBuildInputs`) populates
  `node_modules` offline, then the package's `esbuild.config.mjs` bundles
  `src/main.ts` → `dist/main.cjs`, and `makeWrapper` emits a self-contained
  `$out/bin/fxtr-report-builder` (= `${nodejs}/bin/node $out/libexec/main.cjs`,
  node baked in). One pinned build shared by all consumers; a report-step
  builder wires its path via the builder's generic `env`
  (`FXTR_REPORT_BUILDER_CLI`). The cli is deliberately outside the repo pnpm
  workspace (its own lock) so `fetchPnpmDeps` sees a self-contained package.
- `mkEntityFromPython { venv, fromPython, layerKey?, name?, system? }`
  (`mkEntityFromPython.nix`) — returns an entity attr whose `layer`
  derivation runs `mk_entity_from_python_writer.py` in the **caller-supplied**
  `venv`, resolving `fromPython` (`module:attr` / `module:Class.attr`) to an
  entity object and pinning it under `layerKey` (default `"entity"`).
- `builtinEntity { idString }` (`builtinEntity.nix`) — the non-layer entity
  attr `{ type = "builtin"; idString; }` for a **primitive** FxtrID (the
  `!`-tag family, e.g. `!basic_type`, plus the bare `.`). No layer to build:
  the dispatcher preset path validates `idString == <key>` directly (no build)
  and `retrieve_nix_entity` constructs the FxtrID from it. **Throws** for any
  other id — a content-addressed type id has a real buildable type entity and
  must go through `mkEntityFromPython` instead.
- `entities` (`entities.nix`) — `lib.fxtr.entities.<name>`: ready-made
  `forType` values (`{ idString; entity; }`, the shape `mkUiPreset` consumes)
  for the library/builtin type ids the assembly demo's `builtin/` renderer
  routes (the agent panel + log-event + report slots + the `!basic_type`
  fallback). Each `entity` is a `mkEntityFromPython` layer built over the
  light `fxtr_lib_type_ids` stub venv (fxtr2-only) — so a consumer flake
  imports these instead of standing up a venv over `fxtr_workstation_mcp` /
  `fxtr_report_builder`. idStrings are content-stable literals the
  dispatcher's eager validation re-checks against the built layer.
- `mkWasmService { src, name?, system?, sourcePreference? }`
  (`mkWasmService.nix`) — builds a wasm-kind `fxtr.services.<name>` attr:
  `{ kind = "wasm"; sitePackages = <_mkGuestSitePackages drv>; entryPoint = "module:function"; }`.
  `entryPoint` is read from `src/pyproject.toml`'s single
  `[project.entry-points."fxtr.service"]` entry via the shared
  `_readServiceEntryPoint.nix` (errors on a missing/ambiguous declaration).
  A service is git-referenced *code*, not an entity — no entity write, no
  `.drv` pointer, no closure-scan. The spawner builds `sitePackages` lazily
  from a git ref and checks `kind` per spawn.
- `mkContainerService { src, name?, system?, sourcePreference? }`
  (`mkContainerService.nix`) — builds a container-kind `fxtr.services.<name>`
  attr: `{ kind = "container"; image = <nix2container drv>; }`. `image` is a
  nix2container JSON manifest referencing store paths as layers (no image
  tarball in the store); the spawner builds it lazily and delivers it to the
  container runtime via the nix2container-patched skopeo (`nix:` transport).
  Internally: full-Linux `mkVenv` over `src` (no wasi marker override) +
  `buildImage` with `Cmd = [ basePython -m fxtr_service_runner <entryPoint> ]`
  and `Env = [ PYTHONPATH=<venv site-packages> ]` — the base interpreter, not
  `${venv}/bin/python`, because pyproject-nix venv activation rides on an
  argv[0] exec shim that binfmt_misc-based emulation drops (Rosetta under
  Apple Container and similar runtimes); the venv is a packaging
  intermediate, PYTHONPATH is the runtime contract. **No `entryPoint` leaf**
  — the entry point lives only in the baked Cmd; the contract is "any
  container image that launches into a fxtr service runner" (hand-built
  `{ kind = "container"; image }` attrs are fine). Guest pyproject convention
  (full-fxtr2 git sources, build-dep hints): see
  `projects/fxtr_sandboxing/fxtr_sandboxing/tests/_workdir_templates/container_greeter/`.
  Design:
  `projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md`.
- `esbuildRenderer { src, externals ? workstationExternals, name?, system? }`
  (`esbuildRenderer.nix`) — runs `pkgs.esbuild` *inside a nix derivation*
  over `src/index.tsx` (`--bundle --format=esm --platform=browser
  --jsx=automatic` + a `--external:` per entry); `$out` is the compiled
  `.mjs` **file**. Returns a derivation, *not* an entity — the dispatcher
  (`NixConfigurableDispatchImpl`) builds it and wraps the bytes in a
  `RendererEntity` at serve time. No venv / fxtr2. `workstationExternals`
  (a `lib.fxtr` member) is the default externals list — the bare specifiers
  a renderer must not bundle (the frontend's import-map shim resolves them);
  **kept in sync** with `DEFAULT_EXTERNALS` in `fxtr_workstation`'s
  `basic_dynamic_dispatch/compile.py`.
- `mkUiPreset entries` (`mkUiPreset.nix`) — a **pure structural transpose**
  (no factory args, builds nothing): takes a name-keyed attrset of
  per-renderer declarations (each `{ renderer; subtypes = { <subtypeKey> =
  { forType = { idString; entity; }; forDispatchKeys = [ uuid ]; }; }; }`) and
  folds them into the type-id-keyed `fxtr.uiPresets.<name>`
  table the nix/git dispatcher consumes (the same shape a hand-written
  preset has, so `eval_preset_table` / `_PRESET_APPLY` are unchanged),
  `throw`ing on a duplicate `(type-id, dispatch-key)` claim. The nix analog
  of the pre-nix `demo_dispatch.py:_build_routing_table`; `forType` bundles
  the hand-pasted id_string with its `mkEntityFromPython` entity so a type
  is defined once and referenced from every shard. The `renderer` /
  `entity` values pass through untouched (the dispatcher builds them).

**Internal helper (not in the public attrset):**

- `_mkGuestSitePackages { workspaceRoot, name?, system?, sourcePreference? }`
  (`_mkGuestSitePackages.nix`) — builds a host-runnable venv via `mkVenv`
  with `environ = { sys_platform = "wasi"; platform_machine = "wasm32"; }`,
  then extracts its `lib/python<X.Y>/site-packages/` subtree via `cp -RL`
  into a flat directory at `$out/`. The `environ` override aligns uv2nix's
  marker eval with a workspace whose `[tool.uv] environments` is restricted
  to wasi (forcing pure-python wheel selection); without it uv2nix asserts
  the lock's supported-markers must match the linux/x86_64 build host. The
  `cp -RL` dereferences uv2nix's symlinks into the inner venv so the
  runnable venv stays out of the output closure. The real consumer is
  `mkWasmService` (wired in `default.nix`); not bound in `lib.fxtr.*`.
  Promote-to-public + a leaner uv2nix wiring that skips `mkVirtualEnv` are
  both tracked in
  `projects/fxtr_workstation/plans/phase0/part5_nix_workdir/followups.md`.

No helpers are stubbed anymore: the last `notYet` stub
(`mkDockerPackageBundle`) was replaced by the real `mkContainerService`
(`projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md`).
The old preset-helper stubs (`mkRendererEntity`,
`esbuildRendererEntity`, `mkDispatcherPreset`) were removed in Stage 5 —
presets are plain hand-written attrsets and the dispatcher wraps/assembles
at runtime, so the only preset-side helper is `esbuildRenderer`.

**Two writer scaffolds, by necessity.** `runFxtrWriter` uses the
lib-internal venv (fxtr2 owned inside this repo). `mkEntityFromPython`'s
writer runs in the *caller's* venv (which supplies fxtr2 + the target
module), so it is a separate standalone script, not the `fxtr-nix-writer`
cli.

### Writer convention (nix → Python)

A writer derivation passes structured input via `__structuredAttrs` and
reads `$out` for the output directory. The Python opens a
`LayeredFileStoreIsolatedWriter`, stores/pins entities (the pin walk pulls
each entity's transitive closure — type entity, ctx marker — into the
layer), and finalizes a self-contained layer (`pins.json`, `parents.json`,
`data/`, `precomp/`, `layer_format.json`).

### `py_testing/` — test harness

Tests build flake outputs through the `nix_build` / `flake_ref` fixtures
(git reference to the repo root) and read the results back with a fxtr2
`LayeredFileStoreReader`. Deserializing an entity needs its class imported,
so `py_testing` depends on `fxtr_nix_writer`. Its local venv is `uv sync`ed;
when used as the `mkEntityFromPython` test target it is built by `mkVenv`.

## Where to make changes

- **New entity-writer kind:** add the async writer to
  `fxtr_nix_writer/writers.py`, register it in `cli.py`'s `_WRITERS`, and
  drive it with a `spec.kind` through `lib.fxtr.runFxtrWriter`.
- **Implement a stubbed helper:** add `lib/<helper>.nix`, wire it into
  `default.nix` (replacing the `notYet` stub). The stub's throw message
  names the plan file with the spec.
- **New buildable example/test target:** add it under
  `packages.x86_64-linux` in the **repo-root `flake.nix`**.
- **Build / test:** `nix build .#<attr>` and `uv run pytest` in
  `py_testing/` (needs nix + a reachable `x86_64-linux` builder). Works
  from colocated *and* non-colocated jj workspaces: the workdir harness
  auto-resolves the lib ref to the workspace's `@` commit via a bare
  mirror under `~/.cache/fxtr-workdir-harness/` (see
  `fxtr_workdir_harness`'s module docstring, "Working from a jj
  workspace").
