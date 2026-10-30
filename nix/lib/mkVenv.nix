# mkVenv — build a Python virtual environment from a uv workspace via uv2nix.
#
# The workspace's own pyproject.toml + uv.lock decide how it reaches fxtr2
# (a relative path-dep for in-repo lib workspaces such as
# `nix/fxtr_nix_writer`, a git-dep on the fxtr repo for downstream
# user flakes) and any build-dep hints. Returns the venv derivation;
# `${venv}/bin/python` has the workspace's default dependency closure
# importable. See design_notes/10_nix_infrastructure.md.
{
  inputs,
  canonicalSystem,
  pkgsFor,
}:
{
  # Directory holding the workspace's pyproject.toml + uv.lock.
  workspaceRoot,
  # Single-arch pin (overview.md "A principled single architecture");
  # override to build the venv on a different builder.
  system ? canonicalSystem,
  # Prefer prebuilt wheels; fall back to building sdists.
  sourcePreference ? "wheel",
  # Derivation name for the resulting venv.
  name ? "fxtr-venv",
  # Interpreter from the target pkgs; defaults to CPython 3.12.
  python ? null,
  # PEP 508 marker overrides passed to uv2nix's `mkPyprojectOverlay`.
  # Lets callers retarget marker evaluation (e.g. `sys_platform`,
  # `platform_machine`) to match a lockfile whose supported-environments
  # set is different from the host. See uv2nix's "platform quirks" docs.
  environ ? { },
}:
let
  inherit (inputs)
    nixpkgs
    uv2nix
    pyproject-nix
    pyproject-build-systems
    ;
  pkgs = pkgsFor system;
  py = if python != null then python else pkgs.python312;

  workspace = uv2nix.lib.workspace.loadWorkspace { inherit workspaceRoot; };
  overlay = workspace.mkPyprojectOverlay { inherit sourcePreference environ; };

  pythonSet =
    (pkgs.callPackage pyproject-nix.build.packages { python = py; }).overrideScope
      (nixpkgs.lib.composeManyExtensions [
        pyproject-build-systems.overlays.wheel
        overlay
      ]);
in
pythonSet.mkVirtualEnv name workspace.deps.default
