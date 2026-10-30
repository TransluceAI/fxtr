{
  description = "pyodide_greeter — test fixture: a fxtr.services workdir for the NixPyodideServiceDefn spawn tests.";

  # Placeholder; prepare_workdir_repo rewrites it to a pinned git ref in the
  # workdir copy. See projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage3_bundle_and_spawner.md.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    {
      fxtr.services.pyodide_greeter = fxtr.lib.fxtr.mkPyodideService {
        src = ./.;
        name = "fxtr-pyodide-greeter-service";
      };
    };
}
