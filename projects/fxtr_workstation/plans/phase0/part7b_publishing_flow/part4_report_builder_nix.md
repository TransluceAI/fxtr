# Part 4 — `lib.fxtr.reportBuilderCli` + the report build step

> Status: RESHAPED (2026-06-19, sixth sync). The report step is **not** a
> special recipe — it is just a part-3 builder (`mkFxtrFnBuilder` or
> `mkFxtrScriptBuilder`) whose venv includes `fxtr_report_builder` and whose
> generic `env` points at a
> nix-built CLI. **Nix has zero report-builder-specific code.** Depends on
> part 3; independent of parts 1–2. Design context: `design/04` §4, `design/06`
> ("Report step"), DECIDED-5/6/12.

## Goal

Make `fxtr_report_builder` Just Work inside a part-3 builder derivation:

1. A pure pnpm-in-nix build of its TS CLI as a **self-contained executable**
   (`lib.fxtr.reportBuilderCli`), pinned by the fxtr flake, built once, shared.
2. `fxtr_report_builder` learns to find that executable via a generic
   resolution order (arg → env → dev fallback).
3. The report step is then an ordinary part-3 builder — the user wires the CLI
   in through the builder's **generic `env` arg**, not a system special case.

## The seam: "wire a binary dep into the env, then run it"

The general problem is not report-builder-specific: a python build step needs a
binary that can't live in pure-python/uv (here, a node bundle). The nix side
must **not** know about `fxtr_report_builder`. So:

- `reportBuilderCli` is a normal nix package exposing one wrapped executable.
- The user passes its absolute path via the builder's generic `env`:
  `env.FXTR_REPORT_BUILDER_CLI = "${lib.fxtr.reportBuilderCli}/bin/fxtr-report-builder"`.
  Interpolating the store path already makes it a build dependency (no PATH, no
  `extraBuildInputs` needed — part 3 §5). Nix just forwards a user-defined env
  var; it has no idea what it's for.
- Not on PATH: the CLI is an internal helper, not a user-invokable command, so
  we deliberately avoid the ambient-PATH idiom.

## Moving parts

### 1. `lib.fxtr.reportBuilderCli` (self-contained executable)

Pure pnpm-in-nix build of `projects/fxtr_report_builder/ts/packages/cli/` →
the single `main.cjs` (`@mdx-js/mdx` + `cbor-x` inlined via the package's own
`esbuild.config.mjs`), then `makeWrapper` so:

```
$out/bin/fxtr-report-builder  ==  ${nodejs}/bin/node ${bundle}/main.cjs "$@"
```

Node is bundled into the wrapper, so a consumer wires **one** thing (the
executable path) and needs nothing else in the build env.

- Route: nixpkgs `pnpm.fetchDeps` (lockfile FOD, standard `fakeHash`
  workflow) + `pnpm install --offline` + `node esbuild.config.mjs` build phase
  (cf. `esbuildRenderer.nix` for esbuild-in-derivation precedent).
- Exposed as a derivation attr (not a function) so all consumers share one
  pinned build.
- **Lockfile — DECIDED: pull the cli out of the repo pnpm workspace and give it
  its own standalone lockfile.** `pnpm.fetchDeps` wants a self-contained
  package+lock, but the cli currently rides the repo-root workspace lock (so a
  `fetchDeps` over it would fetch the *whole* repo's TS closure and rebuild on
  unrelated changes). The cli is `private: true`, genuinely self-contained
  (only `@mdx-js/mdx` + `cbor-x`; no `@fxtr-*`/`workspace:*` deps), the **only**
  package under its glob, and **already has** an in-dir `pnpm-lock.yaml` (it's
  currently in *both* the root lock and its own — a dual state this resolves).
  Concretely:
  - remove `projects/fxtr_report_builder/ts/packages/*` from
    `pnpm-workspace.yaml`;
  - `fetchDeps` consumes the cli's own `pnpm-lock.yaml`; `src` filtered to the
    cli dir (+ its lock) so unrelated repo changes don't rebuild;
  - pre-commit: drop `projects/fxtr_report_builder/ts/` from the root
    `pnpm-lock-ts` hook's trigger and add a sibling hook running
    `pnpm --dir <cli> install --frozen-lockfile` so the standalone lock stays
    checked;
  - dev build becomes `pnpm build` in the cli dir (nothing in root CI builds it
    today, so low blast radius).
  - Reversible later (rejoin the workspace) if the cli ever needs a `@fxtr-*`
    dep — but the v2/ts redo eventually retires this package anyway
    (`followups.md`).

