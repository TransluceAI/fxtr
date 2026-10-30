{
  description = "fxtr_nix_runner provenanced (pure) builder E2E fixture (mkFxtrFnBuilder, provenanced = true).";

  # Placeholder; prepare_workdir_repo rewrites it to a pinned ref in the copy.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    let
      lib = fxtr.lib.fxtr;
      # Host fxtr2 venv built from this workdir (fxtr2 reached via the git source
      # prepare_workdir_repo activates). Backs the builder's `python`.
      venv = lib.mkVenv {
        name = "provenanced-builder-venv";
        workspaceRoot = ./.;
      };
      # A *pure* provenanced builder: run `provenanced_builder:build` (constructs
      # an entity from spec and pins it). `provenanced = true` opens the $out
      # writer in provenanced mode, so the pin round-trips provenance — read back
      # through the manager it is a layered (data, prov) id.
      thePin = lib.mkFxtrFnBuilder {
        inherit venv;
        entry = "provenanced_builder:build";
        spec.text = "hello-provenance";
        provenanced = true;
        name = "provenanced-builder";
      };
    in
    {
      # Published as fxtr.entities.<name> (the builder layer, pin key "pinned").
      fxtr.entities.thePin = {
        type = "fxtr_layer_entity";
        layer = thePin;
        layerKey = "pinned";
      };
    };
}
