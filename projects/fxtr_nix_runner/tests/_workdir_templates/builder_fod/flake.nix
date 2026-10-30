{
  description = "fxtr_nix_runner FOD-builder E2E fixture (mkFxtrFnBuilder over the rehost).";

  # Placeholder; prepare_workdir_repo rewrites it to a pinned ref in the copy.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    let
      lib = fxtr.lib.fxtr;
      # Host fxtr2 venv built from this workdir (fxtr2 reached via the git source
      # prepare_workdir_repo activates). Backs the builder's `python`.
      venv = lib.mkVenv {
        name = "builder-fod-venv";
        workspaceRoot = ./.;
      };
      # The FOD's outputHash comes from the lock managed by `fxtr_nix
      # update-hashes` (the decoupled lock-reader path; no fakeHash markers).
      fodLock = lib.fodLockReader { lockFile = ./fod_lock.toml; };
      # A FOD *builder*: run `builder_fod:build` (fetches @ENTITY_ID@'s bytes
      # from the build span's rehost, pins it, leaving its content dep remote).
      # This is the part-3 builder primitive in its FOD shape.
      thePin = lib.mkFxtrFnBuilder {
        inherit venv;
        entry = "builder_fod:build";
        spec.entityId = "@ENTITY_ID@";
        outputHash = fodLock "thePin";
        impureEnvVars = [
          "FXTR_REMOTE_BASE_URL"
          "FXTR_REMOTE_BEARER_TOKEN"
        ];
        name = "builder-fod";
      };
    in
    {
      # Published as fxtr.entities.<name> (the builder layer, pin key "pinned").
      fxtr.entities.thePin = {
        type = "fxtr_layer_entity";
        layer = thePin;
        layerKey = "pinned";
      };
      # Declared as a hash-locked slot for `update-hashes` (lockKey = "thePin").
      fxtr.preloads.thePin = thePin;
    };
}
