# lib.fxtr — the fxtr nix helper namespace.
#
# Consumed by the repo-root flake as `lib.fxtr`, and by downstream user
# workdir flakes (which take the fxtr flake as an input). See
# `projects/fxtr_workstation/plans/phase0/part5_nix_workdir/design_notes/10_nix_infrastructure.md`
# for the full helper inventory and rationale.
#
{ inputs }:
let
  inherit (inputs) nixpkgs;

  # Principled single-architecture stance (overview.md / OQ3): the
  # `fxtr.*` namespace is not indexed by system; each buildable-producing
  # helper instead defaults its `system` arg to this.
  canonicalSystem = "x86_64-linux";

  pkgsFor = system: nixpkgs.legacyPackages.${system};
  pkgs = pkgsFor canonicalSystem;

  # nix2container's per-system package set (buildImage etc.), backing
  # mkContainerService. The spawner-side patched skopeo is pinned
  # separately in `fxtr_sandboxing/nix.py` (it runs on the *host*).
  nix2containerFor = system: inputs.nix2container.packages.${system}.nix2container;

  mkVenv = import ./mkVenv.nix { inherit inputs canonicalSystem pkgsFor; };

  mkEntityFromPython = import ./mkEntityFromPython.nix { inherit canonicalSystem pkgsFor; };
  # Non-layer entity attr for a primitive FxtrID (e.g. `!basic_type`); consumed
  # by the preset path + retrieve_nix_entity. See ./builtinEntity.nix.
  builtinEntity = import ./builtinEntity.nix;

  # Ready-made `forType` values for the library/builtin type ids the assembly
  # demo's `builtin/` renderer routes — built here so consumers just import
  # them. See ./entities.nix.
  entities = import ./entities.nix { inherit mkVenv mkEntityFromPython builtinEntity; };

  # Shared scaffold the no-venv writer helpers build on: runs a
  # fxtr_nix_writer entry point in a derivation to write a layered store.
  runFxtrWriter = import ./runFxtrWriter.nix { inherit canonicalSystem pkgsFor mkVenv; };

  # Pure publication of a committed tsv namespace as a JSON manifest
  # (backs `fxtr.namespaces.<name>`). See ./tsvNamespaceExport.nix.
  tsvNamespaceExport = import ./tsvNamespaceExport.nix { inherit canonicalSystem runFxtrWriter; };

  # A single tsv-namespace pin as a `fxtr.entities.<name>` (a preload FOD that
  # fetches the pinned entity's bytes via the rehost). See ./entityFromTsvPin.nix.
  entityFromTsvPin = import ./entityFromTsvPin.nix {
    inherit canonicalSystem runFxtrWriter;
    lib = nixpkgs.lib;
  };

  # Standalone reader for FOD `outputHash`es out of a `fod_lock.toml` — dropped
  # in wherever `lib.fakeHash` would go; managed by `fxtr_nix update-hashes`.
  # See ./fodLockReader.nix.
  fodLockReader = import ./fodLockReader.nix;

  # Internal scaffold beneath the two builder helpers: run a user command in a
  # derivation that writes a layered store at $out (pure, or FOD by outputHash).
  # See ./_runFxtrBuilder.nix.
  _runFxtrBuilder = import ./_runFxtrBuilder.nix { inherit canonicalSystem pkgsFor; };

  # Run user Python that writes into a layer writer: an importable `module:fn`
  # (mkFxtrFnBuilder) or a loose `.py` script (mkFxtrScriptBuilder), both over a
  # caller-supplied mkVenv venv. The Python half is fxtr2's
  # `data.layered_file_store.nix_builder`. See those files.
  mkFxtrFnBuilder = import ./mkFxtrFnBuilder.nix { inherit canonicalSystem _runFxtrBuilder; };
  mkFxtrScriptBuilder = import ./mkFxtrScriptBuilder.nix { inherit canonicalSystem _runFxtrBuilder; };

  # The fxtr_report_builder MDX→FxtrCbor CLI, built pure pnpm-in-nix as a
  # self-contained `node` executable ($out/bin/fxtr-report-builder). A pinned
  # derivation shared by all consumers (a report-step builder wires its path via
  # the builder's generic `env`). See ./reportBuilderCli.nix.
  reportBuilderCli = import ./reportBuilderCli.nix { inherit canonicalSystem pkgsFor; };

  # Internal helper (not exposed in lib.fxtr.*): uv.lock -> flat guest
  # site-packages dir, bypassing mkVirtualEnv. Consumed by BOTH guest-venv
  # helpers: mkPyodideService (with pyemscripten binary-wheel selection)
  # and mkWasmService (binaryPlatformTags = [] — pure-python only).
  _mkLockedSitePackages = import ./_mkLockedSitePackages.nix {
    inherit inputs canonicalSystem pkgsFor;
  };

  # Internal helper: the pyodide runtime assets (npm tarball + guest
  # bootstrap) for the pyodide backend. See ./_mkPyodideRuntimeBundle.nix.
  _mkPyodideRuntimeBundle = import ./_mkPyodideRuntimeBundle.nix {
    inherit canonicalSystem pkgsFor;
  };

  # Default pyodide pin for mkPyodideService. The four fields move together:
  # pyodide release ↔ its npm tarball hash ↔ the PEP 783 ABI year it
  # implements ↔ the CPython minor it embeds. User flakes override per
  # service via mkPyodideService's `pyodidePin` arg.
  pyodideDefaultPin = {
    version = "314.0.2";
    tarballHash = "sha256-avFTlaQAtW3ZYfywQ7QBmf5GJHnFbNcEQeiCzt3IA9g=";
    abiTag = "pyemscripten_2026_0_wasm32";
    pythonMinor = "314";
  };

  # Default esbuild externals for renderer modules: the bare specifiers a
  # compiled renderer must NOT bundle — the frontend's import-map shim
  # resolves them to the host's single bundled instance at load time.
  # KEEP IN SYNC with `DEFAULT_EXTERNALS` in
  # `projects/fxtr_workstation/src/fxtr_workstation/dynamic_dispatch/basic_dynamic_dispatch/compile.py`.
  workstationExternals = [
    "react"
    "react/jsx-runtime"
    "jotai"
    "jotai-effect"
    "@fxtr-ui/core"
    "@fxtr-ui/state-util"
    "@fxtr-ui/react"
    "@fxtr-ui/react-dispatch"
    "@fxtr-ui/workstation-dispatch"
  ];

  esbuildRenderer = import ./esbuildRenderer.nix {
    inherit canonicalSystem pkgsFor workstationExternals;
  };

  # The fxtr_experiment renderer slice (dashboard + frame + stored-config
  # renderers built from the repo frontend package); a function of
  # { keyIds } returning mergeable preset entries. See ./experimentRenderers.nix.
  experimentRenderers = import ./experimentRenderers.nix {
    inherit
      canonicalSystem
      pkgsFor
      esbuildRenderer
      entities
      ;
  };
