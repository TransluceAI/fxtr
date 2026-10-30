{
  description = "pyodide_transporter — test fixture: a pyodide fxtr.services workdir for the transport E2E tests over the git-ref spawn path.";

  # Placeholder; prepare_workdir_repo rewrites it to a pinned git ref in the
  # workdir copy. See projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage3_bundle_and_spawner.md.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    {
      fxtr.services.pyodide_transporter = fxtr.lib.fxtr.mkPyodideService {
        src = ./.;
        name = "fxtr-pyodide-transporter-service";
      };
    };
}
