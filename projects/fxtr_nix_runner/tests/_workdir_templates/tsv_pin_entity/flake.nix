{
  description = "fxtr_nix_runner entityFromTsvPin / fod_lock E2E fixture.";

  # Placeholder; prepare_workdir_repo rewrites it to a pinned ref in the copy.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    let
      lib = fxtr.lib.fxtr;
      # The FOD's outputHash comes from the lock file managed by
      # `fxtr_nix update-hashes` — the real (decoupled) lock-reader path, no
      # fakeHash markers.
      fodLock = lib.fodLockReader { lockFile = ./fod_lock.toml; };
      # A single tsv-namespace pin: the FOD resolves @PIN_KEY@ -> id from this
      # dir's committed TSV and fetches the entity's bytes from the build span's
      # rehost.
      thePin = lib.entityFromTsvPin {
        namespaceRoot = ./.;
        key = "@PIN_KEY@";
        outputHash = fodLock "thePin";
      };
    in
    {
      # Published as fxtr.entities.<name> (drops in wherever a layer entity goes).
      fxtr.entities.thePin = thePin;
      # Declared as a hash-locked slot for `update-hashes` (lockKey = "thePin").
      fxtr.preloads.thePin = thePin.layer;
    };
}
