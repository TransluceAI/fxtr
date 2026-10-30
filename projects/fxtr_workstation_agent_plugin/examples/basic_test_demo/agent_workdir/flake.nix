{
  description = "basic_test_demo agent workdir — declares the nix-backed fxtr.services it spawns + UI presets.";

  # The fxtr library flake. Placeholder in the committed template;
  # `prepare_workdir_repo` (tests) / the scaffold script rewrite it to a
  # pinned git ref in the runnable workdir copy. See
  # projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage3_bundle_and_spawner.md.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    let
      lib = fxtr.lib.fxtr;

      # One host venv (full fxtr2) built from packages/demo_data, shared by
      # the DATA entity below and the preset's typeEntity introspection slot.
      demoDataVenv = lib.mkVenv {
        name = "fxtr-demo-data-venv";
        workspaceRoot = ./packages/demo_data;
      };

      # The DemoDataEntity *type* descriptor: its hand-pasted id_string paired
      # with the type entity (the type-id FxtrID pinned into a layer). Bundling
      # them as one `forType` value means it's defined once here and referenced
      # by every preset shard; the dispatcher builds + reads `entity` to
      # validate the `idString` key matches the real type id. The id_string is
      # obtained from a run_script: `print(DemoDataEntity.fxtr_type.id_string())`
      # (content-addressed + stable from the package's NS UUID + label).
      demoDataType = {
        idString = "dlt7gpfp765qazhxo2tkdx5evpkzgycifm657eocbl4ozqe5piuhq";
        entity = lib.mkEntityFromPython {
          venv = demoDataVenv;
          fromPython = "demo_data:DEMO_DATA_TYPE_ID";
          layerKey = "demo_data_type";
        };
      };

      # The DemoDataEntity renderer module, compiled to an ESM derivation.
      demoDataRenderer = lib.esbuildRenderer { src = ./renderers/demo_data; };
    in
    {
      # The single-arch `fxtr` object namespace (not indexed by ${system}).
      # An agent commits this flake, then spawns the service by reference:
      #   NixWasmServiceDefn(git_commit=current_commit(),
      #                      flake_attr="fxtr.services.hello_service")
      fxtr.services.hello_service = lib.mkWasmService {
        src = ./packages/hello_service;
        name = "fxtr-hello-service";
      };

      # DATA entity (built on demand by the MCP's retrieve_nix_entity, from
      # the live workdir). `mkEntityFromPython` runs the host venv built from
      # packages/demo_data and pins its `DEMO_DATA` object into a layered
      # store. Returns the full { type, layer, layerKey } entity attr.
      #   await retrieve_nix_entity("demo_data")
      fxtr.entities.demo_data = lib.mkEntityFromPython {
        venv = demoDataVenv;
        fromPython = "demo_data:DEMO_DATA";
        layerKey = "demo_data";
      };

      # Builtin DATA entity: a primitive FxtrID (the `!basic_type` fallback)
      # whose id_string IS its content, so there's no layer to build. The MCP's
      # retrieve_nix_entity resolves it straight to a bare FxtrID. Written as
      # the plain attrset on purpose (the shape `lib.fxtr.builtinEntity`
      # produces) so this fixture doesn't depend on a freshly-committed lib.
      #   await retrieve_nix_entity("basic_type")
      fxtr.entities.basic_type = {
        type = "builtin";
        idString = "!basic_type";
      };

      # UI preset (CODE, referenced by git commit). The agent pushes it to a
      # running NixConfigurableDispatchImpl:
      #   schedule_invoke("p", dispatcher, lambda d: d.set_preset(
      #       git_commit=current_commit(),
      #       flake_attr="fxtr.uiPresets.demo_preset"))
      # Assembled by `lib.mkUiPreset`, which transposes per-renderer
      # declarations (the `fxtr_renderer.json` analog) into the type-id-keyed
      # routing table the dispatcher consumes. Here: one entry (`demo_data`),
      # one subtype routing the four shell dispatch-key UUIDs (FullPanel /
      # BlockLink / InlineLink / HoverPreview) to the one renderer. A
      # hand-written table (the `mkUiPreset` output shape) also works.
      fxtr.uiPresets.demo_preset = lib.mkUiPreset {
        demo_data = {
          renderer = demoDataRenderer;
          subtypes."demo_data" = {
            forType = demoDataType;
            forDispatchKeys = [
              "21b2f3a8-7d54-4a1e-9c63-6a4b8e0f1234" # FullPanel
              "b3c4d5e6-7f89-4a01-9b23-4c5d6e7f8901" # BlockLink
              "c4d5e6f7-8901-4a12-9b34-5d6e7f890123" # InlineLink
              "d5e6f7a8-9012-4a23-9b45-6e7f89012345" # HoverPreview
            ];
          };
        };
      };
    };
}
