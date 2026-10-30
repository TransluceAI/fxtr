# Piece: the nix layer — `lib.fxtr`, writers, runner, and existing build→read paths

> Status: provisional first pass (2026-06-12). Line numbers approximate.

The publishing flake is "a new example workdir … intended as a proxy for
publishing a report as a nix flake". It will be built from the same nix
infrastructure the workstation workdirs use. This doc maps that
infrastructure and the two existing Python paths that already do
"nix-build a layer, then read entities out of it" — the pattern the new
serving script generalizes.

## 1. `lib.fxtr` (nix/lib/) — the helper namespace

See `nix/CODE_OVERVIEW.md` (authoritative) and the part5 orientation doc
(`../part5_nix_workdir/overview.md`). The members most relevant here:

- `mkVenv { workspaceRoot, ... }` (`nix/lib/mkVenv.nix`) — uv2nix workspace →
  venv derivation. The publishing flake's python postprocessing step runs in
  one of these.
- `runFxtrWriter { name, spec }` (`nix/lib/runFxtrWriter.nix`) — runs the
  lib-internal `fxtr-nix-writer` console script
  (`nix/fxtr_nix_writer/src/fxtr_nix_writer/cli.py`) in a derivation; the
  writer spec rides `__structuredAttrs` (`writerSpec`) and the script writes a
  layered file store to `$out`. New writer kinds get added to
  `fxtr_nix_writer/writers.py` + the `_WRITERS` table in `cli.py`. **This is
  the natural home for a "tsv-pins → layer" writer kind** (sketch item: "a way
  for nix to parse and interpret tsv-namespace data and turn it into layered
  file store entities").
- `mkEntityFromPython { venv, fromPython, layerKey? }`
  (`nix/lib/mkEntityFromPython.nix` +
  `nix/lib/mk_entity_from_python_writer.py`) — the caller-venv writer that
  resolves `module:attr` to an entity object and pins it into a fresh layer.
  The pattern to imitate for "a python script which loads some entities … and
  does some postprocessing": same scaffold, but the writer additionally
  *reads* (from parent layers / a remote) before writing.
- The entity attr convention: `{ type = "fxtr_layer_entity"; layer = <drv>;
  layerKey = "<key>"; }` (part5 `design_notes/03_entity_declarations.md`);
  `builtinEntity` for `!`/`.` ids; `mkUiPreset` / `esbuildRenderer` for the
  renderer side (not central to part 7b but the flake shape to mirror).
- Build-time remote contract (pinned, unimplemented): `FXTR_REMOTE_BASE_URL` +
  `FXTR_REMOTE_BEARER_TOKEN` env vars consumed by writer derivations
  (`../part5_nix_workdir/design_notes/07_remote_entity_store.md`). Note:
  derivations that fetch from a remote at build time need to be
  fixed-output-style (FOD) or run with relaxed sandboxing; note 07 assumes
  FOD-with-pinned-hash. Part 7b is the first real consumer of this contract.

Also relevant: `nix/fxtr_workdir_harness/` (`prepare_workdir_repo`) — the
test/scaffold helper that copies an in-repo workdir template into a fresh git
repo with the fxtr lib ref substituted. The publishing example will want the
same treatment (cf. `examples/fxtr_workstation_demo/scaffold_workdir.py`).

## 2. `fxtr_nix_runner` — how Python invokes nix

`projects/fxtr_nix_runner/` (see its `CODE_OVERVIEW.md`). The shared backend
beneath all nix wrappers: `resolve_backend()` →
`async with backend.builder_for(repo) as h:` →
`h.flake_ref_root()` / `h.run_nix(argv)` / `h.lock()` /
`h.materialize(store_path, closure=...)`. Two backends (host nix; podman
builder container). `materialize` returns `Materialized(host_path, remap)`;
on podman, `remap = (Path("/nix/store"), <materialize_root>/nix/store)` and
readers must rewrite `parents.json` paths via
`LayeredFileStoreReader(..., parent_path_remap=...)`.

The serving script should consume nix exclusively through this layer
(anyio-only, no fxtr2 dependency, transport-not-policy).

## 3. Existing path A: `retrieve_nix_entity` (MCP)

`projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/src/fxtr_workstation_mcp/host_impl.py:222-303`
+ `nix_util.py`. End-to-end:

1. Flake ref = the bare workdir path via `flake_ref_root()`
   (`nix_util.py:115-130`) — nix auto-resolves to git-form in a repo
   (non-frozen: tracked-file edits count, untracked files are invisible and
   surfaced as a notice).
2. `nix eval --json <ref>#fxtr.entities.<name> --apply <filter>` extracts
   `{type, layerKey}` only — no build (`nix_util.py:143-182`).
3. Builtin short-circuit for `{type="builtin"}` (`host_impl.py:257-276`).
4. `nix build --no-link --print-out-paths <ref>#<attr>.layer`
   (`nix_util.py:185-215`), then `h.materialize(out_path)`.
5. `async with LayeredFileStoreReader(mat.host_path, parent_path_remap=remap):
   await reader.retrieve(layer_key, into_scope=script_scope,
   copy_into_scope=True)` (`host_impl.py:283-303`) — the entity + closure is
   copied into the live scope before the reader closes.

Errors: `NixError` carries verbatim stderr; MCP wraps as
`NixEntityBuildFailed` + a log dump file.

## 4. Existing path B: the nix dispatcher's `set_preset`

`projects/fxtr_workstation/src/fxtr_workstation/dynamic_dispatch/nix_configurable_dispatch/service_impl.py:195-275`
+ `nix.py`. Differences from path A worth stealing:

- **Eval once, batch-build many.** `eval_preset_table` (`nix.py:103-125`)
  `nix eval --json … --apply` reshapes the whole preset to
  `{typeEntityLayerDrv, typeEntityLayerKey, byDispatchKey{rendererDrv}}`
  (drvPaths only, no realization), then one
  `nix build "<drv>^out" … --json --no-link` over the deduped batch
  (`nix.py:128-156`), then `materialize` per output.
- **Eager validation**: each built typeEntity layer is opened with
  `LayeredFileStoreReader` and the pinned id is asserted equal to the
  table key (`service_impl.py:277-306`) — mismatch rejects the whole preset
  atomically.
- **Caching**: `(git_commit, drvPath) → RendererEntity` cache
  (`service_impl.py:125-126, 221-226`).
- **Git-pinned refs**: `git+file://<repo>?rev=<commit>&dir=<flake_path>#<attr>`
  — committed code only, vs. path A's live-workdir refs. The publishing flake
  is "what a workstation project graduates to", so it likely sits on the
  committed side of this divide (discussion topic).
- Graceful-degradation convention for boot-time auto-load
  (`service_impl.py:160-184`): warn + empty table, never crash; explicit
  invocations raise.

## 5. What part 7b adds on top (post-sync version — see `design/06`)

1. **Namespace export** (pure derivation): parse/validate the committed
   `*.fxtr.tsv` files (format: `design/01`) into a publication manifest —
   backs the new `fxtr.namespaces.<name>` attr slice; server-only, no layer,
   no network. Plus the **`entityFromTsvPin` writer kind** (FOD): resolve a
   single TSV pin key → id, fetch its bytes via the rehost into a one-pin
   layered store for in-build reads (the `fxtr.entities.<name>`-from-a-TSV-pin
   case). Networked build steps are exactly the FODs that read `FXTR_REMOTE_*`;
   pinned output hashes (`lib.fakeHash` + manual re-pin in v0, lock-file
   followup `part2b_fod_lock.md`). (Reshaped 2026-06-14: no generic
   `preload_from_remote` helper — see `part2_namespace_export_and_preload.md`.)
2. Python build-step helpers (part 3): `mkFxtrFnBuilder` (an importable
   `mod:fn`) and `mkFxtrScriptBuilder` (a loose script file), over a shared
   `_runFxtrBuilder` scaffold — run user python with a handed-in layer writer;
   pure (no remote) or FOD (remote + `outputHash`) by flag. Generalizes
   `mkEntityFromPython`'s caller-venv pattern.
3. A **pure** report-layer writer running `fxtr_report_builder`, with the
   CLI bundle built pure-pnpm-in-nix as a new `lib.fxtr` member
   (`reportBuilderCli`; see `design/04` §4 and `design/06`).
4. A `fxtr_nix_runner` extension exposing the host rehost endpoint inside
   builds on both backends (`design/07_build_rehost_networking.md`).
5. The `fxtr_nix_runner` manager (part 1): open the repo entity store + rehost
   it + eval the flake's `fxtr` attrset (the source of truth for what's
   exposed) + build. The example serving script drives it and serves the
   declared entities/namespaces with the repo entity store (`RepoEntityStore`,
   a `DeepFetchable`) as runtime remote (see `design/05` §6, `design/06`).
