{
  description = "container_greeter — test fixture: a container-kind fxtr.services workdir for the NixContainerServiceDefn spawn tests.";

  # Placeholder; prepare_workdir_repo rewrites it to a pinned git ref in the
  # workdir copy. See projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    {
      fxtr.services.greeter = fxtr.lib.fxtr.mkContainerService {
        src = ./.;
        name = "fxtr-container-greeter";
      };
    };
}