### 2. Generic CLI resolution in `fxtr_report_builder`

`fxtr_report_builder/_ts_dist_path.py:resolve_ts_dist_path` currently uses a
fixed relative path. Change to a generic order (no PATH lookup):

1. explicit argument threaded from `build_mdx_tree(..., cli_path=...)`,
2. env var (e.g. `FXTR_REPORT_BUILDER_CLI`),
3. existing relative-path dev fallback (keep its "run pnpm build" message).

The env var is **generic wiring**, not a system injection: whoever runs the
builder sets it (the demo via the builder's `env`). Confirm the resolved
path is invoked the same way the current `node main.cjs` call is (the wrapper
takes the place of `node` + the bundle path).

### 3. The report step (no new recipe)

Just a part-3 builder:

```nix
mkFxtrFnBuilder {                       # or mkFxtrScriptBuilder { venv; script; }
  venv = mkVenv { ... };                # venv depends on fxtr_report_builder (+ demo entity pkgs)
  entry = "demo.report:build";
  parents = [ postprocessLayer ... ];   # MDX input entities read from parents
  spec = { mdxSources = ...; title = ...; summary = ...; };
  env.FXTR_REPORT_BUILDER_CLI = "${lib.fxtr.reportBuilderCli}/bin/fxtr-report-builder";
}
```

The entrypoint reads MDX text from flake-source paths (pure inputs), loads
referenced entities from parents (`ctx.readers["<name>"]`), calls
`build_root_report` / `build_sub_report` (provenance projection handled
Python-side around the subprocess — resolves OPEN-6), and pins the roots. The
output is exposed by the demo as `fxtr.entities.report` (a pure layer, no FOD).

### 4. Determinism check

`build_root_report` output must be byte-stable for fixed inputs (content
addressing should guarantee it, but a JS toolchain is in the loop). Known risk
spots: object key ordering through the JSON/CBOR envelope (Python re-encodes
CBOR canonically — likely fine), anything time-dependent in MDX compile (none
known). Build-twice/compare test is the gate. **[verify]**

## Implementation steps

- [x] Lockfile investigation + chosen arrangement for the cli package.
      (Confirmed: the cli's own `pnpm-lock.yaml` is self-contained + fresh, but
      was dormant — the root workspace shadowed it. Pulled
      `projects/fxtr_report_builder/ts/packages/*` out of `pnpm-workspace.yaml`,
      regenerated the root lock (cli importer pruned), and made the cli's
      standalone lock authoritative. Pre-commit: dropped the cli path from all
      three TS hooks (lock/biome/tsc — they run workspace-wide, so they no longer
      cover the cli) and added a dedicated `pnpm-report-cli` hook running
      `pnpm install --frozen-lockfile --ignore-workspace` + `pnpm run check` in
      the cli dir. `--ignore-workspace` is required so pnpm uses the cli's own
      lock rather than walking up to the root workspace; the nix `fetchDeps`
      build is already isolated (cli src copied into the store, no root workspace
      above it).)
- [x] `reportBuilderCli.nix` (pnpm-in-nix bundle + `makeWrapper`) + repo-root
      flake check target; py_testing test: build, run `$out/bin/fxtr-report-builder`
      with the README's smoke-test envelope, assert `{ok: true}`.
      (Used the current nixpkgs API: `fetchPnpmDeps` + `pnpmConfigHook` (needs
      `pkgs.pnpm` in `nativeBuildInputs`) + `fetcherVersion = 3`. Since the bundle
      is x86_64-linux and can't execute on a mac dev host, the smoke runs the CLI
      **in-derivation** via a `report-builder-cli-smoke` flake target that asserts
      `{ok:true}`; the py_testing test just builds it. `fetchPnpmDeps` worked
      first try — no hand-rolled `pnpm fetch` fallback needed.)
- [x] `_ts_dist_path.py` resolution order (arg → env → dev) + unit test; update
      `projects/fxtr_report_builder/CODE_OVERVIEW.md`. (Per the pre-work #2
      decision, `resolve_report_cli_command` returns the **launch argv** — the
      wrapper executable is `[exe]`, the dev fallback is `["node", main.cjs]` —
      since the resolved thing replaces `node` + the bundle path. Threaded
      `cli_path` through `build_mdx_tree`/`build_root_report`/`build_sub_report`;
      `_run_subprocess` runs the argv. `test_ts_dist_path.py` covers the order;
      the existing `test_mdx_builder` integration tests pass on the refactored
      dev-fallback path.)
- [x] Report-step fixture in py_testing: MDX + two inputs (one parent-layer
      entity, one derived by a part-3 step) → a part-3 builder with `env` wiring
      → read back `RootReport` in Python → assert tree shape + input refs
      round-tripped with provenance intact. (`report_fixture.py` +
      `report-step-check` flake target (parents = builderInputLayer +
      fnBuilderLayer; `env.FXTR_REPORT_BUILDER_CLI`) + `test_report_step.py`.
      **Provenance note:** the layered store is bare-data on disk (the format
      strips provenance, capturing it via precomp `unwraps_to`), so the
      round-tripped refs are the inputs' bare *data* ids — provenance is intact
      *through* the build (`build_root_report` re-layers) but not on-disk. The
      test asserts the report references exactly the two inputs.
      **Fixed along the way:** `_runFxtrBuilder` now `export`s the builder's
      `env` vars in the build command — under `__structuredAttrs` scalar attrs
      aren't exported to child processes, so `FXTR_REPORT_BUILDER_CLI` wasn't
      reaching `os.environ`. The `env` knob was latent (unused by parts 3a–3d).)
- [x] Determinism test (build twice, compare layer bytes). (Done as
      `report-step-check-alt` — an independent build with identical inputs; the
      test asserts both pin the same content-addressed `RootReport` id. `nix build
      --rebuild` can't offload an x86_64-linux build from a mac host, so two
      normal builds are the portable equivalent.)
- [x] Update `nix/CODE_OVERVIEW.md`.

## Discuss / TODOs

Settled:
- **Bundle delivery — external+env.** A self-contained `reportBuilderCli`
  executable wired via the generic `env` arg. (Rejected: an overlay injecting
  the JS bundle into `fxtr_report_builder`'s site-packages — couples the JS
  artifact into the python build, more magic.)
- **Lockfile** (§1): pull the cli out of the pnpm workspace, use its own
  standalone lock (self-contained + already has one). Mechanical care captured
  in §1 (workspace-yaml edit, pre-commit hook split, dev build in-dir).
- **nodejs pin**: nixpkgs' default `nodejs` from the lib's pinned nixpkgs (the
  CLI is small).
- **CLI outside nix**: host dev loop keeps using the pnpm-built dist via the
  relative dev fallback; additive, fine as-is.

Verify at implementation time (tests/risks, not design):
- **Output determinism** (§4): `build_root_report` byte-stable for fixed inputs
  — build-twice byte-diff is the gate.
- **esbuild + pnpm-offline**: `esbuildRenderer.nix` proves esbuild-in-derivation;
  the new bit is `pnpm install --offline`. If `fetchDeps` fights it, fallback is
  a hand-rolled `pnpm fetch` FOD cache. Flag early.
- **Dual-lock cleanup**: first step — make the cli's standalone lock the single
  source of truth (confirm it's fresh, not a stale leftover) before trusting it
  for `fetchDeps`.
