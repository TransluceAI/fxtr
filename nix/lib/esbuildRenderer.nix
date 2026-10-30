# esbuildRenderer — compile a renderer's `index.tsx` into an ESM module,
# *inside a nix derivation*, returning the compiled `.mjs` derivation.
#
# A preset's `byDispatchKey.<uuid>.renderer` slot points at the output of
# this helper (see design_notes/06_ui_presets.md). The dispatcher
# (`NixConfigurableDispatchImpl`) `nix build`s it and wraps the bytes in a
# `RendererEntity` at serve time — so this returns a **derivation, not an
# entity**, and needs no venv / fxtr2.
#
# `$out` is the compiled `.mjs` **file** (not a directory); the dispatcher
# reads it with `Path(out).read_bytes()`.
#
# Bundling: every name in `externals` stays a bare-specifier import for the
# frontend's import-map shim to resolve at load time (the same contract as
# the runtime `npx esbuild` path in
# `basic_dynamic_dispatch/compile.py` — keep the default list in sync with
# that module's `DEFAULT_EXTERNALS`). Anything not externalized is bundled;
# since renderer dirs carry no `node_modules`, a non-external import that
# can't be resolved fails the build (the intended guard).
#
# Design:
#   projects/fxtr_workstation/plans/phase0/part5_nix_workdir/design_notes/06_ui_presets.md
{
  canonicalSystem,
  pkgsFor,
  # Default externals (the workstation import-map set). Threaded in from
  # default.nix so `lib.fxtr.workstationExternals` is the single source.
  workstationExternals,
}:
{
  # Path to the renderer source dir (must contain `index.tsx`).
  src,
  # Bare specifiers to leave un-bundled. Defaults to the workstation set.
  externals ? workstationExternals,
  # Optional path whose CONTENTS are a `node_modules` tree, for bare
  # imports that must be BUNDLED rather than externalized (real npm deps
  # like elkjs — not workstation-provided modules). Symlinked beside a
  # copy of `src` so esbuild's upward node_modules resolution finds it.
  # Renderers that don't pass it keep the no-node_modules guard: any
  # non-external bare import fails the build.
  nodeModules ? null,
  # Derivation name.
  name ? "fxtr-renderer-esm",
  # Single-arch pin; override to build on a different builder.
  system ? canonicalSystem,
}:
let
  pkgs = pkgsFor system;
  externalArgs = builtins.concatStringsSep " " (map (e: "--external:${e}") externals);
  # Re-add the renderer sources content-addressed: a flake's whole source
  # is one store path, so building straight from `src` would rebuild this
  # slice on ANY change to the enclosing flake's tree. `builtins.path`
  # gives the subdir its own content-addressed store path, so the slice
  # rebuilds only when the renderer dir itself changes.
  narrowedSrc = builtins.path {
    path = src;
    name = "fxtr-renderer-src";
  };
in
pkgs.runCommand name { } ''
  cp -r ${narrowedSrc} src
  ${pkgs.lib.optionalString (nodeModules != null) "ln -s ${nodeModules} node_modules"}
  ${pkgs.esbuild}/bin/esbuild src/index.tsx \
    --bundle \
    --format=esm \
    --platform=browser \
    --jsx=automatic \
    ${externalArgs} \
    --outfile="$out"
''
