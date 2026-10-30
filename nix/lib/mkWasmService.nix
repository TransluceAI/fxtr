# mkWasmService — build a `fxtr.services.<name>` attr from a guest package.
#
# A service is git-referenced *code*, not an entity: this helper produces
# the attr shape the spawner consumes —
#
#   { kind         = "wasm";          # discriminator the spawner checks
#     sitePackages = <derivation>;    # flat eryx-runnable guest site-packages
#     entryPoint   = "module:function"; }
#
# `sitePackages` is built lazily by the spawner; `kind` + `entryPoint` are
# plain metadata read via `nix eval`. No entity write, no `.drv` pointer.
#
# The site-packages dir is built by `_mkLockedSitePackages` (shared with
# `mkPyodideService`) with an empty binary-tag list: eryx's WASI CPython has
# no binary-wheel ABI, so every registry package must lock a pure
# `py3-none-any` wheel (the builder fails loudly per-package otherwise) and
# local/git workspace members are built as pure wheels via the pyproject-nix
# pythonSet. The `[tool.uv] environments` wasi constraint in guest
# workspaces matches the `guestEnviron` here.
#
# Design:
#   projects/fxtr_workstation/plans/phase0/part5_nix_workdir/design_notes/05_sandbox_bundle.md
#   projects/fxtr_workstation/plans/phase0/part5_nix_workdir/design_notes/12_nix_drv_reference.md
#   projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md (kind)
{
  canonicalSystem,
  _mkLockedSitePackages,
}:
{
  # Path to the guest package source (pyproject.toml + uv.lock + src/).
  src,
  # Derivation name; defaults to "fxtr-service-<project.name>".
  name ? null,
  # Single-arch pin; override to build on a different builder.
  system ? canonicalSystem,
  # Wheel-first; threaded through to the pythonSet building local/git packages.
  sourcePreference ? "wheel",
}:
let
  pyproject = builtins.fromTOML (builtins.readFile (src + "/pyproject.toml"));
  projectName = pyproject.project.name or "service";
  effectiveName = if name != null then name else "fxtr-service-${projectName}";

  entryPoint = import ./_readServiceEntryPoint.nix {
    inherit src;
    helper = "mkWasmService";
  };

  sitePackages = _mkLockedSitePackages {
    workspaceRoot = src;
    name = effectiveName;
    inherit system sourcePreference;
    # Match the `[tool.uv] environments` constraint wasi guest workspaces
    # declare. Marker evaluation otherwise uses the builder's default
    # python (3.12) — same semantics the previous mkVenv-based builder had.
    guestEnviron = {
      sys_platform = "wasi";
      platform_machine = "wasm32";
    };
    binaryPlatformTags = [ ];
  };
in
{
  kind = "wasm";
  inherit sitePackages entryPoint;
}
