# mkFxtrFnBuilder — run an importable `module:fn` entrypoint as a layer builder.
#
# The user's `entry` (an `async def fn(ctx)`) lives in an installed package, so a
# `venv` (mkVenv output bearing fxtr2 + that package) is required. The `-m`
# launcher (`fxtr2.data.layered_file_store.nix_builder`) reads `builderSpec.entry`
# and runs it; the entrypoint reads inputs from `ctx.readers` / the remote and
# pins outputs via `ctx.writer`. Returns the layer derivation — the user flake
# wraps it as `fxtr.entities.<name> = { type = "fxtr_layer_entity"; layer; layerKey; }`.
#
# Purity is the `outputHash` flag (see `_runFxtrBuilder.nix`): omit for a pure
# builder (reads only its parents); set it + `impureEnvVars = [ FXTR_REMOTE_* ]`
# for a FOD that fetches via the build-time rehost.
{
  canonicalSystem,
  _runFxtrBuilder,
}:
{
  # A venv derivation (mkVenv output) bearing fxtr2 + the entry's module.
  venv,
  # "module:fn" / "module:Class.fn" naming an `async def fn(ctx)` entrypoint.
  entry,
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
  name ? "fxtr-fn-builder",
  # Single-arch pin; override to build on a different builder.
  system ? canonicalSystem,
}:
_runFxtrBuilder {
  inherit
    name
    system
    parents
    spec
    entry
    outputHash
    impureEnvVars
    provenanced
    provenanceInfo
    extraBuildInputs
    env
    ;
  runCmd = "${venv}/bin/python -m fxtr2.data.layered_file_store.nix_builder";
}
