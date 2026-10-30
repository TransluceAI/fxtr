# lib.fxtr.experimentRenderers — the fxtr_experiment renderer slice.
#
# The repo-owned renderers for fxtr_experiment's types, compiled from
# `projects/fxtr_experiment/frontend/src` (one esbuild module, several
# subtypes — the workflow dashboard keyed on the user capability's
# SERVICE type, plus the frame / stored-config entity renderers). A
# function of `keyIds` (the preset's shared dispatch-key UUID set)
# returning entries in the same shape a workdir `fxtr_renderer.nix`
# slice returns, so a preset flake merges it with one line:
#
#   rendererEntries = ... // (lib.experimentRenderers { inherit keyIds; });
#
# The `forType` values come from `lib.fxtr.entities.experiment*` (the
# fxtr_lib_type_ids stub venv — content-identical to the canonical
# fxtr_experiment ids; the dispatcher's eager validation checks them).
#
# elkjs (the plate view's layout engine) is the module's one bare
# import that is NOT a workstation external: a real npm dep bundled
# INTO the compiled .mjs. It has zero runtime dependencies, so one
# pinned registry tarball is the whole node_modules tree.
{
  canonicalSystem,
  pkgsFor,
  esbuildRenderer,
  entities,
}:
{ keyIds }:
let
  pkgs = pkgsFor canonicalSystem;
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
  shellKeys = [
    fullPanel
    blockLink
    inlineLink
    hoverPreview
  ];

  # KEEP IN SYNC with the `elkjs` version pnpm resolves for
  # projects/fxtr_experiment/frontend (pnpm-lock.yaml) so the nix-built
  # module matches the dev/typecheck/vitest environment.
  elkjsVersion = "0.11.1";
  elkjsTarball = pkgs.fetchurl {
    url = "https://registry.npmjs.org/elkjs/-/elkjs-${elkjsVersion}.tgz";
    hash = "sha256-g5c+JDtEhCNTcXQn7o6hiA1ojr55Y01AF+PMMPMhSko=";
  };
  # esbuildRenderer's `nodeModules` contract: a path whose CONTENTS are
  # the node_modules tree.
  elkjsNodeModules = pkgs.runCommand "elkjs-node-modules" { } ''
    mkdir -p $out/elkjs
    tar -xzf ${elkjsTarball} -C $out/elkjs --strip-components=1
  '';
in
{
  experiment_dashboard = {
    renderer = esbuildRenderer {
      src = ../../projects/fxtr_experiment/frontend/src;
      nodeModules = elkjsNodeModules;
      name = "fxtr-experiment-dashboard-esm";
    };
    # Subtype keys MUST match index.tsx's default-export keys.
    subtypes = {
      # The workflow dashboard (a SERVICE type: the user capability).
      experiment_workflow = {
        forType = entities.experimentUserCapability;
        forDispatchKeys = shellKeys;
      };
      # The canonical frame entity: link slots deliberately fall back
      # to the generic entity links.
      frame_v0 = {
        forType = entities.experimentFrame;
        forDispatchKeys = [
          fullPanel
          hoverPreview
        ];
      };
      grid_config = {
        forType = entities.experimentGridConfig;
        forDispatchKeys = shellKeys;
      };
      slice_template = {
        forType = entities.experimentSliceTemplate;
        forDispatchKeys = shellKeys;
      };
    };
  };
}
