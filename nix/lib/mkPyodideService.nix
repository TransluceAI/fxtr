# mkPyodideService — build a `fxtr.services.<name>` attr for the pyodide
# sandbox backend (pyodide CPython-wasm inside a permission-locked deno
# subprocess on the host).
#
# Produces the attr shape the spawner consumes:
#
#   { kind          = "pyodide";       # discriminator the spawner checks
#     sitePackages  = <derivation>;    # flat guest site-packages (may include
#                                      #   PEP 783 pyemscripten binary wheels)
#     runtimeBundle = <derivation>;    # pyodide assets + guest bootstrap
#     entryPoint    = "module:function"; }
#
# `sitePackages` is built by `_mkLockedSitePackages` (lock-driven, bypassing
# mkVirtualEnv so pyemscripten wheels can be selected); `runtimeBundle` by
# `_mkPyodideRuntimeBundle`. Both are built lazily by the spawner; `kind` +
# `entryPoint` are plain metadata read via `nix eval`.
#
# Workspace requirements for `src` (pyproject.toml + uv.lock + sources):
# - Set `[tool.uv] required-environments =
#   ["sys_platform == 'emscripten' and platform_machine == 'wasm32'"]` so the
#   lock is guaranteed to carry emscripten-compatible resolution; also
#   constraining `environments` the same way keeps the lock small (author's
#   choice). The builder fails loudly per-package when neither a matching
#   pyemscripten wheel nor a pure wheel is locked.
# - Local (path/workspace) members must be self-contained — out-of-dir hatch
#   force-include only works for git-sourced deps (which build from the full
#   fetched repo). fxtr2-portable et al. should arrive as git sources, as in
#   the eryx workdir convention.
#
# Design: projects/fxtr_sandboxing/plans/stage2/2p5_deno_py_sandbox/plan.md
{
  canonicalSystem,
  pkgsFor,
  _mkLockedSitePackages,
  _mkPyodideRuntimeBundle,
  pyodideDefaultPin,
}:
{
  # Path to the guest package source (pyproject.toml + uv.lock + src/).
  src,
  # Derivation name; defaults to "fxtr-service-<project.name>".
  name ? null,
  # Single-arch pin; override to build on a different builder.
  system ? canonicalSystem,
  # Wheel-first for locally/git-built packages; threaded to the pythonSet.
  sourcePreference ? "wheel",
  # The pyodide pin: { version, tarballHash, abiTag, pythonMinor } — the
  # four move together (pyodide release ↔ npm tarball ↔ PEP 783 ABI year ↔
  # CPython minor). Defaults to the fxtr-lib pin; override per service to
  # control the guest runtime from the flake.
  pyodidePin ? pyodideDefaultPin,
}:
let
  pyproject = builtins.fromTOML (builtins.readFile (src + "/pyproject.toml"));
  projectName = pyproject.project.name or "service";
  effectiveName = if name != null then name else "fxtr-service-${projectName}";

  entryPoint = import ./_readServiceEntryPoint.nix {
    inherit src;
    helper = "mkPyodideService";
  };

  pkgs = pkgsFor system;

  sitePackages = _mkLockedSitePackages {
    workspaceRoot = src;
    name = effectiveName;
    inherit system sourcePreference;
    guestEnviron = {
      sys_platform = "emscripten";
      platform_machine = "wasm32";
    };
    binaryPlatformTags = [ pyodidePin.abiTag ];
    python = pkgs."python${pyodidePin.pythonMinor}";
  };

  runtimeBundle = _mkPyodideRuntimeBundle {
    pyodideVersion = pyodidePin.version;
    pyodideTarballHash = pyodidePin.tarballHash;
    inherit system;
  };
in
{
  kind = "pyodide";
  inherit sitePackages runtimeBundle entryPoint;
}
