{
  description = "sandbox_smoke — minimal fxtr.services workdir for the eryx-import smoke test.";

  # The fxtr library flake. This is a placeholder in the committed template;
  # `prepare_workdir_repo` rewrites it to a pinned `git+file://…?rev=…` ref in
  # the workdir copy (then `nix flake lock`s + commits). See
  # projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage3_bundle_and_spawner.md
  # "Test & scaffold workdir setup".
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    let
      libFxtr = fxtr.lib.fxtr;
    in
    {
      # The single-arch `fxtr` object namespace (deliberately not indexed by
      # ${system}). `fxtr.services.smoke.sitePackages` is what the spawner /
      # the smoke test builds; `.entryPoint` is the no-op `run` above.
      fxtr.services.smoke = libFxtr.mkWasmService {
        src = ./.;
        name = "fxtr-sandbox-smoke";
      };
    };
}
