{
  description = "fxtr_nix_runner publishing-serve E2E fixture: a provenanced entity (mkFxtrFnBuilder, provenanced = true) + a pure tsv-namespace export, served together over HTTP.";

  # Placeholder; prepare_workdir_repo rewrites it to a pinned ref in the copy.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    let
      lib = fxtr.lib.fxtr;
      # Host fxtr2 venv built from this workdir; backs the builder's `python`.
      venv = lib.mkVenv {
        name = "publishing-demo-venv";
        workspaceRoot = ./.;
      };
      # A *pure* provenanced builder: `publishing_demo:build` constructs a
      # genuinely-provenanced entity and pins it. `provenanced = true` opens the
      # $out writer in provenanced mode, so the pin round-trips provenance — read
      # back through the manager (and served) it is a layered (data, prov) id.
      reportLayer = lib.mkFxtrFnBuilder {
        inherit venv;
        entry = "publishing_demo:build";
        spec.text = "hello-publishing";
        provenanced = true;
        name = "publishing-demo-report";
      };
      # Renderer preset: one esbuild-built renderer keyed to a builtin type
      # (a primitive `!`-id — no typeEntity layer to build, keeping the fixture
      # light). The serving dispatcher builds this into a resolve table.
      rendererEntries = import ./renderers/report/fxtr_renderer.nix { inherit lib; };
    in
    {
      # A provenanced layer entity (served layered, with provenance exposed).
      fxtr.entities.report = {
        type = "fxtr_layer_entity";
        layer = reportLayer;
        layerKey = "pinned";
      };
      # A pure tsv-namespace export (no FOD, no network): the committed
      # fxtr_namespace.toml + root.fxtr.tsv become a `key -> id` manifest the
      # serving namespace view reads. Served as bare "<ns>:<key>" pins.
      fxtr.namespaces.demo = lib.tsvNamespaceExport {
        namespaceRoot = ./.;
        name = "demo";
      };
      # A flake-built renderer preset (`fxtr_nix serve` builds it into the
      # dynamic-dispatch service). `lib.mkUiPreset` transposes the per-renderer
      # declaration into the type-id-keyed table the dispatcher consumes.
      fxtr.uiPresets.demo = lib.mkUiPreset rendererEntries;
    };
}
