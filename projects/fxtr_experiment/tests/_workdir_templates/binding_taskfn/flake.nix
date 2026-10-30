{
  description = "binding_taskfn — test fixture: a container-kind fxtr.services workdir serving a real fxtr_experiment task fn (the sandboxed task-fn e2e).";

  # Placeholder; prepare_workdir_repo rewrites it to a pinned git ref in the
  # workdir copy (the fxtr_sandboxing container_greeter fixture's convention).
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    {
      fxtr.services.binding_taskfn = fxtr.lib.fxtr.mkContainerService {
        src = ./.;
        name = "fxtr-binding-taskfn";
      };
    };
}
