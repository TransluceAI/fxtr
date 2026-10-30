# mkFxtrScriptBuilder — run a loose `.py` script file as a layer builder.
#
# The script owns `main` and calls `nix_builder.main(entrypoint)` itself; it runs
# as `__main__` (not imported as a package) and imports what it needs *from* the
# venv (fxtr2's `nix_builder`, plus any package the venv's uv workspace declares).
# A `venv` (mkVenv output) is required in v0. Returns the layer derivation (the
# user flake wraps it into a `fxtr.entities.<name>` attr).
#
# "One pyproject, many scripts": a single `venv` backs any number of script
# builders pointed at different `.py` files — a new step is "drop a `.py` + add a
# builder call", no per-step dir. Sibling-file caveat: `${script}` copies a
# *single* file into the store, so loose sibling `.py` files are not on
# `sys.path`; shared local code should be a path-member package in the venv's
# workspace. (Follow-up: optional `venv` defaulted from the script's PEP-723
# inline metadata — git/registry-dep flakes only.)
#
# Purity is the `outputHash` flag (see `_runFxtrBuilder.nix`).
{
  canonicalSystem,
  _runFxtrBuilder,
}:
{
  # A venv derivation (mkVenv output) bearing fxtr2 + any packages the script imports.
  venv,
  # A `.py` file (interpolated into the store) that calls `nix_builder.main(...)`.
  script,
  # name -> parent layer derivation (pre-opened as `ctx.readers["<name>"]`).
  parents ? { },
  # Arbitrary JSON passthrough (the entrypoint's `ctx.spec`).
  spec ? { },
  # FOD pinning + impure env (see `_runFxtrBuilder.nix`); pure when unset.
  outputHash ? null,
  impureEnvVars ? [ ],
  # Round-trip provenance: open the $out writer in provenanced mode (pins stored
  # as bare provenance-node ids). Default false = bare layer. See `_runFxtrBuilder.nix`.
  provenanced ? false,
  # Author-declared default build provenance (arbitrary JSON; only legal when
  # provenanced; REQUIRED for a fixed-output build). See `_runFxtrBuilder.nix`.
  provenanceInfo ? null,
  # Generic escape hatches (bare-name tools on PATH / extra derivation env).
  extraBuildInputs ? [ ],
  env ? { },
  # Derivation name.
  name ? "fxtr-script-builder",
  # Single-arch pin; override to build on a different builder.
  system ? canonicalSystem,
}:
# No `entry`: the script form owns `main`, so the launcher path is unused.
_runFxtrBuilder {
  inherit
    name
    system
    parents
    spec
    outputHash
    impureEnvVars
    provenanced
    provenanceInfo
    extraBuildInputs
    env
    ;
  runCmd = "${venv}/bin/python ${script}";
}
