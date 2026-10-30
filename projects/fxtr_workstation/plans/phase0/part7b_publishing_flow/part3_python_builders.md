# Part 3 — Python builders (`mkFxtrFnBuilder` + `mkFxtrScriptBuilder`)

> Status: RESHAPED (2026-06-19, sixth sync). Supersedes the old
> `mkProcessedLayer` draft (purity-enforcing, pre-resolved `ctx.inputs`,
> wrapped readers) — **not built**. The primitive is now: *run a user python
> script that writes into a fxtr layer writer, in a user-specified locked venv,
> pure or as a FOD by flag.* Design context: `design/06` ("Build steps"),
> `design/02` (writer/reader APIs), `design/03` §1 (the `lib.fxtr` precedents
> this generalizes). Part 4 (the report builder) is a thin consumer of this.

## Goal

One `lib.fxtr` primitive for "run user Python over entities from parent layers
(and/or a build-time remote), write the results as a new layer." Minimal
abstraction beyond what nix + the layer-writer API already force: no
input-declaration DSL, no structural purity wall, no harness owning the user's
`main`. The same primitive is the demo's postprocess step and (part 4) the
report step.

## Shape in one paragraph

A nix helper builds a derivation that runs a **command in a user-supplied
venv**; the command runs user python that imports a small fxtr2 helper
(`nix_builder`) to parse what nix passed and get an **open layer writer** over
`$out`. The user does its own reads and pins. **Purity is the `outputHash`
flag**: unset → an ordinary (pure) derivation with no remote; set → a FOD that
reads the `FXTR_REMOTE_*` contract and builds a remote. No new mechanism — this
recombines `mkEntityFromPython`'s caller-venv pattern with `runFxtrWriter`'s
FOD branch.

## Moving parts

### 1. The python helper — `fxtr2.data.layered_file_store.nix_builder`

Colocated with the writer; **"nix" is in the name** because it parses the nix
build contract (`NIX_ATTRS_JSON_FILE` structured-attrs, `$out`, `FXTR_REMOTE_*`
env). Two public functions + a nix-agnostic core:

```python
# script form (mkFxtrScriptBuilder) — the script owns main:
#   if __name__ == "__main__":
#       nix_builder.main(entrypoint)
#
# fn form (mkFxtrFnBuilder) runs:  python -m ...nix_builder
#   -> __main__ reads spec.entry, resolves "mod:fn", calls main(fn)

async def amain(entrypoint) -> None        # parse nix env -> _run(...)
def main(entrypoint) -> None               # anyio.run(amain, entrypoint)
def run_entry(spec_entry: str) -> None     # resolve "mod:fn" -> main(fn)

async def _run(                            # NIX-AGNOSTIC core (unit-testable)
    entrypoint, *, out_dir, parents, remote, spec
) -> None: ...                             # parents: dict[name -> layer path]
```

- `main`/`run_entry` are the only nix-coupled surface: they read
  `$NIX_ATTRS_JSON_FILE` → `{ parents, spec, entry? }` (parents a name→path
  map), read `$out`, and build the remote from `FXTR_REMOTE_*` **iff** those env
  vars are present (FOD case; reuse part 2's `http_deep_fetchable` in
  `fxtr2.experimental.http.client`). Then they delegate to `_run`. Keeping the
  coupling to one thin function lets the interesting logic be tested with a
  tmpdir + a fake remote, no nix.
- `_run` (via an `AsyncExitStack`): opens a builder `FxtrScope` and enters it as
  the active scope; opens the writer (parents + remote clone) and one reader per
  named parent (each a remote clone); builds `ctx`; `await entrypoint(ctx)`;
  finalizes the writer (`validate_on_close`) and closes the readers/scope.

### 2. The `ctx` handed to the entrypoint

Bare, but with the readers + scope pre-wired (we dropped pre-resolved
`ctx.inputs`). **Parents are a name→layer map**, so inputs are referenced by
meaningful name, not ordinal:

- `ctx.spec` — passthrough JSON from the flake (the user's arbitrary args).
- `ctx.scope` — the builder's `FxtrScope`, **set as the active scope** by the
  helper for the entrypoint's duration. Because fxtr2's read/construct APIs
  default to the active scope (`_resolve_scope` → `get_active_scope`),
  `reader.retrieve(key)` lands here and entity construction uses it — **no scope
  passing for reads/builds**. Exposed so it can be passed where wanted.
- `ctx.writer` — an **open** `LayeredFileStoreIsolatedWriter` over `$out`,
  constructed with the parents + (FOD) the remote. `pin_to_this_layer(name, id,
  ctx.scope)` writes output entities — `from_scope` is passed **explicitly**
  (the writer is not a `FxtrNamespace`, so it doesn't read the active scope; we
  do *not* change fxtr2's writer API for this). **FOD-only:** `mark_as_remote` +
  `preload(ref)` pull a *known-remote* entity's bytes into the output layer
  through the configured remote (the rehost) and return the decoded value;
  `preload` requires a remote (`LayerRemoteNotConfiguredError` otherwise) — it
  is **not** a way to read parent inputs.
- `ctx.readers: dict[str, LayeredFileStoreReader]` — one per named parent,
  **already open** (the helper enters them in an `AsyncExitStack` and closes
  them on exit; FOD: each gets a fresh remote-lease clone). Read an input with
  `await ctx.readers["notes"].retrieve("notes:all")` → resolves into the active
  `ctx.scope`. A reader reads a *single* layer (discovering its ancestors via
  `parents.json`), so this is one reader per named parent, not a merged view.
- `ctx.parents: dict[str, Path]` / `ctx.remote: tuple[DeepFetchable, Lease] |
  None` — raw, for power users who want to open their own reader (`remote` is
  present iff FOD; pass `.clone()` per extra reader).

**Lease ownership:** a `LayeredFileStoreReader` / the writer *takes* the lease
it's handed (pass `.clone()` to keep yours; the reader also clones per-retrieve
and discards on exit). The helper holds the master `(dfetch, lease)`, hands the
writer a clone and each `ctx.readers` entry a fresh clone, and discards the
master at finalize. No double-ownership; the pre-opened readers never exhaust
one lease.

### 3. The helper's contract (the parents/writer boundary)

The opinionated-convenience boundary, stated for users:

> Name in `parents` every layer you want the output to reference — the writer is
> opened with exactly those parents (recorded in `parents.json`, deduped
> against) and each is pre-opened as `ctx.readers["<name>"]`. A **pure** builder
> can only read inputs that live in its parents; a **FOD** builder can fetch
> from the remote
> instead. If you need to control *how* the writer/readers are opened, don't
> use this helper — write a plain python derivation that constructs whatever
> writer/readers you want. (If you're customizing writer construction you
> weren't getting much from the helper anyway.)

`parents` is **not a special nix mechanism** — it is just derivation references
passed as JSON args (any store path interpolated into the spec already becomes
a build dependency; see §5). It is a *named map* purely so the helper knows
which layers to wire into `ctx.writer` and to pre-open as `ctx.readers`.

### 4. The nix side — one scaffold, two faces (split by *what they run*)

Two public helpers distinguished by **what they run** (a function vs a script),
not by where the venv comes from — the venv is a parameter. Both delegate to one
internal scaffold.

**Internal scaffold `_runFxtrBuilder`** (generalizes `runFxtrWriter`'s FOD
branch; that helper could later be re-expressed on top of it):

```nix
_runFxtrBuilder {
  venv;                    # any venv derivation (mkVenv output)
  runCmd;                  # the command to run inside the venv
  parents ? { };           # name -> layer derivation; out-paths land in the spec as a name->path map
  spec ? { };              # arbitrary JSON passthrough
  extraBuildInputs ? [ ];  # generic "I need bare-name tools / setup hooks" knob
  env ? { };               # generic extra env (part 4 wires the report CLI path here)
  outputHash ? null;       # null = pure derivation; set = FOD
  impureEnvVars ? [ ];     # FOD: e.g. [ FXTR_REMOTE_BASE_URL FXTR_REMOTE_BEARER_TOKEN ]
  name; system ? ...;
}  ->  <layer drv>
```

- Builds `runCommand name` with `__structuredAttrs = true` and a `writerSpec`
  attr `= { inherit parents spec; entry = ...; }` (parents as a name→out-path
  map). FOD branch mirrors `runFxtrWriter`: `outputHashMode=recursive`,
  `outputHashAlgo=sha256`, `impureEnvVars`, `nativeBuildInputs += cacert`.
  `extraBuildInputs` → `nativeBuildInputs`; `env` merged into the derivation
  env. Build phase: `runCmd`.

**`mkFxtrFnBuilder` — run an importable function:**

```nix
mkFxtrFnBuilder { venv; entry; parents ? {}; spec ? {}; outputHash ? null; ... }
```

`venv` required (a function lives in an installed package). Sets
`runCmd = "${venv}/bin/python -m fxtr2.data.layered_file_store.nix_builder"` and
puts `entry = "mod:fn"` into the spec; the `-m` launcher reads it and runs
`run_entry`. Owns all "invoke a user-defined entrypoint" logic.

**`mkFxtrScriptBuilder` — run a loose script file:**

```nix
mkFxtrScriptBuilder { venv; script; parents ? {}; spec ? {}; outputHash ? null; ... }
```

`venv` required in v0; `script` is a `.py` file (interpolated into the store).
Sets `runCmd = "${venv}/bin/python ${script}"`; the script owns `main` and calls
`nix_builder.main(entrypoint)` itself. Owns all "invoke a script" logic. The
script need not be importable as a package — it runs as `__main__` and imports
whatever it needs *from* the venv (fxtr2's `nix_builder`, plus any third-party /
local-path-source packages the venv's uv workspace declares). **(Follow-up:**
`venv` becomes optional, defaulting to a venv built from the script's inline
PEP-723 metadata — see below.)

Either helper returns the layer drv; the user flake wraps it as
`fxtr.entities.<name> = { type = "fxtr_layer_entity"; layer; layerKey; }` for
whichever pins it publishes (the entrypoint pins under those keys).

**Modes (v0):**

| helper | runs | venv |
|---|---|---|
| `mkFxtrFnBuilder` | an importable `mod:fn` | required (`mkVenv`) |
| `mkFxtrScriptBuilder` | a loose `.py` file (owns `main`) | required (`mkVenv`) |

**"One pyproject, many scripts."** A single `mkVenv` (one `pyproject.toml` /
`uv.lock`) can back any number of `mkFxtrScriptBuilder` calls pointed at
different `.py` files. So "cheaply add a build step" = "drop a `.py` file + add a
builder call" — no dir-with-pyproject per step, and (v0) no inline-metadata
machinery at all. This is the intended way to add steps; it also handles
**local path-source packages** cleanly (the venv's workspace can declare them),
which the inline follow-up cannot (see the constraint there).

**Sibling-file caveat.** `${./script.py}` copies a *single* file into the store,
so loose sibling `.py` files next to it are **not** on `sys.path`. Shared local
code should be a path-member package in the venv's workspace (then imported from
the venv). A directory form (`script = ./dir; entrypoint = "main.py"`) is a
possible extension if loose sibling modules ever matter; left out of v0.

### 5. Purity is a flag, not a wall

- **Pure**: omit `outputHash`. Ordinary derivation, no `FXTR_REMOTE_*`, writer
  opened with `remote=None`. An undeclared read fails naturally
  (`LayerRemoteNotConfiguredError` / `LayerEntityMissingError`) — not enforced
  machinery, just the absence of a remote.
- **FOD**: pass `outputHash` (v0: `lib.fakeHash` / `fodLockReader` — part 2b)
  + `impureEnvVars = [ FXTR_REMOTE_* ]`. The helper sees the env vars and
  builds the remote; `ctx.remote` is populated; the user can `mark_as_remote` /
  `preload` / read through the remote.

No store reference needs a special declaration: interpolating any derivation
into `spec`/`env`/`parents` makes it a build dependency automatically (nix
scans the structured-attrs JSON for store paths). PATH/`buildInputs` are a
*separate*, opt-in concern (setup hooks) — only needed for bare-name tools.

### `mkEntityFromPython` stays

Unchanged. It resolves a **pre-existing** `module:attr` entity object and pins
it — it never calls a function, so it is genuinely not a special case of
`mkFxtrFnBuilder` (which calls `entrypoint(ctx)`), and it saves the boilerplate
of writing a one-line "pin this entity into `$out`" function.

## Follow-up — inline-metadata scripts (defer past v0)

Once v0 lands, `mkFxtrScriptBuilder`'s `venv` becomes **optional**: when
omitted, build the venv from the script's PEP-723 `# /// script` block via
uv2nix `lib.scripts.loadScript` (`mkOverlay` / `mkVirtualEnv`), then run the
rendered script. This is the "single self-contained file, no workspace dir"
ergonomic — purely additive over v0, which already covers "cheaply add scripts"
via one shared venv.

Why it's a follow-up, not v0 — it carries machinery v0 sidesteps entirely:

- **`getLocalProjects` constraint (verified in uv2nix `lib/lock1.nix`).**
  `loadScript` resolves *local* path/editable/directory/virtual lock sources
  relative to a `workspaceRoot` it sets to `throw` — forced **iff** the script's
  lock has any such local dep. So inline metadata works **only with git/registry
  deps**: a published flake (fxtr2 as a **git** dep) is fine; anything with a
  **path** dep is not. The v0 explicit-venv form has no such limit.
- **`.py.lock`.** Inline scripts need a sibling `${script}.lock` (a
  `uv.lock`-format file from `uv lock --script`), a committed artifact kept in
  sync with the inline deps. v0's explicit venv uses the workspace `uv.lock`
  via `mkVenv` — no `.py.lock`.
- **Workdir-harness surface.** For inline scripts, `prepare_workdir_repo` would
  additionally need to `uv lock --script` each script *and* substitute the
  fxtr2 git ref into each script's inline `[tool.uv.sources]` block (`[tool.uv]`
  in inline metadata is supported — confirmed via uv2nix's `config.py`
  fixture). This is the real new work; v0 has none of it.
- **Orthogonal to the part-2b FOD lock** (not an interaction, just a
  don't-conflate): `.py.lock` pins python dep resolution; `fod_lock.toml` pins
  FOD output hashes. A FOD inline script would carry both, doing different jobs.

## Implementation steps (v0)

- [x] `fxtr2.data.layered_file_store.nix_builder`: `_run` core + `ctx`
      (`spec`/`scope`/`writer`/`readers`/`parents`/`remote`) + active-scope set
      + lease-cloning; the
      nix-coupled `main`/`run_entry`/`__main__` (reads structured-attrs + env,
      FOD remote via `http_deep_fetchable`). Unit-test `_run` with a tmpdir +
      in-process `RepoEntityStore` remote (pure + FOD reads, pin round-trip).
      **Note:** the `ctx` reader→scope wiring uses the option-(a) design from the
      pre-work discussion — `_run` opens each parent reader with
      `isolate_scope=True` and builds `ctx.scope` with
      `include=[reader.scope, …]` (active), so a bare
      `ctx.readers["x"].retrieve(key)` lands accessible in `ctx.scope`. (The
      pre-work fix to `LayeredFileStoreReader` makes `into_scope=None` resolve to
      the active scope + lift, so this also lifts the id into `ctx.scope`.)
- [x] `_runFxtrBuilder.nix` (scaffold; pure + FOD branches) + `mkFxtrFnBuilder.nix`
      + `mkFxtrScriptBuilder.nix` (explicit venv) + wiring in `nix/lib/default.nix`.
      (The scaffold builds the `builderSpec` structured attr `{ parents; spec;
      entry?; }`; `venv` is baked into `runCmd` by each helper rather than passed
      to the scaffold separately. Eval-checked both helpers are functions.
      **Later fix (in 7b.4d):** the `env` knob now `export`s its vars at the top
      of the build command — under `__structuredAttrs` scalar attrs aren't
      exported to child processes, so an `env` var wouldn't reach the python
      builder's `os.environ`. Latent until part 4's report step first used `env`.)
- [x] py_testing fixture: `mkEntityFromPython` input layer → a builder step
      (fn *and* script form) reading it (parent) and pinning a derived entity →
      read back + assert (pure; host + podman sweep). (`py_testing.builder_fixture`
      + `sample_build_script.py` + flake targets `fxtr-fn-builder-check` /
      `fxtr-script-builder-check` + `test_nix_builder.py`. Validated on the podman
      backend / linux builder.)
- [x] FOD-path test: a builder with `outputHash` reading a pin via the part-1
      rehost (`fakeHash` → re-pin), assert the derived layer + remote pointer
      resolution. (`fxtr_nix_runner` `_workdir_templates/builder_fod` + a
      `test_fod_builder_through_rehost` E2E using `update_hashes` + `mgr.entities`,
      mirroring `test_entity_from_tsv_pin_remote_pointer`. **Test-infra fix
      folded in:** the `nix_backend` sweep fixture now mounts the harness lib
      source (`resolve_lib_ref().repo_path`) into the podman builder so a workdir
      `mkVenv` can fetch fxtr2 from the mirror at build time — the first test to
      need a live lib fetch in-build; the others use the cached lib-internal venv.)
- [x] Update `nix/CODE_OVERVIEW.md` + `v2/.../layered_file_store/LOCAL_CODE_OVERVIEW.md`
      (the `nix_builder` helper). (Also `fxtr_nix_runner/CODE_OVERVIEW.md` for the
      builder_fod fixture + the conftest mountable_paths change.)

## Implementation steps (inline-metadata follow-up)

- [ ] Make `mkFxtrScriptBuilder`'s `venv` optional → default via `loadScript`;
      example inline `build.py` + `build.py.lock` (git-dep fxtr2) + nix E2E.
- [ ] Workdir-harness: `uv lock --script` + fxtr2-git-ref substitution into
      inline `[tool.uv.sources]`.
- [ ] Update `nix/CODE_OVERVIEW.md`.

## Discuss / TODOs

Settled:
- **`nix_builder` home**: in `fxtr2` (the already-required dep carries it, zero
  extra dep); nix coupling isolated to the thin `main`/`run_entry` layer, `_run`
  nix-agnostic.
- **`extraBuildInputs` *and* `env`**: both offered. A derivation referenced by
  **absolute path** in `env` is already a build dependency (no PATH needed);
  `extraBuildInputs` is the opt-in PATH/setup-hook path. Part 4 uses `env`;
  `extraBuildInputs` is kept as the generic bare-name-tool knob (cheap, unused
  in v0).

Verify at implementation time (tests/risks, not design):
- **FOD determinism**: layer output must be byte-stable for fixed inputs
  (sorted pins/parents, canonical CBOR, precomp JSON key order) — the
  build-twice byte-diff is the gate, same as part 2 §5.
- **Active scope ↔ reader**: confirm `reader.retrieve(key)` with `into_scope`
  defaulted to the active `ctx.scope` does the right cross-scope copy (the path
  exists — `_retrieve_impl` + the ABC's active-scope default — but it's the one
  interaction to test first).

Deferred:
- **Provenance**: should the entrypoint's scope set a `provenance_context` so
  derived entities carry a build-step c-node? Needs a deterministic payload to
  stay byte-stable. Plain scope in v0, provenance-context as a fast-follow.
