{
  description = "binding_echo — test fixture: a wasm fxtr.services workdir for the sandboxed-binding e2e tests (step-connect path).";

  # Placeholder; prepare_workdir_repo rewrites it to a pinned git ref in the
  # workdir copy (the fxtr_sandboxing greeter fixture's convention).
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    {
      fxtr.services.binding_echo = fxtr.lib.fxtr.mkWasmService {
        src = ./.;
        name = "fxtr-binding-echo-service";
      };
      # The same package as a pyodide-kind service: the e2e sweeps both
      # runtimes (an eryx-vs-us discriminator for runtime-specific hangs).
      fxtr.services.binding_echo_pyodide = fxtr.lib.fxtr.mkPyodideService {
        src = ./.;
        name = "fxtr-binding-echo-pyodide";
      };
    };
}
