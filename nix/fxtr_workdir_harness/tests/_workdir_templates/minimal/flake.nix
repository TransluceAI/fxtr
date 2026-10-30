{
  description = "Minimal workdir template — fixture for prepare_workdir_repo mechanics tests.";

  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { self, fxtr }:
    {
      # Intentionally empty — these mechanics tests never build this flake.
    };
}