in
{
  inherit canonicalSystem pkgs pkgsFor;

  inherit mkVenv runFxtrWriter tsvNamespaceExport entityFromTsvPin fodLockReader workstationExternals;
  inherit pyodideDefaultPin;
  inherit mkEntityFromPython builtinEntity entities;
  inherit mkFxtrFnBuilder mkFxtrScriptBuilder reportBuilderCli;

  mkWasmService = import ./mkWasmService.nix { inherit canonicalSystem _mkLockedSitePackages; };
  # Pyodide-kind sibling of mkWasmService: pyodide CPython-wasm run inside a
  # permission-locked deno subprocess on the host; supports PEP 783
  # pyemscripten binary wheels. See ./mkPyodideService.nix + the 2p5 plan.
  mkPyodideService = import ./mkPyodideService.nix {
    inherit
      canonicalSystem
      pkgsFor
      _mkLockedSitePackages
      _mkPyodideRuntimeBundle
      pyodideDefaultPin
      ;
  };
  # Container-kind sibling of mkWasmService: a nix2container OCI image with
  # the service entry point baked into Cmd. See ./mkContainerService.nix +
  # the stage7 plan.
  mkContainerService = import ./mkContainerService.nix {
    inherit canonicalSystem pkgsFor mkVenv nix2containerFor;
    lib = nixpkgs.lib;
  };
  inherit esbuildRenderer experimentRenderers;
  # Pure transpose: per-renderer declarations -> the type-id-keyed preset table
  # the nix/git dispatcher consumes (the `_build_routing_table` analog). No
  # factory args; takes the entries attrset directly.
  mkUiPreset = import ./mkUiPreset.nix;
}
